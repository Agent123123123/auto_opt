"""
runtime_config.py
-----------------
加载 runtime_config.yaml，解析每个 agent 的模型配置（model, api_base, api_key,
extra_headers）。

用法:
    from config.runtime_config import load_agent_model_config

    task_cfg   = load_agent_model_config("task_agent")
    judge_cfg  = load_agent_model_config("judge_agent")
    meta_cfg   = load_agent_model_config("meta_agent")

    # judge_cfg: {"model": "openai/claude-sonnet-4.6", "api_base": "...",
    #             "api_key": "<ghu_... OAuth token>", "extra_headers": {...}, ...}
    #
    # GitHub Copilot (api.githubcopilot.com) accepts the long-lived ghu_ OAuth
    # token directly as Bearer — no short-lived token exchange needed.
    # extra_headers (Copilot-Integration-Id, editor-version, etc.) are read from
    # runtime_config.yaml and passed through to litellm / the LLM call.
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

    The returned dict is suitable for passing directly to agent classes:
        {
            "model":         str,          # litellm model string
            "api_base":      str | None,
            "api_key":       str | None,   # resolved from api_key_env
            "extra_headers": dict | None,  # e.g. Copilot-Integration-Id headers
            "max_tokens":    int,
            "temperature":   float,
        }

    For GitHub Copilot agents (api_base contains 'githubcopilot.com'):
      - api_key is the long-lived ghu_ OAuth token from GITHUB_TOKEN — used directly
      - extra_headers carries the required Copilot integration headers from yaml
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
    api_key: str | None = None
    if api_key_env:
        api_key = os.environ.get(api_key_env)

    api_base: str | None = agent_raw.get("api_base") or None  # convert empty/null to None

    extra_headers: dict | None = agent_raw.get("extra_headers") or None

    return {
        "model":         agent_raw["model"],
        "api_base":      api_base,
        "api_key":       api_key,
        "extra_headers": extra_headers,
        "max_tokens":    int(agent_raw.get("max_tokens", 8192)),
        "temperature":   float(agent_raw.get("temperature", 0.0)),
        # Keep the raw env var name for debugging / error messages
        "_api_key_env":  api_key_env,
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
        result[agent_name] = bool(env_var and os.environ.get(env_var))
    return result


if __name__ == "__main__":
    import json

    print("=== Agent Model Configs ===")
    for name in ("task_agent", "judge_agent", "meta_agent"):
        try:
            mcfg = load_agent_model_config(name)
            safe = {k: v for k, v in mcfg.items() if k != "api_key"}
            safe["api_key"] = "***" if mcfg.get("api_key") else "(not set)"
            print(f"\n[{name}]")
            print(json.dumps(safe, indent=2))
        except Exception as e:
            print(f"[{name}] ERROR: {e}")

    print("\n=== API Key Status ===")
    for agent, ok in check_agent_api_keys().items():
        status = "✓ set" if ok else "✗ MISSING"
        print(f"  {agent}: {status}")
