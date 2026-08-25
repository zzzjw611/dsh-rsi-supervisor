from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import AgentRequest, AgentResult, VerificationRequest, VerificationResult


@runtime_checkable
class HarnessAdapter(Protocol):
    """The only contract the graph core needs from an inner agent harness."""

    @property
    def name(self) -> str: ...

    def execute(self, request: AgentRequest) -> AgentResult: ...

    def close(self) -> None: ...


@runtime_checkable
class Verifier(Protocol):
    @property
    def name(self) -> str: ...

    def verify(self, request: VerificationRequest) -> VerificationResult: ...


class HarnessExecutionError(RuntimeError):
    pass


class VerificationError(RuntimeError):
    pass
