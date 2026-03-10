from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from ..llm.client import LLMClient
from ..llm.model_selector import ModelSelector
from ..llm.types import CompletionRequest, ModelConfig, DEFAULT_MODELS, ModelTier
from ..memory.memory_graph import MemoryGraph
from ..memory.memory_node import MemoryNode, MemoryEdge
from ..messaging.bus import MessageBus
from ..messaging.channel import AgentChannel
from ..messaging.message import Message, MessageType
from .base import AgentNode
from .conversation import ConversationHistory
from .prompt_builder import build_system_prompt
from .tools import send_message_tool, complete_task_tool, filesystem_tools
from .types import AgentConfig, AgentStatus

logger = logging.getLogger(__name__)

# Callback for when an agent completes
CompletionCallback = Callable[[str, str], Awaitable[None] | None]
# Callback for agent activity updates (agent_id, activity_text)
ActivityCallback = Callable[[str, str], Awaitable[None] | None]


class LLMAgent(AgentNode):
    """Agent node that uses an LLM to process messages and communicate with neighbors."""

    def __init__(
        self,
        config: AgentConfig,
        channel: AgentChannel,
        bus: MessageBus,
        providers: dict[str, LLMClient],
        model_overrides: dict[ModelTier, ModelConfig] | None = None,
        on_complete: CompletionCallback | None = None,
        on_activity: ActivityCallback | None = None,
        tier_override: ModelTier | None = None,
    ) -> None:
        super().__init__(config, channel, bus)
        self._providers = providers
        self._model_overrides = model_overrides or {}
        self._selector = ModelSelector(base_complexity=config.base_complexity)
        self._conversation = ConversationHistory(max_messages=config.max_history)
        self._memory = MemoryGraph()
        self._on_complete = on_complete
        self._on_activity = on_activity
        self._tier_override = tier_override
        self._system_prompt: str = ""
        self._tools: list[dict] = []
        self._memory_counter = 0

    async def _emit(self, activity: str) -> None:
        """Fire the activity callback if set."""
        if self._on_activity:
            cb = self._on_activity(self.node_id, activity)
            if asyncio.iscoroutine(cb):
                await cb

    def initialize(self, neighbor_roles: dict[str, str]) -> None:
        """Set up system prompt and tools after graph is configured."""
        self._system_prompt = build_system_prompt(
            role=self.config.role,
            description=self.config.description,
            neighbors=neighbor_roles,
            enable_filesystem=self.config.enable_filesystem,
        )
        self._tools = [
            send_message_tool(sorted(neighbor_roles.keys())),
            complete_task_tool(),
        ]
        if self.config.enable_filesystem:
            self._tools.extend(filesystem_tools())

    async def process(self, msg: Message) -> None:
        # Handle consensus shutdown: skip LLM and auto-complete immediately.
        if msg.type == MessageType.COMPLETE:
            if self.status != AgentStatus.COMPLETED:
                await self._handle_complete("consensus", {"result": msg.payload})
            return

        # Build user message from incoming
        user_content = self._format_incoming(msg)
        self._conversation.add_user(user_content)

        # Store in memory
        self._store_memory(msg)

        # Build ordered candidate list: preferred tier first, then all cloud tiers as fallback.
        # This ensures local-unavailable tiers are skipped transparently.
        from ..llm.types import OPENAI_MODELS, CODEX_MODELS

        def _candidate_configs() -> list:
            seen: set[str] = set()
            candidates = []
            if self.config.pinned_model:
                tiers = [self.config.pinned_model.tier]
            else:
                preferred = self._tier_override or self._selector.select_with_available(
                    msg, set(self._providers.keys())
                )
                tiers = [preferred,
                         ModelTier.CLOUD_LITE, ModelTier.CLOUD_FAST, ModelTier.CLOUD_STRONG]
            for t in tiers:
                for cfg in [
                    self._model_overrides.get(t),
                    DEFAULT_MODELS.get(t),
                    OPENAI_MODELS.get(t),
                    CODEX_MODELS.get(t),
                ]:
                    if cfg and cfg.provider in self._providers:
                        key = f"{cfg.provider}:{cfg.model_id}"
                        if key not in seen:
                            seen.add(key)
                            candidates.append(cfg)
            return candidates

        candidates = _candidate_configs()
        if not candidates:
            err = "No LLM provider available. Check your API keys (orb auth status)."
            logger.error(f"[{self.node_id}] {err}")
            await self._handle_complete("no_provider", {"result": f"[ERROR] {err}"})
            return

        model_config = candidates[0]
        provider = self._providers[model_config.provider]

        # Call LLM
        request = CompletionRequest(
            messages=self._conversation.get_messages(),
            tools=self._tools,
            system=self._system_prompt,
            model_config=model_config,
        )

        logger.debug(
            f"[{self.node_id}] LLM call → model={model_config.model_id} "
            f"messages={len(request.messages)} system_len={len(self._system_prompt)}"
        )
        logger.debug(f"[{self.node_id}] system_prompt:\n{self._system_prompt}")
        for i, m in enumerate(request.messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " | ".join(
                    (c.get("text", "") if c.get("type") == "text" else f"[{c.get('type','?')}]")
                    for c in content
                )
            logger.debug(f"[{self.node_id}] msg[{i}] {role}: {str(content)[:300]}")

        MAX_TOOL_NUDGES = 3
        nudge_count = 0
        # Track which candidate index we're on for mid-call fallback
        candidate_idx = 0

        while True:
            await self._emit(f"Calling {model_config.model_id}…")
            try:
                response = await provider.complete(request)
            except Exception as exc:
                logger.warning(
                    f"[{self.node_id}] LLM call failed "
                    f"({model_config.provider}/{model_config.model_id}): {exc}"
                )
                # Walk through remaining candidates in order
                candidate_idx += 1
                response = None
                while candidate_idx < len(candidates):
                    fallback_config = candidates[candidate_idx]
                    logger.warning(
                        f"[{self.node_id}] Retrying with "
                        f"{fallback_config.provider}/{fallback_config.model_id}"
                    )
                    await self._emit(f"Retrying with {fallback_config.model_id}…")
                    try:
                        fb_request = CompletionRequest(
                            messages=request.messages,
                            tools=request.tools,
                            system=request.system,
                            model_config=fallback_config,
                        )
                        response = await self._providers[fallback_config.provider].complete(fb_request)
                        model_config = fallback_config
                        provider = self._providers[model_config.provider]
                        request = fb_request
                        break
                    except Exception as exc2:
                        logger.warning(f"[{self.node_id}] Fallback failed: {exc2}")
                        candidate_idx += 1
                if response is None:
                    err = "All LLM providers failed. Check API keys and network access."
                    logger.error(f"[{self.node_id}] {err}")
                    await self._handle_complete("all_providers_failed", {"result": f"[ERROR] {err}"})
                    return

            logger.info(
                f"Agent {self.node_id} used model {response.model} "
                f"(tier={tier.value}, tokens={response.usage})"
            )
            if response.content:
                logger.debug(f"[{self.node_id}] response text: {response.content[:500]}")
            for tc in response.tool_calls:
                logger.debug(f"[{self.node_id}] tool_call: {tc.name}({tc.input})")

            # Build assistant message content for history
            assistant_content: list[dict] = []
            if response.content:
                assistant_content.append({"type": "text", "text": response.content})
            for tc in response.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })

            if assistant_content:
                self._conversation.add_assistant(assistant_content)

            # If no tool calls were made, nudge the model to use one
            if not response.tool_calls:
                if nudge_count >= MAX_TOOL_NUDGES:
                    logger.warning(
                        f"Agent {self.node_id} produced {MAX_TOOL_NUDGES} text-only responses; "
                        "giving up waiting for tool call"
                    )
                    break
                nudge_count += 1
                logger.info(f"Agent {self.node_id} returned text without tool call — nudging ({nudge_count}/{MAX_TOOL_NUDGES})")
                nudge = (
                    "You must use one of your tools to continue. "
                    "Do NOT write a plain text reply. "
                    "Call `send_message` to communicate with another agent, "
                    "or call `complete_task` if your work is fully done."
                )
                self._conversation.add_user(nudge)
                request = CompletionRequest(
                    messages=self._conversation.get_messages(),
                    tools=self._tools,
                    system=self._system_prompt,
                    model_config=model_config,
                )
                continue

            # Process tool calls
            for tc in response.tool_calls:
                if tc.name == "send_message":
                    to = tc.input.get("to", "?")
                    await self._emit(f"Sending message to {to}…")
                    await self._handle_send(msg, tc.id, tc.input, response.model)
                elif tc.name == "complete_task":
                    await self._emit("Completing task…")
                    await self._handle_complete(tc.id, tc.input)
                elif tc.name == "write_file":
                    path = tc.input.get("path", "?")
                    await self._emit(f"Writing {path}")
                    await self._handle_write_file(tc.id, tc.input)
                elif tc.name == "read_file":
                    path = tc.input.get("path", "?")
                    await self._emit(f"Reading {path}")
                    await self._handle_read_file(tc.id, tc.input)
                elif tc.name == "list_directory":
                    path = tc.input.get("path", ".") or "."
                    await self._emit(f"Listing {path}")
                    await self._handle_list_directory(tc.id, tc.input)
                elif tc.name == "run_command":
                    cmd = tc.input.get("command", "")[:60]
                    await self._emit(f"$ {cmd}")
                    await self._handle_run_command(tc.id, tc.input)

            # After filesystem tool calls (no send/complete), loop back so the
            # agent can act on the results immediately (e.g. read → write → send)
            has_action = any(
                tc.name in ("send_message", "complete_task")
                for tc in response.tool_calls
            )
            if not has_action and response.tool_calls:
                # All calls were filesystem ops — add results and let the model continue
                request = CompletionRequest(
                    messages=self._conversation.get_messages(),
                    tools=self._tools,
                    system=self._system_prompt,
                    model_config=model_config,
                )
                continue

            break

        self._selector.reset_retries()

    def _format_incoming(self, msg: Message) -> str:
        parts = [f"[From {msg.from_} | type={msg.type.value} | depth={msg.depth}]"]
        parts.append(msg.payload)
        if msg.context_slice:
            parts.append("\n--- Context ---")
            for ctx in msg.context_slice:
                parts.append(ctx)
        return "\n".join(parts)

    async def _handle_send(
        self, original: Message, tool_id: str, input_data: dict, model: str
    ) -> None:
        to = input_data.get("to", "")
        content = input_data.get("content", "")
        context = input_data.get("context", [])

        if to not in self.neighbors:
            self._conversation.add_tool_result(tool_id, f"Error: {to!r} is not a neighbor")
            return

        outgoing = original.reply(
            from_=self.node_id,
            to=to,
            payload=content,
            type=MessageType.RESPONSE,
            context_slice=context,
        )
        outgoing.metadata["model"] = model

        try:
            await self.send(outgoing)
            self._conversation.add_tool_result(tool_id, f"Message sent to {to}")
        except Exception as e:
            self._conversation.add_tool_result(tool_id, f"Failed to send: {e}")

    async def _handle_complete(self, tool_id: str, input_data: dict) -> None:
        result = input_data.get("result", "")
        self.status = AgentStatus.COMPLETED
        self._conversation.add_tool_result(tool_id, "Task marked as complete")
        logger.info(f"Agent {self.node_id} completed: {result[:100]}")

        if self._on_complete:
            cb_result = self._on_complete(self.node_id, result)
            if asyncio.iscoroutine(cb_result):
                await cb_result

    def _sandbox(self):
        """Return the agent's sandbox, creating a fallback if none was configured."""
        if self.config.sandbox:
            return self.config.sandbox
        # Lazy fallback: create a per-agent sandbox if none was injected
        if not hasattr(self, "_fallback_sandbox"):
            from ..sandbox.sandbox import Sandbox
            self._fallback_sandbox = Sandbox()
        return self._fallback_sandbox

    async def _handle_write_file(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", "").strip()
        content = input_data.get("content", "")
        if not path:
            self._conversation.add_tool_result(tool_id, "Error: path is required")
            return
        try:
            result = self._sandbox().write_file(path, content)
            self._conversation.add_tool_result(tool_id, result)
        except Exception as e:
            self._conversation.add_tool_result(tool_id, f"Error writing {path}: {e}")

    async def _handle_read_file(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", "").strip()
        if not path:
            self._conversation.add_tool_result(tool_id, "Error: path is required")
            return
        try:
            content = self._sandbox().read_file(path)
            self._conversation.add_tool_result(tool_id, content)
        except Exception as e:
            self._conversation.add_tool_result(tool_id, f"Error reading {path}: {e}")

    async def _handle_list_directory(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", ".").strip() or "."
        try:
            listing = self._sandbox().list_directory(path)
            self._conversation.add_tool_result(tool_id, listing)
        except Exception as e:
            self._conversation.add_tool_result(tool_id, f"Error listing {path}: {e}")

    async def _handle_run_command(self, tool_id: str, input_data: dict) -> None:
        command = input_data.get("command", "").strip()
        if not command:
            self._conversation.add_tool_result(tool_id, "Error: command is required")
            return
        result = await self._sandbox().run_command(command)
        self._conversation.add_tool_result(tool_id, result)

    def _store_memory(self, msg: Message) -> None:
        self._memory_counter += 1
        node_id = f"msg_{self._memory_counter}"
        node_type = "incoming" if msg.from_ != self.node_id else "outgoing"
        self._memory.add_node(MemoryNode(
            id=node_id,
            content=msg.payload[:500],
            node_type=node_type,
        ))
        # Link to previous memory node
        if self._memory_counter > 1:
            prev_id = f"msg_{self._memory_counter - 1}"
            try:
                self._memory.add_edge(MemoryEdge(
                    from_id=prev_id,
                    to_id=node_id,
                    relation="followed_by",
                ))
            except KeyError:
                pass
