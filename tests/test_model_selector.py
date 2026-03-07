from orb.llm.model_selector import ModelSelector
from orb.llm.types import ModelTier
from orb.messaging.message import Message, MessageType


def _msg(depth=0, payload="test", msg_type=MessageType.TASK, metadata=None):
    return Message(
        from_="a", to="b", type=msg_type,
        payload=payload, depth=depth,
        metadata=metadata or {},
    )


class TestModelSelector:
    def test_low_complexity(self):
        sel = ModelSelector(base_complexity=20)
        tier = sel.select(_msg())
        assert tier == ModelTier.LOCAL_SMALL

    def test_medium_complexity(self):
        sel = ModelSelector(base_complexity=50)
        tier = sel.select(_msg())
        assert tier == ModelTier.LOCAL_MEDIUM

    def test_high_depth_bump(self):
        sel = ModelSelector(base_complexity=50)
        tier = sel.select(_msg(depth=5))
        assert tier in (ModelTier.LOCAL_LARGE, ModelTier.CLOUD_FAST)

    def test_feedback_bump(self):
        sel = ModelSelector(base_complexity=50)
        tier = sel.select(_msg(msg_type=MessageType.FEEDBACK))
        assert tier in (ModelTier.LOCAL_LARGE, ModelTier.CLOUD_FAST)

    def test_complexity_hint_high(self):
        sel = ModelSelector(base_complexity=50)
        tier = sel.select(_msg(metadata={"complexity": "high"}))
        assert tier in (ModelTier.LOCAL_LARGE, ModelTier.CLOUD_FAST)

    def test_complexity_hint_low(self):
        sel = ModelSelector(base_complexity=50)
        tier = sel.select(_msg(metadata={"complexity": "low"}))
        assert tier in (ModelTier.LOCAL_SMALL, ModelTier.LOCAL_MEDIUM)

    def test_escalation(self):
        sel = ModelSelector(base_complexity=50)
        tier_before = sel.select(_msg())
        sel.escalate()
        sel.escalate()
        tier_after = sel.select(_msg())
        tier_order = list(ModelTier)
        assert tier_order.index(tier_after) >= tier_order.index(tier_before)

    def test_reset_retries(self):
        sel = ModelSelector(base_complexity=50)
        sel.escalate()
        sel.escalate()
        sel.reset_retries()
        tier = sel.select(_msg())
        assert tier == ModelTier.LOCAL_MEDIUM
