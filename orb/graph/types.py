from dataclasses import dataclass
from typing import Any

NodeId = str


@dataclass(frozen=True)
class Edge:
    a: NodeId
    b: NodeId

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Edge):
            return NotImplemented
        return {self.a, self.b} == {other.a, other.b}

    def __hash__(self) -> int:
        return hash(frozenset({self.a, self.b}))
