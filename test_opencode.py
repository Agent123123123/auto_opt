#!/usr/bin/env python3
"""
Standalone test for opencode_runner.py
Run from mem_solution/:  python3 test_opencode.py

Tests
-----
1. Direct ``opencode run --format json`` streaming — shows LIVE NDJSON events
   as they arrive (step_start / text / tool_use / tool_result / step_finish).
2. ``opencode_runner.run()`` HTTP-API wrapper — verifies server startup,
   blocking POST, final text extraction, and chat log writing.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time

# ── Logging setup ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(name)-22s  %(levelname)-7s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("test_opencode")

# ── Config ────────────────────────────────────────────────────────────────
STREAM_MODEL = "github-copilot/claude-sonnet-4.6"
HTTP_MODEL    = "github-copilot/claude-sonnet-4.6"
CWD           = tempfile.mkdtemp(prefix="opencode_test_")
CHAT_LOG      = os.path.join(CWD, "chat_history.md")

PROMPT = (
    "You are a helpful assistant.  "
    "Use the bash tool to run EXACTLY these three commands one by one:\n"
    "  1. echo 'OPENCODE_HELLO'\n"
    "  2. date '+%Y-%m-%d %H:%M:%S'\n"
    "  3. uname -a\n"
    "After you have run all three commands, reply with a single line: "
    "RESULT:<output_of_command_1>|<output_of_command_2>|<output_of_command_3>\n"
    "Do not add anything else after the RESULT: line."
)

SEP = "=" * 70

# ──────────────────────────────────────────────────────────────────────────
# TEST 1 — streaming ``opencode run --format json``
# ──────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("TEST 1  opencode run --format json  (real-time NDJSON streaming)")
print(SEP)

cmd = [
    "opencode", "run",
    "--format", "json",
    "--model", STREAM_MODEL,
    PROMPT,
]
log.info("CMD: %s", " ".join(cmd))

t0 = time.time()
proc = subprocess.Popen(
    cmd,
    cwd=CWD,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,          # line-buffered so we get events as they arrive
)

events: list[tuple[float, str, str]] = []   # (elapsed, type, summary)
last_texts: list[str] = []
current_texts: list[str] = []

for raw in proc.stdout:
    raw = raw.strip()
    if not raw:
        continue
    elapsed = time.time() - t0
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [{elapsed:6.1f}s]  RAW (non-JSON): {raw[:120]}")
        continue

    etype = ev.get("type", "?")
    part  = ev.get("part", {})

    if etype == "step_start":
        current_texts = []
        summary = ""
    elif etype == "text":
        t = part.get("text", "")
        current_texts.append(t)
        summary = repr(t[:100])
    elif etype == "tool_use":
        name = ""
        if isinstance(part.get("tool"), dict):
            name = part["tool"].get("name", "?")
        else:
            name = part.get("name", "?")
        inp = part.get("input", {})
        cmd_snippet = (inp.get("command") or str(inp))[:80]
        summary = f"tool={name}  cmd={cmd_snippet!r}"
    elif etype == "tool_result":
        content = part.get("content", "")
        summary = repr(str(content)[:120])
    elif etype == "step_finish":
        reason = part.get("reason", "?")
        summary = f"reason={reason}"
        if reason == "stop":
            last_texts = current_texts[:]
    else:
        summary = str(part)[:80]

    print(f"  [{elapsed:6.1f}s]  {etype:<14}  {summary}")
    events.append((elapsed, etype, summary))

proc.wait()
stderr_out = proc.stderr.read()
total = time.time() - t0

print(f"\n  returncode : {proc.returncode}")
print(f"  total time : {total:.1f}s")
print(f"  event count: {len(events)}")
if stderr_out.strip():
    print(f"  STDERR     : {stderr_out[:400]}")

if last_texts:
    final_text_1 = "".join(last_texts)
    print(f"\n  ── Final text returned ──")
    print(f"  {final_text_1!r}")
else:
    final_text_1 = ""
    print("\n  !! No stop-reason step found — last current_texts:")
    print(f"  {''.join(current_texts)!r}")

result_line_1 = ""
for line in final_text_1.splitlines():
    if line.startswith("RESULT:"):
        result_line_1 = line
        break

print(f"\n  RESULT line: {result_line_1!r}")
print(f"  TEST 1 {'PASS ✓' if result_line_1 else 'FAIL ✗  (no RESULT: line)'}")


# ──────────────────────────────────────────────────────────────────────────
# TEST 2 — opencode_runner.run()  HTTP-API wrapper
# ──────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("TEST 2  opencode_runner.run()  (CLI streaming / opencode run --format json)")
print(SEP)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "evaluation"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../downloads/Hyperagents"))

try:
    from phase1a.opencode_runner import run as opencode_run
    log.info("opencode_runner imported OK")
except ImportError as exc:
    print(f"  !! ImportError: {exc}")
    sys.exit(1)

log.info("Calling opencode_runner.run() — this blocks until the full agent loop finishes")
log.info("model=%s  cwd=%s", HTTP_MODEL, CWD)

t1 = time.time()
final_text_2 = opencode_run(
    prompt=PROMPT,
    cwd=CWD,
    model=HTTP_MODEL,
    chat_history_file=CHAT_LOG,
    timeout=300,
)
total2 = time.time() - t1

print(f"\n  total time     : {total2:.1f}s")
print(f"  final_text len : {len(final_text_2)}")
print(f"  final_text     : {final_text_2!r}")

result_line_2 = ""
for line in final_text_2.splitlines():
    if line.startswith("RESULT:"):
        result_line_2 = line
        break

print(f"  RESULT line    : {result_line_2!r}")
print(f"  chat log       : {CHAT_LOG}")
print(f"  chat log exists: {os.path.exists(CHAT_LOG)}")
if os.path.exists(CHAT_LOG):
    size = os.path.getsize(CHAT_LOG)
    print(f"  chat log size  : {size} bytes")
    with open(CHAT_LOG) as f:
        head = f.read(1500)
    print(f"\n  ── chat log head (1500 chars) ──\n{head}\n  ──")

print(f"\n  TEST 2 {'PASS ✓' if result_line_2 else 'FAIL ✗  (no RESULT: line)'}")

# ──────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("SUMMARY")
print(SEP)
print(f"  Test 1 (streaming) : {'PASS ✓' if result_line_1 else 'FAIL ✗'}")
print(f"  Test 2 (HTTP API)  : {'PASS ✓' if result_line_2 else 'FAIL ✗'}")
print(f"  Temp dir: {CWD}")
print(SEP)
