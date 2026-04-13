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

import json
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
    from runner import CallCapabilities, CallEnvironment, CallExecution, CallInputs, CallSpec, CallTarget, call_once_sync
except ImportError:
    _SUBMODULE_PATH = (Path(__file__).parent.parent / "agent_container").resolve()
    if _SUBMODULE_PATH.exists():
        sys.path.insert(0, str(_SUBMODULE_PATH))
    from runner import CallCapabilities, CallEnvironment, CallExecution, CallInputs, CallSpec, CallTarget, call_once_sync  # type: ignore[import]

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

    call_id        = uuid.uuid4().hex[:12]
    artifacts_root = os.path.join(os.path.dirname(cwd), ".ac_runs")

    spec = CallSpec(
        call_id=call_id,
        target=CallTarget(
            platform="opencode",
            agent=agent,
            model=model,
        ),
        inputs=CallInputs(prompt=prompt),
        environment=CallEnvironment(
            workspace=cwd,
            inherit_auth=True,
        ),
        execution=CallExecution(
            timeout_seconds=timeout,
            artifacts_root=artifacts_root,
        ),
    )

    log.info(
        "opencode run  cwd=%s  model=%s  agent=%s  timeout=%ds  prompt_len=%d",
        cwd, model, agent or "(default)", timeout, len(prompt),
    )

    # ── Pre-run: symlink chat_history_file → stream.md for live viewing ──
    # call_id is known here, so stream.md path is deterministic.
    # The symlink persists after the run — it always points to the real file.
    if chat_history_file:
        _stream_md_path = str(Path(artifacts_root) / call_id / "stream.md")
        os.makedirs(os.path.dirname(os.path.abspath(chat_history_file)), exist_ok=True)
        if os.path.lexists(chat_history_file):
            os.remove(chat_history_file)
        os.symlink(_stream_md_path, chat_history_file)

    run_result = call_once_sync(spec)

    log.info(
        "opencode run finished  status=%s  exit=%s  duration_ms=%d",
        run_result.status, run_result.exit_code, run_result.duration_ms,
    )

    # ── Sync sandbox workspace → original cwd ────────────────────────────
    # Do this BEFORE checking for errors: the task agent may have completed its
    # work successfully even if the post-run session export/parse step failed.
    sandbox_workspace = Path(run_result.artifact_root) / "sandbox" / "workspace"
    if sandbox_workspace.exists():
        def _safe_copy2(src, dst, **kwargs):
            """copy2 wrapper that silently skips same-file errors (symlinks to
            shared foundry objects like .mco files that appear in multiple combo
            subdirs but point to the same inode)."""
            try:
                shutil.copy2(src, dst, **kwargs)
            except shutil.SameFileError:
                pass  # benign: src and dst are the same underlying file

        try:
            shutil.copytree(
                str(sandbox_workspace), cwd,
                dirs_exist_ok=True,
                copy_function=_safe_copy2,
            )
        except shutil.Error as exc:
            # Catch any remaining multi-file errors; re-raise only non-same-file ones.
            full_msg = str(exc)
            if "same file" not in full_msg.lower():
                raise
            log.debug("copytree: suppressed 'same file' symlink collision(s)")
        log.info("Synced sandbox workspace → %s", cwd)
    else:
        log.warning("Sandbox workspace not found at %s", sandbox_workspace)

    # ── Copy raw stdout.jsonl for debugging ──────────────────────────────
    if chat_history_file:
        stdout_jsonl_str = run_result.artifact_paths.get("stdout_jsonl", "")
        stdout_jsonl     = Path(stdout_jsonl_str) if stdout_jsonl_str else None
        if stdout_jsonl and stdout_jsonl.exists():
            raw_path = chat_history_file.rsplit(".", 1)[0] + "_raw.jsonl"
            try:
                shutil.copy2(str(stdout_jsonl), raw_path)
            except Exception as exc:
                log.warning("Could not copy stdout.jsonl: %s", exc)

    # ── Fail hard only on real execution errors (not post-run artifact issues) ──
    # Session export/parse failures mean opencode ran fine but its session JSON
    # was malformed/truncated (common with very long MiniMax sessions).  The
    # workspace is already synced, so we can still return results from files.
    _ARTIFACT_ERRORS = ("session export", "normalized session", "session export parse")
    if run_result.error:
        is_artifact_only = all(
            any(tag in part for tag in _ARTIFACT_ERRORS)
            for part in run_result.error.split(";")
            if part.strip()
        )
        if not is_artifact_only:
            raise RuntimeError(
                f"opencode run failed (status={run_result.status}): {run_result.error}"
            )
        log.warning(
            "opencode session export/parse failed (workspace already synced, continuing): %s",
            run_result.error,
        )

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
