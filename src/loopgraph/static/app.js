"use strict";

const replayClient = window.LoopGraphStaticReplay;
const state = {
  snapshot: null,
  environment: null,
  busy: false,
  guideOpen: true,
  probeVerified: false,
  staticReplay: Boolean(replayClient?.isEnabled()),
};
const element = (id) => document.getElementById(id);
const percent = (value) => value == null ? "—" : `${Math.round(value * 1000) / 10}%`;
const escapeHtml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

async function request(path, options = {}) {
  if (state.staticReplay) return replayClient.request(path, options);
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed: ${response.status}`);
  return value;
}

function notify(message, kind = "warning") {
  const notice = element("notice");
  notice.hidden = !message;
  notice.className = `notice ${kind === "error" ? "error" : ""}`;
  notice.textContent = message || "";
}

async function guarded(operation) {
  if (state.busy) return;
  state.busy = true;
  notify("");
  updateButtons();
  try {
    await operation();
  } catch (error) {
    notify(error.message || String(error), "error");
  } finally {
    state.busy = false;
    updateButtons();
  }
}

async function loadEnvironment() {
  state.environment = await request("/api/rsi/environment");
  const chip = element("environment-chip");
  element("hosted-replay-note").hidden = !state.staticReplay;
  document.body.classList.toggle("static-replay", state.staticReplay);
  const liveOption = element("mode-select").querySelector('option[value="dsh"]');
  if (state.staticReplay) {
    chip.className = "environment-chip";
    chip.textContent = "HOSTED INTERACTIVE REPLAY";
    liveOption.disabled = true;
    liveOption.textContent = "Live DSH · run locally";
    element("trace-label").textContent = "BROWSER REPLAY EVENTS";
    return;
  }
  const ready = state.environment.live_ready;
  chip.className = `environment-chip ${ready ? "" : "offline"}`;
  chip.textContent = ready ? "DEEPSEEK LIVE READY" : "REPLAY READY · LIVE NEEDS SETUP";
  liveOption.textContent = ready ? "Live DeepSeek Harness" : "Live DSH · setup required";
}

async function loadLatestRun() {
  const payload = await request("/api/rsi/runs");
  if (payload.runs.length) render(payload.runs[0]);
}

function render(snapshot) {
  state.snapshot = snapshot;
  const report = snapshot.report;
  const current = snapshot.state;
  const generations = report.generations;
  const scored = [...generations].reverse().find((generation) => generation.score != null);
  const latest = scored || null;
  const delta = latest ? latest.score - report.baseline.score : null;

  element("run-status").textContent = current.status.replaceAll("_", " ").toUpperCase();
  element("run-id").textContent = `${report.mode.toUpperCase()} · ${report.run_id.slice(0, 19)}…`;
  element("status-indicator").className = `status-indicator ${current.status}`;
  element("baseline-score").textContent = percent(report.baseline.score);
  element("baseline-detail").textContent = `${report.baseline.passed} / ${report.baseline.total} benchmark cases`;
  element("latest-score").textContent = latest ? percent(latest.score) : "—";
  element("latest-detail").textContent = latest ? `Generation ${latest.generation} · ${latest.status}` : "Waiting for evaluation";
  element("holdout-score").textContent = latest ? percent(latest.holdout_score) : "—";
  element("holdout-detail").textContent = latest && latest.holdout_score >= .75 ? "Holdout gate passed" : "Promotion gate ≥ 75%";
  element("uplift-score").textContent = delta == null ? "—" : `${delta >= 0 ? "+" : ""}${Math.round(delta * 1000) / 10} pp`;
  element("uplift-detail").textContent = report.promoted ? "Approved and durably promoted" : "Relative to active baseline";
  element("event-count").textContent = state.staticReplay
    ? `${snapshot.events.length} browser-persisted replay events`
    : `${snapshot.events.length} persisted events`;
  element("skill-version").textContent = report.active_version_id.slice(0, 14).toUpperCase();
  element("skill-location").textContent = state.staticReplay
    ? "HOSTED REPLAY · Native DSH skill projection is exercised by the runnable local backend."
    : report.dsh_skill.materialized
    ? `.dsh/skills/${report.dsh_skill.name}/SKILL.md · ACTIVE PROJECTION`
    : "Native DSH skill has not been materialized.";

  renderPipeline(current);
  renderGenerations(report);
  renderEvents(snapshot.events);
  renderDiff(snapshot);
  updateButtons();
  renderGuide();

  if (current.status === "awaiting_human") {
    notify("The candidate passed its hidden-holdout gate. Release remains blocked until a human approves.");
  }
}

function guideStep() {
  const snapshot = state.snapshot;
  if (!snapshot) return {
    number: 1, target: "new-run", title: "Start the governed experiment",
    description: "Keep deterministic replay selected, then click New experiment. Everything runs locally without an API key.",
    evidence: "What to notice: the initial recovery policy starts at only 12.5%.",
  };

  const current = snapshot.state;
  const generations = snapshot.report.generations;
  if (current.status === "paused") return {
    number: 2, target: "resume-button", title: "Resume the durable workflow",
    description: "The experiment is paused at a safe boundary. Resume it to continue from the persisted state.",
    evidence: "What to notice: no generation or event history is lost while paused.",
  };
  if (current.status === "rolled_back") return {
    number: 7, target: "probe-button", title: "Rollback restored the original skill",
    description: "Evaluate the same permission failure again. The baseline policy now returns inspect_context instead of request_human.",
    evidence: "You demonstrated durable evolution, verification, human review, promotion, and rollback.",
    complete: true,
  };
  if (current.status === "succeeded" && state.probeVerified) return {
    number: 7, target: "rollback-button", title: "Roll back the active release",
    description: "Click Rollback to restore the original version and its projected native DSH skill.",
    evidence: "What to notice: rollback changes the actual policy, not just dashboard state.",
  };
  if (current.status === "succeeded") return {
    number: 6, target: "probe-button", title: "Prove the new capability",
    description: "Evaluate the prefilled 403 permission failure against the newly promoted policy.",
    evidence: "Expected result: request_human. The original policy returned inspect_context.",
  };
  if (current.status === "awaiting_human") return {
    number: 5, target: "approve-button", title: "Approve the human review gate",
    description: "Generation 2 passed its hidden holdout, but cannot release itself. Click Approve & promote.",
    evidence: "What to notice: 100% overall, 100% holdout, and an independent human decision.",
  };
  if (generations.length === 0) return {
    number: 2, target: "step-button", title: "Generate the first candidate",
    description: "Click Advance one step to let the agent evolve its own recovery scaffold.",
    evidence: "What to notice: model weights, the supervisor, and the evaluator remain frozen.",
  };
  if (generations[0].score == null) return {
    number: 3, target: "step-button", title: "Evaluate the first generation",
    description: "Click Advance one step again to score the candidate against independent training and holdout cases.",
    evidence: "Expected: 43.75% overall but only 37.5% holdout, so promotion is rejected.",
  };
  return {
    number: 4, target: "run-button", title: "Run until human review",
    description: "Generation 1 was rejected. Continue the loop to generate and evaluate the improved second version.",
    evidence: "What to notice: failure feedback drives improvement without revealing holdout answers.",
  };
}

function renderGuide() {
  const card = element("demo-tour");
  const step = guideStep();
  card.hidden = !state.guideOpen;
  element("tour-step-count").textContent = step.complete ? "WALKTHROUGH COMPLETE" : `STEP ${step.number} OF 7`;
  element("tour-progress-fill").style.width = `${step.number / 7 * 100}%`;
  element("tour-title").textContent = step.title;
  element("tour-description").textContent = step.description;
  element("tour-evidence").textContent = step.evidence;
  const target = element(step.target);
  const actionName = target?.textContent.trim() || "Continue";
  element("tour-focus").textContent = step.complete
    ? "Verify restored policy →"
    : `Next: ${actionName} →`;
  element("tour-focus").disabled = state.busy;
  for (const highlighted of document.querySelectorAll(".tour-highlight")) {
    highlighted.classList.remove("tour-highlight");
  }
  if (state.guideOpen) element(step.target)?.classList.add("tour-highlight");
}

function focusGuideTarget() {
  const target = element(guideStep().target);
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.focus({ preventScroll: true });
}

function executeGuideStep() {
  if (state.busy) return;
  const target = element(guideStep().target);
  if (!target || target.disabled) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.click();
}

function renderPipeline(current) {
  const order = ["execute", "verify", "hitl", "promote"];
  const index = order.indexOf(current.current_node);
  for (const node of document.querySelectorAll(".pipeline-node")) {
    node.className = "pipeline-node";
    const stage = node.dataset.stage;
    const position = order.indexOf(stage);
    if (current.status === "rolled_back" && stage === "rollback") node.classList.add("reverted");
    else if (current.status === "succeeded" && stage !== "rollback") node.classList.add("complete");
    else if (current.status === "awaiting_human" && stage === "hitl") node.classList.add("waiting");
    else if (stage === current.current_node) node.classList.add("current");
    else if (position !== -1 && index !== -1 && position < index) node.classList.add("complete");
  }
}

function renderGenerations(report) {
  const rows = [{
    label: "ACTIVE BASELINE · V0",
    score: report.baseline.score,
    holdout: report.baseline.holdout_score,
    status: "baseline",
  }, ...report.generations.map((generation) => ({
    label: `GENERATION ${generation.generation}`,
    score: generation.score,
    holdout: generation.holdout_score,
    status: generation.status,
  }))];
  element("generation-list").innerHTML = rows.map((row) => `
    <div class="generation-row">
      <div class="generation-head">
        <strong>${escapeHtml(row.label)}</strong>
        <span class="generation-status ${escapeHtml(row.status)}">${escapeHtml(row.status.toUpperCase())}</span>
      </div>
      <div class="score-track"><div class="score-fill ${escapeHtml(row.status)}" style="width:${Math.round((row.score || 0) * 100)}%"></div></div>
      <div class="generation-caption"><span>OVERALL ${percent(row.score)}</span><span>HOLDOUT ${percent(row.holdout)}</span></div>
    </div>`).join("");
}

function renderEvents(events) {
  element("event-list").innerHTML = [...events].reverse().map((event) => {
    const tone = /promot|succeed|approv|completed/.test(event.event_type) ? "good" : /reject|rollback|fail/.test(event.event_type) ? "bad" : "";
    return `<div class="event-row"><span class="event-seq">${String(event.seq).padStart(2, "0")}</span><span class="event-kind ${tone}">${escapeHtml(event.event_type)}</span><span class="event-node">${escapeHtml(event.node || "run")}</span></div>`;
  }).join("");
}

function renderDiff(snapshot) {
  const source = snapshot.scaffold_diff || snapshot.active_source || "No scaffold available.";
  const lines = source.split("\n").map((line) => {
    const css = line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@") ? "diff-header"
      : line.startsWith("+") ? "diff-added"
        : line.startsWith("-") ? "diff-removed"
          : "";
    return `<span class="${css}">${escapeHtml(line)}</span>`;
  }).join("\n");
  element("code-diff").innerHTML = `<code>${lines}</code>`;
}

function updateButtons() {
  const current = state.snapshot?.state;
  const blocked = state.busy || !current;
  const running = current?.status === "running";
  element("new-run").disabled = state.busy;
  element("run-button").disabled = blocked || !running;
  element("step-button").disabled = blocked || !running;
  element("approve-button").disabled = blocked || current.status !== "awaiting_human";
  element("pause-button").disabled = blocked || !running;
  element("resume-button").disabled = blocked || current.status !== "paused";
  element("rollback-button").disabled = blocked || current.status !== "succeeded";
  element("probe-button").disabled = blocked;
  element("tour-focus").disabled = state.busy;
}

async function operate(action, body = {}) {
  if (!state.snapshot) return;
  const runId = state.snapshot.report.run_id;
  const snapshot = await request(`/api/rsi/runs/${runId}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  render(snapshot);
}

element("new-run").addEventListener("click", () => guarded(async () => {
  const mode = element("mode-select").value;
  if (mode === "dsh" && !state.environment.live_ready) {
    const reasons = [];
    if (!state.environment.sdk_installed) reasons.push("install the official SDK with pip install -e '.[dsh]'");
    if (!state.environment.credential_configured) reasons.push("set DEEPSEEK_API_KEY in the server environment");
    throw new Error(`Live DeepSeek is not ready: ${reasons.join("; ")}. Replay remains fully operational.`);
  }
  const snapshot = await request("/api/rsi/runs", {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
  state.probeVerified = false;
  render(snapshot);
}));

for (const [id, action] of [
  ["run-button", "run"], ["step-button", "step"], ["approve-button", "approve"],
  ["pause-button", "pause"], ["resume-button", "resume"], ["rollback-button", "rollback"],
]) {
  element(id).addEventListener("click", () => guarded(() => operate(action)));
}

element("refresh-button").addEventListener("click", () => guarded(async () => {
  await loadEnvironment();
  if (state.snapshot) render(await request(`/api/rsi/runs/${state.snapshot.report.run_id}`));
  else await loadLatestRun();
}));

element("probe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guarded(async () => {
    const value = await request(`/api/rsi/runs/${state.snapshot.report.run_id}/probe`, {
      method: "POST",
      body: JSON.stringify({ trace: { error: element("probe-input").value, failure_count: 1 } }),
    });
    element("probe-result").querySelector("strong").textContent = value.action;
    state.probeVerified = state.snapshot.state.status === "succeeded";
    renderGuide();
  });
});

element("tour-launch").addEventListener("click", () => {
  state.guideOpen = true;
  renderGuide();
  focusGuideTarget();
});
for (const id of ["tour-close", "tour-dismiss"]) {
  element(id).addEventListener("click", () => {
    state.guideOpen = false;
    renderGuide();
  });
}
element("tour-focus").addEventListener("click", executeGuideStep);

guarded(async () => {
  await loadEnvironment();
  await loadLatestRun();
  renderGuide();
});
