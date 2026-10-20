"""env_config 单元测试：全部使用临时合成 .env，绝不读取真实的仓库根 .env。"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from planner.env_config import load_env_file, llm_env, resolve_llm


def _write_env(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return tmp_path


def test_load_env_file_parses_keys_ignores_comments(tmp_path):
    root = _write_env(tmp_path, "# comment\n\nOPENAI_MODEL=m-x\nQUOTED=\"v v\"\nBAD line\n")
    values = load_env_file(root / ".env")
    assert values["OPENAI_MODEL"] == "m-x"
    assert values["QUOTED"] == "v v"
    assert values == {"OPENAI_MODEL": "m-x", "QUOTED": "v v"}


def test_llm_env_missing_file_returns_empty(tmp_path):
    env = llm_env(tmp_path / "nonexistent-dir")
    assert env == {"base_url": None, "model": None, "api_key": None,
                   "requests_per_minute": None}


def test_llm_env_reads_all_keys(tmp_path):
    root = _write_env(tmp_path,
                      "OPENAI_BASE_URL=https://example.test/v1\n"
                      "OPENAI_MODEL=test-model\n"
                      "OPENAI_API_KEY=sk-fake-for-tests\n"
                      "LLM_REQUESTS_PER_MINUTE=20\n")
    env = llm_env(root)
    assert env["base_url"] == "https://example.test/v1"
    assert env["model"] == "test-model"
    assert env["api_key"] == "sk-fake-for-tests"
    assert env["requests_per_minute"] == 20


def test_llm_env_bad_rpm_falls_back_to_unlimited(tmp_path):
    root = _write_env(tmp_path, "LLM_REQUESTS_PER_MINUTE=not-a-number\n")
    assert llm_env(root)["requests_per_minute"] is None


def test_resolve_llm_user_values_override_env(tmp_path):
    root = _write_env(tmp_path,
                      "OPENAI_BASE_URL=https://env.test/v1\nOPENAI_MODEL=env-model\n"
                      "OPENAI_API_KEY=sk-env\n")
    config, public = resolve_llm({"base_url": "https://user.test/v1",
                                  "model": "user-model", "api_key": "sk-user"}, root=root)
    assert config["base_url"] == "https://user.test/v1"
    assert config["model"] == "user-model"
    assert config["api_key"] == "sk-user"
    assert config["min_interval"] == 3.0
    assert public["complete"] is True
    assert public["api_key_from_request"] is True


def test_resolve_llm_fills_missing_from_env(tmp_path):
    root = _write_env(tmp_path,
                      "OPENAI_BASE_URL=https://env.test/v1\nOPENAI_MODEL=env-model\n"
                      "OPENAI_API_KEY=sk-env\nLLM_REQUESTS_PER_MINUTE=30\n")
    config, public = resolve_llm({"base_url": "", "model": "", "api_key": ""}, root=root)
    assert config["base_url"] == "https://env.test/v1"
    assert config["model"] == "env-model"
    assert config["api_key"] == "sk-env"
    assert config["min_interval"] == 2.0
    assert public["api_key_from_env"] is True
    assert public["api_key_from_request"] is False


def test_resolve_llm_incomplete_returns_none_but_public_reports(tmp_path):
    root = _write_env(tmp_path, "OPENAI_MODEL=env-model\n")
    config, public = resolve_llm(None, root=root)
    assert config is None
    assert public["complete"] is False
    assert public["base_url"] is None
    assert public["model"] == "env-model"


def test_public_info_never_contains_api_key_value(tmp_path):
    root = _write_env(tmp_path, "OPENAI_API_KEY=sk-secret-value\n")
    _, public = resolve_llm({"base_url": "u", "model": "m", "api_key": "sk-user"}, root=root)
    assert "api_key" not in public
    assert "sk-secret-value" not in json.dumps(public)


def _load_server_module():
    spec = importlib.util.spec_from_file_location(
        "demo_server", ROOT / "server" / "demo_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_state_always_includes_llm_env_without_key_value():
    module = _load_server_module()
    info = module._state()
    assert "llm_env" in info
    assert "api_key" not in info["llm_env"]
    assert set(info["llm_env"]) == {"base_url", "model", "api_key_from_env",
                                    "api_key_from_request", "requests_per_minute",
                                    "complete"}
