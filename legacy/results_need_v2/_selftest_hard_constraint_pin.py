"""Does the §一 hard-constraint pin actually have teeth? Prove it against an in-memory mutant.

``tests/test_semantic_needs_v2.py::HardConstraintsStayOnTheRuleSide`` asserts that a record's
``hard_requirements`` equals the rule side even when the stubbed model hands over a forged list. A
passing assertion only means something if removing the protection makes it fail. So this script
re-reads the production source, swaps the overwrite for "trust whatever the model returned", and
runs the same fixture through both versions.

The mutant lives only in memory: nothing is written next to ``semantic_needs_v2.py``, so the running
v2 batch cannot pick up a mutated module on its next chunk. No solver, no network — the model call is
stubbed in both runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic_needs_v2 as real
from planner_core import generate_world
from semantic_needs import hard_requirements, narrative_skill_files, truth_need_labels
from tests.test_semantic_needs_v2 import SETTINGS, _fabricated_reply, hostile_hard_requirements

ANCHOR = ('        "goals": goals,\n'
          '        "hard_requirements": hard_requirements(st),\n')
MUTANT = ('        "goals": goals,\n'
          '        "hard_requirements": raw.get("hard_requirements") or hard_requirements(st),\n')


def build(module, world, skills, eligible, cache_dir):
    """The J2 arm with a model that hands over a forged 必修清单 alongside its other over-claims."""
    def reply(api_key, base_url, model, batch, catalogue):
        subset = {sid: text for sid, text in skills.items() if sid in dict(batch)}
        records, usage = _fabricated_reply(subset, eligible, world)
        for record in records:
            record["hard_requirements"] = hostile_hard_requirements(
                world.students[record["student_id"]], world)
        return records, usage

    with mock.patch.object(module, "_chat_call_v2", side_effect=reply):
        return module.build_need_model_v2(world, parser="llm", llm_settings=SETTINGS,
                                          skills=skills, cache_dir=cache_dir)


def leaks(module, world, skills, eligible):
    """Students whose record carries something other than the rule-derived list."""
    model = build(module, world, skills, eligible, tempfile.mkdtemp())
    if model["cost"]["llm_fallback"]:
        raise AssertionError("LLM 臂回退成规则臂了——桩没生效，这次比对什么也没测")
    bad = []
    for record in model["records"]:
        sid = record["student_id"]
        st = world.students[sid]
        want = hard_requirements(st)
        if record["hard_requirements"] != want:
            bad.append((sid, record["hard_requirements"], want,
                        hostile_hard_requirements(st, world)))
    return bad


def main() -> int:
    world = generate_world(n_students=12, seed=3)
    skills = narrative_skill_files(world)
    eligible = {sid: labels["eligible"] for sid, labels in truth_need_labels(world).items()}

    source = open(real.__file__, encoding="utf-8").read()
    if source.count(ANCHOR) != 1:
        print(f"FAIL：生产源码里那段『用规则侧重写』的锚点出现 {source.count(ANCHOR)} 次（应为 1 次）；"
              "代码形状变了，本变异脚本要跟着改，不能拿一次跑不通当证据")
        return 1

    before = leaks(real, world, skills, eligible)
    if before:
        print(f"FAIL：真模块就已经漏了 {len(before)} 名学生的硬性要求，用例断言的是别的东西")
        return 1

    mutant = types.ModuleType("semantic_needs_v2_mutant")
    mutant.__file__ = real.__file__
    sys.modules["semantic_needs_v2_mutant"] = mutant
    exec(compile(source.replace(ANCHOR, MUTANT, 1), "semantic_needs_v2_mutant", "exec"),
         mutant.__dict__)
    after = leaks(mutant, world, skills, eligible)

    if not after:
        print("FAIL：把保护改成采信模型清单之后，记录仍然等于规则输出——"
              "那条用例其实没有钉住任何东西")
        return 1
    if not all(got == forged for _, got, _, forged in after):
        print(f"FAIL：变异臂有 {len(after)} 名学生变了，但变的不是模型那份清单，"
              "说明差异来自别处，不能算抓到")
        return 1
    sid, got, want, _ = after[0]
    invented = sorted({row["course_id"] for row in got} - {row["course_id"] for row in want})
    dropped = sorted({row["course_id"] for row in want} - {row["course_id"] for row in got})
    print(f"OK：同一份夹具下，真模块 0/{len(world.students)} 名学生的硬性要求偏离培养方案；把 "
          f"`hard_requirements(st)` 换成 `raw.get(...)` 之后 {len(after)}/{len(world.students)} 名学生的"
          f"记录逐字段等于模型伪造的那一份。以 {sid} 为例：编造的 {invented} 进了记录，被删掉的 "
          f"{dropped} 从记录里消失。那条 v2 用例确有鉴别力。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
