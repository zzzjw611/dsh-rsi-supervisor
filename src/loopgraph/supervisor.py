from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .adapters.demo import DemoHarnessAdapter
from .adapters.dsh import DeepSeekHarnessAdapter
from .graph import LoopGraph
from .models import (
    AgentRequest,
    Decision,
    EventSpec,
    NodeName,
    RunConfig,
    RunState,
    RunStatus,
    VerificationRequest,
    VersionRecord,
    VersionStatus,
    new_id,
    utc_now,
)
from .ports import HarnessAdapter, Verifier
from .storage import (
    Promotion,
    PromotionConflict,
    Rollback,
    RunBusy,
    SQLiteRepository,
    Transition,
    VersionUpdate,
)
from .verifiers import AlwaysPassVerifier, CommandVerifier, ContainsVerifier


class InvalidTransition(RuntimeError):
    pass


class UnknownAdapter(RuntimeError):
    pass


class _LeaseHeartbeat(AbstractContextManager["_LeaseHeartbeat"]):
    def __init__(
        self,
        repository: SQLiteRepository,
        run_id: str,
        owner: str,
        ttl_seconds: float,
    ) -> None:
        self.repository = repository
        self.run_id = run_id
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=min(2.0, self.ttl_seconds))

    def _loop(self) -> None:
        interval = max(0.25, self.ttl_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self.repository.extend_lease(
                    self.run_id, self.owner, ttl_seconds=self.ttl_seconds
                )
            except RunBusy:
                return


class LoopGraphSupervisor:
    def __init__(
        self,
        repository: SQLiteRepository,
        *,
        harnesses: Mapping[str, HarnessAdapter] | None = None,
        verifiers: Mapping[str, Verifier] | None = None,
        worker_id: str | None = None,
        lease_ttl_seconds: float = 60,
    ) -> None:
        self.repository = repository
        defaults: dict[str, HarnessAdapter] = {
            "demo": DemoHarnessAdapter(),
            "dsh": DeepSeekHarnessAdapter(),
        }
        if harnesses:
            defaults.update(harnesses)
        self.harnesses = defaults
        verifier_defaults: dict[str, Verifier] = {"always_pass": AlwaysPassVerifier()}
        if verifiers:
            verifier_defaults.update(verifiers)
        self.verifiers = verifier_defaults
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex}"
        self.lease_ttl_seconds = lease_ttl_seconds

    def create_run(self, config: RunConfig) -> RunState:
        workspace = Path(config.workspace)
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError(f"workspace does not exist or is not a directory: {workspace}")
        now = utc_now()
        run_id = new_id("run")
        state = RunState(
            run_id=run_id,
            config=config,
            status=RunStatus.RUNNING,
            current_node=NodeName.EXECUTE,
            iteration=1,
            base_version_id=self.repository.get_channel(config.channel),
            candidate_version_id=None,
            feedback=None,
            pending_step_id=None,
            pending_step_owner=None,
            pending_reason=None,
            pause_requested=False,
            rollback_target_version_id=None,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        graph = LoopGraph(require_approval=config.require_approval)
        return self.repository.create_run(
            state,
            EventSpec(
                "run.created",
                payload={
                    "config": config.to_dict(),
                    "base_version_id": state.base_version_id,
                    "graph": graph.to_dict(),
                },
                idempotency_key=f"{run_id}:create",
            ),
        )

    def drive(self, run_id: str, *, max_steps: int | None = None) -> RunState:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.repository.acquire_lease(
            run_id, self.worker_id, ttl_seconds=self.lease_ttl_seconds
        )
        try:
            with _LeaseHeartbeat(
                self.repository,
                run_id,
                self.worker_id,
                self.lease_ttl_seconds,
            ):
                state = self.repository.restore_run(run_id)
                if (
                    state.pending_step_id is not None
                    and state.pending_step_owner != self.worker_id
                ):
                    return self._mark_recovery_required(state)

                steps = 0
                while True:
                    state = self.repository.get_state(run_id)
                    if state.status != RunStatus.RUNNING:
                        return state
                    if max_steps is not None and steps >= max_steps:
                        return state
                    if state.pause_requested and state.pending_step_id is None:
                        state = self._pause_at_boundary(state)
                        return state
                    state = self._drive_once(state)
                    steps += 1
                    if state.status != RunStatus.RUNNING:
                        return state
        finally:
            self.repository.release_lease(run_id, self.worker_id)

    def pause(self, run_id: str) -> RunState:
        def transform(state: RunState) -> Transition:
            if state.status.terminal:
                raise InvalidTransition("a terminal run cannot be paused")
            if state.status == RunStatus.PAUSED:
                return Transition(
                    state=state,
                    events=(EventSpec("run.pause_unchanged", payload={"status": "paused"}),),
                )
            if state.status in {RunStatus.AWAITING_HUMAN, RunStatus.RECOVERY_REQUIRED}:
                raise InvalidTransition(f"run is blocked in {state.status.value}; resolve it instead")
            if state.pending_step_id is not None:
                return Transition(
                    state=state.copy(pause_requested=True),
                    events=(
                        EventSpec(
                            "run.pause_requested",
                            node=state.current_node,
                            payload={"safe_boundary": "after_current_step"},
                        ),
                    ),
                )
            return Transition(
                state=state.copy(status=RunStatus.PAUSED, pause_requested=False),
                events=(EventSpec("run.paused", node=state.current_node),),
            )

        state, _ = self.repository.apply(run_id, transform)
        return state

    def resume(self, run_id: str) -> RunState:
        def transform(state: RunState) -> Transition:
            if state.status != RunStatus.PAUSED:
                raise InvalidTransition(f"cannot resume a run in {state.status.value}")
            return Transition(
                state=state.copy(status=RunStatus.RUNNING, pause_requested=False),
                events=(EventSpec("run.resumed", node=state.current_node),),
            )

        state, _ = self.repository.apply(run_id, transform)
        return state

    def decide(
        self,
        run_id: str,
        decision: Decision,
        *,
        feedback: str | None = None,
        rollback_target_version_id: str | None = None,
    ) -> RunState:
        def transform(state: RunState) -> Transition:
            if state.status == RunStatus.RECOVERY_REQUIRED:
                return self._resolve_recovery(
                    state, decision=decision, feedback=feedback
                )
            if state.status != RunStatus.AWAITING_HUMAN:
                raise InvalidTransition(f"run is not waiting for a human: {state.status.value}")

            graph = LoopGraph(require_approval=state.config.require_approval)
            payload = {"decision": decision.value, "feedback": feedback}
            if decision == Decision.APPROVE:
                target = graph.route(NodeName.HITL, "approve")
                next_state = state.copy(
                    status=RunStatus.RUNNING,
                    current_node=target,
                    pending_reason=None,
                    feedback=feedback or state.feedback,
                )
            elif decision in {Decision.REVISE, Decision.RETRY}:
                target = graph.route(NodeName.HITL, "revise")
                base_version = state.base_version_id
                if state.pending_reason and state.pending_reason.startswith("promotion_conflict"):
                    base_version = self.repository.get_channel(state.config.channel)
                next_state = state.copy(
                    status=RunStatus.RUNNING,
                    current_node=target,
                    iteration=state.iteration + 1,
                    base_version_id=base_version,
                    pending_reason=None,
                    feedback=feedback or state.feedback or "Human requested another iteration",
                )
            elif decision in {Decision.REJECT, Decision.ABORT}:
                next_state = state.copy(
                    status=RunStatus.FAILED,
                    current_node=NodeName.DONE,
                    pending_reason=None,
                    feedback=feedback or state.feedback,
                )
            elif decision == Decision.ROLLBACK:
                target = graph.route(NodeName.HITL, "rollback")
                next_state = state.copy(
                    status=RunStatus.RUNNING,
                    current_node=target,
                    pending_reason=None,
                    rollback_target_version_id=rollback_target_version_id,
                    feedback=feedback or state.feedback,
                )
                payload["rollback_target_version_id"] = rollback_target_version_id
            else:
                raise InvalidTransition(f"unsupported HITL decision {decision.value}")
            return Transition(
                state=next_state,
                events=(EventSpec("hitl.resolved", node=NodeName.HITL, payload=payload),),
            )

        state, _ = self.repository.apply(run_id, transform)
        return state

    def request_rollback(
        self, run_id: str, *, target_version_id: str | None = None
    ) -> RunState:
        state = self.repository.get_state(run_id)
        active = self.repository.get_channel(state.config.channel)
        target = target_version_id or self.repository.previous_channel_version(
            state.config.channel
        )
        if active is None:
            raise InvalidTransition(f"channel {state.config.channel!r} has no active version")
        if target is None:
            raise InvalidTransition(f"channel {state.config.channel!r} has no rollback target")

        def transform(current: RunState) -> Transition:
            if current.pending_step_id is not None:
                raise InvalidTransition("cannot roll back while an external step is in flight")
            return Transition(
                state=current.copy(
                    status=RunStatus.RUNNING,
                    current_node=NodeName.ROLLBACK,
                    rollback_target_version_id=target,
                    pending_reason=None,
                    pause_requested=False,
                ),
                events=(
                    EventSpec(
                        "rollback.requested",
                        node=NodeName.ROLLBACK,
                        payload={"from": active, "target": target},
                    ),
                ),
            )

        rolled, _ = self.repository.apply(run_id, transform)
        return rolled

    def cancel(self, run_id: str, *, reason: str = "cancelled by user") -> RunState:
        def transform(state: RunState) -> Transition:
            if state.pending_step_id is not None:
                raise InvalidTransition("cannot cancel while an external step is in flight")
            if state.status.terminal:
                raise InvalidTransition("run is already terminal")
            return Transition(
                state=state.copy(
                    status=RunStatus.CANCELLED,
                    current_node=NodeName.DONE,
                    pending_reason=None,
                    pause_requested=False,
                ),
                events=(EventSpec("run.cancelled", payload={"reason": reason}),),
            )

        state, _ = self.repository.apply(run_id, transform)
        return state

    def inspect(self, run_id: str) -> dict[str, Any]:
        state = self.repository.get_state(run_id)
        events = self.repository.list_events(run_id)
        versions = self.repository.list_versions(run_id)
        return {
            "state": state.to_dict(),
            "graph": LoopGraph(require_approval=state.config.require_approval).to_dict(),
            "channel": {
                "name": state.config.channel,
                "active_version_id": self.repository.get_channel(state.config.channel),
            },
            "versions": [version.to_dict() for version in versions],
            "event_count": len(events),
            "last_event": events[-1].to_dict() if events else None,
        }

    def close(self) -> None:
        for harness in self.harnesses.values():
            harness.close()

    def _drive_once(self, state: RunState) -> RunState:
        if state.current_node == NodeName.EXECUTE:
            return self._execute(state)
        if state.current_node == NodeName.VERIFY:
            return self._verify(state)
        if state.current_node == NodeName.HITL:
            return self._request_human(state)
        if state.current_node == NodeName.PROMOTE:
            return self._promote(state)
        if state.current_node == NodeName.ROLLBACK:
            return self._rollback(state)
        if state.current_node == NodeName.DONE:
            if not state.status.terminal:
                raise InvalidTransition("done node requires a terminal status")
            return state
        raise InvalidTransition(f"unknown node {state.current_node}")

    def _execute(self, state: RunState) -> RunState:
        harness = self.harnesses.get(state.config.adapter)
        if harness is None:
            raise UnknownAdapter(state.config.adapter)
        started = self._begin_external_step(state, NodeName.EXECUTE, "agent.started")
        previous = self.repository.get_version(started.candidate_version_id)
        request = AgentRequest(
            run_id=started.run_id,
            step_id=str(started.pending_step_id),
            goal=started.config.goal,
            workspace=started.config.workspace,
            iteration=started.iteration,
            feedback=started.feedback,
            base_version_id=started.base_version_id,
            previous_candidate=previous.artifact if previous is not None else None,
            metadata=started.config.metadata,
        )
        try:
            result = harness.execute(request)
        except Exception as exc:
            return self._fail_external_step(started, NodeName.EXECUTE, exc)

        version = VersionRecord(
            version_id=new_id("ver"),
            run_id=started.run_id,
            parent_version_id=started.candidate_version_id or started.base_version_id,
            iteration=started.iteration,
            status=VersionStatus.CANDIDATE,
            artifact=result.to_dict(),
            validation=None,
            created_at=utc_now(),
        )
        graph = LoopGraph(require_approval=started.config.require_approval)

        def transform(current: RunState) -> Transition:
            self._assert_pending_owner(current, started.pending_step_id)
            target = graph.route(NodeName.EXECUTE, "completed")
            next_status, pause_events = self._status_after_boundary(current, target)
            next_state = current.copy(
                status=next_status,
                current_node=target,
                candidate_version_id=version.version_id,
                pending_step_id=None,
                pending_step_owner=None,
                pending_reason=None,
                pause_requested=False if next_status == RunStatus.PAUSED else current.pause_requested,
            )
            return Transition(
                state=next_state,
                events=(
                    EventSpec(
                        "agent.completed",
                        node=NodeName.EXECUTE,
                        payload={
                            "step_id": started.pending_step_id,
                            "adapter": harness.name,
                            "version_id": version.version_id,
                            "artifact_ref": result.artifact_ref,
                            "output_preview": result.output[:2000],
                            "metadata": result.metadata,
                        },
                    ),
                    EventSpec(
                        "version.candidate_created",
                        node=NodeName.EXECUTE,
                        payload={
                            "version_id": version.version_id,
                            "parent_version_id": version.parent_version_id,
                            "iteration": version.iteration,
                        },
                    ),
                    *pause_events,
                ),
                create_version=version,
            )

        completed, _ = self.repository.apply(started.run_id, transform)
        return completed

    def _verify(self, state: RunState) -> RunState:
        if state.candidate_version_id is None:
            raise InvalidTransition("verify node has no candidate version")
        candidate = self.repository.get_version(state.candidate_version_id)
        if candidate is None:
            raise InvalidTransition(f"candidate {state.candidate_version_id} does not exist")
        verifier = self._resolve_verifier(state.config)
        started = self._begin_external_step(state, NodeName.VERIFY, "verification.started")
        request = VerificationRequest(
            run_id=started.run_id,
            step_id=str(started.pending_step_id),
            workspace=started.config.workspace,
            goal=started.config.goal,
            iteration=started.iteration,
            candidate_version_id=str(started.candidate_version_id),
            candidate=candidate.artifact,
            metadata=started.config.metadata,
        )
        try:
            result = verifier.verify(request)
        except Exception as exc:
            return self._fail_external_step(started, NodeName.VERIFY, exc)

        graph = LoopGraph(require_approval=started.config.require_approval)

        def transform(current: RunState) -> Transition:
            self._assert_pending_owner(current, started.pending_step_id)
            if result.passed:
                outcome = "passed"
                target = graph.route(NodeName.VERIFY, outcome)
                iteration = current.iteration
                feedback = None
                pending_reason = "approval_required" if target == NodeName.HITL else None
                version_status = VersionStatus.VALIDATED
            else:
                outcome = "retry" if current.iteration < current.config.max_iterations else "exhausted"
                target = graph.route(NodeName.VERIFY, outcome)
                iteration = current.iteration + 1 if outcome == "retry" else current.iteration
                feedback = result.summary
                pending_reason = "verification_exhausted" if outcome == "exhausted" else None
                version_status = VersionStatus.REJECTED
            next_status, pause_events = self._status_after_boundary(current, target)
            next_state = current.copy(
                status=next_status,
                current_node=target,
                iteration=iteration,
                feedback=feedback,
                pending_reason=pending_reason,
                pending_step_id=None,
                pending_step_owner=None,
                pause_requested=False if next_status == RunStatus.PAUSED else current.pause_requested,
            )
            return Transition(
                state=next_state,
                events=(
                    EventSpec(
                        "verification.completed",
                        node=NodeName.VERIFY,
                        payload={
                            "step_id": started.pending_step_id,
                            "verifier": verifier.name,
                            "candidate_version_id": current.candidate_version_id,
                            **result.to_dict(),
                            "outcome": outcome,
                        },
                    ),
                    *pause_events,
                ),
                version_updates=(
                    VersionUpdate(
                        version_id=str(current.candidate_version_id),
                        status=version_status,
                        validation=result.to_dict(),
                    ),
                ),
            )

        verified, _ = self.repository.apply(started.run_id, transform)
        return verified

    def _request_human(self, state: RunState) -> RunState:
        def transform(current: RunState) -> Transition:
            if current.status != RunStatus.RUNNING or current.current_node != NodeName.HITL:
                raise InvalidTransition("HITL request raced with another transition")
            reason = current.pending_reason or "approval_required"
            return Transition(
                state=current.copy(
                    status=RunStatus.AWAITING_HUMAN,
                    pending_reason=reason,
                ),
                events=(
                    EventSpec(
                        "hitl.requested",
                        node=NodeName.HITL,
                        payload={
                            "reason": reason,
                            "candidate_version_id": current.candidate_version_id,
                            "allowed_decisions": [
                                Decision.APPROVE.value,
                                Decision.REVISE.value,
                                Decision.REJECT.value,
                                Decision.ROLLBACK.value,
                            ],
                        },
                    ),
                ),
            )

        waiting, _ = self.repository.apply(state.run_id, transform)
        return waiting

    def _promote(self, state: RunState) -> RunState:
        if state.candidate_version_id is None:
            raise InvalidTransition("promote node has no candidate")
        candidate = state.candidate_version_id
        graph = LoopGraph(require_approval=state.config.require_approval)

        def transform(current: RunState) -> Transition:
            target = graph.route(NodeName.PROMOTE, "promoted")
            return Transition(
                state=current.copy(
                    status=RunStatus.SUCCEEDED,
                    current_node=target,
                    base_version_id=candidate,
                    pending_reason=None,
                    feedback=None,
                ),
                events=(
                    EventSpec(
                        "release.promoted",
                        node=NodeName.PROMOTE,
                        payload={
                            "channel": current.config.channel,
                            "version_id": candidate,
                            "replaced_version_id": current.base_version_id,
                        },
                    ),
                    EventSpec("run.succeeded", node=NodeName.DONE),
                ),
                promotion=Promotion(
                    channel=current.config.channel,
                    version_id=candidate,
                    expected_version_id=current.base_version_id,
                ),
            )

        try:
            promoted, _ = self.repository.apply(state.run_id, transform)
            return promoted
        except PromotionConflict as conflict:
            def record_conflict(current: RunState) -> Transition:
                reason = (
                    f"promotion_conflict:expected={conflict.expected};actual={conflict.actual}"
                )
                return Transition(
                    state=current.copy(
                        status=RunStatus.AWAITING_HUMAN,
                        current_node=graph.route(NodeName.PROMOTE, "conflict"),
                        pending_reason=reason,
                    ),
                    events=(
                        EventSpec(
                            "release.promotion_conflict",
                            node=NodeName.PROMOTE,
                            payload={
                                "channel": conflict.channel,
                                "expected": conflict.expected,
                                "actual": conflict.actual,
                            },
                        ),
                        EventSpec(
                            "hitl.requested",
                            node=NodeName.HITL,
                            payload={
                                "reason": reason,
                                "allowed_decisions": [
                                    Decision.REVISE.value,
                                    Decision.REJECT.value,
                                ],
                            },
                        ),
                    ),
                )

            blocked, _ = self.repository.apply(state.run_id, record_conflict)
            return blocked

    def _rollback(self, state: RunState) -> RunState:
        target = state.rollback_target_version_id or self.repository.previous_channel_version(
            state.config.channel
        )
        if target is None:
            raise InvalidTransition(f"channel {state.config.channel!r} has no rollback target")
        current_active = self.repository.get_channel(state.config.channel)
        graph = LoopGraph(require_approval=state.config.require_approval)

        def transform(current: RunState) -> Transition:
            return Transition(
                state=current.copy(
                    status=RunStatus.ROLLED_BACK,
                    current_node=graph.route(NodeName.ROLLBACK, "rolled_back"),
                    base_version_id=target,
                    rollback_target_version_id=target,
                    pending_reason=None,
                ),
                events=(
                    EventSpec(
                        "release.rolled_back",
                        node=NodeName.ROLLBACK,
                        payload={
                            "channel": current.config.channel,
                            "from": current_active,
                            "target": target,
                        },
                    ),
                ),
                rollback=Rollback(
                    channel=current.config.channel,
                    target_version_id=target,
                ),
            )

        rolled, _ = self.repository.apply(state.run_id, transform)
        return rolled

    def _begin_external_step(
        self, state: RunState, node: NodeName, event_type: str
    ) -> RunState:
        step_id = new_id("step")

        def transform(current: RunState) -> Transition:
            if current.status != RunStatus.RUNNING or current.current_node != node:
                raise InvalidTransition(f"cannot start {node.value} from current run state")
            if current.pending_step_id is not None:
                raise InvalidTransition("another external step is already pending")
            return Transition(
                state=current.copy(
                    pending_step_id=step_id,
                    pending_step_owner=self.worker_id,
                ),
                events=(
                    EventSpec(
                        event_type,
                        node=node,
                        payload={
                            "step_id": step_id,
                            "iteration": current.iteration,
                            "worker_id": self.worker_id,
                        },
                        idempotency_key=f"{step_id}:started",
                    ),
                ),
            )

        started, _ = self.repository.apply(state.run_id, transform)
        return started

    def _fail_external_step(
        self, state: RunState, node: NodeName, error: Exception
    ) -> RunState:
        graph = LoopGraph(require_approval=state.config.require_approval)

        def transform(current: RunState) -> Transition:
            self._assert_pending_owner(current, state.pending_step_id)
            outcome = "retry" if current.iteration < current.config.max_iterations else "exhausted"
            target = graph.route(node, outcome)
            iteration = current.iteration + 1 if outcome == "retry" else current.iteration
            reason = f"{type(error).__name__}: {str(error)[:2000]}"
            pending_reason = f"{node.value}_exhausted" if outcome == "exhausted" else None
            next_status, pause_events = self._status_after_boundary(current, target)
            next_state = current.copy(
                status=next_status,
                current_node=target,
                iteration=iteration,
                feedback=reason,
                pending_reason=pending_reason,
                pending_step_id=None,
                pending_step_owner=None,
                pause_requested=False if next_status == RunStatus.PAUSED else current.pause_requested,
            )
            return Transition(
                state=next_state,
                events=(
                    EventSpec(
                        f"{node.value}.failed",
                        node=node,
                        payload={
                            "step_id": state.pending_step_id,
                            "error_type": type(error).__name__,
                            "error": str(error)[:2000],
                            "outcome": outcome,
                        },
                    ),
                    *pause_events,
                ),
            )

        failed, _ = self.repository.apply(state.run_id, transform)
        return failed

    def _mark_recovery_required(self, state: RunState) -> RunState:
        def transform(current: RunState) -> Transition:
            if current.pending_step_id is None:
                raise InvalidTransition("run no longer has an interrupted external step")
            reason = f"interrupted_{current.current_node.value}_step"
            return Transition(
                state=current.copy(
                    status=RunStatus.RECOVERY_REQUIRED,
                    pending_reason=reason,
                ),
                events=(
                    EventSpec(
                        "recovery.required",
                        node=current.current_node,
                        payload={
                            "step_id": current.pending_step_id,
                            "previous_worker": current.pending_step_owner,
                            "reason": reason,
                            "allowed_decisions": [
                                Decision.RETRY.value,
                                Decision.ABORT.value,
                            ],
                        },
                    ),
                ),
            )

        recovered, _ = self.repository.apply(state.run_id, transform)
        return recovered

    def _resolve_recovery(
        self, state: RunState, *, decision: Decision, feedback: str | None
    ) -> Transition:
        if decision == Decision.RETRY:
            next_state = state.copy(
                status=RunStatus.RUNNING,
                pending_step_id=None,
                pending_step_owner=None,
                pending_reason=None,
                feedback=feedback or state.feedback,
            )
        elif decision in {Decision.ABORT, Decision.REJECT}:
            next_state = state.copy(
                status=RunStatus.FAILED,
                current_node=NodeName.DONE,
                pending_step_id=None,
                pending_step_owner=None,
                pending_reason=None,
                feedback=feedback or "Interrupted step was aborted",
            )
        else:
            raise InvalidTransition("recovery accepts only retry or abort")
        return Transition(
            state=next_state,
            events=(
                EventSpec(
                    "recovery.resolved",
                    node=state.current_node,
                    payload={
                        "interrupted_step_id": state.pending_step_id,
                        "decision": decision.value,
                        "feedback": feedback,
                    },
                ),
            ),
        )

    def _pause_at_boundary(self, state: RunState) -> RunState:
        def transform(current: RunState) -> Transition:
            if current.pending_step_id is not None:
                raise InvalidTransition("cannot enter paused state inside an external step")
            return Transition(
                state=current.copy(status=RunStatus.PAUSED, pause_requested=False),
                events=(EventSpec("run.paused", node=current.current_node),),
            )

        paused, _ = self.repository.apply(state.run_id, transform)
        return paused

    @staticmethod
    def _status_after_boundary(
        state: RunState, target: NodeName
    ) -> tuple[RunStatus, tuple[EventSpec, ...]]:
        if state.pause_requested:
            return (
                RunStatus.PAUSED,
                (
                    EventSpec(
                        "run.paused",
                        node=target,
                        payload={"safe_boundary": True},
                    ),
                ),
            )
        return RunStatus.RUNNING, ()

    def _resolve_verifier(self, config: RunConfig) -> Verifier:
        if config.verifier == "command":
            return CommandVerifier(config.verification_commands)
        if config.verifier.startswith("contains:"):
            return ContainsVerifier(config.verifier.partition(":")[2])
        verifier = self.verifiers.get(config.verifier)
        if verifier is None:
            raise UnknownAdapter(f"unknown verifier {config.verifier!r}")
        return verifier

    def _assert_pending_owner(self, state: RunState, step_id: str | None) -> None:
        if state.pending_step_id != step_id or state.pending_step_owner != self.worker_id:
            raise InvalidTransition("external step ownership changed before completion")
