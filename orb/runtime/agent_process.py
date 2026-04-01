"""Agent subprocess wrapper — Phase 4 of dual-mode spec.

Spawns an agent in its own OS process. Socket server ownership belongs
exclusively to UDSServer (Phase 5). AgentProcess is a thin lifecycle
wrapper: spawn, monitor, and terminate the child process. The child
connects to UDSServer's socket as a client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import struct

from ..agent.types import AgentConfig, TopologyContext
from ..messaging.uds_channel import _HEADER_FMT, _HEADER_SIZE

logger = logging.getLogger(__name__)


class AgentProcess:
    """Manages a single agent running in a child process.

    UDSServer creates and owns the UDS socket. AgentProcess only:
    - serialises the bootstrap config for the subprocess
    - spawns the subprocess
    - terminates it on stop()
    """

    def __init__(
        self,
        config: AgentConfig,
        topology_context: TopologyContext | None,
        socket_dir: str,
    ) -> None:
        self.config = config
        self.topology_context = topology_context
        self._socket_dir = socket_dir
        self._socket_path = os.path.join(socket_dir, f"{config.node_id}.sock")
        self._process: multiprocessing.Process | None = None
        self._alive = False

    @property
    def node_id(self) -> str:
        return self.config.node_id

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.is_alive()

    async def start(self) -> None:
        """Spawn the agent subprocess.

        UDSServer must already be listening on the socket before this is called.
        The subprocess will connect to UDSServer's socket as a client.
        """
        bootstrap = {
            "agent_config": self.config.to_dict(),
            "socket_path": self._socket_path,
        }
        if self.topology_context:
            bootstrap["topology_context"] = self.topology_context.to_dict()

        self._process = multiprocessing.Process(
            target=_agent_subprocess_entry,
            args=(json.dumps(bootstrap),),
            name=f"orb-agent-{self.config.node_id}",
            daemon=True,
        )
        self._process.start()
        self._alive = True
        logger.info(f"Agent {self.node_id} spawned as PID {self._process.pid}")

    async def stop(self, timeout: float = 5.0) -> None:
        """Wait for the subprocess to exit, escalating to SIGTERM / SIGKILL if needed."""
        if self._process and self._process.is_alive():
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                logger.warning(f"Agent {self.node_id} did not exit, sending SIGTERM")
                self._process.terminate()
                self._process.join(timeout=2.0)
                if self._process.is_alive():
                    logger.error(f"Agent {self.node_id} did not terminate, killing")
                    self._process.kill()
        self._alive = False


# ── Subprocess entry point ──


def _agent_subprocess_entry(bootstrap_json: str) -> None:
    """Entry point for agent child process. Runs its own asyncio event loop."""
    asyncio.run(_agent_subprocess_main(bootstrap_json))


async def _agent_subprocess_main(bootstrap_json: str) -> None:
    """Async main for agent subprocess — connects to UDSServer, runs agent loop."""
    import json as _json
    bootstrap = _json.loads(bootstrap_json)

    config = AgentConfig.from_dict(bootstrap["agent_config"])
    socket_path = bootstrap["socket_path"]

    topo_ctx = None
    if "topology_context" in bootstrap:
        topo_ctx = TopologyContext.from_dict(bootstrap["topology_context"])

    logger.info(f"Agent subprocess {config.node_id} starting, connecting to {socket_path}")

    # Connect to orchestrator UDS server (UDSServer owns the socket)
    reader, writer = await asyncio.open_unix_connection(socket_path)

    # Build providers inside subprocess
    from ..llm.registry import build_providers
    providers = build_providers()

    # Build a minimal graph + bus for this single agent
    from ..graph.graph import Graph
    from ..messaging.bus import MessageBus
    from ..messaging.uds_channel import UDSChannel

    graph = Graph()
    graph.add_node(config.node_id)
    if topo_ctx:
        for neighbor_id in topo_ctx.direct_neighbors:
            graph.add_node(neighbor_id)
            graph.add_edge(config.node_id, neighbor_id)

    bus = MessageBus(graph)
    channel = UDSChannel(reader, writer)
    bus.register_channel(config.node_id, channel)

    # Create agent
    from ..agent.llm_agent import LLMAgent
    from ..messaging.control import make_control, ControlType
    from ..messaging.message import Message

    async def on_complete(agent_id: str, result: str) -> None:
        ctrl = make_control(ControlType.COMPLETE, agent_id, result=result)
        header = struct.pack(_HEADER_FMT, len(ctrl))
        writer.write(header + ctrl)
        await writer.drain()

    async def on_activity(agent_id: str, text: str) -> None:
        ctrl = make_control(ControlType.ACTIVITY, agent_id, text=text)
        header = struct.pack(_HEADER_FMT, len(ctrl))
        writer.write(header + ctrl)
        await writer.drain()

    async def on_heartbeat(agent_id: str, payload: dict) -> None:
        ctrl = make_control(ControlType.HEARTBEAT, agent_id, payload=payload)
        header = struct.pack(_HEADER_FMT, len(ctrl))
        writer.write(header + ctrl)
        await writer.drain()

    agent = LLMAgent(
        config=config,
        channel=channel,
        bus=bus,
        providers=providers,
        on_complete=on_complete,
        on_activity=on_activity,
        on_heartbeat=on_heartbeat,
    )

    if topo_ctx:
        neighbor_roles = {
            nid: role for nid, role in topo_ctx.node_roles.items()
            if nid in topo_ctx.direct_neighbors
        }
        agent.initialize(neighbor_roles, topo_ctx)

    # Override bus.route to send outbound messages over UDS to orchestrator
    async def uds_route(msg: Message) -> None:
        payload = msg.to_json().encode("utf-8")
        header = struct.pack(_HEADER_FMT, len(payload))
        writer.write(header + payload)
        await writer.drain()

    bus.route = uds_route

    # Run until agent task completes (or channel closes on shutdown)
    await agent.start()
    logger.info(f"Agent subprocess {config.node_id} exiting")
