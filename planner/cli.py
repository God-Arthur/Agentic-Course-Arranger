"""命令行入口：headless 跑演示流水线（LLM 配置来自 redo/.env 或命令行参数）。"""
from __future__ import annotations

import argparse
import json
import sys

from .env_config import resolve_llm
from .llm import LLMError
from .pipeline import DemoError, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI preset timetable planner (redo)")
    parser.add_argument("--n", type=int, default=8, help="student count")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--courses", default="data/courses.json")
    parser.add_argument("--restarts", type=int, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--out", default=None, help="write full result JSON to this file")
    args = parser.parse_args(argv)

    llm, _public = resolve_llm({"base_url": args.base_url, "model": args.model,
                                "api_key": args.api_key})
    if llm is None:
        print("ERROR: 需要完整 LLM 配置：redo/.env 可提供默认值，"
              "缺失项用 --base-url/--model/--api-key 补齐", file=sys.stderr)
        return 2
    try:
        result = run_pipeline(args.n, args.courses, llm=llm,
                              seed=args.seed, restarts=args.restarts)
    except (DemoError, LLMError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result["summary"], ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1)
        print(f"written: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
