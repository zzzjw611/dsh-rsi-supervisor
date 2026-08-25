from __future__ import annotations

from dataclasses import dataclass

from .models import NodeName


@dataclass(frozen=True, slots=True)
class Edge:
    source: NodeName
    outcome: str
    target: NodeName


class LoopGraph:
    """An explicit, inspectable control-flow graph for the supervisor outer loop."""

    def __init__(self, *, require_approval: bool) -> None:
        verified_target = NodeName.HITL if require_approval else NodeName.PROMOTE
        self._edges = (
            Edge(NodeName.EXECUTE, "completed", NodeName.VERIFY),
            Edge(NodeName.EXECUTE, "retry", NodeName.EXECUTE),
            Edge(NodeName.EXECUTE, "exhausted", NodeName.HITL),
            Edge(NodeName.VERIFY, "passed", verified_target),
            Edge(NodeName.VERIFY, "retry", NodeName.EXECUTE),
            Edge(NodeName.VERIFY, "exhausted", NodeName.HITL),
            Edge(NodeName.HITL, "approve", NodeName.PROMOTE),
            Edge(NodeName.HITL, "revise", NodeName.EXECUTE),
            Edge(NodeName.HITL, "rollback", NodeName.ROLLBACK),
            Edge(NodeName.HITL, "reject", NodeName.DONE),
            Edge(NodeName.PROMOTE, "promoted", NodeName.DONE),
            Edge(NodeName.PROMOTE, "conflict", NodeName.HITL),
            Edge(NodeName.ROLLBACK, "rolled_back", NodeName.DONE),
        )
        self._index = {(edge.source, edge.outcome): edge.target for edge in self._edges}

    def route(self, source: NodeName, outcome: str) -> NodeName:
        try:
            return self._index[(source, outcome)]
        except KeyError as exc:
            raise ValueError(f"no edge from {source.value!r} for outcome {outcome!r}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": [node.value for node in NodeName],
            "edges": [
                {
                    "source": edge.source.value,
                    "outcome": edge.outcome,
                    "target": edge.target.value,
                }
                for edge in self._edges
            ],
        }
