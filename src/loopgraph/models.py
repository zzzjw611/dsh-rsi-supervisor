from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class RunStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.ROLLED_BACK,
        }


class NodeName(StrEnum):
    EXECUTE = "execute"
    VERIFY = "verify"
    HITL = "hitl"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    DONE = "done"


class Decision(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    RETRY = "retry"
    REJECT = "reject"
    ABORT = "abort"
    ROLLBACK = "rollback"


class VersionStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class RunConfig:
    goal: str
    workspace: str
    adapter: str = "dsh"
    verifier: str = "always_pass"
    verification_commands: tuple[tuple[str, ...], ...] = ()
    max_iterations: int = 3
    require_approval: bool = True
    channel: str = "production"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if not self.channel.strip():
            raise ValueError("channel must not be empty")
        workspace = Path(self.workspace).expanduser()
        object.__setattr__(self, "workspace", str(workspace))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["verification_commands"] = [list(command) for command in self.verification_commands]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunConfig:
        return cls(
            goal=str(value["goal"]),
            workspace=str(value["workspace"]),
            adapter=str(value.get("adapter", "dsh")),
            verifier=str(value.get("verifier", "always_pass")),
            verification_commands=tuple(
                tuple(str(part) for part in command)
                for command in value.get("verification_commands", [])
            ),
            max_iterations=int(value.get("max_iterations", 3)),
            require_approval=bool(value.get("require_approval", True)),
            channel=str(value.get("channel", "production")),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(slots=True)
class RunState:
    run_id: str
    config: RunConfig
    status: RunStatus
    current_node: NodeName
    iteration: int
    base_version_id: str | None
    candidate_version_id: str | None
    feedback: str | None
    pending_step_id: str | None
    pending_step_owner: str | None
    pending_reason: str | None
    pause_requested: bool
    rollback_target_version_id: str | None
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "current_node": self.current_node.value,
            "iteration": self.iteration,
            "base_version_id": self.base_version_id,
            "candidate_version_id": self.candidate_version_id,
            "feedback": self.feedback,
            "pending_step_id": self.pending_step_id,
            "pending_step_owner": self.pending_step_owner,
            "pending_reason": self.pending_reason,
            "pause_requested": self.pause_requested,
            "rollback_target_version_id": self.rollback_target_version_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunState:
        return cls(
            run_id=str(value["run_id"]),
            config=RunConfig.from_dict(dict(value["config"])),
            status=RunStatus(value["status"]),
            current_node=NodeName(value["current_node"]),
            iteration=int(value["iteration"]),
            base_version_id=value.get("base_version_id"),
            candidate_version_id=value.get("candidate_version_id"),
            feedback=value.get("feedback"),
            pending_step_id=value.get("pending_step_id"),
            pending_step_owner=value.get("pending_step_owner"),
            pending_reason=value.get("pending_reason"),
            pause_requested=bool(value.get("pause_requested", False)),
            rollback_target_version_id=value.get("rollback_target_version_id"),
            revision=int(value["revision"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )

    def copy(self, **changes: Any) -> RunState:
        value = self.to_dict()
        value.update(changes)
        value["config"] = (
            changes["config"].to_dict()
            if isinstance(changes.get("config"), RunConfig)
            else value["config"]
        )
        return RunState.from_dict(value)


@dataclass(frozen=True, slots=True)
class Event:
    run_id: str
    seq: int
    event_type: str
    node: str | None
    payload: dict[str, Any]
    timestamp: str
    idempotency_key: str | None
    previous_hash: str
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EventSpec:
    event_type: str
    node: NodeName | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    run_id: str
    step_id: str
    goal: str
    workspace: str
    iteration: int
    feedback: str | None
    base_version_id: str | None
    previous_candidate: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentResult:
    output: str
    artifact_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    run_id: str
    step_id: str
    workspace: str
    goal: str
    iteration: int
    candidate_version_id: str
    candidate: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    score: float | None = None
    evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        return value


@dataclass(frozen=True, slots=True)
class VersionRecord:
    version_id: str
    run_id: str
    parent_version_id: str | None
    iteration: int
    status: VersionStatus
    artifact: dict[str, Any]
    validation: dict[str, Any] | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
