from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .models import (
    Event,
    EventSpec,
    RunState,
    VersionRecord,
    VersionStatus,
    canonical_json,
    utc_now,
)

T = TypeVar("T")
GENESIS_HASH = "0" * 64


class StorageError(RuntimeError):
    pass


class RunNotFound(StorageError):
    pass


class ConcurrentUpdate(StorageError):
    pass


class RunBusy(StorageError):
    pass


class IntegrityViolation(StorageError):
    pass


class PromotionConflict(StorageError):
    def __init__(self, *, channel: str, expected: str | None, actual: str | None) -> None:
        self.channel = channel
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"channel {channel!r} moved: expected {expected!r}, current value is {actual!r}"
        )


@dataclass(frozen=True, slots=True)
class Promotion:
    channel: str
    version_id: str
    expected_version_id: str | None


@dataclass(frozen=True, slots=True)
class Rollback:
    channel: str
    target_version_id: str | None


@dataclass(frozen=True, slots=True)
class VersionUpdate:
    version_id: str
    status: VersionStatus | None = None
    validation: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Transition:
    state: RunState
    events: tuple[EventSpec, ...]
    result: Any = None
    create_version: VersionRecord | None = None
    version_updates: tuple[VersionUpdate, ...] = ()
    promotion: Promotion | None = None
    rollback: Rollback | None = None


class SQLiteRepository:
    """SQLite event store plus a rebuildable run projection and release registry."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    current_node TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until REAL
                );

                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    node TEXT,
                    payload_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    idempotency_key TEXT,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency
                    ON events(run_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );

                CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    parent_version_id TEXT REFERENCES versions(version_id),
                    iteration INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    validation_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS versions_run ON versions(run_id, created_at);

                CREATE TABLE IF NOT EXISTS channels (
                    channel TEXT PRIMARY KEY,
                    active_version_id TEXT REFERENCES versions(version_id),
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_history (
                    channel TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    version_id TEXT REFERENCES versions(version_id),
                    previous_version_id TEXT REFERENCES versions(version_id),
                    action TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel, revision)
                );
                """
            )

    def create_run(self, state: RunState, event: EventSpec) -> RunState:
        now = utc_now()
        persisted = state.copy(revision=1, updated_at=now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, status, current_node, revision, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persisted.run_id,
                    persisted.status.value,
                    persisted.current_node.value,
                    persisted.revision,
                    canonical_json(persisted.to_dict()),
                    persisted.created_at,
                    persisted.updated_at,
                ),
            )
            events = self._append_events(connection, persisted.run_id, (event,))
            self._write_checkpoint(connection, persisted, events[-1])
            connection.commit()
        return persisted

    def get_state(self, run_id: str) -> RunState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return RunState.from_dict(json.loads(row["state_json"]))

    def list_runs(self, *, limit: int = 100) -> list[RunState]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunState.from_dict(json.loads(row["state_json"])) for row in rows]

    def apply(
        self,
        run_id: str,
        transform: Callable[[RunState], Transition],
        *,
        expected_revision: int | None = None,
    ) -> tuple[RunState, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_json, revision FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            current = RunState.from_dict(json.loads(row["state_json"]))
            if expected_revision is not None and current.revision != expected_revision:
                raise ConcurrentUpdate(
                    f"run {run_id} revision is {current.revision}, expected {expected_revision}"
                )

            transition = transform(current)
            if transition.state.run_id != run_id:
                raise StorageError("a transition cannot change run_id")
            now = utc_now()
            state = transition.state.copy(revision=current.revision + 1, updated_at=now)

            if transition.create_version is not None:
                self._insert_version(connection, transition.create_version)
            for update in transition.version_updates:
                self._update_version(connection, update)
            if transition.promotion is not None:
                self._promote(connection, run_id, transition.promotion)
            if transition.rollback is not None:
                rollback_target = self._rollback(connection, run_id, transition.rollback)
                if state.rollback_target_version_id is None:
                    state = state.copy(rollback_target_version_id=rollback_target)

            events = self._append_events(connection, run_id, transition.events)
            if not events:
                raise StorageError("every state transition must emit at least one event")
            connection.execute(
                """
                UPDATE runs
                SET status = ?, current_node = ?, revision = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    state.status.value,
                    state.current_node.value,
                    state.revision,
                    canonical_json(state.to_dict()),
                    state.updated_at,
                    run_id,
                ),
            )
            self._write_checkpoint(connection, state, events[-1])
            connection.commit()
            return state, transition.result

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[Event]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise RunNotFound(run_id)
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq ASC LIMIT ?
                """,
                (run_id, after, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def find_event(self, run_id: str, idempotency_key: str) -> Event | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def get_version(self, version_id: str | None) -> VersionRecord | None:
        if version_id is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
        return self._version_from_row(row) if row is not None else None

    def list_versions(self, run_id: str) -> list[VersionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM versions WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def get_channel(self, channel: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT active_version_id FROM channels WHERE channel = ?", (channel,)
            ).fetchone()
        return row["active_version_id"] if row is not None else None

    def previous_channel_version(self, channel: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT previous_version_id FROM channel_history
                WHERE channel = ? ORDER BY revision DESC LIMIT 1
                """,
                (channel,),
            ).fetchone()
        return row["previous_version_id"] if row is not None else None

    def acquire_lease(self, run_id: str, owner: str, ttl_seconds: float) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE runs SET lease_owner = ?, lease_until = ?
                WHERE run_id = ?
                  AND (lease_owner IS NULL OR lease_until < ? OR lease_owner = ?)
                """,
                (owner, now + ttl_seconds, run_id, now, owner),
            )
            if result.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if exists is None:
                    raise RunNotFound(run_id)
                raise RunBusy(f"run {run_id} is leased by another worker")
            connection.commit()

    def extend_lease(self, run_id: str, owner: str, ttl_seconds: float) -> None:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE runs SET lease_until = ? WHERE run_id = ? AND lease_owner = ?
                """,
                (time.time() + ttl_seconds, run_id, owner),
            )
            if result.rowcount != 1:
                raise RunBusy(f"worker {owner} no longer owns run {run_id}")

    def release_lease(self, run_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs SET lease_owner = NULL, lease_until = NULL
                WHERE run_id = ? AND lease_owner = ?
                """,
                (run_id, owner),
            )

    def restore_run(self, run_id: str) -> RunState:
        """Verify the journal and rebuild the mutable projection from its last checkpoint."""
        self.verify_integrity(run_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state_json FROM checkpoints
                WHERE run_id = ? ORDER BY seq DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise IntegrityViolation(f"run {run_id} has no checkpoint")
            state = RunState.from_dict(json.loads(row["state_json"]))
            connection.execute(
                """
                UPDATE runs
                SET status = ?, current_node = ?, revision = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    state.status.value,
                    state.current_node.value,
                    state.revision,
                    canonical_json(state.to_dict()),
                    state.updated_at,
                    run_id,
                ),
            )
            connection.commit()
        return state

    def verify_integrity(self, run_id: str) -> None:
        events = self.list_events(run_id, limit=1_000_000)
        if not events:
            raise IntegrityViolation(f"run {run_id} has an empty event journal")
        previous_hash = GENESIS_HASH
        expected_seq = 1
        for event in events:
            if event.seq != expected_seq:
                raise IntegrityViolation(
                    f"run {run_id} has sequence gap: expected {expected_seq}, got {event.seq}"
                )
            expected_hash = self._hash_event(
                run_id=event.run_id,
                seq=event.seq,
                event_type=event.event_type,
                node=event.node,
                payload=event.payload,
                timestamp=event.timestamp,
                idempotency_key=event.idempotency_key,
                previous_hash=previous_hash,
            )
            if event.previous_hash != previous_hash or event.event_hash != expected_hash:
                raise IntegrityViolation(f"run {run_id} event {event.seq} failed hash verification")
            previous_hash = event.event_hash
            expected_seq += 1

        with self._connect() as connection:
            checkpoint = connection.execute(
                """
                SELECT seq, event_hash FROM checkpoints
                WHERE run_id = ? ORDER BY seq DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if checkpoint is None or checkpoint["seq"] != events[-1].seq:
            raise IntegrityViolation(f"run {run_id} checkpoint does not cover the journal tail")
        if checkpoint["event_hash"] != events[-1].event_hash:
            raise IntegrityViolation(f"run {run_id} checkpoint hash does not match the journal")

    def _append_events(
        self, connection: sqlite3.Connection, run_id: str, specs: Iterable[EventSpec]
    ) -> list[Event]:
        tail = connection.execute(
            """
            SELECT seq, event_hash FROM events
            WHERE run_id = ? ORDER BY seq DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        seq = int(tail["seq"]) + 1 if tail is not None else 1
        previous_hash = str(tail["event_hash"]) if tail is not None else GENESIS_HASH
        appended: list[Event] = []
        for spec in specs:
            timestamp = utc_now()
            node = spec.node.value if spec.node is not None else None
            event_hash = self._hash_event(
                run_id=run_id,
                seq=seq,
                event_type=spec.event_type,
                node=node,
                payload=spec.payload,
                timestamp=timestamp,
                idempotency_key=spec.idempotency_key,
                previous_hash=previous_hash,
            )
            connection.execute(
                """
                INSERT INTO events(
                    run_id, seq, event_type, node, payload_json, timestamp,
                    idempotency_key, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    seq,
                    spec.event_type,
                    node,
                    canonical_json(spec.payload),
                    timestamp,
                    spec.idempotency_key,
                    previous_hash,
                    event_hash,
                ),
            )
            event = Event(
                run_id=run_id,
                seq=seq,
                event_type=spec.event_type,
                node=node,
                payload=spec.payload,
                timestamp=timestamp,
                idempotency_key=spec.idempotency_key,
                previous_hash=previous_hash,
                event_hash=event_hash,
            )
            appended.append(event)
            seq += 1
            previous_hash = event_hash
        return appended

    @staticmethod
    def _hash_event(
        *,
        run_id: str,
        seq: int,
        event_type: str,
        node: str | None,
        payload: dict[str, Any],
        timestamp: str,
        idempotency_key: str | None,
        previous_hash: str,
    ) -> str:
        body = canonical_json(
            {
                "run_id": run_id,
                "seq": seq,
                "event_type": event_type,
                "node": node,
                "payload": payload,
                "timestamp": timestamp,
                "idempotency_key": idempotency_key,
                "previous_hash": previous_hash,
            }
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _write_checkpoint(
        connection: sqlite3.Connection, state: RunState, event: Event
    ) -> None:
        connection.execute(
            """
            INSERT INTO checkpoints(run_id, seq, state_json, event_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                event.seq,
                canonical_json(state.to_dict()),
                event.event_hash,
                utc_now(),
            ),
        )

    @staticmethod
    def _insert_version(connection: sqlite3.Connection, version: VersionRecord) -> None:
        connection.execute(
            """
            INSERT INTO versions(
                version_id, run_id, parent_version_id, iteration, status,
                artifact_json, validation_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.version_id,
                version.run_id,
                version.parent_version_id,
                version.iteration,
                version.status.value,
                canonical_json(version.artifact),
                canonical_json(version.validation) if version.validation is not None else None,
                version.created_at,
            ),
        )

    @staticmethod
    def _update_version(connection: sqlite3.Connection, update: VersionUpdate) -> None:
        if update.status is None and update.validation is None:
            return
        if update.status is not None and update.validation is not None:
            result = connection.execute(
                "UPDATE versions SET status = ?, validation_json = ? WHERE version_id = ?",
                (update.status.value, canonical_json(update.validation), update.version_id),
            )
        elif update.status is not None:
            result = connection.execute(
                "UPDATE versions SET status = ? WHERE version_id = ?",
                (update.status.value, update.version_id),
            )
        else:
            result = connection.execute(
                "UPDATE versions SET validation_json = ? WHERE version_id = ?",
                (canonical_json(update.validation), update.version_id),
            )
        if result.rowcount != 1:
            raise StorageError(f"unknown version {update.version_id}")

    @staticmethod
    def _promote(connection: sqlite3.Connection, run_id: str, promotion: Promotion) -> None:
        row = connection.execute(
            "SELECT active_version_id, revision FROM channels WHERE channel = ?",
            (promotion.channel,),
        ).fetchone()
        actual = row["active_version_id"] if row is not None else None
        revision = int(row["revision"]) if row is not None else 0
        if actual != promotion.expected_version_id:
            raise PromotionConflict(
                channel=promotion.channel,
                expected=promotion.expected_version_id,
                actual=actual,
            )
        candidate = connection.execute(
            "SELECT status FROM versions WHERE version_id = ?", (promotion.version_id,)
        ).fetchone()
        if candidate is None:
            raise StorageError(f"unknown candidate version {promotion.version_id}")
        if actual is not None:
            connection.execute(
                "UPDATE versions SET status = ? WHERE version_id = ?",
                (VersionStatus.SUPERSEDED.value, actual),
            )
        connection.execute(
            "UPDATE versions SET status = ? WHERE version_id = ?",
            (VersionStatus.ACTIVE.value, promotion.version_id),
        )
        next_revision = revision + 1
        connection.execute(
            """
            INSERT INTO channels(channel, active_version_id, revision, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                active_version_id = excluded.active_version_id,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (promotion.channel, promotion.version_id, next_revision, utc_now()),
        )
        connection.execute(
            """
            INSERT INTO channel_history(
                channel, revision, version_id, previous_version_id, action, run_id, created_at
            ) VALUES (?, ?, ?, ?, 'promote', ?, ?)
            """,
            (
                promotion.channel,
                next_revision,
                promotion.version_id,
                actual,
                run_id,
                utc_now(),
            ),
        )

    @staticmethod
    def _rollback(connection: sqlite3.Connection, run_id: str, rollback: Rollback) -> str:
        row = connection.execute(
            "SELECT active_version_id, revision FROM channels WHERE channel = ?",
            (rollback.channel,),
        ).fetchone()
        if row is None or row["active_version_id"] is None:
            raise StorageError(f"channel {rollback.channel!r} has no active version")
        current = str(row["active_version_id"])
        target = rollback.target_version_id
        if target is None:
            history = connection.execute(
                """
                SELECT previous_version_id FROM channel_history
                WHERE channel = ? ORDER BY revision DESC LIMIT 1
                """,
                (rollback.channel,),
            ).fetchone()
            target = history["previous_version_id"] if history is not None else None
        if target is None:
            raise StorageError(f"channel {rollback.channel!r} has no previous version")
        exists = connection.execute(
            "SELECT 1 FROM versions WHERE version_id = ?", (target,)
        ).fetchone()
        if exists is None:
            raise StorageError(f"unknown rollback version {target}")
        revision = int(row["revision"]) + 1
        connection.execute(
            "UPDATE versions SET status = ? WHERE version_id = ?",
            (VersionStatus.ROLLED_BACK.value, current),
        )
        connection.execute(
            "UPDATE versions SET status = ? WHERE version_id = ?",
            (VersionStatus.ACTIVE.value, target),
        )
        connection.execute(
            """
            UPDATE channels SET active_version_id = ?, revision = ?, updated_at = ?
            WHERE channel = ?
            """,
            (target, revision, utc_now(), rollback.channel),
        )
        connection.execute(
            """
            INSERT INTO channel_history(
                channel, revision, version_id, previous_version_id, action, run_id, created_at
            ) VALUES (?, ?, ?, ?, 'rollback', ?, ?)
            """,
            (rollback.channel, revision, target, current, run_id, utc_now()),
        )
        return str(target)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            run_id=str(row["run_id"]),
            seq=int(row["seq"]),
            event_type=str(row["event_type"]),
            node=row["node"],
            payload=json.loads(row["payload_json"]),
            timestamp=str(row["timestamp"]),
            idempotency_key=row["idempotency_key"],
            previous_hash=str(row["previous_hash"]),
            event_hash=str(row["event_hash"]),
        )

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> VersionRecord:
        return VersionRecord(
            version_id=str(row["version_id"]),
            run_id=str(row["run_id"]),
            parent_version_id=row["parent_version_id"],
            iteration=int(row["iteration"]),
            status=VersionStatus(row["status"]),
            artifact=json.loads(row["artifact_json"]),
            validation=(
                json.loads(row["validation_json"])
                if row["validation_json"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
        )
