from __future__ import annotations

import json
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .dashboard import asset, dsh_environment, experiment_for_run, snapshot
from .models import Decision, RunConfig
from .rsi import RsiExperiment
from .storage import RunNotFound, StorageError
from .supervisor import InvalidTransition, LoopGraphSupervisor


class SupervisorHTTPServer(ThreadingHTTPServer):
    supervisor: LoopGraphSupervisor
    workspace: str


class SupervisorAPIHandler(BaseHTTPRequestHandler):
    server: SupervisorHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            selected = asset(parsed.path)
            if selected is not None:
                content, content_type = selected
                self._content(HTTPStatus.OK, content, content_type)
                return
            if parts == ["api", "rsi", "environment"]:
                self._json(HTTPStatus.OK, dsh_environment())
                return
            if parts == ["api", "rsi", "runs"]:
                states = self.server.supervisor.repository.list_runs()
                experiments = [
                    snapshot(self.server.supervisor.repository, state.run_id)
                    for state in states
                    if state.config.adapter == "rsi-evolver"
                ]
                self._json(HTTPStatus.OK, {"runs": experiments})
                return
            if len(parts) == 4 and parts[:3] == ["api", "rsi", "runs"]:
                self._json(HTTPStatus.OK, snapshot(self.server.supervisor.repository, parts[3]))
                return
            if parts == ["health"]:
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if parts == ["runs"]:
                states = self.server.supervisor.repository.list_runs()
                self._json(HTTPStatus.OK, {"runs": [state.to_dict() for state in states]})
                return
            if len(parts) == 2 and parts[0] == "runs":
                self._json(HTTPStatus.OK, self.server.supervisor.inspect(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                query = parse_qs(parsed.query)
                after = int(query.get("after", ["0"])[0])
                events = self.server.supervisor.repository.list_events(parts[1], after=after)
                self._json(HTTPStatus.OK, {"events": [event.to_dict() for event in events]})
                return
            if len(parts) == 4 and parts[0] == "runs" and parts[2:] == ["events", "stream"]:
                query = parse_qs(parsed.query)
                after = int(query.get("after", ["0"])[0])
                self._stream_events(parts[1], after=after)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except RunNotFound as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, InvalidTransition) as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except StorageError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            body = self._read_json()
            if parts == ["api", "rsi", "runs"]:
                self._create_rsi_run(body)
                return
            if len(parts) == 5 and parts[:3] == ["api", "rsi", "runs"]:
                self._operate_rsi_run(parts[3], parts[4], body)
                return
            if parts == ["runs"]:
                state = self.server.supervisor.create_run(RunConfig.from_dict(body))
                self._json(HTTPStatus.CREATED, state.to_dict())
                return
            if len(parts) != 3 or parts[0] != "runs":
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                return
            run_id, action = parts[1], parts[2]
            if action == "drive":
                state = self.server.supervisor.drive(run_id, max_steps=body.get("max_steps"))
            elif action == "pause":
                state = self.server.supervisor.pause(run_id)
            elif action == "resume":
                state = self.server.supervisor.resume(run_id)
            elif action == "decisions":
                state = self.server.supervisor.decide(
                    run_id,
                    Decision(body["decision"]),
                    feedback=body.get("feedback"),
                    rollback_target_version_id=body.get("rollback_target_version_id"),
                )
            elif action == "rollback":
                self.server.supervisor.request_rollback(
                    run_id, target_version_id=body.get("target_version_id")
                )
                state = self.server.supervisor.drive(run_id)
            elif action == "cancel":
                state = self.server.supervisor.cancel(
                    run_id, reason=body.get("reason", "cancelled through API")
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                return
            self._json(HTTPStatus.OK, state.to_dict())
        except RunNotFound as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (KeyError, ValueError, InvalidTransition) as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except StorageError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _create_rsi_run(self, body: dict[str, Any]) -> None:
        mode = str(body.get("mode", "replay"))
        if mode == "dsh":
            readiness = dsh_environment()
            if not readiness["sdk_installed"]:
                raise ValueError("Live DSH requires the official SDK: pip install -e '.[dsh]'")
            if not readiness["credential_configured"]:
                raise ValueError(
                    "Live DSH requires DEEPSEEK_API_KEY in the server process environment"
                )
        workspace = str(body.get("workspace") or getattr(self.server, "workspace", Path.cwd()))
        channel = str(body.get("channel") or f"rsi-dashboard-{uuid.uuid4().hex[:10]}")
        experiment = RsiExperiment(
            self.server.supervisor.repository,
            mode=mode,
            workspace=workspace,
            channel=channel,
            require_approval=bool(body.get("require_approval", True)),
            max_generations=int(body.get("max_generations", 3)),
            minimum_holdout_score=float(body.get("minimum_holdout_score", 0.75)),
        )
        try:
            state = experiment.start()
            self._json(HTTPStatus.CREATED, snapshot(experiment.repository, state.run_id))
        finally:
            experiment.close()

    def _operate_rsi_run(self, run_id: str, action: str, body: dict[str, Any]) -> None:
        repository = self.server.supervisor.repository
        if action == "probe":
            experiment = experiment_for_run(repository, run_id)
            try:
                trace = dict(body.get("trace", {}))
                self._json(HTTPStatus.OK, experiment.act(trace))
            finally:
                experiment.close()
            return

        experiment = experiment_for_run(repository, run_id, executable=True)
        try:
            if action == "step":
                experiment.supervisor.drive(run_id, max_steps=1)
                experiment.materialize_active_skill()
            elif action == "run":
                experiment.drive(run_id, auto_approve=False)
            elif action == "approve":
                experiment.supervisor.decide(
                    run_id,
                    Decision.APPROVE,
                    feedback=str(body.get("feedback", "Approved from the RSI control plane")),
                )
                experiment.drive(run_id)
            elif action == "revise":
                experiment.supervisor.decide(
                    run_id,
                    Decision.REVISE,
                    feedback=str(body.get("feedback", "Revision requested from dashboard")),
                )
            elif action == "pause":
                experiment.supervisor.pause(run_id)
            elif action == "resume":
                experiment.supervisor.resume(run_id)
            elif action == "rollback":
                experiment.rollback(run_id, target_version_id=body.get("target_version_id"))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown RSI action"})
                return
            self._json(HTTPStatus.OK, snapshot(repository, run_id))
        finally:
            experiment.close()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        self._content(status, body, "application/json; charset=utf-8")

    def _content(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_events(self, run_id: str, *, after: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        deadline = time.monotonic() + 30
        cursor = after
        while time.monotonic() < deadline:
            events = self.server.supervisor.repository.list_events(run_id, after=cursor)
            if events:
                for event in events:
                    payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
                    self.wfile.write(f"id: {event.seq}\nevent: {event.event_type}\ndata: {payload}\n\n".encode())
                    cursor = event.seq
                self.wfile.flush()
            else:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            state = self.server.supervisor.repository.get_state(run_id)
            if state.status.terminal:
                return
            time.sleep(0.5)


def serve(
    supervisor: LoopGraphSupervisor,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    workspace: str | None = None,
) -> None:
    server = SupervisorHTTPServer((host, port), SupervisorAPIHandler)
    server.supervisor = supervisor
    server.workspace = str(Path(workspace or ".").resolve())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        supervisor.close()
