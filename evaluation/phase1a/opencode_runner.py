"""
opencode_runner.py
------------------
Run opencode agents via agent_container for proper sandbox isolation.

Each invocation:
  - Creates an isolated sandbox (HOME, XDG_*, copies workspace)
  - Inherits host PATH and all env vars (module system, API keys, etc.)
  - Runs ``opencode run --format json`` inside the sandbox
  - After completion, syncs the sandbox workspace back to the original ``cwd``
    so callers always find artifacts at their expected paths
  - Returns the final text response (or result_file contents if specified)

API surface (public)
--------------------
  run(prompt, cwd, model, chat_history_file, timeout, result_file, agent) -> str
  extract_json(text) -> str
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("opencode_runner")

# ---------------------------------------------------------------------------
# agent_container import  (pip-installed package, or submodule fallback)
# ---------------------------------------------------------------------------
try:
    from runner import ExperimentDefaults, ExperimentSpec, RunSpec, run_experiment_sync
except ImportError:
    _SUBMODULE_PATH = (Path(__file__).parent.parent / "agent_container").resolve()
    if _SUBMODULE_PATH.exists():
        sys.path.insert(0, str(_SUBMODULE_PATH))
    from runner import ExperimentDefaults, ExperimentSpec, RunSpec, run_experiment_sync  # type: ignore[import]

# Keys that agent_container injects with isolated values — exclude from passthrough
# so they don't accidentally survive into run.env and override the isolation.
_ISOLATED_ENV_KEYS = frozenset({
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
})


# ---------------------------------------------------------------------------
# Markdown log helpers  (post-process stdout.jsonl → human-readable markdown)
# ---------------------------------------------------------------------------

def _events_from_jsonl(jsonl_path: Path) -> tuple[list[dict], dict]:
    """Parse agent_container's stdout.jsonl into (all_parts, total_tokens)."""
    all_parts: list[dict] = []
    total_tokens: dict = {}
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                part  = event.get("part", {})
                if etype in ("tool_use", "text", "step_finish", "step_start", "reasoning"):
                    all_parts.append(part)
                    if etype == "step_finish":
                        tokens = part.get("tokens", {})
                        if tokens:
                            total_tokens.update(tokens)
    except Exception as exc:
        log.warning("Could not read stdout.jsonl at %s: %s", jsonl_path, exc)
    return all_parts, total_tokens


def _format_log(prompt: str, messages: list, info: dict) -> str:
    """Build a human-readable markdown log from session messages."""
    lines: list[str] = ["# opencode Session Log\n"]
    lines.append(f"## User Prompt\n\n```\n{prompt}\n```\n")
    lines.append("## Interaction\n")

    for msg in messages:
        role  = msg.get("info", {}).get("role", "?")
        parts = msg.get("parts", [])
        lines.append(f"### [{role.upper()}]\n")
        for p in parts:
            ptype = p.get("type", "")
            if ptype == "text":
                lines.append(p.get("text", ""))
            elif ptype == "tool":
                tool_name = p.get("tool", "?")
                state     = p.get("state", {})
                inp       = state.get("input", {})
                out       = state.get("output", "")
                meta      = state.get("metadata", {})
                exit_code = meta.get("exit", "?")
                lines.append(
                    f"**Tool: `{tool_name}`**\n"
                    f"```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```\n"
                    f"**Output** (exit={exit_code}):\n"
                    f"```\n{str(out)}\n```"
                )
            elif ptype == "reasoning":
                lines.append(f"*Reasoning:* {p.get('text', '')}")
        lines.append("")

    tokens = info.get("tokens", {})
    lines.append(
        f"## Stats\n"
        f"- finish: `{info.get('finish', '?')}`\n"
        f"- tokens: total={tokens.get('total', 0)}"
        f"  input={tokens.get('input', 0)}"
        f"  output={tokens.get('output', 0)}\n"
    )
    return "\n".join(lines)


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
    Run an opencode agent via agent_container (isolated sandbox) and return
    the final text response.

    agent_container creates an isolated HOME and XDG directory tree per call
    so opencode's SQLite state, config, and auth never leak between runs.
    The sandbox workspace is a copy of ``cwd``; after the run it is synced
    back so callers always find artifacts at their expected paths.

    Parameters
    ----------
    prompt : str
        Full prompt (system + user) as one string.
    cwd : str
        Working directory.  Copied to an isolated sandbox for the run,
        then synced back here on completion.
    model : str
        ``"providerID/modelID"`` e.g. ``"dashscope/qwen-max"``.
    chat_history_file : str | None
        If given, write a markdown interaction log here after the run.
        A ``_raw.jsonl`` sibling is also written for deep debugging.
    timeout : int
        Maximum wall-clock seconds before the run is killed.
    result_file : str | None
        If given, read this filename from ``cwd`` after sync-back and return
        its contents (used by judge/meta agents for structured output).
    agent : str | None
        opencode agent name, e.g. ``"no-skill"``.
    """
    cwd = os.path.abspath(cwd)
    os.makedirs(cwd, exist_ok=True)

    run_id = "run"
    exp_id = uuid.uuid4().hex[:12]
    # Place agent_container artifacts in a sibling .ac_runs/ directory so they
    # never pollute the workspace that gets synced back to cwd.
    artifacts_root = os.path.join(os.path.dirname(cwd), ".ac_runs")

    # Pass ALL current env vars except the ones agent_container replaces with
    # isolated sandbox paths.  This preserves the module system (MODULEPATH,
    # LMOD_*), API keys, LANG, TERM, and any other required host settings.
    env_passthrough: dict[str, str] = {
        k: v
        for k, v in os.environ.items()
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
        runs=[
            RunSpec(
                run_id=run_id,
                candidate_id="run",
                prompt=prompt,
                env=env_passthrough,
            )
        ],
    )

    log.info(
        "opencode run  cwd=%s  model=%s  agent=%s  timeout=%ds  prompt_len=%d",
        cwd, model, agent or "(default)", timeout, len(prompt),
    )

    experiment_result = run_experiment_sync(spec)
    run_result = experiment_result.results[0]

    log.info(
        "opencode run finished  status=%s  exit=%s  duration_ms=%d",
        run_result.status, run_result.exit_code, run_result.duration_ms,
    )

    # ── Locate the sandbox workspace ──────────────────────────────────────
    sandbox_workspace = (
        Path(experiment_result.artifact_root)
        / "runs" / run_id / "sandbox" / "workspace"
    )

    # ── Sync sandbox workspace → original cwd ────────────────────────────
    # Every file the agent wrote in the sandbox becomes visible at the
    # original cwd, so downstream callers don't need sandbox-awareness.
    if sandbox_workspace.exists():
        shutil.copytree(str(sandbox_workspace), cwd, dirs_exist_ok=True)
        log.info("Synced sandbox workspace → %s", cwd)
    else:
        log.warning("Sandbox workspace not found at %s", sandbox_workspace)

    # ── Write chat history log ────────────────────────────────────────────
    if chat_history_file:
        stdout_jsonl_str = run_result.artifact_paths.get("stdout_jsonl", "")
        stdout_jsonl = Path(stdout_jsonl_str) if stdout_jsonl_str else None
        if stdout_jsonl and stdout_jsonl.exists():
            all_parts, total_tokens = _events_from_jsonl(stdout_jsonl)
            messages = [{"info": {"role": "assistant"}, "parts": all_parts}]
            log_md = _format_log(prompt, messages, {"tokens": total_tokens})
            os.makedirs(
                os.path.dirname(os.path.abspath(chat_history_file)), exist_ok=True
            )
            with open(chat_history_file, "w", encoding="utf-8") as fh:
                fh.write(log_md)
            # Copy raw events JSONL alongside the markdown for deep debugging
            raw_jsonl_path = chat_history_file.rsplit(".", 1)[0] + "_raw.jsonl"
            try:
                shutil.copy2(str(stdout_jsonl), raw_jsonl_path)
            except Exception as exc:
                log.warning("Could not copy stdout_jsonl: %s", exc)

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

    # ── Return final text from the normalized session ─────────────────────
    if run_result.normalized_session:
        return run_result.normalized_session.final_output_text or ""
    return ""


# ---------------------------------------------------------------------------
# JSON extraction helper (used by judge_agent)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> str:
    """
    Extract the first balanced JSON object from text that may contain
    surrounding boilerplate (e.g. opencode's skill-call-log footer injected
    by AGENTS.md/CLAUDE.md).

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
