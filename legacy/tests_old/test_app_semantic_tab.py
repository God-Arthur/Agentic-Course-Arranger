"""End-to-end headless run of app.py: the I/J/K semantic-need tab must render, not just import.

No test here switches on an LLM arm, so this file makes zero API calls. It does run `app.py`,
which calls `load_dotenv()` and therefore writes the real key into `os.environ`; `tearDownClass`
puts the environment back so later test modules cannot silently inherit a live credential.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import unittest
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
RULE_ARM = "I 规则解析（离线，无 API 成本）"
TRUTH_ARM = "K 隐藏真实需求标签（不可部署的上限参照）"
EXPECTED_EXPORTS = {
    "semantic_need_model.csv": "text/csv", "semantic_preset_courses.csv": "text/csv",
    "preassigned_courses.csv": "text/csv", "final_courses_after_two_rounds.csv": "text/csv",
    "course_demand.csv": "text/csv", "need_model.csv": "text/csv",
    "coordination_proposals.csv": "text/csv", "mechanism_comparison.csv": "text/csv",
    "run_summary.json": "application/json",
}


def _no_exception(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.exception]


class SemanticNeedTabInApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = dict(os.environ)
        cls.at = AppTest.from_file(APP, default_timeout=900)
        cls.at.run()
        failures = _no_exception(cls.at)
        if failures:
            raise AssertionError(failures)
        # 产品默认规划层是 AI（需 LLM）；本文件保持零 API 成本，先切到离线对照再运行主仿真。
        cls.at.sidebar.select_slider[0].set_value("规则规划（离线对照）").run()
        cls.at.sidebar.button[0].click().run()          # 运行集体规划仿真
        failures = _no_exception(cls.at)
        if failures:
            raise AssertionError(failures)

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.clear()
        os.environ.update(cls._env)

    def _run_arm(self, label: str) -> dict:
        self.at.get_by_key("sem_arm").set_value(label).run()
        self.at.get_by_key("run_sem").click().run()
        self.assertEqual(_no_exception(self.at), [])
        return self.at.session_state["sem"]

    def test_1_semantic_tab_exists(self) -> None:
        labels = [tab.label for tab in self.at.tabs]
        self.assertEqual(len(labels), 8)
        self.assertTrue(any("AI 语义需求解析" in label for label in labels), labels)

    def test_1b_exports_are_reachable_without_running_mechanisms(self) -> None:
        # Runs after test_1 and before test_2/test_5, so session_state has the main run
        # but not yet the twelve-mechanism comparison.
        labels = [el.label for el in self.at.get("download_button")]
        for expected in ["下载预置课表 CSV", "下载最终课表 CSV", "下载课程供需 CSV",
                         "下载需求模型 CSV", "下载协调提案 CSV", "下载本次运行摘要 JSON"]:
            self.assertIn(expected, labels)
        self.assertNotIn("下载对照实验 CSV", labels,
                         "机制对照还没运行，对照 CSV 不应出现，避免导出空表或旧世界的结果")
        self.assertEqual([str(e.value) for e in self.at.exception], [])

    def test_2_rule_arm_reaches_preset_and_two_rounds(self) -> None:
        sem = self._run_arm(RULE_ARM)
        self.assertEqual(sem["model"]["parser"], "规则解析")
        self.assertGreater(sem["stats"]["pairs_added"], 0)
        self.assertFalse(any(r.get("hidden_utility_used") for r in sem["model"]["records"]))
        self.assertEqual(sem["model"]["cost"]["llm_calls"], 0)
        for record in sem["model"]["records"]:
            for row in record["course_relevance"]:
                self.assertIn("category", row)
                self.assertTrue(row["evidence"], f'{record["student_id"]} 的判断没有原文依据')
        self.assertEqual(len(sem["outcomes"]), 3)
        self.assertGreaterEqual(sem["scores"]["need_f1_pct"], 0.0)

    def test_3_truth_arm_is_flagged_and_makes_no_calls(self) -> None:
        sem = self._run_arm(TRUTH_ARM)
        self.assertEqual(sem["model"]["parser"], "隐藏真实需求标签")
        self.assertTrue(all(r.get("hidden_utility_used") for r in sem["model"]["records"]))
        self.assertEqual(sem["model"]["cost"]["llm_calls"], 0)
        self.assertEqual(sem["scores"]["key_need_recall_pct"], 100.0,
                         "K 是同一接口下的完美解析器，重要需求应当全部识别出来")

    def test_4_llm_arms_stay_off_by_default(self) -> None:
        self.assertFalse(self.at.get_by_key("mech_include_llm").value)
        self.assertFalse(self.at.get_by_key("mech_include_need").value)

    def test_5_mechanism_tab_runs_need_arms_without_llm(self) -> None:
        self.at.get_by_key("run_mechanisms").click().run()
        self.assertEqual([str(e.value) for e in self.at.exception], [])
        rows = self.at.session_state["mech_rows"]
        status = {r["mechanism"]: r["status"] for r in rows}
        self.assertEqual(status["I_规则语义需求解析"], "已运行")
        self.assertEqual(status["K_真实需求标签参照"], "已运行")
        self.assertEqual(status["J_AI语义需求解析"], "未运行")
        self.assertEqual(status["C_AI集体规划"], "未运行")

    def _capture_exports(self) -> dict[str, tuple[bytes, str]]:
        """Re-render the page with `st.download_button` spied on: return file_name -> (bytes, mime).

        Streamlit 1.66 no longer keeps the payload on the element proto — it hands the bytes to
        a mock media store that AppTest tears down when the run ends, so `proto` only shows a
        `/mock/media/<hash>.csv` URL. The call itself is therefore the last headless-visible
        boundary of what the browser would receive. The spy delegates, so the widget tree still
        renders and the button assertions below keep working on real elements.
        """
        captured: dict[str, tuple[bytes, str]] = {}
        seen: list[str] = []
        original = st.download_button

        def spy(label, data, file_name=None, mime=None, *args, **kwargs):
            seen.append(str(file_name))
            captured[str(file_name)] = (data, str(mime))
            return original(label, data, file_name, mime, *args, **kwargs)

        with mock.patch.object(st, "download_button", spy):
            self.at.run()
        self.assertEqual(_no_exception(self.at), [])
        self.assertEqual(len(seen), len(set(seen)), f"同名按钮出现两次，导出会互相覆盖：{seen}")
        return captured

    def test_6_every_download_button_carries_the_data_it_computed(self) -> None:
        """A button existing is not a file: check the bytes the page hands to Streamlit.

        Verified here: every export's file name, mime and full payload, and that each payload
        still agrees with the numbers sitting in session state. Not verified: the operating
        system's own save dialog after a real click — that is Streamlit's code, not ours.
        """
        if not self.at.session_state.get("mech_rows"):
            self.at.get_by_key("run_mechanisms").click().run()
        self._run_arm(RULE_ARM)   # 前面的用例停在 K 臂，这里显式回到可部署的规则解析

        exports = self._capture_exports()
        self.assertEqual(set(exports), set(EXPECTED_EXPORTS),
                         "导出集合变了：README §8 记录的 9 个按钮需要一并核对")
        self.assertEqual(len(self.at.download_button), len(exports))

        for name, (data, mime) in exports.items():
            self.assertIsInstance(data, bytes, f"{name} 的载荷不是字节串")
            self.assertTrue(data, f"{name} 的导出载荷是空的")
            self.assertEqual(mime, EXPECTED_EXPORTS[name], f"{name} 的 mime 不对")
            if name.endswith(".csv"):
                self.assertTrue(data.startswith(b"\xef\xbb\xbf"),
                                f"{name} 缺 UTF-8 BOM，Excel 打开中文列名会乱码")

        def table(name: str) -> list[list[str]]:
            return list(csv.reader(io.StringIO(exports[name][0].decode("utf-8-sig"))))

        def column(name: str, header: str) -> list[str]:
            rows = table(name)
            return [r[rows[0].index(header)] for r in rows[1:]]

        mech = table("mechanism_comparison.csv")
        self.assertEqual(len(mech) - 1, len(self.at.session_state["mech_rows"]),
                         "对照实验 CSV 的行数与页面算出的 mech_rows 不一致")
        self.assertIn("valid_for_ai_claim", mech[0],
                      "导出必须带回退标记，否则离线读 CSV 分不清 AI 证据与规则回退")
        col = mech[0].index("valid_for_ai_claim")
        self.assertIn("False", {r[col] for r in mech[1:]},
                      "J 组默认不调 LLM，导出里必须看得见这条判据为 False")

        sem = self.at.session_state["sem"]
        need = table("semantic_need_model.csv")
        self.assertEqual(len(need) - 1,
                         sum(len(r["course_relevance"]) for r in sem["model"]["records"]),
                         "语义需求 CSV 少一行判断就少一份证据")
        for header in ("evidence", "category", "uncertainty", "hidden_utility_used"):
            self.assertIn(header, need[0], f"导出的语义需求模型缺 {header} 列")

        def oracle_flags(payload: dict[str, tuple[bytes, str]]) -> dict[str, str]:
            rows = list(csv.reader(io.StringIO(payload["semantic_need_model.csv"][0].decode("utf-8-sig"))))
            sid, hid = rows[0].index("student_id"), rows[0].index("hidden_utility_used")
            return {r[sid]: r[hid] for r in rows[1:]}

        expected = {r["student_id"]: "True" if r.get("hidden_utility_used") else "False"
                    for r in sem["model"]["records"] if r["course_relevance"]}
        self.assertEqual(set(expected.values()), {"False"},
                         f'{sem["model"]["parser"]} 不该把隐藏标签写进导出')
        # 零判断学生整行不存在是有意的（解析器没敢下判断，不是导出漏行），
        # 所以这里比对的是“有判断的那批人”，并要求页面把两者人数讲清楚。
        self.assertEqual(oracle_flags(exports), expected,
                         "导出里的 hidden_utility_used 与页面算出的模型不一致")
        blank = sum(1 for r in sem["model"]["records"] if not r["course_relevance"])
        cover = [c.value for c in self.at.caption if "覆盖情况" in c.value]
        self.assertEqual(len(cover), 1, "导出的覆盖面没有说明，读 CSV 的人会把缺行当成漏导出")
        self.assertEqual(re.findall(r"\d+", cover[0])[:3],
                         [str(len(sem["model"]["records"])), str(len(expected)), str(blank)],
                         f"页面写的覆盖人数与算出来不符：{cover[0]}")

        self.assertEqual(
            set(column("final_courses_after_two_rounds.csv", "student_id")),
            set(column("need_model.csv", "student_id")),
            "两份导出覆盖的学生不是同一批")
        self.assertLessEqual(set(column("semantic_need_model.csv", "student_id")),
                             set(column("semantic_preset_courses.csv", "student_id")),
                             "预置课表导出应包含语义需求模型里的每个学生")

        summary = json.loads(exports["run_summary.json"][0].decode("utf-8"))
        for key in ("config", "final_metrics", "preset_stats", "round_stats", "caveat"):
            self.assertIn(key, summary, f"运行摘要缺 {key}，复现者拿不到可核对的输入")
        self.assertIn("合成数据", summary["data"])
        self.assertIn("不能外推", summary["caveat"])

        self.assertEqual(len(table("semantic_preset_courses.csv")) - 1,
                         len(set(column("need_model.csv", "student_id"))),
                         "预置课表导出应是一人一行，行数对不上说明有人整行丢了")

        # K 是同一接口下的完美解析器：它的导出必须自曝用过隐藏标签，
        # 否则上限参照会被离线读 CSV 的人误当成可部署的 AI 效果。
        self._run_arm(TRUTH_ARM)
        oracle = oracle_flags(self._capture_exports())
        self.assertEqual(set(oracle.values()), {"True"},
                         'K 臂的导出没有把 hidden_utility_used 标成 True')

    def test_7_verdict_status_matches_what_is_on_disk(self):
        """The tab must state the pre-registration status the way the filesystem actually is.

        While `FROZEN_VERDICT.md` is absent the page may not read as if it held a verdict; once
        the file lands, the page must show the file's own text instead of a paraphrase of it.
        """
        self._run_arm(RULE_ARM)
        verdict = os.path.join(os.path.dirname(APP), "results_semantic_need", "FROZEN_VERDICT.md")
        texts = [m.value for m in self.at.markdown] + [c.value for c in self.at.caption]
        if not os.path.exists(verdict):
            pending = [t for t in texts if "不是预注册判定" in t]
            self.assertEqual(len(pending), 1, "判定文件不在盘上，页面却没任何一处说明")
            self.assertIn("FROZEN_VERDICT.md", pending[0], "'尚未产出'必须点名判定文件")
        else:
            with open(verdict, encoding="utf-8") as handle:
                head = handle.readline().strip()
            self.assertTrue(head and any(head in t for t in texts),
                            "判定已落盘，页面仍在说'尚未产出'，或把判定改写成另一套说法")


if __name__ == "__main__":
    unittest.main()
