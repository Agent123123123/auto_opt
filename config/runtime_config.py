"""
runtime_config.py
-----------------
加载 runtime_config.yaml，解析每个 agent 的模型配置。

用法:
    from config.runtime_config import load_agent_model_config

    task_cfg   = load_agent_model_config("task_agent")
    judge_cfg  = load_agent_model_config("judge_agent")
    meta_cfg   = load_agent_model_config("meta_agent")

    # cfg: {"opencode_model": "bailian-coding-plan/glm-5", ...}
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise ImportError("PyYAML is required: pip install pyyaml") from exc


_DEFAULT_CONFIG_PATH = Path(__file__).parent / "runtime_config.yaml"


def _expand_env_vars(value: str) -> str:
    """Expand ${VAR} or $VAR patterns in a string."""
    return re.sub(
        r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
        lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)),
        value,
    )


def load_runtime_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load and return the raw runtime config dict (with env var expansion on string values)."""
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"runtime_config not found: {path}")

    with open(path, encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(i) for i in obj]
        if isinstance(obj, str):
            return _expand_env_vars(obj)
        return obj

    return _walk(raw)


def load_agent_model_config(
    agent_name: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Return the model configuration dict for a specific agent.

    The returned dict:
        {
            "opencode_model": str,   # e.g. "copilot-direct/claude-sonnet-4.6"
            "_api_key_env":   str,   # env var name, for error messages
        }
    """
    cfg = load_runtime_config(config_path)
    agents_section: dict = cfg.get("agents", {})

    if agent_name not in agents_section:
        raise KeyError(
            f"Agent '{agent_name}' not found in runtime_config.yaml. "
            f"Available agents: {list(agents_section.keys())}"
        )

    agent_raw: dict = agents_section[agent_name]
    api_key_env: str | None = agent_raw.get("api_key_env")

    return {
        "opencode_model": agent_raw["opencode_model"],
        "_api_key_env":   api_key_env,
    }


def check_agent_api_keys(config_path: str | Path | None = None) -> dict[str, bool]:
    """
    Check that all agents have their API keys set in the environment.
    Returns: {agent_name: bool}  (True = key is present)
    """
    cfg = load_runtime_config(config_path)
    result: dict[str, bool] = {}
    for agent_name, agent_raw in cfg.get("agents", {}).items():
        env_var: str | None = agent_raw.get("api_key_env")
        if env_var is None:
            # No API key required (auth managed by opencode itself)
            result[agent_name] = True
        else:
            result[agent_name] = bool(os.environ.get(env_var))
    return result


if __name__ == "__main__":
    import json

    print("=== Agent Model Configs ===")
    for name in ("task_agent", "judge_agent", "meta_agent"):
        try:
            mcfg = load_agent_model_config(name)
            print(f"\n[{name}]")
            print(json.dumps(mcfg, indent=2))
        except Exception as e:
            print(f"[{name}] ERROR: {e}")

    print("\n=== API Key Status ===")
    for agent, ok in check_agent_api_keys().items():
        status = "✓ set" if ok else "✗ MISSING"
        print(f"  {agent}: {status}")
