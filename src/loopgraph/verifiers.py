from __future__ import annotations

import subprocess

from .models import VerificationRequest, VerificationResult
from .ports import VerificationError


class AlwaysPassVerifier:
    @property
    def name(self) -> str:
        return "always_pass"

    def verify(self, request: VerificationRequest) -> VerificationResult:
        return VerificationResult(passed=True, summary="No-op verification passed", score=1.0)


class ContainsVerifier:
    def __init__(self, expected: str) -> None:
        self.expected = expected

    @property
    def name(self) -> str:
        return f"contains:{self.expected}"

    def verify(self, request: VerificationRequest) -> VerificationResult:
        output = str(request.candidate.get("output", ""))
        passed = self.expected in output
        return VerificationResult(
            passed=passed,
            summary=(
                f"Candidate contains required marker {self.expected!r}"
                if passed
                else f"Candidate is missing required marker {self.expected!r}"
            ),
            score=1.0 if passed else 0.0,
            evidence=({"kind": "substring", "expected": self.expected, "passed": passed},),
        )


class CommandVerifier:
    def __init__(self, commands: tuple[tuple[str, ...], ...], *, timeout_seconds: float = 300) -> None:
        if not commands:
            raise ValueError("command verifier requires at least one command")
        self.commands = commands
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "command"

    def verify(self, request: VerificationRequest) -> VerificationResult:
        evidence: list[dict[str, object]] = []
        for command in self.commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=request.workspace,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VerificationError(f"could not execute {command!r}: {exc}") from exc
            item = {
                "command": list(command),
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
            evidence.append(item)
            if completed.returncode != 0:
                return VerificationResult(
                    passed=False,
                    summary=f"Verification command failed: {' '.join(command)}",
                    score=0.0,
                    evidence=tuple(evidence),
                )
        return VerificationResult(
            passed=True,
            summary=f"All {len(self.commands)} verification command(s) passed",
            score=1.0,
            evidence=tuple(evidence),
        )
