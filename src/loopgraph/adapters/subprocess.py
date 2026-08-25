from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from ..models import AgentRequest, AgentResult
from ..ports import HarnessExecutionError


class SubprocessHarnessAdapter:
    """Harness-neutral JSON stdin/stdout adapter for any CLI-based agent runtime."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 900) -> None:
        if not command:
            raise ValueError("subprocess adapter command must not be empty")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "subprocess"

    def execute(self, request: AgentRequest) -> AgentResult:
        payload = {
            "protocol_version": "loopgraph.harness.v1",
            "request": {
                "run_id": request.run_id,
                "step_id": request.step_id,
                "goal": request.goal,
                "workspace": request.workspace,
                "iteration": request.iteration,
                "feedback": request.feedback,
                "base_version_id": request.base_version_id,
                "previous_candidate": request.previous_candidate,
                "metadata": request.metadata,
            },
        }
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=request.workspace,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessExecutionError(f"subprocess harness failed: {exc}") from exc
        if completed.returncode != 0:
            raise HarnessExecutionError(
                f"subprocess harness exited {completed.returncode}: {completed.stderr[-2000:]}"
            )
        try:
            response = json.loads(completed.stdout)
            return AgentResult(
                output=str(response["output"]),
                artifact_ref=response.get("artifact_ref"),
                metadata=dict(response.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HarnessExecutionError("subprocess harness returned invalid JSON") from exc

    def close(self) -> None:
        return None
