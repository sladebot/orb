from __future__ import annotations

import asyncio
import logging
import shutil

from ..agent.llm_agent import LLMAgent
from ..agent.types import AgentConfig, AgentStatus, TopologyContext
from ..messaging.bus import MessageBus
from ..messaging.channel import ChannelClosed
from ..messaging.message import Message, MessageType
from ..tracing.logger import EventLogger
from ..tracing.run_trace import RunTrace
from .types import OrchestratorConfig, RunResult

try:
    from ..sandbox.sandbox import Sandbox
except ImportError:
    Sandbox = None  # type: ignore

logger = logging.getLogger(__name__)

CONSENSUS_PREFIX = "Consensus:"


class Orchestrator:
    """Manages agent lifecycle, injects tasks, and collects results."""

    def __init__(
        self,
        agents: dict[str, LLMAgent],
        bus: MessageBus,
        config: OrchestratorConfig | None = None,
        event_logger: EventLogger | None = None,
        trace: RunTrace | None = None,
        topology_id: str = "",
        sandbox=None,
    ) -> None:
        self.agents = agents
        self.bus = bus
        self.config = config or OrchestratorConfig()
        self._completions: dict[str, str] = {}
        self._event_logger = event_logger
        self._trace = trace or getattr(event_logger, "trace", None)
        self._topology_id = topology_id
        self._completion_event = asyncio.Event()
        self._consensus_sent = False
        self._consensus_lock = asyncio.Lock()
        self._sandbox = sandbox

        # Process mode attributes (set by factory when mode="amux")
        self._process_mode: bool = False
        self._uds_server = None
        self._socket_dir: str | None = None
        self._agent_configs_for_process: dict | None = None
        self._agent_processes: list = []

        if self._event_logger:
            self.bus.on_event(self._event_logger)

    @property
    def trace(self) -> RunTrace | None:
        return self._trace

    async def _on_agent_complete(self, agent_id: str, result: str) -> None:
        self._completions[agent_id] = result
        transcript = getattr(self, "_transcript", None)
        if transcript is not None:
            transcript.add_completion(agent_id, result)
        if self._trace is not None:
            self._trace.record_completion(agent_id, result)

        all_agent_ids = (
            set(self.agents.keys())
            if self.agents
            else set(self._agent_configs_for_process.keys())
            if self._agent_configs_for_process
            else set()
        )
        logger.info(f"Agent {agent_id} completed ({len(self._completions)}/{len(all_agent_ids)})")

        synthesis = self.config.synthesis_agent

        if not synthesis:
            if len(self._completions) >= len(all_agent_ids):
                self._completion_event.set()
            return

        if synthesis and agent_id == synthesis:
            # Synthesis agent finished — gracefully stop remaining workers, then signal done.
            for other_id in all_agent_ids:
                if other_id != synthesis and other_id not in self._completions:
                    shutdown_msg = Message(
                        from_="orchestrator",
                        to=other_id,
                        type=MessageType.COMPLETE,
                        payload=f"Run complete. {result[:200]}",
                    )
                    try:
                        await self._send_to_agent(other_id, shutdown_msg)
                        if transcript is not None:
                            transcript.add_message(shutdown_msg)
                    except (ChannelClosed, RuntimeError):
                        logger.warning(f"Could not send shutdown COMPLETE to {other_id}")
            self._completion_event.set()
            return

        # Worker completed. Broadcast COMPLETE to other workers (not synthesis agent).
        async with self._consensus_lock:
            if not self._consensus_sent:
                self._consensus_sent = True
                for other_id in all_agent_ids:
                    if other_id != agent_id and other_id != synthesis:
                        consensus_msg = Message(
                            from_="orchestrator",
                            to=other_id,
                            type=MessageType.COMPLETE,
                            payload=f"{CONSENSUS_PREFIX} task completed by {agent_id}. {result[:200]}",
                        )
                        try:
                            await self._send_to_agent(other_id, consensus_msg)
                            if transcript is not None:
                                transcript.add_message(consensus_msg)
                        except (ChannelClosed, RuntimeError):
                            logger.warning(f"Could not send consensus COMPLETE to {other_id}")

        # When all workers are done, forward a summary to the synthesis agent.
        if synthesis:
            workers = [aid for aid in all_agent_ids if aid != synthesis]
            if all(w in self._completions for w in workers):
                notify_msg = Message(
                    from_="orchestrator",
                    to=synthesis,
                    type=MessageType.RESPONSE,
                    payload="All workers have completed. Synthesize the results:\n\n"
                    + "\n\n".join(
                        f"[{wid}]: {self._completions[wid][:400]}" for wid in workers
                    ),
                )
                try:
                    await self._send_to_agent(synthesis, notify_msg)
                    if transcript is not None:
                        transcript.add_message(notify_msg)
                except (ChannelClosed, RuntimeError):
                    logger.warning("Could not notify synthesis agent")

    async def _send_to_agent(self, agent_id: str, msg: Message) -> None:
        """Send a message to an agent — works in both local and process mode."""
        if self._process_mode and self._uds_server:
            await self._uds_server.send_to_agent(agent_id, msg)
        elif agent_id in self.agents:
            await self.agents[agent_id].channel.send(msg)
        else:
            raise RuntimeError(f"No channel for agent {agent_id}")

    async def run(self, query: str, entry_agent: str | None = None) -> RunResult:
        """Run the agent graph with the given query."""
        if self._process_mode:
            return await self._run_process_mode(query, entry_agent)
        return await self._run_local_mode(query, entry_agent)

    async def _run_local_mode(self, query: str, entry_agent: str | None = None) -> RunResult:
        """Run in local (in-process) mode — original behavior."""
        self._completions.clear()
        self._completion_event.clear()
        self._consensus_sent = False

        if self._event_logger:
            self._event_logger.reset()
        elif self._trace is not None:
            self._trace.reset()

        if self._trace is not None and self._topology_id:
            self._trace.record_topology_choice(
                self._topology_id,
                reason="selected for orchestrated run",
                task_type="software_task",
                candidates=[self._topology_id],
            )

        # Wire up completion callbacks
        for agent in self.agents.values():
            agent._on_complete = self._on_agent_complete
            if self._trace is not None:
                self._trace.record_agent_spawn(
                    agent.node_id,
                    role=agent.config.role,
                    model=getattr(getattr(agent.config, "pinned_model", None), "model_id", ""),
                )

        # Start all agents
        tasks = []
        for agent in self.agents.values():
            tasks.append(agent.start())

        # Inject the initial task to the entry agent
        entry = entry_agent or self.config.entry_agent
        if entry not in self.agents:
            if self._trace is not None:
                self._trace.record_final_outcome(
                    success=False,
                    message="entry agent not found",
                    error=f"Entry agent {entry!r} not found",
                    data={"entry_agent": entry},
                )
            return RunResult(success=False, error=f"Entry agent {entry!r} not found")

        initial_msg = Message(
            from_="user",
            to=entry,
            type=MessageType.TASK,
            payload=query,
        )

        # Direct delivery to entry agent (user is not a graph node)
        await self.agents[entry].channel.send(initial_msg)
        transcript = getattr(self, "_transcript", None)
        if transcript is not None:
            transcript.add_message(initial_msg)

        if self._event_logger:
            self._event_logger("injected", initial_msg)

        # Wait for completion or timeout
        try:
            await asyncio.wait_for(
                self._completion_event.wait(),
                timeout=self.config.timeout,
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Orchestrator timed out")

        # Stop all agents
        for agent in self.agents.values():
            await agent.stop()

        # Cancel any remaining tasks
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        sandbox_dir = str(self._sandbox.root) if self._sandbox else None

        if self._sandbox:
            self._sandbox.cleanup()

        if self._trace is not None:
            self._trace.record_final_outcome(
                success=len(self._completions) > 0,
                message="timed out" if timed_out else "completed",
                result=next(iter(self._completions.values()), ""),
                error=None,
                data={
                    "timed_out": timed_out,
                    "message_count": self.bus.message_count,
                    "sandbox_dir": sandbox_dir,
                    "completions": dict(self._completions),
                },
            )

        return RunResult(
            success=len(self._completions) > 0,
            completions=dict(self._completions),
            message_count=self.bus.message_count,
            timed_out=timed_out,
            sandbox_dir=sandbox_dir,
        )

    async def _run_process_mode(self, query: str, entry_agent: str | None = None) -> RunResult:
        """Run in process mode — agents in separate OS processes over UDS."""
        self._completions.clear()
        self._completion_event.clear()
        self._consensus_sent = False

        if self._event_logger:
            self._event_logger.reset()
        elif self._trace is not None:
            self._trace.reset()

        if self._trace is not None and self._topology_id:
            self._trace.record_topology_choice(
                self._topology_id,
                reason="selected for orchestrated run (process mode)",
                task_type="software_task",
                candidates=[self._topology_id],
            )

        assert self._uds_server is not None
        assert self._agent_configs_for_process is not None

        # Wire UDS server callbacks
        self._uds_server.on_complete = self._on_agent_complete
        self._uds_server.on_route = self.bus.route

        # Start UDS server
        await self._uds_server.start()

        # Spawn agent subprocesses
        from ..runtime.agent_process import AgentProcess

        for nid, (agent_config, topo_ctx_dict) in self._agent_configs_for_process.items():
            topo_ctx = TopologyContext.from_dict(topo_ctx_dict)
            proc = AgentProcess(
                config=agent_config,
                topology_context=topo_ctx,
                socket_dir=self._socket_dir,
            )
            self._agent_processes.append(proc)

            if self._trace is not None:
                self._trace.record_agent_spawn(
                    nid,
                    role=agent_config.role,
                    model=getattr(getattr(agent_config, "pinned_model", None), "model_id", ""),
                )

        # Start all agent processes (they connect to UDS server)
        for proc in self._agent_processes:
            await proc.start()

        # Wait for all agents to connect
        await self._uds_server.wait_for_all(timeout=30.0)

        # Re-register live UDS channels on the bus so routing delivers to subprocesses.
        # The factory initially registered InProcessChannels as placeholders; swap them
        # now that the real connections are established.
        for agent_id in self._agent_configs_for_process:
            uds_channel = self._uds_server.get_channel(agent_id)
            if uds_channel is not None:
                self.bus.register_channel(agent_id, uds_channel)

        # Start reading from all agents
        self._uds_server.start_reading_all()

        # Inject the initial task
        entry = entry_agent or self.config.entry_agent
        all_ids = set(self._agent_configs_for_process.keys())
        if entry not in all_ids:
            if self._trace is not None:
                self._trace.record_final_outcome(
                    success=False,
                    message="entry agent not found",
                    error=f"Entry agent {entry!r} not found",
                    data={"entry_agent": entry},
                )
            await self._cleanup_process_mode()
            return RunResult(success=False, error=f"Entry agent {entry!r} not found")

        initial_msg = Message(
            from_="user",
            to=entry,
            type=MessageType.TASK,
            payload=query,
        )
        await self._uds_server.send_to_agent(entry, initial_msg)

        transcript = getattr(self, "_transcript", None)
        if transcript is not None:
            transcript.add_message(initial_msg)

        if self._event_logger:
            self._event_logger("injected", initial_msg)

        # Wait for completion or timeout
        try:
            await asyncio.wait_for(
                self._completion_event.wait(),
                timeout=self.config.timeout,
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Orchestrator timed out (process mode)")

        # Cleanup
        await self._cleanup_process_mode()

        sandbox_dir = str(self._sandbox.root) if self._sandbox else None
        if self._sandbox:
            self._sandbox.cleanup()

        if self._trace is not None:
            self._trace.record_final_outcome(
                success=len(self._completions) > 0,
                message="timed out" if timed_out else "completed",
                result=next(iter(self._completions.values()), ""),
                error=None,
                data={
                    "timed_out": timed_out,
                    "message_count": self.bus.message_count,
                    "sandbox_dir": sandbox_dir,
                    "completions": dict(self._completions),
                    "mode": "amux",
                },
            )

        return RunResult(
            success=len(self._completions) > 0,
            completions=dict(self._completions),
            message_count=self.bus.message_count,
            timed_out=timed_out,
            sandbox_dir=sandbox_dir,
        )

    async def _cleanup_process_mode(self) -> None:
        """Gracefully shut down agents, then clean up processes and sockets."""
        # Step 1: UDSServer sends _SHUTDOWN to all connected agents and closes
        # channels. This signals agents to exit their run loops cleanly.
        if self._uds_server:
            await self._uds_server.stop()

        # Step 2: Wait for each subprocess to exit. SIGTERM / SIGKILL if needed.
        for proc in self._agent_processes:
            try:
                await proc.stop()
            except Exception:
                logger.exception(f"Error stopping agent process {proc.node_id}")

        # Step 3: Remove the shared socket directory.
        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

        self._agent_processes.clear()
