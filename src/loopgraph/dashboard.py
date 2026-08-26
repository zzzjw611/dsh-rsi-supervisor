from __future__ import annotations

import importlib.util
import os
from importlib import resources
from pathlib import Path
from typing import Any

from .rsi import ReplayEvolver, RsiExperiment
from .storage import SQLiteRepository


ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/replay.js": ("replay.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def asset(path: str) -> tuple[bytes, str] | None:
    selected = ASSETS.get(path)
    if selected is None:
        return None
    filename, content_type = selected
    content = resources.files("loopgraph").joinpath("static", filename).read_bytes()
    return content, content_type


def dsh_environment() -> dict[str, Any]:
    sdk_installed = importlib.util.find_spec("deepseek_harness") is not None
    credential_configured = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
    custom_composition = bool(os.getenv("DSH_CORDIS", "").strip())
    return {
        "sdk_installed": sdk_installed,
        "credential_configured": credential_configured,
        "live_ready": sdk_installed and credential_configured,
        "provider": os.getenv("DSH_PROVIDER", "deepseek-official"),
        "model": os.getenv("DSH_MODEL", "deepseek-v4-flash"),
        "custom_endpoint_configured": bool(os.getenv("DEEPSEEK_BASE_URL", "").strip()),
        "custom_composition_configured": custom_composition,
        "skill_discovery": (
            "depends on plugins enabled in the configured Cordis composition"
            if custom_composition
            else "project skill is materialized; SDK default composition may not expose skills"
        ),
        "installation": "python -m pip install -e '.[dsh]'",
        "credential_variable": "DEEPSEEK_API_KEY",
    }


def experiment_for_run(
    repository: SQLiteRepository, run_id: str, *, executable: bool = False
) -> RsiExperiment:
    state = repository.get_state(run_id)
    if state.config.adapter != "rsi-evolver":
        raise ValueError("the selected run is not an RSI experiment")
    values: dict[str, Any] = {
        "mode": state.config.metadata.get("mode", "replay") if executable else "replay",
        "workspace": state.config.workspace,
        "channel": state.config.channel,
        "require_approval": state.config.require_approval,
        "max_generations": state.config.max_iterations,
        "minimum_holdout_score": float(
            state.config.metadata.get("minimum_holdout_score", 0.75)
        ),
    }
    if not executable:
        values["inner_harness"] = ReplayEvolver()
    return RsiExperiment(repository, **values)


def snapshot(repository: SQLiteRepository, run_id: str) -> dict[str, Any]:
    experiment = experiment_for_run(repository, run_id)
    try:
        state = repository.get_state(run_id)
        report = experiment.report(run_id)
        baseline = repository.get_version(str(state.config.metadata["baseline_version_id"]))
        active = repository.get_version(repository.get_channel(state.config.channel))
        candidate = repository.get_version(state.candidate_version_id)
        return {
            "report": report,
            "state": state.to_dict(),
            "events": [event.to_dict() for event in repository.list_events(run_id)],
            "baseline_source": str(baseline.artifact["output"]) if baseline else "",
            "active_source": str(active.artifact["output"]) if active else "",
            "candidate_source": str(candidate.artifact["output"]) if candidate else "",
            "scaffold_diff": experiment.scaffold_diff(run_id) if candidate else "",
            "workspace": str(Path(state.config.workspace)),
        }
    finally:
        experiment.close()
