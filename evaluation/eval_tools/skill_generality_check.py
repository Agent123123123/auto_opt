"""eval_tools/skill_generality_check.py
-------------------------------------
收集 skill workspace 内的 .md 文档内容，供 Judge Agent LLM 直接评估。

职责边界：
  - 本模块只做文件收集（纯 Python，确定性操作）。
  - 供应商中立性 / 类型枚举的语义判断由 Judge Agent 的 LLM 完成。
  - 不在这里调用 LLM：Judge Agent 已经是 LLM，不需要"LLM 调用 LLM"的嵌套。

豁免规则：
  - reference/, examples/, behavior_models/ 等目录内的文件豁免，
    因为这些目录存放具体平台的示例 / golden 文件，属于"参考资料"。
"""

from pathlib import Path

# ── 豁免目录名 ────────────────────────────────────────────────────────────
_EXEMPT_DIRS = frozenset({
    "reference", "examples", "example",
    "behavior_models", "bmodel", "models",
    "golden", "ref",
})


def _is_in_exempt_dir(path: Path, workspace: Path) -> bool:
    """判断文件是否在豁免目录下（只检查相对路径各级目录名）。"""
    try:
        rel = path.relative_to(workspace)
    except ValueError:
        return False
    parts_lower = {p.lower() for p in rel.parts[:-1]}  # 去掉文件名本身
    return bool(parts_lower & _EXEMPT_DIRS)


def read_skill_docs(
    workspace_dir: str,
    skill_dirs: list[str] | None = None,
    max_chars_per_file: int = 12000,
) -> dict:
    """
    收集 workspace_dir 内的 .md 文件内容，返回给 Judge Agent prompt 用于语义评估。

    Args:
        workspace_dir      : Task Agent 工作目录（或已生成 skill 的目录）。
        skill_dirs         : 若指定，只收集这些子目录内的 .md 文件；
                             若为 None，收集全部 .md 文件（豁免目录除外）。
        max_chars_per_file : 单文件截断长度（避免 prompt 超长）。

    Returns:
        {
          "files":          dict[rel_path, str],  # 路径 → 文件内容
          "total_md_files": int,
        }
    """
    workspace = Path(workspace_dir)

    if skill_dirs:
        md_files: list[Path] = []
        for d in skill_dirs:
            target = workspace / d
            if target.exists():
                md_files.extend(target.rglob("*.md"))
    else:
        md_files = list(workspace.rglob("*.md"))

    md_files = [f for f in md_files if not _is_in_exempt_dir(f, workspace)]

    files: dict[str, str] = {}
    for md_file in sorted(md_files):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content.strip()) < 10:
            continue
        rel_path = str(md_file.relative_to(workspace))
        files[rel_path] = content[:max_chars_per_file]

    return {
        "files":          files,
        "total_md_files": len(files),
    }
