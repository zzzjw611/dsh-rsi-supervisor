from __future__ import annotations

from ..models import AgentRequest, AgentResult


class DemoHarnessAdapter:
    """Deterministic adapter used by the zero-dependency demo and tests."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first

    @property
    def name(self) -> str:
        return "demo"

    def execute(self, request: AgentRequest) -> AgentResult:
        marker = "DRAFT" if self.fail_first and request.iteration == 1 else "VERIFIED"
        feedback = f" Applied feedback: {request.feedback}" if request.feedback else ""
        return AgentResult(
            output=f"{marker}: completed {request.goal}.{feedback}",
            artifact_ref=f"demo://{request.run_id}/{request.step_id}",
            metadata={"adapter": self.name, "iteration": request.iteration},
        )

    def close(self) -> None:
        return None
