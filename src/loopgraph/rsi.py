from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters.dsh import DeepSeekHarnessAdapter
from .models import (
    AgentRequest,
    AgentResult,
    Decision,
    RunConfig,
    RunState,
    RunStatus,
    VerificationRequest,
    VerificationResult,
)
from .ports import HarnessAdapter
from .storage import SQLiteRepository
from .supervisor import LoopGraphSupervisor


SEED_SKILL = textwrap.dedent(
    '''
    def choose_next_action(trace: dict) -> str:
        """Choose a recovery action after an agent execution step."""
        return "inspect_context"
    '''
).strip()

FIRST_GENERATION = textwrap.dedent(
    '''
    def choose_next_action(trace: dict) -> str:
        """Recover from the most obvious execution failures."""
        error = str(trace.get("error", "")).lower()
        if trace.get("status") == "passed":
            return "finish"
        if "module" in error or "import" in error:
            return "inspect_dependency"
        if "assert" in error or "test failed" in error:
            return "inspect_tests"
        return "inspect_context"
    '''
).strip()

SECOND_GENERATION = textwrap.dedent(
    '''
    def choose_next_action(trace: dict) -> str:
        """Select the safest useful action from a durable execution trace."""
        error = str(trace.get("error", "")).lower()
        status = str(trace.get("status", "")).lower()
        failures = int(trace.get("failure_count", 0))

        if failures >= 3:
            return "rollback"
        if status in ("passed", "success", "completed"):
            return "finish"
        if "permission" in error or "access denied" in error or "forbidden" in error:
            return "request_human"
        if "rate limit" in error or "too many requests" in error or "429" in error:
            return "backoff"
        if "timeout" in error or "timed out" in error or "deadline" in error:
            return "reduce_scope"
        if "module" in error or "import" in error or "dependency" in error:
            return "inspect_dependency"
        if "assert" in error or "test failed" in error:
            return "inspect_tests"
        return "inspect_context"
    '''
).strip()

ALLOWED_ACTIONS = frozenset(
    {
        "inspect_context",
        "inspect_dependency",
        "inspect_tests",
        "request_human",
        "reduce_scope",
        "backoff",
        "rollback",
        "finish",
    }
)

DSH_SKILL_NAME = "loopgraph-failure-recovery"


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    split: str
    trace: dict[str, Any]
    expected_action: str

    def training_example(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "trace": self.trace,
            "expected_action": self.expected_action,
        }


BENCHMARK_CASES = (
    BenchmarkCase(
        "train-missing-module",
        "train",
        {"error": "ModuleNotFoundError: No module named 'pandas'", "failure_count": 1},
        "inspect_dependency",
    ),
    BenchmarkCase(
        "train-permission",
        "train",
        {"error": "PermissionError: write access denied", "failure_count": 1},
        "request_human",
    ),
    BenchmarkCase(
        "train-timeout",
        "train",
        {"error": "TimeoutError: test command timed out", "failure_count": 1},
        "reduce_scope",
    ),
    BenchmarkCase(
        "train-assertion",
        "train",
        {"error": "AssertionError: expected 3 but got 2", "failure_count": 1},
        "inspect_tests",
    ),
    BenchmarkCase(
        "train-rate-limit",
        "train",
        {"error": "HTTP 429 rate limit exceeded", "failure_count": 1},
        "backoff",
    ),
    BenchmarkCase(
        "train-repeat",
        "train",
        {"error": "same unexplained failure", "failure_count": 3},
        "rollback",
    ),
    BenchmarkCase(
        "train-success",
        "train",
        {"status": "passed", "failure_count": 0},
        "finish",
    ),
    BenchmarkCase(
        "train-unknown",
        "train",
        {"error": "unexpected worker state", "failure_count": 1},
        "inspect_context",
    ),
    BenchmarkCase(
        "holdout-import",
        "holdout",
        {"error": "ImportError: dependency resolver could not load yaml", "failure_count": 1},
        "inspect_dependency",
    ),
    BenchmarkCase(
        "holdout-access",
        "holdout",
        {"error": "403 Forbidden: access denied by policy", "failure_count": 1},
        "request_human",
    ),
    BenchmarkCase(
        "holdout-deadline",
        "holdout",
        {"error": "build deadline exceeded after 120 seconds", "failure_count": 1},
        "reduce_scope",
    ),
    BenchmarkCase(
        "holdout-mismatch",
        "holdout",
        {"error": "test failed: expected JSON schema v2", "failure_count": 1},
        "inspect_tests",
    ),
    BenchmarkCase(
        "holdout-throttle",
        "holdout",
        {"error": "Too Many Requests from upstream model", "failure_count": 1},
        "backoff",
    ),
    BenchmarkCase(
        "holdout-repeat",
        "holdout",
        {"error": "non-deterministic tool crash", "failure_count": 4},
        "rollback",
    ),
    BenchmarkCase(
        "holdout-success",
        "holdout",
        {"status": "completed", "failure_count": 0},
        "finish",
    ),
    BenchmarkCase(
        "holdout-unknown",
        "holdout",
        {"error": "opaque runtime observation", "failure_count": 1},
        "inspect_context",
    ),
)


class UnsafeScaffold(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    split: str
    expected_action: str
    actual_action: str
    passed: bool


@dataclass(frozen=True, slots=True)
class Scorecard:
    outcomes: tuple[CaseOutcome, ...]

    @property
    def score(self) -> float:
        return sum(item.passed for item in self.outcomes) / len(self.outcomes)

    @property
    def train_score(self) -> float:
        values = tuple(item for item in self.outcomes if item.split == "train")
        return sum(item.passed for item in values) / len(values)

    @property
    def holdout_score(self) -> float:
        values = tuple(item for item in self.outcomes if item.split == "holdout")
        return sum(item.passed for item in values) / len(values)

    @property
    def passed_count(self) -> int:
        return sum(item.passed for item in self.outcomes)

    def to_dict(self, *, include_holdout_details: bool = False) -> dict[str, Any]:
        failures = [
            asdict(item)
            for item in self.outcomes
            if not item.passed and (item.split == "train" or include_holdout_details)
        ]
        return {
            "score": self.score,
            "train_score": self.train_score,
            "holdout_score": self.holdout_score,
            "passed": self.passed_count,
            "total": len(self.outcomes),
            "training_failures": failures,
            "holdout_failed_count": sum(
                not item.passed for item in self.outcomes if item.split == "holdout"
            ),
        }


class FailureRecoveryBenchmark:
    name = "failure-recovery-v1"

    def __init__(self, cases: tuple[BenchmarkCase, ...] = BENCHMARK_CASES) -> None:
        self.cases = cases

    @property
    def training_cases(self) -> tuple[BenchmarkCase, ...]:
        return tuple(case for case in self.cases if case.split == "train")

    def evaluate(self, source: str, *, timeout_seconds: float = 3) -> Scorecard:
        payload = [{"case_id": case.case_id, "trace": case.trace} for case in self.cases]
        actual = self._run_cases(source, payload, timeout_seconds=timeout_seconds)
        outcomes = tuple(
            CaseOutcome(
                case_id=case.case_id,
                split=case.split,
                expected_action=case.expected_action,
                actual_action=actual.get(case.case_id, "<missing>"),
                passed=actual.get(case.case_id) == case.expected_action,
            )
            for case in self.cases
        )
        return Scorecard(outcomes)

    def act(self, source: str, trace: dict[str, Any]) -> str:
        result = self._run_cases(source, [{"case_id": "live-probe", "trace": trace}])
        action = result.get("live-probe")
        if action not in ALLOWED_ACTIONS:
            raise UnsafeScaffold(f"candidate returned an unsupported recovery action: {action}")
        return action

    def _run_cases(
        self,
        source: str,
        payload: list[dict[str, Any]],
        *,
        timeout_seconds: float = 3,
    ) -> dict[str, str]:
        validate_scaffold_source(source)
        runner = (
            "import json,runpy,sys\n"
            "module=runpy.run_path(sys.argv[1])\n"
            "fn=module['choose_next_action']\n"
            "cases=json.load(sys.stdin)\n"
            "json.dump([{'case_id':c['case_id'],'action':fn(c['trace'])} for c in cases],sys.stdout)\n"
        )
        with tempfile.TemporaryDirectory(prefix="loopgraph-rsi-") as directory:
            skill_path = Path(directory) / "recovery_skill.py"
            skill_path.write_text(source + "\n", encoding="utf-8")
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", runner, str(skill_path)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    cwd=directory,
                    timeout=timeout_seconds,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONIOENCODING": "utf-8",
                    },
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise UnsafeScaffold("candidate exceeded its evaluation time budget") from exc

        if result.returncode != 0:
            raise UnsafeScaffold(f"candidate evaluation failed: {result.stderr[-1000:]}")
        try:
            actual = {item["case_id"]: str(item["action"]) for item in json.loads(result.stdout)}
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise UnsafeScaffold("candidate did not return a valid benchmark result") from exc
        return actual


def validate_scaffold_source(source: str) -> None:
    if len(source.encode("utf-8")) > 16_000:
        raise UnsafeScaffold("candidate exceeds the 16 KiB scaffold budget")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise UnsafeScaffold(f"candidate is not valid Python: {exc}") from exc

    definitions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(definitions) != 1 or definitions[0].name != "choose_next_action":
        raise UnsafeScaffold("candidate must define exactly one choose_next_action function")
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            continue
        if isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant):
            continue
        raise UnsafeScaffold("only a module docstring and the recovery function are allowed")

    banned = (
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.Raise,
        ast.Global,
        ast.Nonlocal,
        ast.Delete,
        ast.Lambda,
        ast.Yield,
        ast.YieldFrom,
        ast.Await,
    )
    nodes = tuple(ast.walk(tree))
    if len(nodes) > 400:
        raise UnsafeScaffold("candidate exceeds the scaffold complexity budget")
    safe_builtins = {"str", "int", "len", "max", "min", "bool", "isinstance"}
    safe_methods = {"get", "lower", "casefold", "strip", "startswith", "endswith"}
    for node in nodes:
        if isinstance(node, banned):
            raise UnsafeScaffold(f"disallowed scaffold syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and "__" in node.id:
            raise UnsafeScaffold("dunder names are not permitted")
        if isinstance(node, ast.Attribute) and (
            "__" in node.attr or node.attr not in safe_methods
        ):
            raise UnsafeScaffold(f"disallowed attribute access: {node.attr}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id not in safe_builtins:
                raise UnsafeScaffold(f"disallowed function call: {node.func.id}")
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                raise UnsafeScaffold("indirect function calls are not permitted")


class SeedScaffoldAdapter:
    name = "rsi-seed"

    def __init__(self, source: str = SEED_SKILL) -> None:
        self.source = source

    def execute(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            output=self.source,
            artifact_ref="scaffold://failure-recovery/v0",
            metadata={"role": "seed", "model_weights": "frozen"},
        )

    def close(self) -> None:
        return None


class ReplayEvolver:
    name = "rsi-replay"

    def execute(self, request: AgentRequest) -> AgentResult:
        source = FIRST_GENERATION if request.iteration == 1 else SECOND_GENERATION
        return AgentResult(
            output=source,
            artifact_ref=f"replay://generation/{request.iteration}",
            metadata={"mode": "replay", "generation": request.iteration},
        )

    def close(self) -> None:
        return None


class RsiEvolutionAdapter:
    name = "rsi-evolver"

    def __init__(self, inner: HarnessAdapter, benchmark: FailureRecoveryBenchmark) -> None:
        self.inner = inner
        self.benchmark = benchmark

    def execute(self, request: AgentRequest) -> AgentResult:
        previous = (
            str(request.previous_candidate.get("output", ""))
            if request.previous_candidate is not None
            else str(request.metadata["baseline_source"])
        )
        goal = self._evolution_prompt(request, previous)
        delegated = AgentRequest(
            run_id=request.run_id,
            step_id=request.step_id,
            goal=goal,
            workspace=request.workspace,
            iteration=request.iteration,
            feedback=request.feedback,
            base_version_id=request.base_version_id,
            previous_candidate=request.previous_candidate,
            metadata={**request.metadata, "role": "scaffold_evolver"},
        )
        result = self.inner.execute(delegated)
        source = extract_python_source(result.output)
        validate_scaffold_source(source)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return AgentResult(
            output=source,
            artifact_ref=result.artifact_ref or f"scaffold://sha256/{digest}",
            metadata={
                **result.metadata,
                "role": "scaffold_evolver",
                "inner_harness": self.inner.name,
                "generation": request.iteration,
                "source_sha256": digest,
                "improvement_target": "failure_recovery_skill",
                "model_weights": "frozen",
            },
        )

    def close(self) -> None:
        self.inner.close()

    def _evolution_prompt(self, request: AgentRequest, previous: str) -> str:
        training_cases = [case.training_example() for case in self.benchmark.training_cases]
        return "\n".join(
            [
                "Improve the agent's own failure-recovery scaffold; do not solve a user task.",
                "The underlying model and outer supervisor are frozen.",
                "Your only editable surface is one pure Python function:",
                "    def choose_next_action(trace: dict) -> str:",
                f"Allowed actions: {', '.join(sorted(ALLOWED_ACTIONS))}",
                "Rules: no imports, filesystem access, network access, classes, or extra functions.",
                "Return ONLY the complete Python function, optionally in one fenced python block.",
                "Generalize from training cases; hidden holdout cases are scored independently.",
                "Current scaffold:",
                previous,
                "Training examples:",
                json.dumps(training_cases, ensure_ascii=False, indent=2),
                "Prior verifier feedback:",
                request.feedback or "This is the first improvement generation.",
            ]
        )


def extract_python_source(output: str) -> str:
    stripped = output.strip()
    if "```" not in stripped:
        return stripped
    blocks = stripped.split("```")
    for block in blocks[1::2]:
        content = block.strip()
        if content.startswith("python"):
            content = content[len("python") :].lstrip("\r\n ")
        if "def choose_next_action" in content:
            return content.strip()
    raise UnsafeScaffold("the agent did not return a recovery-skill code block")


class RsiBenchmarkVerifier:
    name = "rsi-benchmark"

    def __init__(self, benchmark: FailureRecoveryBenchmark) -> None:
        self.benchmark = benchmark

    def verify(self, request: VerificationRequest) -> VerificationResult:
        candidate = self.benchmark.evaluate(str(request.candidate["output"]))
        baseline_score = float(request.metadata["baseline_score"])
        baseline_holdout_score = float(request.metadata["baseline_holdout_score"])
        minimum_holdout_score = float(request.metadata.get("minimum_holdout_score", 0.75))
        improvement = candidate.score - baseline_score
        passed = (
            improvement > 0
            and candidate.holdout_score >= baseline_holdout_score
            and candidate.holdout_score >= minimum_holdout_score
        )
        training_failures = [
            item
            for item in candidate.outcomes
            if item.split == "train" and not item.passed
        ]
        failure_feedback = "; ".join(
            f"{item.case_id}: expected {item.expected_action}, got {item.actual_action}"
            for item in training_failures
        )
        summary = (
            f"score={candidate.score:.0%}, holdout={candidate.holdout_score:.0%}, "
            f"baseline={baseline_score:.0%}, improvement={improvement:+.0%}"
        )
        if failure_feedback:
            summary = f"{summary}; training failures: {failure_feedback}"
        evidence = {
            "kind": "scaffold_benchmark",
            "benchmark": self.benchmark.name,
            "baseline_score": baseline_score,
            "baseline_holdout_score": baseline_holdout_score,
            "minimum_holdout_score": minimum_holdout_score,
            "improvement": improvement,
            **candidate.to_dict(),
        }
        return VerificationResult(
            passed=passed,
            summary=summary,
            score=candidate.score,
            evidence=(evidence,),
        )


class RsiExperiment:
    """A bounded scaffold-evolution experiment over the durable supervisor."""

    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        mode: str,
        workspace: str,
        channel: str = "rsi-failure-recovery",
        require_approval: bool = True,
        max_generations: int = 3,
        minimum_holdout_score: float = 0.75,
        inner_harness: HarnessAdapter | None = None,
    ) -> None:
        if mode not in {"dsh", "replay"}:
            raise ValueError("RSI mode must be 'dsh' or 'replay'")
        self.repository = repository
        self.mode = mode
        self.workspace = str(Path(workspace).resolve())
        self.channel = channel
        self.require_approval = require_approval
        self.max_generations = max_generations
        self.minimum_holdout_score = minimum_holdout_score
        self.benchmark = FailureRecoveryBenchmark()
        inner = inner_harness
        if inner is None:
            if mode == "dsh":
                if importlib.util.find_spec("deepseek_harness") is None:
                    raise ValueError(
                        "live RSI mode requires the DSH SDK: pip install -e '.[dsh]'"
                    )
                inner = DeepSeekHarnessAdapter()
            else:
                inner = ReplayEvolver()
        self.supervisor = LoopGraphSupervisor(
            repository,
            harnesses={
                "rsi-seed": SeedScaffoldAdapter(),
                "rsi-evolver": RsiEvolutionAdapter(inner, self.benchmark),
            },
            verifiers={"rsi-benchmark": RsiBenchmarkVerifier(self.benchmark)},
        )

    def start(self) -> RunState:
        active_version_id = self.repository.get_channel(self.channel)
        if active_version_id is None:
            seed = self.supervisor.create_run(
                RunConfig(
                    goal="Bootstrap the frozen-model recovery scaffold",
                    workspace=self.workspace,
                    adapter="rsi-seed",
                    verifier="always_pass",
                    max_iterations=1,
                    require_approval=False,
                    channel=self.channel,
                    metadata={"role": "bootstrap", "benchmark": self.benchmark.name},
                )
            )
            seeded = self.supervisor.drive(seed.run_id)
            active_version_id = seeded.candidate_version_id

        active = self.repository.get_version(active_version_id)
        if active is None:
            raise RuntimeError("active RSI scaffold is missing")
        baseline_source = str(active.artifact["output"])
        baseline = self.benchmark.evaluate(baseline_source)
        self.materialize_active_skill()
        return self.supervisor.create_run(
            RunConfig(
                goal="Improve the agent's own failure-recovery scaffold",
                workspace=self.workspace,
                adapter="rsi-evolver",
                verifier="rsi-benchmark",
                max_iterations=self.max_generations,
                require_approval=self.require_approval,
                channel=self.channel,
                metadata={
                    "mode": self.mode,
                    "benchmark": self.benchmark.name,
                    "baseline_version_id": active_version_id,
                    "baseline_source": baseline_source,
                    "baseline_score": baseline.score,
                    "baseline_holdout_score": baseline.holdout_score,
                    "baseline_passed": baseline.passed_count,
                    "benchmark_total": len(baseline.outcomes),
                    "minimum_holdout_score": self.minimum_holdout_score,
                    "editable_surface": "choose_next_action",
                    "frozen_components": ["model_weights", "outer_supervisor", "verifier"],
                },
            )
            )

    def drive(self, run_id: str, *, auto_approve: bool = False) -> RunState:
        state = self.supervisor.drive(run_id)
        if (
            auto_approve
            and state.status == RunStatus.AWAITING_HUMAN
            and state.pending_reason == "approval_required"
        ):
            self.supervisor.decide(
                run_id,
                Decision.APPROVE,
                feedback="Explicit demo approval after a positive holdout-gated improvement",
            )
            state = self.supervisor.drive(run_id)
        self.materialize_active_skill()
        return state

    def rollback(self, run_id: str, *, target_version_id: str | None = None) -> RunState:
        self.supervisor.request_rollback(run_id, target_version_id=target_version_id)
        state = self.supervisor.drive(run_id)
        self.materialize_active_skill()
        return state

    @property
    def skill_path(self) -> Path:
        return Path(self.workspace) / ".dsh" / "skills" / DSH_SKILL_NAME / "SKILL.md"

    def materialize_active_skill(self) -> dict[str, Any]:
        """Atomically project the active immutable scaffold into DSH's native skill root."""
        active_id = self.repository.get_channel(self.channel)
        active = self.repository.get_version(active_id)
        if active is None:
            raise ValueError(f"channel {self.channel!r} has no active recovery scaffold")

        source = str(active.artifact["output"])
        scorecard = self.benchmark.evaluate(source)
        skill_path = self.skill_path
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        if not skill_path.parent.resolve().is_relative_to(Path(self.workspace).resolve()):
            raise ValueError("the DSH skill directory must stay inside the project workspace")

        content = "\n".join(
            [
                "---",
                f"name: {DSH_SKILL_NAME}",
                (
                    "description: Select the safest governed recovery action after an "
                    "agent failure, timeout, permission denial, repeated failure, or success."
                ),
                "---",
                "",
                "# Governed failure recovery",
                "",
                "This skill is an atomic projection of the currently approved LoopGraph version.",
                f"- Release channel: `{self.channel}`",
                f"- Active version: `{active.version_id}`",
                f"- Benchmark: `{self.benchmark.name}`",
                f"- Overall score: `{scorecard.passed_count}/{len(scorecard.outcomes)}`",
                f"- Hidden-holdout score: `{scorecard.holdout_score:.0%}`",
                "",
                "Before retrying a failed execution, apply the policy below to its trace.",
                "A `request_human` result requires a human decision. A `rollback` result",
                "returns control to the outer supervisor. Do not bypass approval, modify the",
                "evaluator, or change this release by editing the skill projection.",
                "",
                "```python",
                source.rstrip("\n"),
                "```",
                "",
            ]
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=skill_path.parent,
                prefix=".skill-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, skill_path)
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        return {
            "name": DSH_SKILL_NAME,
            "path": str(skill_path),
            "channel": self.channel,
            "version_id": active.version_id,
            "score": scorecard.score,
            "holdout_score": scorecard.holdout_score,
        }

    def report(self, run_id: str) -> dict[str, Any]:
        state = self.repository.get_state(run_id)
        metadata = state.config.metadata
        versions = self.repository.list_versions(run_id)
        generations: list[dict[str, Any]] = []
        for version in versions:
            validation = version.validation or {}
            evidence = validation.get("evidence", [])
            benchmark = evidence[0] if evidence else {}
            generations.append(
                {
                    "generation": version.iteration,
                    "version_id": version.version_id,
                    "status": version.status.value,
                    "score": benchmark.get("score"),
                    "train_score": benchmark.get("train_score"),
                    "holdout_score": benchmark.get("holdout_score"),
                    "improvement": benchmark.get("improvement"),
                    "source_sha256": version.artifact.get("metadata", {}).get("source_sha256"),
                }
            )
        active = self.repository.get_channel(state.config.channel)
        active_version = self.repository.get_version(active)
        capability_trace = {
            "error": "Access denied: deployment permission policy",
            "failure_count": 1,
        }
        active_action = (
            self.benchmark.act(str(active_version.artifact["output"]), capability_trace)
            if active_version is not None
            else None
        )
        return {
            "demo": "recursive-scaffold-self-improvement",
            "benchmark": metadata["benchmark"],
            "mode": metadata["mode"],
            "run_id": run_id,
            "status": state.status.value,
            "editable_surface": metadata["editable_surface"],
            "frozen_components": metadata["frozen_components"],
            "baseline": {
                "version_id": metadata["baseline_version_id"],
                "score": metadata["baseline_score"],
                "holdout_score": metadata["baseline_holdout_score"],
                "passed": metadata["baseline_passed"],
                "total": metadata["benchmark_total"],
            },
            "generations": generations,
            "active_version_id": active,
            "promoted": active != metadata["baseline_version_id"],
            "rollback_target_version_id": metadata["baseline_version_id"],
            "dsh_skill": {
                "name": DSH_SKILL_NAME,
                "path": str(self.skill_path),
                "materialized": self.skill_path.is_file(),
            },
            "capability_probe": {
                "trace": capability_trace,
                "baseline_action": self.benchmark.act(
                    str(metadata["baseline_source"]), capability_trace
                ),
                "active_action": active_action,
            },
            "event_count": len(self.repository.list_events(run_id)),
        }

    def act(self, trace: dict[str, Any]) -> dict[str, Any]:
        active_id = self.repository.get_channel(self.channel)
        active = self.repository.get_version(active_id)
        if active is None:
            raise ValueError(f"channel {self.channel!r} has no active recovery scaffold")
        return {
            "channel": self.channel,
            "version_id": active.version_id,
            "trace": trace,
            "action": self.benchmark.act(str(active.artifact["output"]), trace),
        }

    def scaffold_diff(self, run_id: str) -> str:
        state = self.repository.get_state(run_id)
        baseline = self.repository.get_version(
            str(state.config.metadata["baseline_version_id"])
        )
        candidate = self.repository.get_version(state.candidate_version_id)
        if baseline is None or candidate is None:
            raise ValueError("a baseline and candidate are required to generate a scaffold diff")
        baseline_source = str(baseline.artifact["output"]).rstrip("\n") + "\n"
        candidate_source = str(candidate.artifact["output"]).rstrip("\n") + "\n"
        return "".join(
            difflib.unified_diff(
                baseline_source.splitlines(keepends=True),
                candidate_source.splitlines(keepends=True),
                fromfile="baseline/recovery_skill.py",
                tofile=f"generation-{candidate.iteration}/recovery_skill.py",
                lineterm="\n",
            )
        )

    def close(self) -> None:
        self.supervisor.close()
