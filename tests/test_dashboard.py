from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from loopgraph.api import SupervisorAPIHandler, SupervisorHTTPServer
from loopgraph.storage import SQLiteRepository
from loopgraph.supervisor import LoopGraphSupervisor


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.supervisor = LoopGraphSupervisor(SQLiteRepository(self.workspace / "dashboard.db"))
        self.server = SupervisorHTTPServer(("127.0.0.1", 0), SupervisorAPIHandler)
        self.server.supervisor = self.supervisor
        self.server.workspace = str(self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.supervisor.close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        self.connection.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def create_run(self) -> tuple[str, dict[str, object]]:
        status, snapshot = self.request("POST", "/api/rsi/runs", {"mode": "replay"})
        self.assertEqual(status, 201)
        report = snapshot["report"]
        assert isinstance(report, dict)
        return str(report["run_id"]), snapshot

    def test_dashboard_serves_packaged_html_css_and_javascript(self) -> None:
        for route, content_type, marker in (
            ("/", "text/html", b"A frozen agent"),
            ("/dashboard", "text/html", b"A frozen agent"),
            ("/assets/app.css", "text/css", b".pipeline-node"),
            ("/assets/replay.js", "text/javascript", b"LoopGraphStaticReplay"),
            ("/assets/app.js", "text/javascript", b"/api/rsi/environment"),
        ):
            with self.subTest(route=route):
                self.connection.request("GET", route)
                response = self.connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.getheader("Content-Type"))
                self.assertIn(marker, body)

    def test_environment_reports_credential_presence_without_leaking_secret(self) -> None:
        secret = "deepseek-sensitive-test-secret-never-return-this"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            self.connection.request("GET", "/api/rsi/environment")
            response = self.connection.getresponse()
            raw = response.read()
        self.assertEqual(response.status, 200)
        self.assertNotIn(secret.encode(), raw)
        self.assertTrue(json.loads(raw)["credential_configured"])

    def test_dashboard_includes_state_aware_interactive_interview_guide(self) -> None:
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        html = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn('id="demo-tour"', html)
        self.assertIn('id="tour-launch"', html)
        self.assertIn('id="tour-focus"', html)
        self.assertIn("INTERACTIVE WALKTHROUGH", html)
        self.assertIn('id="hosted-replay-note"', html)
        self.assertIn('src="assets/replay.js"', html)
        self.assertNotIn('src="/assets/', html)

        self.connection.request("GET", "/assets/app.js")
        response = self.connection.getresponse()
        javascript = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("function guideStep()", javascript)
        self.assertIn('target: "approve-button"', javascript)
        self.assertIn('target: "rollback-button"', javascript)
        self.assertIn('target: "probe-button"', javascript)
        self.assertIn("function executeGuideStep()", javascript)
        self.assertIn("target.click()", javascript)
        self.assertIn("Next:", javascript)
        self.assertIn("state.staticReplay", javascript)

        self.connection.request("GET", "/assets/replay.js")
        response = self.connection.getresponse()
        replay = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("HOSTED", replay.upper())
        self.assertIn("human_review.requested", replay)
        self.assertIn("release.promoted", replay)
        self.assertIn("release.rolled_back", replay)
        self.assertIn("localStorage", replay)

        self.connection.request("GET", "/assets/app.css")
        response = self.connection.getresponse()
        stylesheet = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("@keyframes tour-pulse", stylesheet)
        self.assertIn("prefers-reduced-motion", stylesheet)

    def test_replay_dashboard_rejects_approves_probes_and_rolls_back(self) -> None:
        run_id, initial = self.create_run()
        initial_report = initial["report"]
        assert isinstance(initial_report, dict)
        baseline = initial_report["baseline"]
        assert isinstance(baseline, dict)
        self.assertEqual(baseline["score"], 0.125)
        self.assertEqual(baseline["holdout_score"], 0.125)

        status, first = self.request("POST", f"/api/rsi/runs/{run_id}/step", {})
        self.assertEqual(status, 200)
        first_report = first["report"]
        assert isinstance(first_report, dict)
        first_generations = first_report["generations"]
        assert isinstance(first_generations, list)
        self.assertEqual(first_generations[0]["generation"], 1)

        status, review = self.request("POST", f"/api/rsi/runs/{run_id}/run", {})
        self.assertEqual(status, 200)
        review_state = review["state"]
        review_report = review["report"]
        assert isinstance(review_state, dict) and isinstance(review_report, dict)
        self.assertEqual(review_state["status"], "awaiting_human")
        review_generations = review_report["generations"]
        assert isinstance(review_generations, list)
        self.assertEqual(review_generations[0]["status"], "rejected")
        self.assertEqual(review_generations[1]["holdout_score"], 1.0)

        status, promoted = self.request("POST", f"/api/rsi/runs/{run_id}/approve", {})
        self.assertEqual(status, 200)
        promoted_report = promoted["report"]
        assert isinstance(promoted_report, dict)
        self.assertTrue(promoted_report["promoted"])

        status, probe = self.request(
            "POST",
            f"/api/rsi/runs/{run_id}/probe",
            {"trace": {"error": "403 Forbidden: deployment denied", "failure_count": 1}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(probe["action"], "request_human")

        status, rolled_back = self.request("POST", f"/api/rsi/runs/{run_id}/rollback", {})
        self.assertEqual(status, 200)
        restored_report = rolled_back["report"]
        assert isinstance(restored_report, dict)
        self.assertEqual(restored_report["status"], "rolled_back")
        self.assertFalse(restored_report["promoted"])
        restored_probe = restored_report["capability_probe"]
        assert isinstance(restored_probe, dict)
        self.assertEqual(restored_probe["active_action"], "inspect_context")

    def test_dashboard_pause_resume_and_run_inventory(self) -> None:
        run_id, _ = self.create_run()
        status, paused = self.request("POST", f"/api/rsi/runs/{run_id}/pause", {})
        self.assertEqual(status, 200)
        paused_state = paused["state"]
        assert isinstance(paused_state, dict)
        self.assertEqual(paused_state["status"], "paused")

        status, resumed = self.request("POST", f"/api/rsi/runs/{run_id}/resume", {})
        self.assertEqual(status, 200)
        resumed_state = resumed["state"]
        assert isinstance(resumed_state, dict)
        self.assertEqual(resumed_state["status"], "running")

        status, inventory = self.request("GET", "/api/rsi/runs")
        self.assertEqual(status, 200)
        runs = inventory["runs"]
        assert isinstance(runs, list)
        self.assertEqual(len(runs), 1)

    def test_live_mode_refuses_to_silently_fall_back_to_replay(self) -> None:
        readiness = {
            "sdk_installed": False,
            "credential_configured": False,
            "live_ready": False,
        }
        with patch("loopgraph.api.dsh_environment", return_value=readiness):
            status, result = self.request("POST", "/api/rsi/runs", {"mode": "dsh"})
        self.assertEqual(status, 409)
        self.assertIn("official SDK", str(result["error"]))


if __name__ == "__main__":
    unittest.main()
