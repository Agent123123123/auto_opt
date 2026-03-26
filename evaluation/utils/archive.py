"""
utils/archive.py
----------------
管理 Phase 1a 的进化档案（Archive）。

Archive 存储每一代的：
  - skill bundle 快照（目录拷贝）
  - 评分向量（per metric）
  - 综合分数
  - 父代 ID

选择逻辑遵循 DGM-H 的 MAP-Elites 风格：
  - 按综合分数排序，返回前 N 代中的一个（加随机扰动防止收敛过早）
"""

import os
import json
import random
import shutil
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class ArchiveEntry:
    generation:  int
    score:       float
    metrics:     dict        # {"artifact_completeness": 0.9, "wrapper_interface_match": 0.8, ...}
    parent_id:   Optional[int]
    skill_path:  str         # 快照目录路径
    judge_report: dict = field(default_factory=dict)


class Archive:
    def __init__(self, archive_dir: str, skill_dir: str, metric_names: list[str]):
        self.archive_dir  = archive_dir
        self.skill_dir    = skill_dir
        self.metric_names = metric_names
        self.entries:  list[ArchiveEntry] = []

        os.makedirs(archive_dir, exist_ok=True)
        self._load_existing()

    # ─── 写入 ────────────────────────────────────────────────────────────────

    def add(
        self,
        generation:   int,
        score:        float,
        metrics:      dict,
        judge_report: dict,
        parent_id:    Optional[int] = None,
    ) -> ArchiveEntry:
        """保存当前 skill 快照并记录本代结果。"""
        snap_dir = os.path.join(self.archive_dir, f"gen_{generation:04d}")
        shutil.copytree(self.skill_dir, snap_dir, dirs_exist_ok=True)

        entry = ArchiveEntry(
            generation   = generation,
            score        = score,
            metrics      = metrics,
            parent_id    = parent_id,
            skill_path   = snap_dir,
            judge_report = judge_report,
        )
        self.entries.append(entry)
        self._save_index()
        return entry

    # ─── 读取 ────────────────────────────────────────────────────────────────

    def best(self) -> Optional[ArchiveEntry]:
        if not self.entries:
            return None
        return max(self.entries, key=lambda e: e.score)

    def best_score(self) -> float:
        b = self.best()
        return b.score if b else 0.0

    def get_best(self) -> Optional[dict]:
        """返回最佳代的信息字典，供 loop.py 直接消费。"""
        b = self.best()
        if not b:
            return None
        return {
            "score":      b.score,
            "skill_dir":  b.skill_path,
            "generation": b.generation,
            "metrics":    b.metrics,
        }

    def add_entry(
        self,
        skill_dir:  str,
        score:      float,
        reports:    list,
        generation: int,
    ) -> ArchiveEntry:
        """
        将本代 working_skill_dir 快照到 archive，记录评分。

        skill_dir : 本代的 working_skill_dir（已应用 patch），从这里拍快照。
        reports   : judge_agent 报告列表（quick + full）。
        """
        snap_dir = os.path.join(self.archive_dir, f"gen_{generation:04d}")
        shutil.copytree(skill_dir, snap_dir, dirs_exist_ok=True)

        # 从 reports 提取 layer1 metric 均值
        metrics: dict = {}
        layer1_keys = [
            "artifact_completeness", "wrapper_interface_match",
            "param_toggle_correctness", "split_correctness",
            "tiein_correctness", "mem_type_full_coverage",
            "interface_spec_consistency", "lint_clean_rate",
        ]
        for key in layer1_keys:
            vals = [
                r["breakdown"]["layer1"][key]
                for r in reports
                if r.get("breakdown", {}).get("layer1", {}).get(key) is not None
            ]
            metrics[key] = sum(vals) / len(vals) if vals else 0.0

        entry = ArchiveEntry(
            generation   = generation,
            score        = score,
            metrics      = metrics,
            parent_id    = None,
            skill_path   = snap_dir,
            judge_report = {"reports": reports},
        )
        self.entries.append(entry)
        self._save_index()
        return entry

    def select_parent(self, top_k: int = 5, strategy: str = "score_prop") -> Optional[str]:
        """
        从 archive 中随机选取一个"父代"，返回其 skill_path (str)。
        为空时返回 None（调用方应 fallback 到原始 skill_dir）。
        strategy 参数保留接口兼容，当前实现均为 top_k random。
        """
        if not self.entries:
            return None
        pool = sorted(self.entries, key=lambda e: e.score, reverse=True)[:top_k]
        return random.choice(pool).skill_path

    def last_n_reports(self, n: int) -> list[dict]:
        """返回最近 n 代的 judge_report 列表（按代数升序）。"""
        return [e.judge_report for e in self.entries[-n:]]

    def restore_skill(self, entry: ArchiveEntry):
        """将 skill_dir 恢复为某代的快照。"""
        if os.path.exists(self.skill_dir):
            shutil.rmtree(self.skill_dir)
        shutil.copytree(entry.skill_path, self.skill_dir)

    # ─── 持久化 ──────────────────────────────────────────────────────────────

    def _save_index(self):
        index_path = os.path.join(self.archive_dir, "index.json")
        with open(index_path, "w") as f:
            json.dump([asdict(e) for e in self.entries], f, indent=2)

    def _load_existing(self):
        index_path = os.path.join(self.archive_dir, "index.json")
        if not os.path.exists(index_path):
            return
        with open(index_path) as f:
            data = json.load(f)
        for d in data:
            self.entries.append(ArchiveEntry(**d))
