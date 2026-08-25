from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from .adapters.demo import DemoHarnessAdapter
from .adapters.dsh import DeepSeekHarnessAdapter
from .adapters.subprocess import SubprocessHarnessAdapter
from .api import serve
from .dashboard import dsh_environment
from .models import AgentRequest, Decision, RunConfig, RunState
from .ports import HarnessExecutionError
from .rsi import ReplayEvolver, RsiExperiment
from .storage import SQLiteRepository, StorageError
from .supervisor import InvalidTransition, LoopGraphSupervisor


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _sync_rsi_skill(supervisor: LoopGraphSupervisor, state: RunState) -> None:
    if state.config.adapter != "rsi-evolver":
        return
    experiment = RsiExperiment(
        supervisor.repository,
        mode="replay",
        workspace=state.config.workspace,
        channel=state.config.channel,
    )
    try:
        experiment.materialize_active_skill()
    finally:
        experiment.close()


def _build_supervisor(
    database: str, *, demo_fail_first: bool = False, run_id: str | None = None
) -> LoopGraphSupervisor:
    repository = SQLiteRepository(database)
    if run_id is not None:
        state = repository.get_state(run_id)
        if state.config.adapter == "rsi-evolver":
            experiment = RsiExperiment(
                repository,
                mode=str(state.config.metadata.get("mode", "replay")),
                workspace=state.config.workspace,
                channel=state.config.channel,
                require_approval=state.config.require_approval,
                max_generations=state.config.max_iterations,
                minimum_holdout_score=float(
                    state.config.metadata.get("minimum_holdout_score", 0.75)
                ),
            )
            return experiment.supervisor
    harnesses = {"demo": DemoHarnessAdapter(fail_first=demo_fail_first)}
    command = os.getenv("LOOPGRAPH_HARNESS_COMMAND")
    if command:
        harnesses["subprocess"] = SubprocessHarnessAdapter(shlex.split(command))
    return LoopGraphSupervisor(repository, harnesses=harnesses)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopgraph",
        description="Durable LoopGraph supervisor for DSH and other agent harnesses.",
    )
    parser.add_argument("--db", default=".loopgraph/loopgraph.db", help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    start = subparsers.add_parser("start", help="create a run")
    start.add_argument("--goal", required=True)
    start.add_argument("--workspace", default=".")
    start.add_argument("--adapter", choices=["demo", "dsh", "subprocess"], default="dsh")
    start.add_argument("--verifier", default="always_pass")
    start.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="verification command; repeat for multiple gates",
    )
    start.add_argument("--max-iterations", type=int, default=3)
    start.add_argument("--channel", default="production")
    start.add_argument("--no-approval", action="store_true")

    drive = subparsers.add_parser("drive", help="advance until blocked or terminal")
    drive.add_argument("run_id")
    drive.add_argument("--steps", type=int)

    status = subparsers.add_parser("status", help="show the run projection")
    status.add_argument("run_id")

    events = subparsers.add_parser("events", help="show append-only run events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)

    for command in ("pause", "resume"):
        item = subparsers.add_parser(command, help=f"{command} a run")
        item.add_argument("run_id")

    decide = subparsers.add_parser("decide", help="resolve HITL or recovery")
    decide.add_argument("run_id")
    decide.add_argument("decision", choices=[item.value for item in Decision])
    decide.add_argument("--feedback")
    decide.add_argument("--rollback-target")

    rollback = subparsers.add_parser("rollback", help="move a channel to a prior version")
    rollback.add_argument("run_id")
    rollback.add_argument("--target")

    cancel = subparsers.add_parser("cancel", help="cancel a run at a safe boundary")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", default="cancelled by user")

    verify = subparsers.add_parser("verify-journal", help="verify the event hash chain")
    verify.add_argument("run_id")

    server = subparsers.add_parser("serve", help="start REST/SSE API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--workspace", default=".")

    dashboard = subparsers.add_parser("dashboard", help="launch the interactive RSI dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8787)
    dashboard.add_argument("--workspace", default=".")

    doctor = subparsers.add_parser("dsh-doctor", help="check official DSH SDK and credentials")
    doctor.add_argument("--probe", action="store_true", help="make one real DeepSeek model call")
    doctor.add_argument("--workspace", default=".")

    demo = subparsers.add_parser("demo", help="run a deterministic loop + HITL demo")
    demo.add_argument("--workspace", default=".")
    demo.add_argument("--reset", action="store_true")

    rsi = subparsers.add_parser(
        "rsi-demo",
        help="evolve and benchmark the agent's own failure-recovery scaffold",
    )
    rsi.add_argument("--mode", choices=["dsh", "replay"], default="dsh")
    rsi.add_argument("--workspace", default=".")
    rsi.add_argument("--channel", default="rsi-failure-recovery")
    rsi.add_argument("--max-generations", type=int, default=3)
    rsi.add_argument("--minimum-holdout-score", type=float, default=0.75)
    rsi.add_argument("--auto-approve", action="store_true")
    rsi.add_argument("--no-approval", action="store_true")
    rsi.add_argument("--rollback-after-promotion", action="store_true")
    rsi.add_argument("--reset", action="store_true")

    rsi_report = subparsers.add_parser("rsi-report", help="inspect one RSI experiment")
    rsi_report.add_argument("run_id")

    rsi_diff = subparsers.add_parser("rsi-diff", help="show how the scaffold improved")
    rsi_diff.add_argument("run_id")

    rsi_act = subparsers.add_parser("rsi-act", help="run the currently promoted recovery skill")
    rsi_act.add_argument("--channel", default="rsi-failure-recovery")
    rsi_act.add_argument("--workspace", default=".")
    rsi_act.add_argument("--error", default="")
    rsi_act.add_argument("--status", default="")
    rsi_act.add_argument("--failure-count", type=int, default=1)

    rsi_export = subparsers.add_parser(
        "rsi-export-skill", help="repair the active DSH-native project skill projection"
    )
    rsi_export.add_argument("--channel", default="rsi-failure-recovery")
    rsi_export.add_argument("--workspace", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name in {"demo", "rsi-demo"} and args.reset:
        database = Path(args.db)
        for suffix in ("", "-shm", "-wal"):
            target = Path(f"{database}{suffix}")
            if target.exists() and target.is_file():
                target.unlink()
    supervisor = _build_supervisor(
        args.db,
        demo_fail_first=args.command_name == "demo",
        run_id=getattr(args, "run_id", None) if args.command_name == "drive" else None,
    )
    experiment: RsiExperiment | None = None
    try:
        if args.command_name == "start":
            commands = tuple(tuple(shlex.split(value)) for value in args.verify_command)
            verifier = "command" if commands else args.verifier
            state = supervisor.create_run(
                RunConfig(
                    goal=args.goal,
                    workspace=str(Path(args.workspace).resolve()),
                    adapter=args.adapter,
                    verifier=verifier,
                    verification_commands=commands,
                    max_iterations=args.max_iterations,
                    require_approval=not args.no_approval,
                    channel=args.channel,
                )
            )
            _json(state.to_dict())
        elif args.command_name == "drive":
            state = supervisor.drive(args.run_id, max_steps=args.steps)
            _sync_rsi_skill(supervisor, state)
            _json(state.to_dict())
        elif args.command_name == "status":
            _json(supervisor.inspect(args.run_id))
        elif args.command_name == "events":
            _json(
                [
                    event.to_dict()
                    for event in supervisor.repository.list_events(
                        args.run_id, after=args.after
                    )
                ]
            )
        elif args.command_name == "pause":
            _json(supervisor.pause(args.run_id).to_dict())
        elif args.command_name == "resume":
            _json(supervisor.resume(args.run_id).to_dict())
        elif args.command_name == "decide":
            _json(
                supervisor.decide(
                    args.run_id,
                    Decision(args.decision),
                    feedback=args.feedback,
                    rollback_target_version_id=args.rollback_target,
                ).to_dict()
            )
        elif args.command_name == "rollback":
            supervisor.request_rollback(args.run_id, target_version_id=args.target)
            state = supervisor.drive(args.run_id)
            _sync_rsi_skill(supervisor, state)
            _json(state.to_dict())
        elif args.command_name == "cancel":
            _json(supervisor.cancel(args.run_id, reason=args.reason).to_dict())
        elif args.command_name == "verify-journal":
            supervisor.repository.verify_integrity(args.run_id)
            _json({"run_id": args.run_id, "integrity": "ok"})
        elif args.command_name in {"serve", "dashboard"}:
            label = "RSI dashboard" if args.command_name == "dashboard" else "LoopGraph API"
            print(f"{label} listening on http://{args.host}:{args.port}", flush=True)
            serve(supervisor, host=args.host, port=args.port, workspace=args.workspace)
            return 0
        elif args.command_name == "dsh-doctor":
            readiness = dsh_environment()
            if not readiness["live_ready"]:
                _json(readiness)
                return 2
            if args.probe:
                adapter = DeepSeekHarnessAdapter()
                try:
                    result = adapter.execute(
                        AgentRequest(
                            run_id="dsh-connectivity-probe",
                            step_id="deepseek-live-probe",
                            goal="Reply with exactly LOOPGRAPH_DSH_OK. Do not call tools.",
                            workspace=str(Path(args.workspace).resolve()),
                            iteration=1,
                            feedback=None,
                            base_version_id=None,
                            previous_candidate=None,
                            metadata={"role": "connection_probe"},
                        )
                    )
                finally:
                    adapter.close()
                readiness["probe"] = {
                    "status": "ok",
                    "response": result.output[:200],
                    "provider": result.metadata.get("provider"),
                    "model": result.metadata.get("model"),
                    "session_id": result.metadata.get("session_id"),
                    "finish_reason": result.metadata.get("finish_reason"),
                }
            _json(readiness)
        elif args.command_name == "demo":
            state = supervisor.create_run(
                RunConfig(
                    goal="Build and validate a durable agent artifact",
                    workspace=str(Path(args.workspace).resolve()),
                    adapter="demo",
                    verifier="contains:VERIFIED",
                    max_iterations=3,
                    require_approval=True,
                    channel="demo",
                )
            )
            state = supervisor.drive(state.run_id)
            if state.status.value == "awaiting_human":
                supervisor.decide(
                    state.run_id,
                    Decision.APPROVE,
                    feedback="Demo approval after deterministic verification",
                )
                state = supervisor.drive(state.run_id)
            _json(supervisor.inspect(state.run_id))
        elif args.command_name == "rsi-demo":
            experiment = RsiExperiment(
                supervisor.repository,
                mode=args.mode,
                workspace=args.workspace,
                channel=args.channel,
                require_approval=not args.no_approval,
                max_generations=args.max_generations,
                minimum_holdout_score=args.minimum_holdout_score,
            )
            state = experiment.start()
            state = experiment.drive(state.run_id, auto_approve=args.auto_approve)
            if args.rollback_after_promotion and state.status.value == "succeeded":
                experiment.rollback(state.run_id)
            _json(experiment.report(state.run_id))
        elif args.command_name in {"rsi-report", "rsi-diff"}:
            state = supervisor.repository.get_state(args.run_id)
            experiment = RsiExperiment(
                supervisor.repository,
                mode=str(state.config.metadata["mode"]),
                workspace=state.config.workspace,
                channel=state.config.channel,
                inner_harness=ReplayEvolver(),
            )
            if args.command_name == "rsi-report":
                _json(experiment.report(args.run_id))
            else:
                print(experiment.scaffold_diff(args.run_id), end="")
        elif args.command_name == "rsi-act":
            experiment = RsiExperiment(
                supervisor.repository,
                mode="replay",
                workspace=args.workspace,
                channel=args.channel,
            )
            trace: dict[str, Any] = {"failure_count": args.failure_count}
            if args.error:
                trace["error"] = args.error
            if args.status:
                trace["status"] = args.status
            _json(experiment.act(trace))
        elif args.command_name == "rsi-export-skill":
            experiment = RsiExperiment(
                supervisor.repository,
                mode="replay",
                workspace=args.workspace,
                channel=args.channel,
            )
            _json(experiment.materialize_active_skill())
        return 0
    except (ValueError, InvalidTransition, StorageError, HarnessExecutionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if experiment is not None:
            experiment.close()
        if args.command_name not in {"serve", "dashboard"}:
            supervisor.close()


if __name__ == "__main__":
    raise SystemExit(main())
