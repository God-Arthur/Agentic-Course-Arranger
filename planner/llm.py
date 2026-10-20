"""OpenAI-compatible Chat Completions 客户端（仅标准库；密钥只在内存中使用，绝不落盘、绝不打印）。

长跑实验需要并发：单次调用约 1.6-3 s 是网络+推理延迟，串行时线程数直接等于耗时倍数。
限流用「最近 60 秒内发起的请求数不超过 max_per_minute」滑动窗口 + 全局最小间隔实现，
所有状态由一把锁保护，因此可以在 ThreadPoolExecutor 里安全共享同一个客户端。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor


class LLMError(RuntimeError):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: float = 300.0, min_interval: float = 3.0, max_retries: int = 3,
                 max_per_minute: float = 0.0):
        self.base_url = base_url.rstrip("/")
        # 有些端点（含校内网关）在 .env 里直接写成 .../v1/chat/completions，
        # 再拼一次会 404；已经指到 chat/completions 的就原样用。
        self.endpoint = self.base_url if self.base_url.endswith("/chat/completions") \
            else f"{self.base_url}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        # 端点限额常见为 20 req/min：默认两次调用之间至少隔 3 秒。
        # 并发时这个间隔是全局的（窗口内计数），不是每线程一份。
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_per_minute = max_per_minute
        self.calls = 0
        self.failures = 0
        self._lock = threading.Lock()
        self._starts: deque = deque()
        self._next_allowed = 0.0
        # 长跑前要用实测值决定人数，所以记录纯网络延迟与整体发起节奏（不含排队等待）。
        self._first_start: float | None = None
        self._last_start: float = 0.0
        self._latency_total = 0.0
        self._latency_count = 0
        self._latency_max = 0.0
        self.backoffs = 0

    def perf(self) -> dict:
        """实测：平均/最大单次网络延迟、发起跨度、跨度内的等效 RPM、退避次数。"""
        with self._lock:
            span = (self._last_start - self._first_start) if self._first_start else 0.0
            return {
                "avg_latency_s": round(self._latency_total / self._latency_count, 2)
                if self._latency_count else 0.0,
                "max_latency_s": round(self._latency_max, 2),
                "call_span_s": round(span, 1),
                "effective_rpm": round(self.calls * 60.0 / span, 1) if span > 1 else None,
                "backoffs": self.backoffs,
            }

    def _record_latency(self, seconds: float) -> None:
        with self._lock:
            self._latency_total += seconds
            self._latency_count += 1
            self._latency_max = max(self._latency_max, seconds)

    def _throttle(self) -> None:
        """在全局锁里预约一个发起时刻：同时受最小间隔和每分钟请求数窗口约束。"""
        while True:
            with self._lock:
                now = time.monotonic()
                if self.max_per_minute > 0:
                    while self._starts and now - self._starts[0] >= 60.0:
                        self._starts.popleft()
                    if len(self._starts) >= int(self.max_per_minute):
                        wait = 60.0 - (now - self._starts[0]) + 0.01
                    else:
                        wait = max(0.0, self._next_allowed - now)
                else:
                    wait = max(0.0, self._next_allowed - now)
                if wait <= 0:
                    self._next_allowed = now + self.min_interval
                    self._starts.append(now)
                    if self._first_start is None:
                        self._first_start = now
                    self._last_start = now
                    return
            time.sleep(min(wait, 5.0))

    def chat_json(self, system: str, user: str) -> dict:
        try:
            content = self._chat(system, user, json_mode=True)
            parsed = _extract_json(content)
            if parsed is not None:
                return parsed
            self._count_failure()
        except LLMError:
            pass  # 例如端点不支持 json_object：退回普通模式重试
        content_plain = self._chat(system, user, json_mode=False)
        parsed = _extract_json(content_plain)
        if parsed is None:
            self._count_failure()
            raise LLMError("LLM 返回内容无法解析为 JSON")
        return parsed

    def _count_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def _chat(self, system: str, user: str, json_mode: bool) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode("utf-8")
        last_error = ""
        for attempt in range(self.max_retries):
            self._throttle()
            request = urllib.request.Request(
                self.endpoint, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
                method="POST")
            with self._lock:
                self.calls += 1
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self._record_latency(time.monotonic() - started)
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code == 400 and json_mode:
                    raise LLMError(f"端点拒绝 json_object 模式（{last_error}）") from exc
                if exc.code in (429, 500, 502, 503):
                    # 撞限额就退避，并把全局窗口整体推后，避免其它线程继续猛打。
                    delay = min(30.0, 5.0 * (attempt + 1))
                    with self._lock:
                        self.backoffs += 1
                        self._next_allowed = max(self._next_allowed,
                                                 time.monotonic() + delay)
                        if exc.code == 429 and self.max_per_minute > 1:
                            self.max_per_minute = max(1.0, self.max_per_minute * 0.7)
                    time.sleep(delay)
                    continue
                raise LLMError(f"LLM 请求失败：{last_error}") from exc
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                continue
        self._count_failure()
        raise LLMError(f"LLM 请求在重试后仍失败：{last_error}")


def parallel_map(items, work, workers: int):
    """并发执行 work(item) 并保持输入顺序；任一任务抛错就原样上抛（LLM 失败是致命的，不兜底）。

    workers<=1 时退化为顺序执行，演示页面和小规模测试的行为与并发改造前完全一致。
    """
    entries = list(items)
    if workers <= 1 or len(entries) <= 1:
        return [work(item) for item in entries]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, entries))


_stats_lock = threading.Lock()


def bump_stats(stats: dict, key: str, amount: int = 1) -> None:
    """并发跑时 llm_stats 会被多个线程同时读写，计数统一走这把锁。"""
    with _stats_lock:
        stats[key] = stats.get(key, 0) + amount


def count_drop(stats: dict, where: str) -> None:
    """记一次事实校验剔除：总数进 sanitized，来源单独进 sanitized_by_<where>。

    只报总数看不出剔除是被并发撞车、类别配额还是非法节次触发的，长跑报告需要拆开。
    """
    bump_stats(stats, "sanitized")
    bump_stats(stats, f"sanitized_by_{where}")


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    candidates = [text.strip()]
    match = _FENCE.search(text)
    if match:
        candidates.insert(0, match.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
