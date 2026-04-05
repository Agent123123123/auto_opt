"""
opencode_runner.py
------------------
Run opencode agents via the ``opencode run --format json`` CLI, streaming
JSON events from stdout in real time.

Each invocation spawns a fresh ``opencode run`` subprocess whose stdout
emits one JSON object per line:

  {"type":"step_start", ...}
  {"type":"tool_use",   "part":{"type":"tool","tool":"bash","state":{...}}}
  {"type":"text",       "part":{"type":"text","text":"..."}}
  {"type":"step_finish","part":{"reason":"stop"|"tool-calls", "tokens":{...}}}

Benefits over the old HTTP-serve approach:
  - True real-time streaming — every event is visible as it happens.
  - The subprocess can be killed if the agent hangs (no more un-cancellable
    blocking HTTP POST).
  - No server lifecycle to manage — each call is independent.

API surface (public)
--------------------
  run(prompt, cwd, model, chat_history_file, timeout, result_file) -> str
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import signal
import tempfile
import threading
import time
from typing import Optional

log = logging.getLogger("opencode_runner")


# ---------------------------------------------------------------------------
# Log formatting
# ---------------------------------------------------------------------------

def _format_log(prompt: str, messages: list, info: dict) -> str:
    """Build a human-readable markdown log from the session messages."""
    lines: list[str] = ["# opencode Session Log\n"]
    lines.append(f"## User Prompt\n\n```\n{prompt}\n```\n")
    lines.append("## Interaction\n")

    for msg in messages:
        role = msg.get("info", {}).get("role", "?")
        parts = msg.get("parts", [])
        lines.append(f"### [{role.upper()}]\n")
        for p in parts:
            ptype = p.get("type", "")
            if ptype == "text":
                lines.append(p.get("text", ""))
            elif ptype == "tool":
                tool_name = p.get("tool", "?")
                state = p.get("state", {})
                inp = state.get("input", {})
                out = state.get("output", "")
                meta = state.get("metadata", {})
                exit_code = meta.get("exit", "?")
                lines.append(
                    f"**Tool: `{tool_name}`**\n"
                    f"```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```\n"
                    f"**Output** (exit={exit_code}):\n"
                    f"```\n{str(out)}\n```"
                )
            elif ptype == "reasoning":
                lines.append(f"*Reasoning:* {p.get('text', '')}")
            # skip step-start / step-finish boilerplate
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
    Run an opencode agent via ``opencode run --format json`` and return the
    final text response.

    Events are streamed in real time from the subprocess stdout.  Each JSON
    line is parsed, logged, and accumulated into ``all_parts`` for the final
    markdown log.

    Parameters
    ----------
    prompt : str
        Full prompt (system instructions + user task) as one string.
    cwd : str
        Working directory for the opencode subprocess.
    model : str
        ``"providerID/modelID"`` e.g. ``"github-copilot/claude-sonnet-4.6"``.
    chat_history_file : str | None
        If given, write a markdown interaction log here (including tool
        calls and outputs).  Incremental snapshots are written as events
        arrive so partial progress is visible even if the process dies.
    timeout : int
        Maximum wall-clock seconds before the subprocess is killed.
    result_file : str | None
        If given, read this filename from ``cwd`` after the agent finishes
        and return its contents instead of the final text response.
    """
    cmd = ["opencode", "run", "--format", "json"]
    if agent:
        cmd += ["--agent", agent]
    if model:
        cmd += ["--model", model]

    log.info("opencode run  cwd=%s  model=%s  agent=%s  timeout=%ds  prompt_len=%d",
             cwd, model, agent or "(default)", timeout, len(prompt))

    # Ensure the target directory exists
    os.makedirs(cwd, exist_ok=True)

    # Linux ARG_MAX is typically ~2MB; passing a large prompt as a CLI arg fails
    # with [Errno 7] Argument list too long.  For prompts over 64KB, write to a
    # temp file and feed via stdin — opencode reads the message from stdin when
    # no positional message arg is given.
    _PROMPT_SIZE_LIMIT = 65536  # bytes
    _stdin_fh = None  # file handle we own and must close after Popen
    _prompt_tmp: Optional[str] = None
    if len(prompt.encode()) > _PROMPT_SIZE_LIMIT:
        _tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=cwd
        )
        _tf.write(prompt)
        _tf.flush()
        _tf.close()
        _prompt_tmp = _tf.name
        _stdin_fh = open(_prompt_tmp, encoding="utf-8")  # child inherits fd via fork
        log.info("prompt > %dKB — writing to stdin (tmpfile: %s)",
                 _PROMPT_SIZE_LIMIT // 1024, os.path.basename(_prompt_tmp))
    else:
        cmd.append(prompt)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=_stdin_fh if _stdin_fh is not None else subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
    except Exception as exc:
        log.warning("Failed to start opencode run: %s", exc)
        return ""
    finally:
        # Close our copy of the file handle; the child process keeps its own fd.
        if _stdin_fh is not None:
            _stdin_fh.close()
        # Unlink the temp file (safe on Linux — child's fd keeps inode alive).
        if _prompt_tmp is not None:
            try:
                os.unlink(_prompt_tmp)
            except Exception:
                pass

    # Collect all events for final log; track final text pieces
    all_parts: list[dict] = []
    final_texts: list[str] = []
    total_tokens: dict = {}
    last_activity = time.time()
    snapshot_count = 0

    def _write_snapshot() -> None:
        """Write incremental chat history snapshot."""
        nonlocal snapshot_count
        if not chat_history_file:
            return
        try:
            messages = [{"info": {"role": "assistant"}, "parts": all_parts}]
            snap = _format_log(prompt, messages, {"tokens": total_tokens})
            _dir = os.path.dirname(os.path.abspath(chat_history_file))
            os.makedirs(_dir, exist_ok=True)
            with open(chat_history_file, "w", encoding="utf-8") as fh:
                fh.write(snap)
            snapshot_count += 1
        except Exception:
            pass

    # Drain stderr in a background thread so the pipe never blocks
    stderr_lines: list[str] = []
    def _drain_stderr():
        try:
            for line in proc.stderr:
                stderr_lines.append(line.rstrip())
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    timed_out = False
    try:
        deadline = time.time() + timeout
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            # Check timeout
            now = time.time()
            if now > deadline:
                log.warning("opencode run timed out after %ds — killing", timeout)
                timed_out = True
                break

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                log.debug("[stream] non-JSON line: %s", raw_line[:200])
                continue

            etype = event.get("type", "")
            part = event.get("part", {})
            last_activity = now

            if etype == "tool_use":
                all_parts.append(part)
                tool_name = part.get("tool", "?")
                state = part.get("state", {})
                inp = state.get("input", {})
                meta = state.get("metadata") or {}
                cmd_snippet = (
                    inp.get("command")
                    or inp.get("path")
                    or str(inp)
                )[:120]
                exit_code = meta.get("exit", "?")
                out_snippet = str(meta.get("output", state.get("output", "")))[:200]
                log.info(
                    "[stream] tool=%s  cmd=%r  exit=%s\n"
                    "         out=%r",
                    tool_name, cmd_snippet, exit_code, out_snippet,
                )
                _write_snapshot()

            elif etype == "text":
                all_parts.append(part)
                text = part.get("text", "")
                if text.strip():
                    final_texts.append(text)
                    log.info("[stream] text: %r", text[:200])
                _write_snapshot()

            elif etype == "step_finish":
                all_parts.append(part)
                tokens = part.get("tokens", {})
                if tokens:
                    total_tokens.update(tokens)
                reason = part.get("reason", "?")
                log.info(
                    "[stream] step_finish reason=%s  tokens=%s",
                    reason, json.dumps(tokens),
                )

            elif etype == "step_start":
                all_parts.append(part)

            elif etype == "reasoning":
                all_parts.append(part)

            elif etype == "error":
                err = event.get("error", {})
                err_msg = err.get("data", {}).get("message", str(err))
                log.warning("[stream] error event: %s", err_msg)

            else:
                # Unknown event type — store but don't log verbosely
                all_parts.append(part)

    except Exception as exc:
        log.warning("Error reading opencode stdout: %s", exc)
    finally:
        if timed_out or proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        else:
            proc.wait()

    exit_code_proc = proc.returncode
    log.info(
        "opencode run finished  exit=%s  parts=%d  timed_out=%s",
        exit_code_proc, len(all_parts), timed_out,
    )

    # ── Final chat history write ──────────────────────────────────────────
    if chat_history_file:
        messages = [{"info": {"role": "assistant"}, "parts": all_parts}]
        log_content = _format_log(prompt, messages, {"tokens": total_tokens})
        os.makedirs(
            os.path.dirname(os.path.abspath(chat_history_file)), exist_ok=True
        )
        with open(chat_history_file, "w", encoding="utf-8") as fh:
            fh.write(log_content)

        # Save raw events JSON for deep debugging
        raw_json_path = chat_history_file.rsplit(".", 1)[0] + "_raw.json"
        try:
            with open(raw_json_path, "w", encoding="utf-8") as fh:
                json.dump(all_parts, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            log.warning("Could not write raw events JSON: %s", exc)

    # ── Read result file if requested ─────────────────────────────────────
    if result_file:
        result_path = os.path.join(cwd, result_file)
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as fh:
                return fh.read()
        log.warning(
            "result_file=%r not found in cwd=%s — falling back to final text",
            result_file, cwd,
        )

    return "\n".join(final_texts)


# ---------------------------------------------------------------------------
# JSON extraction helper (for judge_agent)
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
