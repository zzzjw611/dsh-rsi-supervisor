"use strict";

(function installStaticReplay(global) {
  const STORAGE_KEY = "loopgraph.interactive-replay.v1";
  const BASELINE_SOURCE = `def choose_next_action(trace: dict) -> str:
    return "inspect_context"`;
  const EVOLVED_SOURCE = `def choose_next_action(trace: dict) -> str:
    error = str(trace.get("error", "")).lower()
    failures = int(trace.get("failure_count", 0))
    if failures >= 3:
        return "rollback"
    if "permission" in error or "forbidden" in error:
        return "request_human"
    if "timeout" in error:
        return "reduce_scope"
    if "rate limit" in error:
        return "backoff"
    if "assert" in error:
        return "inspect_tests"
    if "dependency" in error or "module" in error:
        return "inspect_dependency"
    return "inspect_context"`;
  const EVOLVED_DIFF = `--- active/v0/recovery.py
+++ candidate/v2/recovery.py
@@ -1,2 +1,17 @@
 def choose_next_action(trace: dict) -> str:
-    return "inspect_context"
+    error = str(trace.get("error", "")).lower()
+    failures = int(trace.get("failure_count", 0))
+    if failures >= 3:
+        return "rollback"
+    if "permission" in error or "forbidden" in error:
+        return "request_human"
+    if "timeout" in error:
+        return "reduce_scope"
+    if "rate limit" in error:
+        return "backoff"
+    if "assert" in error:
+        return "inspect_tests"
+    if "dependency" in error or "module" in error:
+        return "inspect_dependency"
+    return "inspect_context"`;

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const delay = (milliseconds) => new Promise((resolve) => global.setTimeout(resolve, milliseconds));

  function isEnabled() {
    const query = new URLSearchParams(global.location.search);
    return global.location.hostname.endsWith(".github.io")
      || global.location.protocol === "file:"
      || query.get("runtime") === "static";
  }

  function readSnapshot() {
    try {
      const raw = global.localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_error) {
      return null;
    }
  }

  function persist(snapshot) {
    try {
      global.localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
    } catch (_error) {
      // Private browsing can disable localStorage. The in-memory snapshot still works.
    }
    return clone(snapshot);
  }

  let memorySnapshot = readSnapshot();

  function currentSnapshot() {
    return memorySnapshot ? clone(memorySnapshot) : null;
  }

  function save(snapshot) {
    memorySnapshot = persist(snapshot);
    return currentSnapshot();
  }

  function event(snapshot, eventType, node) {
    snapshot.events.push({
      seq: snapshot.events.length + 1,
      event_type: eventType,
      node,
    });
  }

  function newSnapshot() {
    const runId = `replay-${Date.now().toString(36)}-interview`;
    return {
      report: {
        run_id: runId,
        mode: "replay",
        status: "running",
        baseline: { score: 0.125, holdout_score: 0.125, passed: 2, total: 16 },
        generations: [],
        promoted: false,
        active_version_id: "v0-seed-7c10d2",
        dsh_skill: { name: "loopgraph-failure-recovery", materialized: false },
      },
      state: {
        run_id: runId,
        status: "running",
        current_node: "execute",
        candidate_version_id: null,
      },
      events: [
        { seq: 1, event_type: "run.created", node: "run" },
        { seq: 2, event_type: "baseline.evaluated", node: "verify" },
        { seq: 3, event_type: "checkpoint.committed", node: "execute" },
      ],
      baseline_source: BASELINE_SOURCE,
      active_source: BASELINE_SOURCE,
      candidate_source: "",
      scaffold_diff: "",
      workspace: "browser://interactive-replay",
    };
  }

  function generateFirst(snapshot) {
    snapshot.report.generations = [{
      generation: 1,
      score: null,
      holdout_score: null,
      status: "generated",
    }];
    snapshot.state.current_node = "verify";
    snapshot.state.candidate_version_id = "v1-candidate-390fa1";
    snapshot.candidate_source = `${BASELINE_SOURCE}\n# candidate adds partial timeout handling`;
    snapshot.scaffold_diff = `--- active/v0/recovery.py\n+++ candidate/v1/recovery.py\n@@ -1,2 +1,4 @@\n def choose_next_action(trace: dict) -> str:\n+    if "timeout" in str(trace.get("error", "")).lower():\n+        return "reduce_scope"\n     return "inspect_context"`;
    event(snapshot, "agent.started", "execute");
    event(snapshot, "candidate.generated", "execute");
    event(snapshot, "checkpoint.committed", "verify");
  }

  function rejectFirst(snapshot) {
    Object.assign(snapshot.report.generations[0], {
      score: 0.4375,
      holdout_score: 0.375,
      status: "rejected",
    });
    snapshot.state.current_node = "execute";
    event(snapshot, "candidate.evaluated", "verify");
    event(snapshot, "holdout.gate_failed", "verify");
    event(snapshot, "candidate.rejected", "verify");
  }

  function reachReview(snapshot) {
    if (!snapshot.report.generations.length) generateFirst(snapshot);
    if (snapshot.report.generations[0].score == null) rejectFirst(snapshot);
    snapshot.report.generations.push({
      generation: 2,
      score: 1,
      holdout_score: 1,
      status: "validated",
    });
    snapshot.state.status = "awaiting_human";
    snapshot.state.current_node = "hitl";
    snapshot.state.candidate_version_id = "v2-validated-cc8f72";
    snapshot.candidate_source = EVOLVED_SOURCE;
    snapshot.scaffold_diff = EVOLVED_DIFF;
    snapshot.report.status = "awaiting_human";
    event(snapshot, "agent.started", "execute");
    event(snapshot, "candidate.generated", "execute");
    event(snapshot, "candidate.evaluated", "verify");
    event(snapshot, "holdout.gate_passed", "verify");
    event(snapshot, "human_review.requested", "hitl");
  }

  function operate(snapshot, action) {
    if (action === "step") {
      if (!snapshot.report.generations.length) generateFirst(snapshot);
      else if (snapshot.report.generations[0].score == null) rejectFirst(snapshot);
      else reachReview(snapshot);
    } else if (action === "run") {
      reachReview(snapshot);
    } else if (action === "pause") {
      snapshot.state.status = "paused";
      snapshot.report.status = "paused";
      event(snapshot, "run.paused", snapshot.state.current_node);
      event(snapshot, "checkpoint.committed", snapshot.state.current_node);
    } else if (action === "resume") {
      snapshot.state.status = "running";
      snapshot.report.status = "running";
      event(snapshot, "run.resumed", snapshot.state.current_node);
    } else if (action === "approve") {
      snapshot.state.status = "succeeded";
      snapshot.state.current_node = "promote";
      snapshot.report.status = "succeeded";
      snapshot.report.promoted = true;
      snapshot.report.active_version_id = "v2-promoted-cc8f72";
      snapshot.active_source = EVOLVED_SOURCE;
      event(snapshot, "human_review.approved", "hitl");
      event(snapshot, "release.promoted", "promote");
      event(snapshot, "skill.projected", "promote");
      event(snapshot, "run.succeeded", "promote");
    } else if (action === "rollback") {
      snapshot.state.status = "rolled_back";
      snapshot.state.current_node = "rollback";
      snapshot.report.status = "rolled_back";
      snapshot.report.promoted = false;
      snapshot.report.active_version_id = "v0-seed-7c10d2";
      snapshot.active_source = BASELINE_SOURCE;
      event(snapshot, "rollback.requested", "rollback");
      event(snapshot, "release.rolled_back", "rollback");
      event(snapshot, "skill.projection_restored", "rollback");
    }
    return snapshot;
  }

  async function request(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    await delay(method === "GET" ? 90 : 420);

    if (path === "/api/rsi/environment") {
      return {
        sdk_installed: false,
        credential_configured: false,
        live_ready: false,
        hosted_replay: true,
      };
    }
    if (path === "/api/rsi/runs" && method === "GET") {
      const snapshot = currentSnapshot();
      return { runs: snapshot ? [snapshot] : [] };
    }
    if (path === "/api/rsi/runs" && method === "POST") {
      return save(newSnapshot());
    }

    const match = path.match(/^\/api\/rsi\/runs\/([^/]+)(?:\/([^/]+))?$/);
    if (!match || !memorySnapshot || match[1] !== memorySnapshot.report.run_id) {
      throw new Error("Hosted replay state was not found. Start a new experiment.");
    }
    const action = match[2];
    if (!action && method === "GET") return currentSnapshot();
    if (action === "probe") {
      return {
        action: memorySnapshot.state.status === "succeeded" ? "request_human" : "inspect_context",
      };
    }
    return save(operate(clone(memorySnapshot), action));
  }

  global.LoopGraphStaticReplay = { isEnabled, request };
})(window);
