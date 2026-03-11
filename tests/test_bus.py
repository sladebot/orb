import pytest

from orb.graph import Graph
from orb.messaging.bus import MessageBus, RoutingError
from orb.messaging.channel import AgentChannel
from orb.messaging.message import Message, MessageType
from orb.messaging.middleware import HopLimitExceeded, BudgetExhausted, CooldownExceeded


def _setup_bus(max_depth=10, budget=200, max_cooldown=5):
    g = Graph()
    g.add_node("a")
    g.add_node("b")
    g.add_node("c")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    # No edge a-c

    bus = MessageBus(g, max_depth=max_depth, budget=budget, max_cooldown=max_cooldown)
    ch_a = AgentChannel()
    ch_b = AgentChannel()
    ch_c = AgentChannel()
    bus.register_channel("a", ch_a)
    bus.register_channel("b", ch_b)
    bus.register_channel("c", ch_c)
    return bus, ch_a, ch_b, ch_c


def _msg(from_: str, to: str, depth: int = 0, chain_id: str = "chain1") -> Message:
    return Message(
        from_=from_, to=to, type=MessageType.TASK,
        payload="test", depth=depth, chain_id=chain_id,
    )


class TestMessageBus:
    async def test_route_valid_edge(self):
        bus, ch_a, ch_b, ch_c = _setup_bus()
        await bus.route(_msg("a", "b"))
        received = await ch_b.receive()
        assert received.payload == "test"

    async def test_route_no_edge(self):
        bus, *_ = _setup_bus()
        with pytest.raises(RoutingError):
            await bus.route(_msg("a", "c"))  # no edge a-c

    async def test_route_no_channel(self):
        g = Graph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b")
        bus = MessageBus(g)
        bus.register_channel("a", AgentChannel())
        # b has no channel
        with pytest.raises(RoutingError):
            await bus.route(_msg("a", "b"))

    async def test_hop_limit(self):
        bus, *_ = _setup_bus(max_depth=3)
        with pytest.raises(HopLimitExceeded):
            await bus.route(_msg("a", "b", depth=4))

    async def test_budget_exhaustion(self):
        bus, *_ = _setup_bus(budget=2)
        await bus.route(_msg("a", "b"))
        await bus.route(_msg("b", "a"))
        with pytest.raises(BudgetExhausted):
            await bus.route(_msg("a", "b"))

    async def test_cooldown(self):
        bus, *_ = _setup_bus(max_cooldown=2)
        await bus.route(_msg("a", "b", chain_id="c1"))
        await bus.route(_msg("a", "b", chain_id="c1"))
        with pytest.raises(CooldownExceeded):
            await bus.route(_msg("a", "b", chain_id="c1"))

    async def test_cooldown_different_chains(self):
        bus, *_ = _setup_bus(max_cooldown=1)
        await bus.route(_msg("a", "b", chain_id="c1"))
        # Different chain should be fine
        await bus.route(_msg("a", "b", chain_id="c2"))

    async def test_event_listener(self):
        bus, *_ = _setup_bus()
        events = []

        async def listener(event, msg):
            events.append((event, msg.from_, msg.to))

        bus.on_event(listener)
        await bus.route(_msg("a", "b"))
        assert len(events) == 1
        assert events[0] == ("routed", "a", "b")

    async def test_message_count(self):
        bus, *_ = _setup_bus()
        assert bus.message_count == 0
        await bus.route(_msg("a", "b"))
        assert bus.message_count == 1
        assert bus.budget_remaining == 199
