"""redo/.env 加载：为演示的 AI 通路提供默认 LLM 配置。

值只在内存中使用；API Key 只做透传与“是否已提供”布尔判断，绝不打印、写日志或放进任何 API 响应。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
LLM_KEYS = ("base_url", "model", "api_key")


def load_env_file(path: str | os.PathLike) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def llm_env(root: str | os.PathLike | None = None) -> dict:
    values = load_env_file(Path(root) / ".env" if root else ENV_PATH)
    try:
        rpm = int(values.get("LLM_REQUESTS_PER_MINUTE", "0"))
    except ValueError:
        rpm = 0
    return {
        "base_url": values.get("OPENAI_BASE_URL", "").strip() or None,
        "model": values.get("OPENAI_MODEL", "").strip() or None,
        "api_key": values.get("OPENAI_API_KEY", "").strip() or None,
        "requests_per_minute": rpm if rpm > 0 else None,
    }


def resolve_llm(user_llm: dict | None,
                root: str | os.PathLike | None = None) -> tuple[dict | None, dict]:
    """合并页面/CLI 传入值与 .env 默认值（传入值优先）。

    返回 (resolved_config, public_info)：config 供 run_pipeline/LLMClient 使用，
    public_info 供页面展示——只含 base_url/model 与布尔量，永不含 API Key 值。
    """
    env = llm_env(root)
    user = user_llm or {}
    config: dict = {}
    for key in LLM_KEYS:
        value = str(user.get(key) or "").strip()
        config[key] = value or env.get(key)
    rpm = env.get("requests_per_minute")
    config["min_interval"] = round(60.0 / rpm, 2) if rpm else 3.0
    complete = bool(config["base_url"] and config["model"] and config["api_key"])
    public = {
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key_from_env": bool(env.get("api_key")),
        "api_key_from_request": bool(str(user.get("api_key") or "").strip()),
        "requests_per_minute": rpm,
        "complete": complete,
    }
    return (config if complete else None), public
