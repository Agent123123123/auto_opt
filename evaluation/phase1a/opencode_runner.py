"""
opencode_runner.py
------------------
Run opencode agents via agent_container for proper sandbox isolation.

Each invocation:
  - Creates an isolated sandbox (HOME, XDG_*, copies workspace)
  - Inherits host PATH and all env vars (module system, API keys, etc.)
  - Runs ``opencode run --format json`` inside the sandbox
  - After completion, syncs the sandbox workspace back to the original ``cwd``
  - Returns the final text response (or result_file contents if specified)

Public API
----------
  run(prompt, cwd, model, chat_history_file, timeout, result_file, agent) -> str
  extract_json(text) -> str
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("opencode_runner")

# ---------------------------------------------------------------------------
# agent_container import
# ---------------------------------------------------------------------------
try:
    from runner import ExperimentDefaults, ExperimentSpec, RunSpec, run_experiment_sync
except ImportError:
    _SUBMODULE_PATH = (Path(__file__).parent.parent / "agent_container").resolve()
    if _SUBMODULE_PATH.exists():
        sys.path.insert(0, str(_SUBMODULE_PATH))
    from runner import ExperimentDefaults, ExperimentSpec, RunSpec, run_experiment_sync  # type: ignore[import]

# Keys agent_container injects with isolated values — exclude from passthrough.
_ISOLATED_ENV_KEYS = frozenset({
    "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "XDG_CACHE_HOME",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    prompt: str,
    cwd: str,
    model: str,
    chat_history_file: Optional[str] = None,
    timeout: int = 900,
    result_file: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """
    Run an opencode agent inside an isolated sandbox and return the final
    text response.

    The sandbox workspace is a copy of ``cwd``; it is synced back after
    the run so callers always find artifacts at their expected paths.

    Parameters
    ----------
    prompt : str
        Full prompt (system + user) as one string.
    cwd : str
        Working directory.  Copied to an isolated sandbox, synced back on completion.
    model : str
        ``"providerID/modelID"`` e.g. ``"dashscope/qwen-max"``.
    chat_history_file : str | None
        If given, the raw stdout.jsonl from the run is copied here (as ``_raw.jsonl``),
        and a plain-text session summary from NormalizedSession is written as the
        ``.md`` file.  Useful for inspection and debugging.
    timeout : int
        Maximum wall-clock seconds before the run is killed.
    result_file : str | None
        If given, read this filename from ``cwd`` after sync-back and return its
        contents (used by judge/meta agents for structured output).
    agent : str | None
        opencode agent name, e.g. ``"no-skill"``.
    """
    cwd = os.path.abspath(cwd)
    os.makedirs(cwd, exist_ok=True)

    run_id         = "run"
    exp_id         = uuid.uuid4().hex[:12]
    artifacts_root = os.path.join(os.path.dirname(cwd), ".ac_runs")

    # Forward entire env except keys the sandbox will override.
    env_passthrough: dict[str, str] = {
        k: v for k, v in os.environ.items()
        if k not in _ISOLATED_ENV_KEYS
    }

    spec = ExperimentSpec(
        id=exp_id,
        name="opencode_runner",
        workspace=cwd,
        inherit_auth=True,
        artifacts_root=artifacts_root,
        defaults=ExperimentDefaults(
            platform="opencode",
            timeout_seconds=timeout,
            agent=agent,
            model=model,
        ),
        runs=[RunSpec(run_id=run_id, candidate_id="run", prompt=prompt, env=env_passthrough)],
    )

    log.info(
        "opencode run  cwd=%s  model=%s  agent=%s  timeout=%ds  prompt_len=%d",
        cwd, model, agent or "(default)", timeout, len(prompt),
    )

    experiment_result = run_experiment_sync(spec)
    run_result        = experiment_result.results[0]

    log.info(
        "opencode run finished  status=%s  exit=%s  duration_ms=%d",
        run_result.status, run_result.exit_code, run_result.duration_ms,
    )

    # ── Sync sandbox workspace → original cwd ────────────────────────────
    sandbox_workspace = (
        Path(experiment_result.artifact_root)
        / "runs" / run_id / "sandbox" / "workspace"
    )
    if sandbox_workspace.exists():
        shutil.copytree(str(sandbox_workspace), cwd, dirs_exist_ok=True)
        log.info("Synced sandbox workspace → %s", cwd)
    else:
        log.warning("Sandbox workspace not found at %s", sandbox_workspace)

    # ── Chat history: copy stdout.jsonl + write session summary ──────────
    if chat_history_file:
        stdout_jsonl_str = run_result.artifact_paths.get("stdout_jsonl", "")
        stdout_jsonl     = Path(stdout_jsonl_str) if stdout_jsonl_str else None

        os.makedirs(os.path.dirname(os.path.abspath(chat_history_file)), exist_ok=True)

        # Copy raw events alongside the chat history for debugging
        if stdout_jsonl and stdout_jsonl.exists():
            raw_path = chat_history_file.rsplit(".", 1)[0] + "_raw.jsonl"
            try:
                shutil.copy2(str(stdout_jsonl), raw_path)
            except Exception as exc:
                log.warning("Could not copy stdout.jsonl: %s", exc)

        # Write markdown summary from NormalizedSession
        with open(chat_history_file, "w", encoding="utf-8") as fh:
            fh.write(f"# opencode Session — {exp_id}\n\n")
            fh.write(f"- status : {run_result.status}\n")
            fh.write(f"- exit   : {run_result.exit_code}\n")
            fh.write(f"- ms     : {run_result.duration_ms}\n")
            fh.write(f"- session: {run_result.session_id or '(none)'}\n\n")
            if run_result.error:
                fh.write(f"> **Error**: {run_result.error}\n\n")
            ns = run_result.normalized_session
            if ns:
                stats = ns.stats
                fh.write(f"## Stats\n\n")
                fh.write(f"- messages   : {stats.message_count}\n")
                fh.write(f"- tool calls : {stats.tool_call_count}\n")
                fh.write(f"- skills used: {stats.skills_used}\n")

                fh.write(f"\n## Final Output\n\n")
                fh.write(ns.final_output_text or "(no output)")
                fh.write("\n")

    # ── Read result_file if requested ─────────────────────────────────────
    if result_file:
        result_path = os.path.join(cwd, result_file)
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as fh:
                return fh.read()
        log.warning(
            "result_file=%r not found in cwd=%s — falling back to final text",
            result_file, cwd,
        )

    # ── Return final text ─────────────────────────────────────────────────
    if run_result.normalized_session:
        return run_result.normalized_session.final_output_text or ""
    return ""


# ---------------------------------------------------------------------------
# JSON extraction helper (used by judge_agent)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> str:
    """
    Extract the first balanced JSON object from text that may contain
    surrounding boilerplate (e.g. opencode's skill-call-log footer).
    Returns the JSON object string, or the original text if no { } found.
    """
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return text[start : i + 1]
    return text
