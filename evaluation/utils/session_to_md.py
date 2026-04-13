#!/usr/bin/env python3
"""
Convert opencode session.normalized.json → readable Markdown.

Usage:
    python session_to_md.py session.normalized.json [output.md]

Falls back to task_chat_raw.jsonl for plain event streams (no reasoning).
"""
import json
import sys
import os
from datetime import datetime, timezone


def ts_ms_to_hms(ms: int | None) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S")


def duration_str(started: int | None, ended: int | None, dur_ms: int | None) -> str:
    if dur_ms is not None:
        return f"{dur_ms}ms"
    if started and ended:
        return f"{ended - started}ms"
    return ""


def fmt_output(text: str, max_lines: int = 60) -> str:
    if not text:
        return "(no output)"
    lines = text.splitlines()
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept.append(f"... ({len(lines) - max_lines} more lines omitted)")
        return "\n".join(kept)
    return text


def fmt_tool_input(tool: str, inp: dict) -> str:
    if not inp:
        return "(no input)"
    if tool == "bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        result = f"```bash\n{cmd}\n```"
        if desc:
            result = f"_{desc}_\n\n" + result
        return result
    if tool in ("read", "read_file"):
        path = inp.get("filePath") or inp.get("file_path") or inp.get("path", "")
        sl, el = inp.get("startLine", ""), inp.get("endLine", "")
        rng = f" L{sl}–{el}" if sl else ""
        return f"`{path}`{rng}"
    if tool in ("write", "write_file", "create_file"):
        path = inp.get("filePath") or inp.get("file_path", "")
        content = inp.get("content") or inp.get("newContent") or ""
        ext = os.path.splitext(path)[1].lstrip(".") or "text"
        if content:
            lines = content.splitlines()
            preview = "\n".join(lines[:50])
            note = f"\n... ({len(lines)-50} more lines)" if len(lines) > 50 else ""
            return f"`{path}`\n\n```{ext}\n{preview}{note}\n```"
        return f"`{path}`"
    if tool in ("edit", "edit_file", "replace_string_in_file", "str_replace_based_edit_tool"):
        path = inp.get("filePath") or inp.get("file_path", inp.get("path", ""))
        old = inp.get("oldString") or inp.get("old_str") or inp.get("old_string") or ""
        new = inp.get("newString") or inp.get("new_str") or inp.get("new_string") or ""
        parts = [f"`{path}`"]
        if old:
            parts.append(f"\n**Old:**\n```\n{old[:300]}{'...' if len(old)>300 else ''}\n```")
        if new:
            parts.append(f"\n**New:**\n```\n{new[:300]}{'...' if len(new)>300 else ''}\n```")
        return "\n".join(parts)
    if tool in ("glob", "file_search"):
        return f"pattern: `{inp.get('pattern') or inp.get('path', '')}` in `{inp.get('path', '.')}`"
    if tool == "grep":
        return f"pattern: `{inp.get('pattern', '')}` in `{inp.get('path', '.')}`"
    # fallback
    s = json.dumps(inp, ensure_ascii=False, indent=2)
    if len(s) > 1200:
        s = s[:1200] + "\n... (truncated)"
    return f"```json\n{s}\n```"


def convert_normalized(data: dict, out_path: str):
    info = data.get("info", {})
    title = info.get("title") or data.get("title") or "(untitled)"
    msgs = data.get("messages", [])

    lines = []
    lines.append(f"# {title}\n")

    # header stats
    total_input = total_output = total_cache_r = 0
    for m in msgs:
        if m.get("role") == "assistant":
            tok = m.get("tokens") or {}
            total_input   += tok.get("input", 0) or 0
            total_output  += tok.get("output", 0) or 0
            total_cache_r += tok.get("cache_read", 0) or 0

    lines.append(f"| 字段 | 值 |\n|------|----|\n")
    lines.append(f"| 消息数 | {len(msgs)} |\n")
    lines.append(f"| input tokens | {total_input:,} |\n")
    lines.append(f"| output tokens | {total_output:,} |\n")
    lines.append(f"| cache read | {total_cache_r:,} |\n")
    lines.append("\n---\n")

    msg_num = 0
    for m in msgs:
        role      = m.get("role", "?")
        text      = (m.get("text") or "").strip()
        reasoning_list = m.get("reasoning") or []
        tools     = m.get("tools") or []
        steps     = m.get("steps") or []
        fin       = m.get("finish_reason", "")
        created   = ts_ms_to_hms(m.get("created_at"))
        completed = ts_ms_to_hms(m.get("completed_at"))
        tok       = m.get("tokens") or {}
        model_id  = m.get("model_id") or ""

        msg_num += 1

        if role == "user":
            lines.append(f"\n## 👤 User  <sup>{created}</sup>\n")
            if text:
                # user messages are often long prompts; show first 300 chars
                preview = text[:300] + ("..." if len(text) > 300 else "")
                lines.append(f"\n{preview}\n")
            continue

        # ── Assistant message ──────────────────────────────────────────
        input_tok  = tok.get("input", 0) or 0
        output_tok = tok.get("output", 0) or 0
        cache_r    = tok.get("cache_read", 0) or 0
        tok_str    = f"in={input_tok:,} out={output_tok:,} cache_r={cache_r:,}"
        model_str  = f" · `{model_id}`" if model_id else ""

        lines.append(f"\n## 🤖 Assistant  <sup>{created}–{completed}</sup>{model_str}\n")
        lines.append(f"> tokens: {tok_str} · finish: `{fin}`\n")

        # ── Reasoning / thinking ──────────────────────────────────────
        if reasoning_list:
            for r_idx, r in enumerate(reasoning_list):
                r = (r or "").strip()
                if r:
                    lines.append(f"\n### 💭 Thinking\n")
                    lines.append(f"\n{r}\n")

        # ── Visible text output ───────────────────────────────────────
        if text:
            lines.append(f"\n{text}\n")

        # ── Tool calls ───────────────────────────────────────────────
        for t in tools:
            tname    = t.get("name", "?")
            tinput   = t.get("input") or {}
            toutput  = t.get("output")
            terror   = t.get("error")
            tdur     = t.get("duration_ms")
            tstart   = t.get("started_at")
            tend     = t.get("ended_at")
            tstatus  = t.get("status", "")
            dur      = duration_str(tstart, tend, tdur)
            dur_note = f" _{dur}_" if dur else ""
            status_icon = "✓" if tstatus == "completed" else "✗"

            lines.append(f"\n### 🔧 `{tname}`{dur_note}  {status_icon}\n")
            lines.append(f"**Input:**\n\n{fmt_tool_input(tname, tinput)}\n")

            if terror:
                lines.append(f"\n**Error:** `{terror}`\n")
            elif toutput is not None:
                out_str = fmt_output(str(toutput))
                if out_str and out_str != "(no output)":
                    lines.append(f"\n**Output:**\n\n```\n{out_str}\n```\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Written {len(lines)} lines → {out_path}")


def convert_jsonl_fallback(in_path: str, out_path: str):
    """Fallback: plain event JSONL without reasoning."""
    events = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    lines = [f"# Session Log (JSONL fallback)\n",
             f"> Source: `{os.path.basename(in_path)}` · {len(events)} events\n",
             "\n---\n"]
    step_num = 0
    for e in events:
        etype = e.get("type")
        part  = e.get("part", {})
        ts    = ts_ms_to_hms(e.get("timestamp", 0))
        if etype == "step_start":
            step_num += 1
            lines.append(f"\n---\n\n## Step {step_num}  <sup>{ts}</sup>\n")
        elif etype == "text":
            text = (part.get("text") or "").strip()
            if text:
                lines.append(f"\n{text}\n")
        elif etype == "tool_use":
            tool  = part.get("tool", "?")
            state = part.get("state", {})
            inp   = state.get("input", {})
            out   = state.get("output", "")
            tstart = state.get("time", {}).get("start")
            tend   = state.get("time", {}).get("end")
            dur    = f" _{tend-tstart}ms_" if (tstart and tend) else ""
            lines.append(f"\n### 🔧 `{tool}`{dur}\n")
            lines.append(f"**Input:**\n\n{fmt_tool_input(tool, inp)}\n")
            if out:
                lines.append(f"**Output:**\n\n```\n{fmt_output(str(out))}\n```\n")
        elif etype == "step_finish":
            reason = part.get("reason", "")
            tok = part.get("tokens", {})
            lines.append(f"\n> ✓ step done · reason=`{reason}` · tokens={tok.get('total',0)}\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Written {len(lines)} lines → {out_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage: session_to_md.py <session.normalized.json|*_raw.jsonl> [output.md]")
        sys.exit(1)

    in_path = sys.argv[1]
    if len(sys.argv) > 2:
        out_path = sys.argv[2]
    else:
        base = in_path
        for suffix in (".normalized.json", ".export.json", "_raw.jsonl", ".jsonl", ".json"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        out_path = base + "_readable.md"

    with open(in_path, encoding="utf-8") as f:
        first_char = f.read(1)

    f_ext = os.path.splitext(in_path)[1]
    if first_char == "{":
        with open(in_path, encoding="utf-8") as f:
            data = json.load(f)
        convert_normalized(data, out_path)
    else:
        # JSONL
        convert_jsonl_fallback(in_path, out_path)


if __name__ == "__main__":
    main()
