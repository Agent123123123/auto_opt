"""
utils/skill_patcher.py
-----------------------
安全地将 Meta Agent 输出的 diff patch 应用到 skill bundle，
并验证 patch 不会修改 skill_dir 以外的文件。
"""

import os
import subprocess
import tempfile


# 合法的修改目标前缀（只有这里的文件可以被修改）
ALLOWED_PREFIX = "mem_gen/skill/"


def validate_patch_safety(patch_text: str, skill_dir: str) -> tuple[bool, str]:
    """
    检查 patch 是否只修改 skill_dir 内的文件。

    返回 (is_safe: bool, reason: str)。
    如果 is_safe=False，reason 指出第一个违规文件。
    """
    for line in patch_text.splitlines():
        # unified diff 头：--- a/path 或 +++ b/path
        if line.startswith("--- ") or line.startswith("+++ "):
            # 跳过 /dev/null
            if "/dev/null" in line:
                continue
            # 提取路径（去掉 a/ 或 b/ 前缀）
            raw_path = line[4:].strip()
            for prefix in ("a/", "b/"):
                if raw_path.startswith(prefix):
                    raw_path = raw_path[2:]

            # 规范化为相对路径（去掉绝对路径前缀）
            abs_skill_dir = os.path.abspath(skill_dir)
            if os.path.isabs(raw_path):
                rel = os.path.relpath(raw_path, abs_skill_dir)
            else:
                rel = raw_path

            # 检查是否在允许目录内
            norm_rel = os.path.normpath(rel)
            if norm_rel.startswith(".."):
                return False, f"Patch targets file outside skill_dir: {raw_path}"

    return True, "ok"


def apply_patch(patch_text: str, skill_dir: str) -> tuple[bool, str]:
    """
    将 unified diff 格式的 patch 应用到 skill_dir。

    返回 (success: bool, message: str)。
    """
    is_safe, reason = validate_patch_safety(patch_text, skill_dir)
    if not is_safe:
        return False, f"SAFETY VIOLATION: {reason}"

    # 写入临时 patch 文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(patch_text)
        patch_file = f.name

    try:
        result = subprocess.run(
            ["patch", "-p1", "--directory", skill_dir, "--input", patch_file],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, f"patch failed:\n{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "patch command timed out"
    finally:
        os.unlink(patch_file)


def backup_skill_dir(skill_dir: str, backup_root: str, generation: int) -> str:
    """
    将当前 skill_dir 快照到 backup_root/gen_{generation:04d}/，
    用于 archive 在 patch 出错时回滚。
    """
    import shutil
    backup_path = os.path.join(backup_root, f"gen_{generation:04d}")
    shutil.copytree(skill_dir, backup_path, dirs_exist_ok=True)
    return backup_path
