from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from loopgraph.adapters.demo import DemoHarnessAdapter
from loopgraph.models import AgentRequest, AgentResult, Decision, NodeName, RunConfig, RunStatus
from loopgraph.storage import IntegrityViolation, SQLiteRepository
from loopgraph.supervisor import LoopGraphSupervisor


class BlockingHarness:
    name = "blocking"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, request: AgentRequest) -> AgentResult:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release blocking harness")
        return AgentResult(output="ready")

    def close(self) -> None:
        return None


class CrashHarness:
    name = "crash"

    def execute(self, request: AgentRequest) -> AgentResult:
        raise KeyboardInterrupt("simulated process death")

    def close(self) -> None:
        return None


class SupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "loopgraph.db"
        self.repository = SQLiteRepository(self.db)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def config(self, **changes: object) -> RunConfig:
        values: dict[str, object] = {
            "goal": "produce a candidate",
            "workspace": str(self.root),
            "adapter": "demo",
            "verifier": "always_pass",
            "max_iterations": 3,
            "require_approval": False,
            "channel": "test",
        }
        values.update(changes)
        return RunConfig(**values)  # type: ignore[arg-type]

    def test_happy_path_executes_verifies_and_promotes(self) -> None:
        supervisor = LoopGraphSupervisor(self.repository)
        created = supervisor.create_run(self.config())
        final = supervisor.drive(created.run_id)

        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(final.current_node, NodeName.DONE)
        self.assertEqual(
            self.repository.get_channel("test"), final.candidate_version_id
        )
        event_types = [
            event.event_type for event in self.repository.list_events(created.run_id)
        ]
        self.assertEqual(
            event_types,
            [
                "run.created",
                "agent.started",
                "agent.completed",
                "version.candidate_created",
                "verification.started",
                "verification.completed",
                "release.promoted",
                "run.succeeded",
            ],
        )
        self.repository.verify_integrity(created.run_id)

    def test_verification_failure_loops_then_waits_for_hitl(self) -> None:
        supervisor = LoopGraphSupervisor(
            self.repository,
            harnesses={"demo": DemoHarnessAdapter(fail_first=True)},
        )
        created = supervisor.create_run(
            self.config(verifier="contains:VERIFIED", require_approval=True)
        )
        waiting = supervisor.drive(created.run_id)

        self.assertEqual(waiting.status, RunStatus.AWAITING_HUMAN)
        self.assertEqual(waiting.iteration, 2)
        versions = self.repository.list_versions(created.run_id)
        self.assertEqual([version.status.value for version in versions], ["rejected", "validated"])

        supervisor.decide(
            created.run_id, Decision.APPROVE, feedback="looks good after the retry"
        )
        final = supervisor.drive(created.run_id)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(self.repository.get_channel("test"), final.candidate_version_id)

    def test_paused_run_resumes_in_a_new_supervisor(self) -> None:
        first = LoopGraphSupervisor(self.repository, worker_id="first")
        created = first.create_run(self.config())
        after_execute = first.drive(created.run_id, max_steps=1)
        self.assertEqual(after_execute.current_node, NodeName.VERIFY)

        paused = first.pause(created.run_id)
        self.assertEqual(paused.status, RunStatus.PAUSED)

        second = LoopGraphSupervisor(
            SQLiteRepository(self.db), worker_id="second"
        )
        second.resume(created.run_id)
        final = second.drive(created.run_id)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(final.iteration, 1)

    def test_pause_during_external_step_is_honored_at_safe_boundary(self) -> None:
        harness = BlockingHarness()
        supervisor = LoopGraphSupervisor(
            self.repository,
            harnesses={"blocking": harness},
            worker_id="blocking-worker",
            lease_ttl_seconds=3,
        )
        created = supervisor.create_run(self.config(adapter="blocking"))
        result: list[object] = []

        def run() -> None:
            result.append(supervisor.drive(created.run_id, max_steps=1))

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(harness.started.wait(timeout=2))
        requested = supervisor.pause(created.run_id)
        self.assertTrue(requested.pause_requested)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        harness.release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

        paused = self.repository.get_state(created.run_id)
        self.assertEqual(paused.status, RunStatus.PAUSED)
        self.assertEqual(paused.current_node, NodeName.VERIFY)
        self.assertIsNone(paused.pending_step_id)

    def test_interrupted_side_effect_requires_explicit_recovery_decision(self) -> None:
        first = LoopGraphSupervisor(
            self.repository,
            harnesses={"crash": CrashHarness()},
            worker_id="doomed-worker",
        )
        created = first.create_run(self.config(adapter="crash"))
        with self.assertRaises(KeyboardInterrupt):
            first.drive(created.run_id)

        interrupted = self.repository.get_state(created.run_id)
        self.assertIsNotNone(interrupted.pending_step_id)

        second = LoopGraphSupervisor(
            SQLiteRepository(self.db),
            harnesses={"crash": DemoHarnessAdapter()},
            worker_id="replacement-worker",
        )
        blocked = second.drive(created.run_id)
        self.assertEqual(blocked.status, RunStatus.RECOVERY_REQUIRED)
        self.assertIsNotNone(blocked.pending_step_id)

        retrying = second.decide(created.run_id, Decision.RETRY)
        self.assertEqual(retrying.status, RunStatus.RUNNING)
        self.assertIsNone(retrying.pending_step_id)
        final = second.drive(created.run_id)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)

    def test_channel_can_roll_back_to_previous_active_version(self) -> None:
        supervisor = LoopGraphSupervisor(self.repository)
        first = supervisor.create_run(self.config(goal="version one"))
        first_final = supervisor.drive(first.run_id)
        version_one = first_final.candidate_version_id

        second = supervisor.create_run(self.config(goal="version two"))
        second_final = supervisor.drive(second.run_id)
        version_two = second_final.candidate_version_id
        self.assertNotEqual(version_one, version_two)
        self.assertEqual(self.repository.get_channel("test"), version_two)

        supervisor.request_rollback(second.run_id)
        rolled = supervisor.drive(second.run_id)
        self.assertEqual(rolled.status, RunStatus.ROLLED_BACK)
        self.assertEqual(self.repository.get_channel("test"), version_one)
        self.assertEqual(rolled.rollback_target_version_id, version_one)

    def test_compare_and_swap_blocks_stale_promotion(self) -> None:
        supervisor = LoopGraphSupervisor(self.repository)
        first = supervisor.create_run(self.config(goal="first concurrent run"))
        second = supervisor.create_run(self.config(goal="second concurrent run"))
        self.assertIsNone(first.base_version_id)
        self.assertIsNone(second.base_version_id)

        first_final = supervisor.drive(first.run_id)
        self.assertEqual(first_final.status, RunStatus.SUCCEEDED)
        second_blocked = supervisor.drive(second.run_id)
        self.assertEqual(second_blocked.status, RunStatus.AWAITING_HUMAN)
        self.assertTrue((second_blocked.pending_reason or "").startswith("promotion_conflict"))
        self.assertEqual(
            self.repository.get_channel("test"), first_final.candidate_version_id
        )

    def test_hash_chain_detects_event_tampering(self) -> None:
        supervisor = LoopGraphSupervisor(self.repository)
        created = supervisor.create_run(self.config())
        supervisor.drive(created.run_id)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE events SET payload_json = ? WHERE run_id = ? AND seq = 2",
                ('{"tampered":true}', created.run_id),
            )
        with self.assertRaises(IntegrityViolation):
            self.repository.verify_integrity(created.run_id)


if __name__ == "__main__":
    unittest.main()
