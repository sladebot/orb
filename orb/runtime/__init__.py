from __future__ import annotations

__all__ = ["GraphRuntime", "RuntimeManager"]


def __getattr__(name: str):
    if name == "GraphRuntime":
        from .graph_runtime import GraphRuntime
        return GraphRuntime
    if name == "RuntimeManager":
        from .manager import RuntimeManager
        return RuntimeManager
    raise AttributeError(name)
