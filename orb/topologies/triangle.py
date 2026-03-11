from __future__ import annotations

from .triad import create_triad


def create_triangle(*args, **kwargs):
    """Backward-compatible alias for the renamed triad topology factory."""
    return create_triad(*args, **kwargs)

