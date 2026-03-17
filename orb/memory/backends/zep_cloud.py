"""ZepCloudStore — stub backend for Zep Cloud GraphRAG.

This is a Phase 1 stub. The class is importable and validates its constructor
arguments, but all data methods raise NotImplementedError. Full wiring happens
in Phase 3.

Requirements
------------
- ``zep-python`` must be installed: ``pip install zep-python``
- A valid Zep Cloud API key must be provided, typically via the ``ZEP_API_KEY``
  environment variable.
"""

from __future__ import annotations

from orb.memory.subgraph_store import Fact, SubgraphStore


class ZepCloudStore(SubgraphStore):
    """Stub SubgraphStore backend backed by Zep Cloud.

    This class exists so the factory and imports work end-to-end before Phase 3
    implements the live connection. Every data method raises NotImplementedError
    until Phase 3 wires up the real Zep client calls.

    Parameters
    ----------
    api_key:
        Zep Cloud API key. Set the ``ZEP_API_KEY`` environment variable and
        pass ``os.environ["ZEP_API_KEY"]`` here.
    collection_name:
        Name of the Zep memory collection to use. Defaults to ``"orb_facts"``.

    Raises
    ------
    ImportError
        If ``zep-python`` is not installed.
    ValueError
        If *api_key* is empty.
    """

    def __init__(self, api_key: str, collection_name: str = "orb_facts") -> None:
        if not api_key:
            raise ValueError("ZEP_API_KEY is required")

        try:
            import zep_python  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "zep-python is required for ZepCloudStore. "
                "Install it with: pip install zep-python"
            ) from exc

        self._api_key = api_key
        self._collection_name = collection_name

    async def upsert_fact(self, fact: Fact) -> None:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")

    async def get_facts(self, agent_id: str, *, limit: int = 20) -> list[Fact]:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")

    async def delete_facts(self, agent_id: str) -> int:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")

    async def query(
        self,
        text: str,
        *,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> list[Fact]:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")

    async def get_all_facts(self, *, limit: int = 200) -> list[Fact]:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")

    async def close(self) -> None:
        raise NotImplementedError("ZepCloudStore is not yet fully implemented")
