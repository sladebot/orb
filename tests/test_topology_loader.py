import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from orb.topologies.loader import TopologyLoader


class TestTopologyLoader:
    def test_load_builtin_topologies(self):
        loader = TopologyLoader()
        topos = loader.load()
        assert "triad" in topos
        assert "dual-review" in topos
        assert "hierarchy" in topos

    def test_get_triad(self):
        loader = TopologyLoader()
        loader.load()
        topo = loader.get("triad")
        assert topo is not None
        assert topo.label == "Triad"
        assert "coordinator" in topo.agents
        assert "coder" in topo.agents
        assert "reviewer" in topo.agents
        assert "tester" in topo.agents

    def test_get_dual_review(self):
        loader = TopologyLoader()
        loader.load()
        topo = loader.get("dual-review")
        assert topo is not None
        assert topo.label == "Dual Review"
        assert "reviewer_a" in topo.agents
        assert "reviewer_b" in topo.agents

    def test_get_hierarchy(self):
        loader = TopologyLoader()
        loader.load()
        topo = loader.get("hierarchy")
        assert topo is not None
        assert topo.label == "Hierarchy"
        assert "researcher" in topo.agents
        assert topo.graph_view is not None
        assert topo.agents["researcher"].category == "discovery"

    def test_get_unknown_returns_none(self):
        loader = TopologyLoader()
        loader.load()
        assert loader.get("nonexistent") is None

    def test_list_ids(self):
        loader = TopologyLoader()
        loader.load()
        ids = loader.list_ids()
        assert "triad" in ids
        assert "dual-review" in ids
        assert "hierarchy" in ids

    def test_user_file_overlay(self):
        user_yaml = """\
version: "1.0"
topologies:
  custom:
    id: custom
    label: Custom
    description: A custom topology
    entry_agent: worker
    agents:
      worker:
        role: Worker
        description: Does things
    edges: []
    workflow_steps:
      - "Worker does the work."
    completion_rules: {}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(user_yaml)
            tmp = Path(f.name)

        try:
            with patch(
                "orb.topologies.loader.USER_TOPOLOGIES_PATH", tmp
            ):
                loader = TopologyLoader()
                topos = loader.load()
                assert "custom" in topos
                # builtins still present
                assert "triad" in topos
        finally:
            tmp.unlink()

    def test_reload_if_changed(self):
        user_yaml_v1 = """\
version: "1.0"
topologies:
  custom:
    id: custom
    label: V1
    description: Version 1
    entry_agent: a
    agents:
      a:
        role: A
        description: D
    edges: []
    workflow_steps: []
    completion_rules: {}
"""
        user_yaml_v2 = """\
version: "1.0"
topologies:
  custom:
    id: custom
    label: V2
    description: Version 2
    entry_agent: a
    agents:
      a:
        role: A
        description: D
    edges: []
    workflow_steps: []
    completion_rules: {}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(user_yaml_v1)
            tmp = Path(f.name)

        try:
            with patch(
                "orb.topologies.loader.USER_TOPOLOGIES_PATH", tmp
            ):
                loader = TopologyLoader()
                loader.load()
                assert loader.get("custom").label == "V1"

                # No change yet
                assert loader.reload_if_changed() is False

                # Write v2 with a future mtime
                import os, time

                tmp.write_text(user_yaml_v2)
                # Ensure mtime advances
                future = time.time() + 2
                os.utime(tmp, (future, future))

                assert loader.reload_if_changed() is True
                assert loader.get("custom").label == "V2"
        finally:
            tmp.unlink()

    def test_validate_file_valid(self):
        content = """\
version: "1.0"
topologies:
  ok:
    id: ok
    label: OK
    description: Valid
    entry_agent: x
    agents:
      x:
        role: X
        description: D
    edges: []
    workflow_steps: []
    completion_rules: {}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(content)
            tmp = Path(f.name)

        try:
            loader = TopologyLoader()
            valid, msg = loader.validate_file(tmp)
            assert valid is True
        finally:
            tmp.unlink()

    def test_validate_file_invalid(self):
        content = """\
version: "1.0"
topologies:
  bad:
    id: bad
    label: Bad
    description: Invalid
    entry_agent: ghost
    agents:
      x:
        role: X
        description: D
    edges: []
    workflow_steps: []
    completion_rules: {}
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(content)
            tmp = Path(f.name)

        try:
            loader = TopologyLoader()
            valid, msg = loader.validate_file(tmp)
            assert valid is False
            assert "entry_agent" in msg
        finally:
            tmp.unlink()

    def test_bad_user_file_does_not_break_builtins(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("not: valid: yaml: {{[")
            tmp = Path(f.name)

        try:
            with patch(
                "orb.topologies.loader.USER_TOPOLOGIES_PATH", tmp
            ):
                loader = TopologyLoader()
                topos = loader.load()
                # builtins should still load
                assert "triad" in topos
        finally:
            tmp.unlink()
