"""
Phase 1a Task Agent
-------------------
职责：加载当前 mem_gen skill 文档，执行 MemGen 任务，产出所有 artifacts。

接收信息：
  - mem_gen skill 文档全文（SKILL.md + 所有 0x_*.md）
  - memory_type   (e.g. "spsram_ulvt")
  - mc_path       (MC 编译器路径，若为 None 则由 Agent 自行发现)
  - workspace_dir (本代运行的独立工作目录)

产出：
  - workspace_dir/artifacts/<memory_type>/<combo>/
      ├── *.v / *.sv   (RTL wrapper)
      ├── *.lef
      ├── *.lib
      ├── *.gds
      └── DATASHEET_*.txt
  - workspace_dir/run.log  (完整执行日志)

不接收：
  - 任何历史评估结果（Task Agent 无需知道进化历史，避免 anchoring bias）
  - Judge Agent 的评分细节（防止 task agent 直接针对评分 hack）
"""

import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

from phase1a.opencode_runner import run as opencode_run
from phase1a.config import get_language_directive

# ──────────────────────────────────────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert hardware infrastructure engineer. Your job is to build a \
production-quality Memory Compiler CLI wrapper from scratch, following the \
methodology in the provided skill documents.

Read the skill documents carefully before starting. They contain the \
complete methodology, pitfalls, and best practices you need.
"""

TASK_PROMPT_TEMPLATE = """\
## Your Task

Build a **Memory Compiler CLI tool** (`memgen`) following the methodology in
the skill documents. Read them first, then implement and test the tool.

## Environment

- **Workspace directory**: `{workspace_dir}`  ← all output goes here
- **Skill documents**: `{skill_dir}`
- **Memory Compiler**:
  - Load with: `module load mc2_n12/2013.12`
  - Family packages base: `/data/foundry/TSMC12/Memory_compiler/`

## Required Output Layout (MANDATORY — evaluator checks ONLY this path)

All MC artifacts MUST be placed at:

    {workspace_dir}/artifacts/<memory_family>/<combo_name>/

Examples:
    {workspace_dir}/artifacts/spsram/ts1n12ffcllulvta64x16m4sw_130b/
    {workspace_dir}/artifacts/1prf/ts5n12ffcllulvta16x32m1sw_130c/

Rules:
- The top-level directory inside `{workspace_dir}` for artifacts is `artifacts/`
- Directly under `artifacts/` is the memory family name (e.g. `spsram`, `1prf`)
- Under each family is the combo/model name
- Do NOT use `test_output/`, `output/`, `results/`, or any other intermediate
  directory. The evaluator ONLY scans `{workspace_dir}/artifacts/` — outputs
  placed elsewhere are INVISIBLE to scoring.

## Completion Criterion

Write `{workspace_dir}/DONE.txt` listing every produced artifact path (one per
line). Do NOT write DONE.txt if no real MC artifacts were produced.

Begin by reading the skill docs, then build and run the tool.
"""


class TaskAgent:
    """mem_gen Task Agent for Phase 1a."""

    def __init__(self, model_config: dict, chat_history_file: str = "./outputs/task_chat.md"):
        self.model_config = model_config
        self.chat_history_file = chat_history_file

    def forward(self, inputs: dict) -> tuple[str, list]:
        """
        inputs keys:
            workspace_dir      : str
            skill_dir          : str
            chat_history_file  : str (optional)
            mc_path            : str | None  (unused — MC paths are hardcoded in prompt)
        """
        task_prompt = TASK_PROMPT_TEMPLATE.format(
            workspace_dir = inputs["workspace_dir"],
            skill_dir     = inputs["skill_dir"],
        )

        # opencode runs the full agentic tool loop (bash) internally.
        # Combine system + user prompts as one message.
        lang_directive = get_language_directive()
        full_prompt = (lang_directive + "\n\n" + SYSTEM_PROMPT + "\n\n" + task_prompt).lstrip()

        chat_history_file = inputs.get("chat_history_file", self.chat_history_file)

        final_response = opencode_run(
            prompt            = full_prompt,
            cwd               = inputs["workspace_dir"],
            model             = self.model_config["opencode_model"],
            chat_history_file = chat_history_file,
            timeout           = 7200,   # 120 min
            agent             = "no-skill",
        )
        return final_response, []
