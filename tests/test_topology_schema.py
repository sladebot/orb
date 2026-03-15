import pytest

from orb.topologies.schema import (
    AgentSchema,
    TopologiesFileSchema,
    TopologySchema,
)


class TestTopologySchema:
    def test_valid_minimal_topology(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "Test",
                    "description": "A test topology",
                    "entry_agent": "agent1",
                    "agents": {
                        "agent1": {"role": "Worker", "description": "Does work"},
                    },
                    "edges": [],
                    "workflow_steps": ["Step 1"],
                    "completion_rules": {},
                }
            },
        }
        schema = TopologiesFileSchema.model_validate(data)
        assert "test" in schema.topologies
        assert schema.topologies["test"].label == "Test"

    def test_invalid_entry_agent(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "Test",
                    "description": "A test",
                    "entry_agent": "nonexistent",
                    "agents": {"agent1": {"role": "W", "description": "D"}},
                    "edges": [],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        with pytest.raises(ValueError, match="entry_agent"):
            TopologiesFileSchema.model_validate(data)

    def test_invalid_edge_reference(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "Test",
                    "description": "A test",
                    "entry_agent": "agent1",
                    "agents": {"agent1": {"role": "W", "description": "D"}},
                    "edges": [["agent1", "ghost"]],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        with pytest.raises(ValueError, match="ghost"):
            TopologiesFileSchema.model_validate(data)

    def test_invalid_completion_rules_reference(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "Test",
                    "description": "A test",
                    "entry_agent": "agent1",
                    "agents": {"agent1": {"role": "W", "description": "D"}},
                    "edges": [],
                    "workflow_steps": [],
                    "completion_rules": {"bogus": ["rule"]},
                }
            },
        }
        with pytest.raises(ValueError, match="bogus"):
            TopologiesFileSchema.model_validate(data)

    def test_unsupported_version(self):
        data = {
            "version": "99.0",
            "topologies": {},
        }
        with pytest.raises(ValueError, match="Unsupported schema version"):
            TopologiesFileSchema.model_validate(data)

    def test_edges_normalised_from_lists(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "T",
                    "description": "D",
                    "entry_agent": "a",
                    "agents": {
                        "a": {"role": "A", "description": "D"},
                        "b": {"role": "B", "description": "D"},
                    },
                    "edges": [["a", "b"]],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        schema = TopologiesFileSchema.model_validate(data)
        assert schema.topologies["test"].edges == [("a", "b")]

    def test_invalid_synthesis_agent(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "T",
                    "description": "D",
                    "entry_agent": "a",
                    "synthesis_agent": "nonexistent",
                    "agents": {"a": {"role": "A", "description": "D"}},
                    "edges": [],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        with pytest.raises(ValueError, match="synthesis_agent"):
            TopologiesFileSchema.model_validate(data)

    def test_agent_defaults(self):
        agent = AgentSchema(role="W", description="D")
        assert agent.base_complexity == 50
        assert agent.max_history == 20
        assert agent.enable_filesystem is False
        assert agent.suppress_context_guidelines is False
        assert agent.model_selection is None

    def test_model_selection_schema(self):
        data = {
            "version": "1.0",
            "topologies": {
                "test": {
                    "id": "test",
                    "label": "T",
                    "description": "D",
                    "entry_agent": "a",
                    "agents": {
                        "a": {
                            "role": "A",
                            "description": "D",
                            "model_selection": {
                                "prefer_provider": "anthropic",
                                "fallback_providers": ["openai"],
                            },
                        },
                    },
                    "edges": [],
                    "workflow_steps": [],
                    "completion_rules": {},
                }
            },
        }
        schema = TopologiesFileSchema.model_validate(data)
        ms = schema.topologies["test"].agents["a"].model_selection
        assert ms is not None
        assert ms.prefer_provider == "anthropic"
        assert ms.fallback_providers == ["openai"]
