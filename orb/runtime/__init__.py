from __future__ import annotations

__all__ = ["GraphRuntime"]


def __getattr__(name: str):
    if name == "GraphRuntime":
        from .graph_runtime import GraphRuntime
        return GraphRuntime
    raise AttributeError(name)
