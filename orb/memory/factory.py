"""SubgraphStoreFactory — creates SubgraphStore instances from a backend name."""

from __future__ import annotations

from orb.memory.subgraph_store import SubgraphStore


class SubgraphStoreFactory:
    """Factory for SubgraphStore backends."""

    @staticmethod
    def from_config(backend: str, **kwargs) -> SubgraphStore:
        """Instantiate a SubgraphStore backend by name.

        Supported backends
        ------------------
        ``"chroma"``
            In-process ChromaDB + NetworkX backend (no external services).
            Accepts an optional ``persist_path`` kwarg.
        ``"zep"``
            Zep Cloud backend (imported lazily to avoid hard dependency).
            Requires ``api_key`` kwarg and ``zep-python`` installed.
        Raises
        ------
        ValueError
            If *backend* is not recognised.
        """
        if backend == "chroma":
            from orb.memory.backends.chromadb_networkx import ChromaDBNetworkXStore

            return ChromaDBNetworkXStore(**kwargs)
        elif backend == "zep":
            from orb.memory.backends.zep_cloud import ZepCloudStore  # lazy import

            return ZepCloudStore(**kwargs)
        else:
            raise ValueError(f"Unknown backend: {backend!r}")
