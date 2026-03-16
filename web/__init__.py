from __future__ import annotations

__all__ = ["DashboardServer", "DashboardBridge"]


def __getattr__(name: str):
    if name == "DashboardServer":
        from .server import DashboardServer
        return DashboardServer
    if name == "DashboardBridge":
        from .bridge import DashboardBridge
        return DashboardBridge
    raise AttributeError(name)
