"""优化循环：AI 排课 → 两轮选课模拟 → AI 满意度 → AI 复盘改写预置 → 重复，保留历史最好的一轮。

循环里每一次"生成"和"改写"都是 LLM 调用；代码不替模型排课，只做事实校验与历史最优比较。
"""
from __future__ import annotations

from .ai_planner import ai_presets, ai_retune
from .llm import LLMClient
from .models import NeedModel


def optimize(world, needs: dict[str, NeedModel], skill_texts: dict[str, str],
             client: LLMClient, llm_stats: dict, evaluate, restarts: int,
             refine_frac: float = 0.3, workers: int = 1):
    """evaluate(preset_map) -> 带 mean()、summary()、scores/reasons/result 的评估对象。"""
    best = ai_presets(world, needs, skill_texts, client, llm_stats, workers)
    best_eval = evaluate(best)
    history = [best_eval.summary()]
    for _ in range(restarts):
        candidate = ai_retune(world, needs, skill_texts, client, llm_stats, best,
                              best_eval.scores, best_eval.reasons,
                              best_eval.result.assignments, refine_frac, workers)
        evaluation = evaluate(candidate)
        history.append(evaluation.summary())
        if evaluation.mean() > best_eval.mean():
            best, best_eval = candidate, evaluation
    return best, best_eval, history
