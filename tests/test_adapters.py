from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from loopgraph.adapters.dsh import DeepSeekHarnessAdapter
from loopgraph.models import AgentRequest


class FakeDshClient:
    def __init__(self, **options: object) -> None:
        self.options = options
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def run(self, prompt: str, *, session_id: str) -> SimpleNamespace:
        self.calls.append((prompt, session_id))
        return SimpleNamespace(
            session_id=session_id,
            final_response="implemented and tested",
            finish_reason="completed",
            events=(1, 2),
            notifications=(1,),
            session_root="/tmp/sessions",
        )

    def close(self) -> None:
        self.closed = True


class DshAdapterTest(unittest.TestCase):
    def test_dsh_is_a_bounded_harness_adapter(self) -> None:
        clients: list[FakeDshClient] = []

        def factory(**options: object) -> FakeDshClient:
            client = FakeDshClient(**options)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as directory:
            adapter = DeepSeekHarnessAdapter(factory=factory, session_root=directory)
            result = adapter.execute(
                AgentRequest(
                    run_id="run_1",
                    step_id="step_1",
                    goal="fix tests",
                    workspace=str(Path(directory)),
                    iteration=2,
                    feedback="test x failed",
                    base_version_id="ver_0",
                    previous_candidate={"output": "old draft"},
                    metadata={},
                )
            )
            self.assertEqual(result.output, "implemented and tested")
            self.assertEqual(result.artifact_ref, "dsh-session://loopgraph-step_1")
            self.assertEqual(result.metadata["event_count"], 2)
            prompt, session_id = clients[0].calls[0]
            self.assertEqual(session_id, "loopgraph-step_1")
            self.assertIn("test x failed", prompt)
            self.assertIn("Step idempotency key: step_1", prompt)
            adapter.close()
            self.assertTrue(clients[0].closed)


if __name__ == "__main__":
    unittest.main()
