from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .defaults import BUILTIN_TOPOLOGIES_YAML
from .schema import TopologiesFileSchema, TopologySchema

logger = logging.getLogger(__name__)

USER_TOPOLOGIES_PATH = Path.home() / ".orb" / "topologies.yaml"


class TopologyLoader:
    """Loads, caches, and hot-reloads topology definitions from YAML."""

    def __init__(self) -> None:
        self._cache: dict[str, TopologySchema] = {}
        self._file_mtime: float = 0.0
        self._loaded = False

    def load(self, force: bool = False) -> dict[str, TopologySchema]:
        """Load all topologies, merging builtins with user overrides."""
        if self._loaded and not force:
            return self._cache

        builtin = self._parse_yaml(BUILTIN_TOPOLOGIES_YAML)
        self._cache = dict(builtin.topologies)

        if USER_TOPOLOGIES_PATH.exists():
            try:
                mtime = USER_TOPOLOGIES_PATH.stat().st_mtime
                user_data = USER_TOPOLOGIES_PATH.read_text()
                user = self._parse_yaml(user_data)
                self._cache.update(user.topologies)
                self._file_mtime = mtime
                logger.info(
                    "Loaded %d user topologies from %s",
                    len(user.topologies),
                    USER_TOPOLOGIES_PATH,
                )
            except Exception as exc:
                logger.warning("Failed to load user topologies: %s", exc)

        self._loaded = True
        return self._cache

    def reload_if_changed(self) -> bool:
        """Check if user file changed and reload.  Returns True if reloaded."""
        if not USER_TOPOLOGIES_PATH.exists():
            return False
        try:
            current_mtime = USER_TOPOLOGIES_PATH.stat().st_mtime
            if current_mtime > self._file_mtime:
                self.load(force=True)
                return True
        except OSError:
            pass
        return False

    def get(self, topology_id: str) -> TopologySchema | None:
        if not self._loaded:
            self.load()
        return self._cache.get(topology_id)

    def list_ids(self) -> list[str]:
        if not self._loaded:
            self.load()
        return list(self._cache.keys())

    def list_all(self) -> dict[str, TopologySchema]:
        if not self._loaded:
            self.load()
        return dict(self._cache)

    def validate_file(self, path: Path) -> tuple[bool, str]:
        """Validate a topology YAML file.  Returns (valid, message)."""
        try:
            content = path.read_text()
            self._parse_yaml(content)
            return True, "Valid"
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _parse_yaml(content: str) -> TopologiesFileSchema:
        data = yaml.safe_load(content)
        return TopologiesFileSchema.model_validate(data)


# ---- global singleton ----

_loader: TopologyLoader | None = None


def get_loader() -> TopologyLoader:
    global _loader
    if _loader is None:
        _loader = TopologyLoader()
    return _loader
