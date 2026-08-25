from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from loopgraph.api import SupervisorAPIHandler, SupervisorHTTPServer
from loopgraph.storage import SQLiteRepository
from loopgraph.supervisor import LoopGraphSupervisor


class ApiTest(unittest.TestCase):
    def test_create_drive_and_observe_run_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = LoopGraphSupervisor(
                SQLiteRepository(Path(directory) / "api.db")
            )
            server = SupervisorHTTPServer(("127.0.0.1", 0), SupervisorAPIHandler)
            server.supervisor = supervisor
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=5
            )
            try:
                body = json.dumps(
                    {
                        "goal": "API integration",
                        "workspace": directory,
                        "adapter": "demo",
                        "verifier": "always_pass",
                        "require_approval": False,
                        "channel": "api-test",
                    }
                )
                connection.request(
                    "POST", "/runs", body=body, headers={"Content-Type": "application/json"}
                )
                response = connection.getresponse()
                created = json.loads(response.read())
                self.assertEqual(response.status, 201)
                run_id = created["run_id"]

                connection.request("POST", f"/runs/{run_id}/drive", body="{}")
                response = connection.getresponse()
                final = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(final["status"], "succeeded")

                connection.request("GET", f"/runs/{run_id}/events?after=0")
                response = connection.getresponse()
                observed = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(observed["events"][0]["event_type"], "run.created")
                self.assertEqual(observed["events"][-1]["event_type"], "run.succeeded")
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                supervisor.close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
