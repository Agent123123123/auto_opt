"""
Phase 1a Task Agent
-------------------
职责：加载当前 mem_gen skill 文档，执行 MemGen 任务，产出所有 artifacts。

接收信息：
  - mem_gen skill 文档全文（SKILL.md + 所有 0x_*.md）
  - memory_type   (e.g. "spsram_ulvt")
  - mc_path       (MC 编译器路径或 mock 入口)
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

from agent.llm_withtools import run_agent_to_completion
from agent.tools import load_tools
from utils.skill_loader import load_skill_bundle

# ──────────────────────────────────────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert hardware infrastructure engineer specializing in SRAM memory \
compiler integration. Your job is to follow the provided skill document exactly \
and produce all required artifacts for a given memory type.

CRITICAL RULES:
1. Follow the skill document step-by-step. Do not skip steps.
2. If you encounter an error, diagnose it using the bash tool and retry.
3. All file output must go to the workspace directory provided.
4. When done, write a file called DONE.txt in the workspace root listing all \
   produced artifact paths, one per line.
5. Do NOT invent parameter values not covered by the skill document.
"""

TASK_PROMPT_TEMPLATE = """\
## Your Task

Build the memory compiler wrapper for the following target:

- **Memory type**: {memory_type}
- **MC compiler path**: {mc_path}
- **Workspace directory**: {workspace_dir}
- **Parameter combinations to cover**: {param_combos}

## Skill Document

The following is your complete instruction set. Read it carefully before acting.

---
{skill_content}
---

## Expected Artifacts

For each parameter combination in the list above, produce:
  - RTL wrapper (.sv or .v)
  - DATASHEET file (DATASHEET_<combo>.txt)
  - Compilation kit (LEF, LIB, GDS) if the MC compiler supports it

Place all outputs under: `{workspace_dir}/artifacts/{memory_type}/<combo>/`

Write `{workspace_dir}/DONE.txt` when complete, listing all produced files.

Begin now.
"""


class TaskAgent:
    """mem_gen Task Agent for Phase 1a."""

    def __init__(self, model_config: dict, chat_history_file: str = "./outputs/task_chat.md"):
        self.model_config = model_config
        self.tools = load_tools(["bash"])  # Task Agent 只有 bash 工具
        self.chat_history_file = chat_history_file

    def forward(self, inputs: dict) -> tuple[str, list]:
        """
        inputs keys:
            memory_type   : str
            mc_path       : str
            workspace_dir : str
            param_combos  : list[str]   e.g. ["NW4_NB32_NMUX4", "NW8_NB64_NMUX4", ...]
            skill_dir     : str         path to mem_gen/skill/
        """
        skill_content = load_skill_bundle(inputs["skill_dir"])

        task_prompt = TASK_PROMPT_TEMPLATE.format(
            memory_type   = inputs["memory_type"],
            mc_path       = inputs["mc_path"],
            workspace_dir = inputs["workspace_dir"],
            param_combos  = "\n".join(f"  - {c}" for c in inputs["param_combos"]),
            skill_content = skill_content,
        )

        messages = [{"role": "user", "content": task_prompt}]

        final_response, history = run_agent_to_completion(
            model             = self.model_config["model"],
            system_prompt     = SYSTEM_PROMPT,
            messages          = messages,
            tools             = self.tools,
            max_steps         = 40,
            chat_history_file = self.chat_history_file,
            api_base          = self.model_config.get("api_base"),
            api_key           = self.model_config.get("api_key"),
        )
        return final_response, history
