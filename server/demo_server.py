"""评委演示服务器：单页 HTML + JSON API（仅标准库；密钥按请求传入，只存在于本次请求内存中）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from planner.env_config import resolve_llm  # noqa: E402
from planner.llm import LLMError  # noqa: E402
from planner.models import load_world  # noqa: E402
from planner.pipeline import DemoError, run_pipeline  # noqa: E402

COURSES_PATH = os.path.join(ROOT, "data", "courses.json")
INDEX_PATH = os.path.join(ROOT, "demo", "index.html")
MAX_BODY = 1 << 20


def _state() -> dict:
    info: dict = {"course_data_loaded": False, "error": None, "majors": [],
                  "course_count": 0, "section_count": 0,
                  "source_note": None, "data_note": None}
    _, llm_public = resolve_llm(None)
    info["llm_env"] = llm_public
    if not os.path.exists(COURSES_PATH):
        info["error"] = "缺少 data/courses.json：请放入课程/班次/培养方案数据（格式见 data/README.md）。"
        return info
    try:
        with open(COURSES_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        world = load_world(data)
    except (ValueError, json.JSONDecodeError) as exc:
        info["error"] = f"courses.json 不合规：{exc}"
        return info
    meta = data.get("_meta") or {}
    source = "合成演示数据" if meta.get("synthetic") else "教务系统抓取数据"
    if meta.get("catalog_semester"):
        source += f"：{meta['catalog_semester']}"
    if meta.get("catalog_scraped_at"):
        source += f"（抓取于 {meta['catalog_scraped_at']}）"
    info.update(course_data_loaded=True, majors=world.majors,
                course_count=len(world.courses), section_count=len(world.sections),
                source_note=source, data_note=meta.get("capacity_source"))
    return info


class Handler(BaseHTTPRequestHandler):
    server_version = "RedoDemo/1.0"

    def log_message(self, fmt, *args):  # 只记录非 2xx
        if args and str(args[1]).startswith(("2", "3")):
            return
        super().log_message(fmt, *args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            with open(INDEX_PATH, "rb") as handle:
                body = handle.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._send(200, _state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/api/run":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"error": "请求体为空或超过 1MB 上限"})
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "请求体不是合法 JSON"})
            return
        n_students = request.get("n_students")
        llm = request.get("llm") or {}
        if not isinstance(n_students, int):
            self._send(400, {"error": "n_students 必须是整数"})
            return
        if not isinstance(llm, dict):
            self._send(400, {"error": "llm 必须是对象"})
            return
        config, _public = resolve_llm(llm)
        if config is None:
            self._send(400, {"error": "需要完整 LLM 配置（Base URL、模型名称、API Key）："
                                      "项目根 .env 与页面输入合并后仍有缺失项，请在页面补填。"})
            return
        try:
            result = run_pipeline(n_students, COURSES_PATH, llm=config)
        except DemoError as exc:
            self._send(400, {"error": str(exc)})
            return
        except LLMError as exc:
            self._send(400, {"error": f"LLM 调用失败：{exc}"})
            return
        self._send(200, result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"demo server on http://localhost:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
