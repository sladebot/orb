import os

import pytest

from orb.llm.providers import AnthropicProvider
from orb.orchestrator.types import OrchestratorConfig
from orb.topologies.triangle import create_triangle


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
class TestE2E:
    async def test_fibonacci_task(self):
        provider = AnthropicProvider()
        try:
            config = OrchestratorConfig(timeout=120.0, budget=30, max_depth=5)
            orchestrator = create_triangle(
                providers={"anthropic": provider},
                config=config,
                trace=True,
            )

            result = await orchestrator.run("Write a fibonacci function in Python that handles edge cases")

            assert result.success
            assert len(result.completions) > 0
            print(f"\nCompleted with {result.message_count} messages")
            for agent_id, completion in result.completions.items():
                print(f"\n{agent_id}: {completion[:200]}")
        finally:
            await provider.close()
