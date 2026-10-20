import threading
import time

import pytest

from planner.llm import (LLMError, LLMClient, _extract_json, bump_stats,
                      parallel_map)


class ScriptedLLM(LLMClient):
    def __init__(self, script):
        super().__init__("http://localhost:0", "test-model", "test-key", min_interval=0)
        self.script = list(script)

    def _chat(self, system, user, json_mode):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_chat_json_parses_plain_json():
    client = ScriptedLLM(['{"a": 1}'])
    assert client.chat_json("s", "u") == {"a": 1}


def test_chat_json_parses_fenced_json():
    client = ScriptedLLM(['前言```json\n{"a": 2}\n```后记'])
    assert client.chat_json("s", "u") == {"a": 2}


def test_chat_json_retries_without_json_mode():
    client = ScriptedLLM([LLMError("端点拒绝 json_object 模式（HTTP 400）"), '{"a": 3}'])
    assert client.chat_json("s", "u") == {"a": 3}


def test_chat_json_gives_up_on_garbage():
    client = ScriptedLLM(["不是 json", "还不是 json"])
    with pytest.raises(LLMError):
        client.chat_json("s", "u")
    assert client.failures >= 1


def test_extract_json_braced_substring():
    assert _extract_json('结果 {"satisfaction": 88} 完') == {"satisfaction": 88}
    assert _extract_json("") is None


def test_parallel_map_preserves_input_order():
    assert parallel_map([3, 1, 2], lambda x: x * x, 3) == [9, 1, 4]


def test_parallel_map_one_worker_is_sequential():
    seen = []
    assert parallel_map([1, 2, 3], lambda x: seen.append(x), 1) == [None, None, None]
    assert seen == [1, 2, 3]


def test_parallel_map_runs_concurrently():
    started = time.monotonic()
    parallel_map(range(4), lambda _: time.sleep(0.2), 4)
    assert time.monotonic() - started < 0.6      # 顺序跑要 0.8 s 以上


def test_parallel_map_propagates_llm_failure():
    def work(item):
        if item == 2:
            raise LLMError("端点挂了")
        return item
    with pytest.raises(LLMError):
        parallel_map([1, 2, 3], work, 3)


def test_throttle_spaces_call_starts_across_threads():
    client = LLMClient("http://localhost:0", "m", "k", min_interval=0.02)
    lock = threading.Lock()
    stamps = []

    def reserve(_):
        client._throttle()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(stamps) == 5
    assert max(stamps) - min(stamps) >= 0.02 * 4 - 0.005   # 全局最小间隔，不是每线程一份


def test_bump_stats_loses_no_increment():
    stats = {}
    parallel_map(range(200), lambda _: bump_stats(stats, "sanitized"), 8)
    assert stats["sanitized"] == 200
