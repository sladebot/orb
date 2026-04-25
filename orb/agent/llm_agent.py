from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import PurePosixPath
from typing import Callable, Awaitable, Optional

from ..llm.client import LLMClient
from ..llm.model_selector import ModelSelector
from ..llm.types import CompletionRequest, ModelConfig, DEFAULT_MODELS, ModelTier
from ..memory.memory_graph import MemoryGraph
from ..memory.memory_node import MemoryEdge
from ..messaging.bus import MessageBus
from ..messaging.channel import AgentChannel, ChannelClosed
from ..messaging.message import Message, MessageType
from ..tracing.run_trace import RunTrace
from .base import AgentNode
from .conversation import ConversationHistory
from .prompt_builder import build_system_prompt
from .request_context import (
    build_request_messages,
    build_graph_context_block,
    build_request_messages_with_graph,
)
from .tools import send_message_tool, complete_task_tool, filesystem_tools
from .types import AgentConfig, AgentStatus, TopologyContext

logger = logging.getLogger(__name__)

# Callback for when an agent completes
CompletionCallback = Callable[[str, str], Awaitable[None] | None]
# Callback for agent activity updates (agent_id, activity_text)
ActivityCallback = Callable[[str, str], Awaitable[None] | None]
HeartbeatCallback = Callable[[str, dict], Awaitable[None] | None]


def _display_model_name(model_id: str) -> str:
    if not model_id:
        return ""
    m = re.match(r"^claude-([a-z]+-[\d]+(?:-[\d]+)?)", model_id, re.I)
    if m:
        return m.group(1).lower()
    return model_id


def _normalize_usage(usage: object) -> dict[str, int]:
    if isinstance(usage, dict):
        items = usage.items()
    elif hasattr(usage, "__dict__"):
        items = vars(usage).items()
    else:
        return {}

    normalized: dict[str, int] = {}
    for key, value in items:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            normalized[str(key)] = int(value)
    return normalized


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
        on_heartbeat: HeartbeatCallback | None = None,
        tier_override: ModelTier | None = None,
        trace: RunTrace | None = None,
    ) -> None:
        super().__init__(config, channel, bus)
        self._providers = providers
        self._model_overrides = model_overrides or {}
        self._selector = ModelSelector(base_complexity=config.base_complexity)
        self._conversation = ConversationHistory(max_messages=config.max_history)
        self._shared_transcript = None
        self._memory = MemoryGraph()
        self._on_complete = on_complete
        self._on_activity = on_activity
        self._on_heartbeat = on_heartbeat
        self._trace = trace
        self._on_file_write: Optional[Callable] = None  # (agent_id, path, content) -> None
        # Optional pre-write approval gate. When set (by the runtime when
        # the session has ``approval_required=True``), every
        # ``_handle_write_file`` call awaits this callback before
        # touching the sandbox; ``approved=False`` skips the disk write
        # entirely and ``effective_content`` lets the user edit before
        # commit. Signature: ``(agent_id, path, proposed_content,
        # old_content) -> Awaitable[tuple[bool, str]]``.
        self._on_write_request: Optional[
            Callable[[str, str, str, str], Awaitable[tuple[bool, str]]]
        ] = None
        # Per-token streaming hook. When set by the runtime (only on
        # sessions with ``streaming_enabled=True``), the agent builds an
        # ``on_chunk`` callable that feeds text deltas into this hook
        # with the incoming message's ``chain_id`` + a monotonic
        # per-turn ``index``. Staying unset means the agent omits
        # ``on_chunk`` entirely when calling the provider — the
        # non-streaming back-compat path.
        #
        # Signature: ``(chain_id, delta, index) -> Awaitable[None]``.
        # See ``web/bridge.py::on_message_delta`` + the shared contract
        # with stream-tui/#13 and stream-dashboard/#14.
        self._on_message_delta: Optional[
            Callable[[str, str, int], Awaitable[None]]
        ] = None
        self._tier_override = tier_override
        self._system_prompt: str = ""
        self._tools: list[dict] = []
        self._memory_counter = 0
        self._last_message_memory_id: str | None = None
        self._last_complexity_score: int = config.base_complexity
        self._heartbeat_task: asyncio.Task | None = None
        self._fs_cache: dict[tuple[str, str], str] = {}
        self._subgraph_store = None
        self._fact_extractor = None

    async def _emit(self, activity: str, details: dict | None = None) -> None:
        """Fire the activity callback if set."""
        if self._on_activity:
            cb = self._on_activity(self.node_id, activity, details or {})
            if asyncio.iscoroutine(cb):
                await cb

    async def _emit_heartbeat(self) -> None:
        if self._on_heartbeat:
            payload = {
                "status": self.status.value,
                "ts": time.time(),
            }
            cb = self._on_heartbeat(self.node_id, payload)
            if asyncio.iscoroutine(cb):
                await cb

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await self._emit_heartbeat()
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return

    def start(self) -> asyncio.Task:
        task = super().start()
        if self._on_heartbeat and not self._heartbeat_task:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"heartbeat-{self.node_id}"
            )
        return task

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        await super().stop()

    def set_subgraph_store(self, store) -> None:
        """Attach a SubgraphStore and create a FactExtractor for this agent."""
        self._subgraph_store = store
        from .fact_extractor import FactExtractor
        self._fact_extractor = FactExtractor(store, self.node_id, self._providers)

    def initialize(
        self,
        neighbor_roles: dict[str, str],
        topology_context: TopologyContext | None = None,
    ) -> None:
        """Set up system prompt and tools after graph is configured."""
        # Always expose "user" as a pseudo-neighbor so agents can ask for clarification
        # without being forced to call complete_task.
        all_neighbors = {
            **neighbor_roles,
            "user": "Human operator — use to ask for clarification or report a blocker",
        }
        workdir = str(self.config.sandbox.root) if self.config.sandbox else ""
        self._system_prompt = build_system_prompt(
            role=self.config.role,
            description=self.config.description,
            neighbors=all_neighbors,
            topology=topology_context,
            enable_filesystem=self.config.enable_filesystem,
            suppress_context_guidelines=self.config.suppress_context_guidelines,
            workdir=workdir,
        )
        self._tools = [
            send_message_tool(sorted(all_neighbors.keys())),
            complete_task_tool(),
        ]
        if self.config.enable_filesystem:
            self._tools.extend(filesystem_tools())

    async def process(self, msg: Message) -> None:
        self.status = AgentStatus.RUNNING
        self._fs_cache = {}
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

        from ..llm.types import ANTHROPIC_MODELS, CODEX_MODELS

        def _resolve_model_config() -> ModelConfig | None:
            if self.config.pinned_model is not None:
                cfg = self.config.pinned_model
                return cfg if cfg.provider in self._providers else None

            preferred = self._tier_override or self._selector.select_with_available(
                msg, set(self._providers.keys())
            )
            self._last_complexity_score = self._selector.last_score
            for cfg in (
                self._model_overrides.get(preferred),
                DEFAULT_MODELS.get(preferred),
                ANTHROPIC_MODELS.get(preferred),
                CODEX_MODELS.get(preferred),
            ):
                if cfg and cfg.provider in self._providers:
                    return cfg
            return None

        model_config = _resolve_model_config()
        if model_config is None:
            err = "No LLM provider available. Check your API keys (orb auth status)."
            logger.error(f"[{self.node_id}] {err}")
            await self._handle_complete("no_provider", {"result": f"[ERROR] {err}"})
            return

        provider = self._providers[model_config.provider]
        shared_transcript = ""
        if self._shared_transcript is not None:
            shared_transcript = self._shared_transcript.render_for_model(self.node_id)

        graph_context = ""
        if self._fact_extractor is not None and self._subgraph_store is not None:
            graph_context = await build_graph_context_block(
                self._subgraph_store,
                self.node_id,
                query=msg.payload[:200],
                limit=10,
            )

        request_messages = build_request_messages_with_graph(
            self._conversation.get_messages(),
            graph_context,
            shared_transcript,
        )

        # Call LLM
        request = CompletionRequest(
            messages=request_messages,
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

        MAX_SAME_MODEL_RETRIES = 3

        # Per-turn streaming index. Increments across every LLM call
        # within this ``process()`` invocation — all share the incoming
        # ``msg.chain_id``, so receivers see a single monotonic stream
        # keyed by that chain_id (matches the final ``message`` event's
        # chain_id). Only meaningful when ``_on_message_delta`` is set;
        # the agent skips passing ``on_chunk`` otherwise.
        delta_index = 0
        delta_hook = self._on_message_delta
        stream_chain_id = msg.chain_id

        async def _on_chunk(delta: str) -> None:
            # The contract forbids emitting empty deltas; drop them
            # before the hook so receivers don't need a guard.
            nonlocal delta_index
            if not delta:
                return
            await delta_hook(stream_chain_id, delta, delta_index)
            delta_index += 1

        # Pass ``on_chunk`` only when the runtime attached the delta
        # hook. Non-streaming sessions (and all non-streaming providers)
        # keep the legacy zero-kwarg call shape.
        provider_on_chunk = _on_chunk if delta_hook is not None else None

        while True:
            await self._emit(f"Calling {_display_model_name(model_config.model_id)}…")
            response = None
            last_exc: Exception | None = None
            for attempt in range(1, MAX_SAME_MODEL_RETRIES + 1):
                try:
                    # Only pass ``on_chunk`` when the session opted into
                    # streaming. Legacy mock providers / tests whose
                    # ``complete`` signature predates the kwarg stay
                    # compatible as long as this hook is unset.
                    if provider_on_chunk is not None:
                        response = await provider.complete(
                            request, on_chunk=provider_on_chunk,
                        )
                    else:
                        response = await provider.complete(request)
                    break
                except Exception as exc:
                    last_exc = exc
                    if self._trace is not None:
                        self._trace.record_retry(
                            actor=self.node_id,
                            stage="llm_call",
                            attempt=attempt,
                            message=str(exc),
                            data={
                                "provider": model_config.provider,
                                "model": model_config.model_id,
                            },
                        )
                    logger.warning(
                        f"[{self.node_id}] LLM call failed "
                        f"({model_config.provider}/{model_config.model_id}) attempt {attempt}/{MAX_SAME_MODEL_RETRIES}: {exc}"
                    )
                    if attempt < MAX_SAME_MODEL_RETRIES:
                        reason = type(exc).__name__
                        detail = str(exc).splitlines()[0][:80] if str(exc) else ""
                        suffix = f" — {reason}" + (f": {detail}" if detail else "")
                        await self._emit(
                            f"Retrying {_display_model_name(model_config.model_id)} "
                            f"({attempt + 1}/{MAX_SAME_MODEL_RETRIES}){suffix}"
                        )
                        await asyncio.sleep(0.5 * attempt)

            if response is None:
                err = (
                    "Assigned model call failed after retries. "
                    "Each node is pinned to a single provider/model for the run."
                )
                detail = f" Last provider error: {last_exc}" if last_exc else ""
                logger.error(f"[{self.node_id}] {err}: {last_exc}")
                await self._handle_complete(
                    "assigned_model_failed",
                    {
                        "result": (
                            f"[ERROR] {err} "
                            f"({model_config.provider}/{_display_model_name(model_config.model_id)})."
                            f"{detail}"
                        )
                    },
                )
                return

            # _last_model is read by web/server.py and tui.py at completion time
            self._last_model = response.model
            if self._trace is not None:
                self._trace.record_llm_response(
                    actor=self.node_id,
                    model=response.model or model_config.model_id,
                    provider=model_config.provider,
                    usage=_normalize_usage(response.usage),
                    stop_reason=response.stop_reason,
                    tool_call_count=len(response.tool_calls),
                )
            logger.info(
                f"Agent {self.node_id} used model {response.model} "
                f"(tier={model_config.tier.value}, tokens={response.usage})"
            )
            if response.content:
                logger.debug(f"[{self.node_id}] response text: {response.content[:500]}")
            for tc in response.tool_calls:
                if self._trace is not None:
                    self._trace.record_tool_call(
                        tc.name,
                        actor=self.node_id,
                        message=f"tool call {tc.name}",
                        data={
                            "tool_id": tc.id,
                            "input": tc.input,
                        },
                    )
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
                    messages=build_request_messages_with_graph(
                        self._conversation.get_messages(),
                        graph_context,
                        shared_transcript,
                    ),
                    tools=self._tools,
                    system=self._system_prompt,
                    model_config=model_config,
                )
                continue

            # Process tool calls
            for tc in response.tool_calls:
                if tc.name == "send_message":
                    to = tc.input.get("to", "?")
                    await self._emit(
                        f"Sending message to {to}…",
                        {
                            "kind": "send_message",
                            "to": to,
                            "content": tc.input.get("content", ""),
                            "context": tc.input.get("context", []),
                        },
                    )
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
                    cmd = tc.input.get("command", "")[:200]
                    await self._emit(f"$ {cmd}")
                    await self._handle_run_command(tc.id, tc.input)

            # After filesystem tool calls (no send/complete), loop back so the
            # agent can act on the results immediately (e.g. read → write → send)
            has_action = any(
                tc.name in ("send_message", "complete_task")
                for tc in response.tool_calls
            )
            if not has_action and response.tool_calls:
                if len(response.tool_calls) == 1:
                    self._conversation.add_user(
                        "Continue with the remaining filesystem work. Batch as many related tool calls "
                        "as you can in your next response, and avoid repeating unchanged listings or reads."
                    )
                # All calls were filesystem ops — add results and let the model continue
                request = CompletionRequest(
                    messages=build_request_messages_with_graph(
                        self._conversation.get_messages(),
                        graph_context,
                        shared_transcript,
                    ),
                    tools=self._tools,
                    system=self._system_prompt,
                    model_config=model_config,
                )
                continue

            break

        if self._fact_extractor and assistant_content:
            content_text = " ".join(
                block.get("text", "") for block in assistant_content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if content_text.strip():
                _task = asyncio.create_task(
                    self._fact_extractor.extract_and_store(msg.id, content_text),
                    name=f"fact-extract-{self.node_id}-{msg.id[:8]}",
                )

                def _on_extraction_done(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.warning(
                            "Fact extraction failed for agent %s turn %s: %s",
                            self.node_id, msg.id[:8], exc,
                        )

                _task.add_done_callback(_on_extraction_done)

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

        # Special case: "user" is a pseudo-neighbor — surface the message to the human
        # via the activity/event system without routing through the bus, and without
        # triggering completion.  The agent stays RUNNING and waits for an injected reply.
        if to == "user":
            self.status = AgentStatus.WAITING
            # Keep the activity string compact for status-line consumers, but
            # pass the full question via details so the question banner can
            # render it without truncation.
            await self._emit(
                f"⏳ Waiting for user: {content[:240]}",
                details={"full_content": content},
            )
            # Emit a bus-style event so the dashboard displays this message in the feed
            user_msg = original.reply(
                from_=self.node_id,
                to="user",
                payload=content,
                type=MessageType.RESPONSE,
                context_slice=context,
            )
            user_msg.metadata["model"] = model
            await self.bus._emit("routed", user_msg)
            self._conversation.add_tool_result(
                tool_id,
                "Message sent to user. Waiting for their response — do NOT call complete_task yet.",
            )
            return

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
        outgoing.metadata["complexity"] = self._last_complexity_score

        try:
            await self.send(outgoing)
            self._conversation.add_tool_result(tool_id, f"Message sent to {to}")
        except ChannelClosed as exc:
            self._conversation.add_tool_result(tool_id, f"Failed to send: {exc}")

    async def _handle_complete(self, tool_id: str, input_data: dict) -> None:
        result = input_data.get("result", "")
        self.status = AgentStatus.COMPLETED
        self._conversation.add_tool_result(tool_id, "Task marked as complete")
        logger.info(f"Agent {self.node_id} completed: {result[:100]}")

        if self._on_activity:
            _cb = self._on_activity(self.node_id, "")
            if asyncio.iscoroutine(_cb):
                await _cb

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

    def _normalize_relpath(self, path: str) -> str:
        raw = (path or ".").strip() or "."
        normalized = PurePosixPath(raw).as_posix()
        return "." if normalized == "" else normalized

    def _invalidate_fs_cache_for_path(self, path: str) -> None:
        changed = self._normalize_relpath(path)
        changed_parts = PurePosixPath(changed).parts
        updated: dict[tuple[str, str], str] = {}
        for (kind, cached_path), value in self._fs_cache.items():
            cached_parts = PurePosixPath(cached_path).parts
            same_file = kind == "read" and cached_path == changed
            listing_affected = (
                kind == "list"
                and (
                    cached_path == "."
                    or cached_parts == changed_parts[:len(cached_parts)]
                )
            )
            if same_file or listing_affected:
                continue
            updated[(kind, cached_path)] = value
        self._fs_cache = updated

    async def _handle_write_file(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", "").strip()
        content = input_data.get("content", "")
        if not path:
            self._conversation.add_tool_result(tool_id, "Error: path is required")
            return
        try:
            old_content = self._sandbox().read_file(path)
        except FileNotFoundError:
            old_content = ""
        # Pre-write approval gate. Runtime wires this hook only on
        # opted-in sessions (``ConversationSession.approval_required``).
        # Skip when the hook is unset so the default path stays a single
        # sandbox call.
        if self._on_write_request is not None:
            approved, effective = await self._on_write_request(
                self.node_id, path, content, old_content
            )
            if not approved:
                # The Anthropic API requires every ``tool_use`` block to
                # be paired with a matching ``tool_result`` in history,
                # otherwise the next turn 400s. Record the rejection so
                # the LLM sees it and adapts.
                self._conversation.add_tool_result(
                    tool_id, "Write rejected by user"
                )
                return
            # User-approved (possibly with edits — empty string is a
            # valid edit meaning "wipe the file").
            content = effective
        try:
            result = self._sandbox().write_file(path, content)
        except (PermissionError, OSError, ValueError) as exc:
            self._conversation.add_tool_result(tool_id, f"Error writing {path}: {exc}")
            return
        self._conversation.add_tool_result(tool_id, result)
        self._invalidate_fs_cache_for_path(path)
        self._remember_file_artifact(
            path=path,
            content=content,
            relation="writes_to",
            action="write",
        )
        if self._on_file_write:
            self._on_file_write(self.node_id, path, content, old_content)

    async def _handle_read_file(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", "").strip()
        if not path:
            self._conversation.add_tool_result(tool_id, "Error: path is required")
            return
        norm_path = self._normalize_relpath(path)
        cached = self._fs_cache.get(("read", norm_path))
        if cached is not None:
            self._conversation.add_tool_result(tool_id, cached)
            return
        try:
            content = self._sandbox().read_file(path)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self._conversation.add_tool_result(tool_id, f"Error reading {path}: {exc}")
            return
        self._fs_cache[("read", norm_path)] = content
        self._conversation.add_tool_result(tool_id, content)
        self._remember_file_artifact(
            path=path,
            content=content,
            relation="reads_from",
            action="read",
        )

    async def _handle_list_directory(self, tool_id: str, input_data: dict) -> None:
        path = input_data.get("path", ".").strip() or "."
        norm_path = self._normalize_relpath(path)
        cached = self._fs_cache.get(("list", norm_path))
        if cached is not None:
            self._conversation.add_tool_result(tool_id, cached)
            return
        try:
            listing = self._sandbox().list_directory(path)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            self._conversation.add_tool_result(tool_id, f"Error listing {path}: {exc}")
            return
        self._fs_cache[("list", norm_path)] = listing
        self._conversation.add_tool_result(tool_id, listing)

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
        self._memory.upsert_node(
            node_id=node_id,
            content=msg.payload[:500],
            node_type=node_type,
            metadata={
                "speaker": msg.from_,
                "audience": msg.to,
                "message_type": msg.type.value,
                "depth": msg.depth,
            },
        )
        self._last_message_memory_id = node_id
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
        # Prune oldest node to cap memory graph size
        if self._memory_counter > 100:
            oldest_id = f"msg_{self._memory_counter - 100}"
            try:
                self._memory.remove_node(oldest_id)
            except (KeyError, AttributeError):
                pass

    def _remember_file_artifact(
        self,
        *,
        path: str,
        content: str,
        relation: str,
        action: str,
    ) -> None:
        norm_path = self._normalize_relpath(path)
        node_id = f"file:{norm_path}"
        artifact = self._memory.upsert_node(
            node_id=node_id,
            content=content[:4000],
            node_type="artifact",
            metadata={
                "path": norm_path,
                "agent_id": self.node_id,
                "action": action,
                "lines": content.count("\n") + (1 if content else 0),
                "chars": len(content),
            },
        )
        if not self._last_message_memory_id:
            return
        try:
            self._memory.add_edge(MemoryEdge(
                from_id=self._last_message_memory_id,
                to_id=artifact.id,
                relation=relation,
            ))
        except KeyError:
            logger.debug(
                "Skipping artifact memory link for %s; current message node missing",
                norm_path,
            )
