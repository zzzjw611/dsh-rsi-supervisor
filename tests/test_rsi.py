from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopgraph.models import AgentRequest, AgentResult, Decision, RunStatus
from loopgraph.rsi import (
    DSH_SKILL_NAME,
    FIRST_GENERATION,
    SECOND_GENERATION,
    SEED_SKILL,
    FailureRecoveryBenchmark,
    RsiExperiment,
    UnsafeScaffold,
    extract_python_source,
    validate_scaffold_source,
)
from loopgraph.storage import SQLiteRepository


class CapturingHarness:
    name = "dsh"

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def execute(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(output=f"```python\n{SECOND_GENERATION}\n```")

    def close(self) -> None:
        return None


class RsiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = self.tempdir.name
        self.repository = SQLiteRepository(Path(self.workspace) / "rsi.db")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def experiment(self, **kwargs: object) -> RsiExperiment:
        values: dict[str, object] = {
            "mode": "replay",
            "workspace": self.workspace,
        }
        values.update(kwargs)
        return RsiExperiment(self.repository, **values)  # type: ignore[arg-type]

    def test_benchmark_measures_real_scaffold_improvement(self) -> None:
        benchmark = FailureRecoveryBenchmark()
        seed = benchmark.evaluate(SEED_SKILL)
        partial = benchmark.evaluate(FIRST_GENERATION)
        complete = benchmark.evaluate(SECOND_GENERATION)

        self.assertEqual(seed.passed_count, 2)
        self.assertEqual(seed.score, 0.125)
        self.assertGreater(partial.score, seed.score)
        self.assertLess(partial.holdout_score, 0.75)
        self.assertEqual(complete.score, 1.0)
        self.assertEqual(complete.holdout_score, 1.0)

    def test_replay_evolves_rejects_partial_then_promotes(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        final = experiment.drive(created.run_id, auto_approve=True)
        report = experiment.report(created.run_id)

        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(report["baseline"]["score"], 0.125)
        self.assertEqual(len(report["generations"]), 2)
        self.assertEqual(report["generations"][0]["status"], "rejected")
        self.assertEqual(report["generations"][1]["score"], 1.0)
        self.assertEqual(report["generations"][1]["holdout_score"], 1.0)
        self.assertTrue(report["promoted"])
        self.assertEqual(report["capability_probe"]["baseline_action"], "inspect_context")
        self.assertEqual(report["capability_probe"]["active_action"], "request_human")

    def test_promoted_scaffold_is_used_for_future_failures(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        trace = {"error": "403 Forbidden: denied by deployment policy", "failure_count": 1}
        baseline_action = experiment.act(trace)
        self.assertEqual(baseline_action["action"], "inspect_context")

        experiment.drive(created.run_id, auto_approve=True)
        improved_action = experiment.act(trace)
        self.assertEqual(improved_action["action"], "request_human")
        self.assertNotEqual(baseline_action["version_id"], improved_action["version_id"])

    def test_promotion_materializes_a_discoverable_dsh_project_skill(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        path = Path(self.workspace) / ".dsh" / "skills" / DSH_SKILL_NAME / "SKILL.md"
        baseline = path.read_text(encoding="utf-8")

        self.assertIn(f"name: {DSH_SKILL_NAME}", baseline)
        self.assertIn(str(created.config.metadata["baseline_version_id"]), baseline)
        self.assertNotIn('return "request_human"', baseline)

        experiment.drive(created.run_id, auto_approve=True)
        promoted = path.read_text(encoding="utf-8")
        self.assertIn('return "request_human"', promoted)
        self.assertIn("Hidden-holdout score: `100%`", promoted)
        self.assertTrue(experiment.report(created.run_id)["dsh_skill"]["materialized"])

    def test_rollback_restores_the_original_native_dsh_skill(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        experiment.drive(created.run_id, auto_approve=True)
        self.assertIn('return "request_human"', experiment.skill_path.read_text())

        rolled = experiment.rollback(created.run_id)
        restored = experiment.skill_path.read_text(encoding="utf-8")

        self.assertEqual(rolled.status, RunStatus.ROLLED_BACK)
        self.assertIn(str(created.config.metadata["baseline_version_id"]), restored)
        self.assertNotIn('return "request_human"', restored)

    def test_skill_projection_can_be_rebuilt_from_the_active_version(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        experiment.drive(created.run_id, auto_approve=True)
        experiment.skill_path.unlink()

        exported = experiment.materialize_active_skill()

        self.assertEqual(exported["version_id"], self.repository.get_channel(experiment.channel))
        self.assertEqual(exported["holdout_score"], 1.0)
        self.assertIn('return "request_human"', experiment.skill_path.read_text())

    def test_human_approval_remains_a_durable_gate(self) -> None:
        experiment = self.experiment(require_approval=True)
        created = experiment.start()
        waiting = experiment.drive(created.run_id, auto_approve=False)

        self.assertEqual(waiting.status, RunStatus.AWAITING_HUMAN)
        report = experiment.report(created.run_id)
        self.assertFalse(report["promoted"])

        experiment.supervisor.decide(created.run_id, Decision.APPROVE)
        final = experiment.drive(created.run_id)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)

    def test_promoted_scaffold_can_roll_back_to_seed(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        baseline = created.config.metadata["baseline_version_id"]
        final = experiment.drive(created.run_id, auto_approve=True)
        self.assertEqual(final.status, RunStatus.SUCCEEDED)

        experiment.supervisor.request_rollback(created.run_id)
        rolled = experiment.supervisor.drive(created.run_id)
        self.assertEqual(rolled.status, RunStatus.ROLLED_BACK)
        self.assertEqual(self.repository.get_channel(created.config.channel), baseline)

    def test_diff_shows_the_persisted_scaffold_change(self) -> None:
        experiment = self.experiment()
        created = experiment.start()
        experiment.drive(created.run_id, auto_approve=True)
        diff = experiment.scaffold_diff(created.run_id)

        self.assertIn("--- baseline/recovery_skill.py", diff)
        self.assertIn("+++ generation-2/recovery_skill.py", diff)
        self.assertIn('+        return "request_human"', diff)

    def test_equal_score_cannot_be_promoted_as_fake_improvement(self) -> None:
        first = self.experiment()
        initial = first.start()
        first.drive(initial.run_id, auto_approve=True)

        second = self.experiment(max_generations=2)
        repeated = second.start()
        blocked = second.drive(repeated.run_id, auto_approve=True)

        self.assertEqual(repeated.config.metadata["baseline_score"], 1.0)
        self.assertEqual(blocked.status, RunStatus.AWAITING_HUMAN)
        self.assertEqual(blocked.pending_reason, "verification_exhausted")

    def test_dsh_mode_gets_training_traces_but_not_holdout_answers(self) -> None:
        harness = CapturingHarness()
        experiment = self.experiment(mode="dsh", inner_harness=harness)
        created = experiment.start()
        final = experiment.drive(created.run_id, auto_approve=True)

        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(len(harness.requests), 1)
        request = harness.requests[0]
        self.assertIn("train-missing-module", request.goal)
        self.assertIn("choose_next_action", request.goal)
        self.assertNotIn("holdout-import", request.goal)
        self.assertEqual(request.metadata["role"], "scaffold_evolver")

    def test_scaffold_guard_rejects_imports_and_side_effect_calls(self) -> None:
        with self.assertRaises(UnsafeScaffold):
            validate_scaffold_source(
                "import os\ndef choose_next_action(trace: dict) -> str:\n    return 'finish'"
            )
        with self.assertRaises(UnsafeScaffold):
            validate_scaffold_source(
                "def choose_next_action(trace: dict) -> str:\n    open('/tmp/nope')\n    return 'finish'"
            )
        with self.assertRaises(UnsafeScaffold):
            validate_scaffold_source(
                "def choose_next_action(trace: dict) -> str:\n    return trace.__class__.__name__"
            )

    def test_markdown_fenced_dsh_response_is_normalized(self) -> None:
        output = f"Here is the scaffold:\n```python\n{SECOND_GENERATION}\n```"
        self.assertEqual(extract_python_source(output), SECOND_GENERATION)


if __name__ == "__main__":
    unittest.main()
