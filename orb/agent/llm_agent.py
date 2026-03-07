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
from .tools import send_message_tool, complete_task_tool
from .types import AgentConfig, AgentStatus

logger = logging.getLogger(__name__)

# Callback for when an agent completes
CompletionCallback = Callable[[str, str], Awaitable[None] | None]


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
        tier_override: ModelTier | None = None,
    ) -> None:
        super().__init__(config, channel, bus)
        self._providers = providers
        self._model_overrides = model_overrides or {}
        self._selector = ModelSelector(base_complexity=config.base_complexity)
        self._conversation = ConversationHistory(max_messages=config.max_history)
        self._memory = MemoryGraph()
        self._on_complete = on_complete
        self._tier_override = tier_override
        self._system_prompt: str = ""
        self._tools: list[dict] = []
        self._memory_counter = 0

    def initialize(self, neighbor_roles: dict[str, str]) -> None:
        """Set up system prompt and tools after graph is configured."""
        self._system_prompt = build_system_prompt(
            role=self.config.role,
            description=self.config.description,
            neighbors=neighbor_roles,
        )
        self._tools = [
            send_message_tool(sorted(neighbor_roles.keys())),
            complete_task_tool(),
        ]

    async def process(self, msg: Message) -> None:
        # Build user message from incoming
        user_content = self._format_incoming(msg)
        self._conversation.add_user(user_content)

        # Store in memory
        self._store_memory(msg)

        # Select model
        tier = self._tier_override or self._selector.select(msg)
        model_config = self._model_overrides.get(tier, DEFAULT_MODELS.get(tier))
        if not model_config:
            model_config = DEFAULT_MODELS[ModelTier.CLOUD_FAST]

        provider = self._providers.get(model_config.provider)
        if not provider:
            logger.error(f"No provider for {model_config.provider!r}")
            return

        # Call LLM
        request = CompletionRequest(
            messages=self._conversation.get_messages(),
            tools=self._tools,
            system=self._system_prompt,
            model_config=model_config,
        )

        try:
            response = await provider.complete(request)
        except Exception:
            logger.exception(f"LLM call failed for agent {self.node_id}")
            return

        logger.info(
            f"Agent {self.node_id} used model {response.model} "
            f"(tier={tier.value}, tokens={response.usage})"
        )

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

        # Process tool calls
        for tc in response.tool_calls:
            if tc.name == "send_message":
                await self._handle_send(msg, tc.id, tc.input, response.model)
            elif tc.name == "complete_task":
                await self._handle_complete(tc.id, tc.input)

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
