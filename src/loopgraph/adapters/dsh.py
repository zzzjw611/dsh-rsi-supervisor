from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..models import AgentRequest, AgentResult
from ..ports import HarnessExecutionError


class DeepSeekHarnessAdapter:
    """First-class DSH SDK adapter kept behind the harness-neutral port.

    Each LoopGraph step receives its own DSH session id. LoopGraph owns cross-process
    recovery and feeds durable context back into a new turn; it does not depend on a
    particular DSH release's cold-session resume behavior.
    """

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        session_root: str | None = None,
        cordis: str | None = None,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self.provider = provider or os.getenv("DSH_PROVIDER", "deepseek-official")
        self.model = model or os.getenv("DSH_MODEL", "deepseek-v4-flash")
        self.max_tokens = max_tokens or int(os.getenv("DSH_MAX_TOKENS", "49152"))
        self.session_root = session_root or os.getenv("DSH_SESSION_ROOT", ".dsh-sessions")
        self.cordis = cordis or os.getenv("DSH_CORDIS")
        self._factory = factory
        self._clients: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "dsh"

    def execute(self, request: AgentRequest) -> AgentResult:
        client = self._client_for(request.workspace)
        prompt = self._build_prompt(request)
        session_id = f"loopgraph-{request.step_id}"
        try:
            result = client.run(prompt, session_id=session_id)
        except Exception as exc:
            raise HarnessExecutionError(f"DSH invocation failed: {exc}") from exc

        finish_reason = getattr(result, "finish_reason", None)
        output = getattr(result, "final_response", None)
        if finish_reason == "error" or not isinstance(output, str):
            raise HarnessExecutionError(
                f"DSH turn did not complete successfully (finish_reason={finish_reason!r})"
            )
        events = getattr(result, "events", ())
        notifications = getattr(result, "notifications", ())
        return AgentResult(
            output=output,
            artifact_ref=f"dsh-session://{getattr(result, 'session_id', session_id)}",
            metadata={
                "adapter": self.name,
                "provider": self.provider,
                "model": self.model,
                "finish_reason": finish_reason,
                "session_id": getattr(result, "session_id", session_id),
                "session_root": str(getattr(result, "session_root", self.session_root)),
                "event_count": len(events),
                "notification_count": len(notifications),
            },
        )

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._clients.clear()

    def _client_for(self, workspace: str) -> Any:
        normalized = str(Path(workspace).resolve())
        if normalized in self._clients:
            return self._clients[normalized]
        factory = self._factory
        if factory is None:
            try:
                from deepseek_harness import DeepSeekHarness
            except ImportError as exc:
                raise HarnessExecutionError(
                    "DSH adapter requires `pip install -e '.[dsh]'`"
                ) from exc
            factory = DeepSeekHarness
        options: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "cwd": normalized,
            "session_root": str(Path(self.session_root).resolve()),
        }
        if self.cordis:
            options["cordis"] = str(Path(self.cordis).resolve())
        client = factory(**options)
        self._clients[normalized] = client
        return client

    @staticmethod
    def _build_prompt(request: AgentRequest) -> str:
        if request.metadata.get("role") == "scaffold_evolver":
            return "\n".join(
                [
                    f"Durable run: {request.run_id}",
                    f"Step idempotency key: {request.step_id}",
                    f"Generation: {request.iteration}",
                    request.goal,
                    "Return the candidate Python function as your final response.",
                ]
            )
        lines = [
            "You are executing one bounded step inside a durable LoopGraph supervisor.",
            f"Run: {request.run_id}",
            f"Step idempotency key: {request.step_id}",
            f"Iteration: {request.iteration}",
            f"Goal: {request.goal}",
            f"Workspace: {request.workspace}",
            "Make the requested changes, run focused checks, and summarize the resulting artifact.",
        ]
        if request.feedback:
            lines.extend(["Verifier/HITL feedback from the prior iteration:", request.feedback])
        if request.previous_candidate:
            previous_output = str(request.previous_candidate.get("output", ""))[-4000:]
            lines.extend(["Previous candidate summary (bounded):", previous_output])
        return "\n".join(lines)
