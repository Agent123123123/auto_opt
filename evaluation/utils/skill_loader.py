"""
utils/skill_loader.py
---------------------
将 skill bundle（mem_gen/skill/*.md）中所有文档合并为一个字符串，
供 Task Agent 和 Meta Agent 在 prompt 中直接使用。

合并规则：
  - 按文件名字母序排列
  - 每个文档前后加 <skill_file name="xxx.md"> ... </skill_file> 标签，
    便于 LLM 识别文档边界
"""

import os
import glob


def load_skill_bundle(skill_dir: str) -> str:
    """
    读取 skill_dir 中所有 .md 文件，合并为带标签的字符串。

    返回格式：
    <skill_bundle>
    <skill_file name="01_overview.md">
    ...内容...
    </skill_file>
    <skill_file name="02_workflow.md">
    ...
    </skill_file>
    </skill_bundle>
    """
    md_files = sorted(glob.glob(os.path.join(skill_dir, "**/*.md"), recursive=True))

    if not md_files:
        return f"<skill_bundle>\n<!-- no .md files found in {skill_dir} -->\n</skill_bundle>"

    parts = ["<skill_bundle>"]
    for fpath in md_files:
        rel_name = os.path.relpath(fpath, skill_dir)
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            content = f"[ERROR reading file: {e}]"

        parts.append(f'<skill_file name="{rel_name}">')
        parts.append(content.rstrip())
        parts.append("</skill_file>")

    parts.append("</skill_bundle>")
    return "\n".join(parts)


def skill_file_list(skill_dir: str) -> list[str]:
    """返回 skill_dir 中所有 .md 文件的绝对路径列表（字母序）。"""
    return sorted(glob.glob(os.path.join(skill_dir, "**/*.md"), recursive=True))
