# Failure semantics

## Guarantees

| Boundary | Guarantee |
|---|---|
| State transition | Atomic event + projection + checkpoint transaction |
| Run scheduling | Single active lease owner with heartbeat |
| Event journal | Per-run order, uniqueness, and tamper-evident hash chain |
| Pause | Immediate between nodes; safe-boundary after an in-flight call |
| HITL | Decision and resulting transition commit together |
| Promotion | Compare-and-swap; stale base becomes a visible conflict |
| Rollback | Atomic channel-pointer update with immutable history |
| External side effect | At-most-one automatic attempt per persisted step id |

## Crash matrix

| Crash point | Durable observation | Resume behavior |
|---|---|---|
| Before `step.started` | No pending step | Node may start normally |
| After `step.started`, before external call | Pending owned step | `recovery_required`; retry/abort |
| During external call | Outcome unknown | `recovery_required`; no silent replay |
| After external return, before commit | Outcome unknown to supervisor | `recovery_required`; no silent replay |
| During result transaction | SQLite commits all or none | Load committed checkpoint and continue |
| After result commit | Next node is durable | Continue without repeating prior node |
| During promotion | Channel and event both commit or neither does | Retry safe; CAS prevents stale overwrite |

## Why not claim exactly once?

SQLite cannot atomically commit with an arbitrary model API, shell process, or
remote tool. Exactly-once side effects require the external system to accept and
reconcile an idempotency key. LoopGraph supplies that key but assumes the most
conservative case: the external system may not support reconciliation.

Automatic replay would be at-least-once and could duplicate a deployment,
message, charge, or file mutation. The supervisor therefore turns ambiguity
into a durable human decision. An adapter that can prove the original outcome
may add a reconciliation implementation later.

## Operational recovery

```bash
loopgraph status RUN_ID
loopgraph events RUN_ID
loopgraph verify-journal RUN_ID

# Retry with a fresh step id, retaining prior durable feedback.
loopgraph decide RUN_ID retry --feedback "Confirmed original side effect did not land"
loopgraph drive RUN_ID

# Or terminate without another external attempt.
loopgraph decide RUN_ID abort --feedback "Original outcome could not be reconciled"
```

## Known limits

- The lease protects cooperative LoopGraph workers, not a hostile process that
  writes directly to SQLite.
- A long OS suspension can delay heartbeat execution. Completion still checks
  step ownership; use a sufficiently large lease TTL for the adapter timeout.
- SSE is an observation stream, not a queue. Consumers reconnect with the last
  event sequence via `?after=N`.
- Secrets inside adapter metadata or model output will be persisted. Adapters
  should redact before returning `AgentResult`.
