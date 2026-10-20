"""Deployment plumbing: env-driven paths, the run log, saved runs, and the container contracts.

Every check here is written so it can fail for a real reason: the container files are parsed
against the code that actually reads them, not against a copy of the numbers.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

import llm_controller
import run_store
import runlog
import runtime_paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class EnvDrivenPaths(unittest.TestCase):
    def test_defaults_stay_inside_the_app_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in runtime_paths.ENV_KEYS:
                os.environ.pop(key, None)
            self.assertEqual(runtime_paths.data_dir(), os.path.join(ROOT, "data"))
            self.assertEqual(runtime_paths.cache_dir(), os.path.join(ROOT, "semantic_cache"))
            self.assertEqual(runtime_paths.outputs_dir(), os.path.join(ROOT, "outputs"))

    def test_environment_overrides_are_absolute_or_app_relative(self):
        with mock.patch.dict(os.environ, {runtime_paths.LOG_DIR_ENV: "custom_logs",
                                          runtime_paths.OUTPUT_DIR_ENV: "/tmp/somewhere"}):
            self.assertEqual(runtime_paths.log_dir(), os.path.join(ROOT, "custom_logs"))
            self.assertEqual(runtime_paths.outputs_dir(), os.path.realpath("/tmp/somewhere"))

    def test_table_dir_rejects_escapes_but_allows_subdirs(self):
        with mock.patch.dict(os.environ, {runtime_paths.DATA_ROOT_ENV: ROOT}):
            self.assertEqual(runtime_paths.resolve_table_dir("data"), os.path.join(ROOT, "data"))
            self.assertEqual(runtime_paths.resolve_table_dir(os.path.join(ROOT, "data")),
                             os.path.join(ROOT, "data"))
            for bad in ("../../etc", "inputs/../../secrets", "/etc"):
                with self.subTest(bad=bad):
                    with self.assertRaises(runtime_paths.TablePathNotAllowed):
                        runtime_paths.resolve_table_dir(bad)

    def test_writable_names_the_path_that_is_the_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "a-regular-file")
            open(blocker, "w").close()
            self.assertIsNone(runtime_paths.writable(os.path.join(tmp, "new")))
            reason = runtime_paths.writable(os.path.join(blocker, "sub", "deeper"))
            self.assertIsNotNone(reason)
            self.assertIn("父目录不存在", reason)


class RunLogNeverBreaksARun(unittest.TestCase):
    def test_records_are_appended_as_json_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {runtime_paths.LOG_DIR_ENV: tmp}):
                run_id = runlog.log_event("run_start", source="ui", llm_api_key=SECRET,
                                          settings={"api_key": SECRET, "model": "m"})["run_id"]
                runlog.log_event("run_end", run_id=run_id, duration_ms=1234, llm_calls=3)
                rows = runlog.recent(10)
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["event"] for row in rows}, {"run_start", "run_end"})
            dumped = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn(SECRET, dumped)
            self.assertIn("model", dumped)  # non-secret sibling survives redaction
            self.assertEqual(rows[0]["duration_ms"], 1234)

    def test_env_override_moves_the_log_so_tests_never_write_the_repo_copy(self):
        repo_log = os.path.join(ROOT, "logs", "runs.jsonl")
        before = os.path.getsize(repo_log) if os.path.isfile(repo_log) else None
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {runtime_paths.LOG_DIR_ENV: tmp}):
                runlog.log_event("run_start", source="unittest")
                self.assertTrue(os.path.isfile(os.path.join(tmp, "runs.jsonl")))
                self.assertEqual([row["event"] for row in runlog.recent(5)], ["run_start"])
        after = os.path.getsize(repo_log) if os.path.isfile(repo_log) else None
        self.assertEqual(after, before, "测试往仓库里的运行日志写了东西")

    def test_unwritable_log_directory_degrades_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            open(blocker, "w").close()
            with mock.patch.dict(os.environ, {runtime_paths.LOG_DIR_ENV: os.path.join(blocker, "sub")}):
                record = runlog.log_event("run_start", source="ui")
                self.assertIn("log_write_error", record)
                self.assertEqual(runlog.recent(5), [])


class SavedRunsSurviveARestart(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {runtime_paths.OUTPUT_DIR_ENV: self._tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.frame = pd.DataFrame([{"student_id": "S001", "course_id": "C01", "score": 72.5}])

    def _save(self, run_id="RUN-1", **kwargs):
        return run_store.save_run(run_id, {"mode": "B", "seed": 7}, {"final_metrics": {"u": 1}},
                                  {"preassigned_courses.csv": self.frame}, **kwargs)

    def test_save_then_reread_round_trips_rows_and_types(self):
        target = self._save()
        self.assertTrue(os.path.isfile(os.path.join(target, "config.json")))
        listed = run_store.list_runs()
        self.assertEqual([item["run_id"] for item in listed], ["RUN-1"])
        back = run_store.read_frame(listed[0], "preassigned_courses.csv")
        self.assertEqual(len(back), len(self.frame))
        self.assertAlmostEqual(float(back["score"].iloc[0]), 72.5)
        self.assertEqual(str(back["student_id"].iloc[0]), "S001")

    def test_existing_run_is_never_silently_overwritten(self):
        self._save()
        with self.assertRaises(run_store.RunAlreadyExists):
            self._save()
        self._save(overwrite=True)  # only an explicit request replaces it

    def test_run_id_that_cannot_become_a_directory_is_refused(self):
        for bad in ("../../escape", "", "!!!", "a/b", "..", ".hidden"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    run_store.run_dir(bad)

    def test_frame_names_are_refused_not_silently_renamed(self):
        # 静默清洗会把两份导出挤成同一个文件名，等于丢掉一张表
        for bad in ("../escape.csv", "志愿结果.csv", "a/b.csv", "notes.txt"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    run_store.save_run("RUN-2", {}, {}, {bad: self.frame})

    def test_unwritable_output_directory_says_so_before_writing(self):
        blocker = os.path.join(self._tmp.name, "file")
        open(blocker, "w").close()
        with mock.patch.dict(os.environ, {runtime_paths.OUTPUT_DIR_ENV: os.path.join(blocker, "sub")}):
            with self.assertRaises(OSError):
                self._save()


SECRET = "sk-secret-should-never-be-logged"


class ContainerContracts(unittest.TestCase):
    """The build files and the code must not drift apart silently."""

    def test_healthcheck_uses_the_endpoint_streamlit_actually_serves(self):
        dockerfile = read("Dockerfile")
        compose = read("compose.yaml")
        self.assertIn("/_stcore/health", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/_stcore/health", compose)

    def test_one_port_number_everywhere_and_loopback_by_default(self):
        port = None
        with open(os.path.join(ROOT, ".streamlit", "config.toml"), encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("port"):
                    port = line.split("=")[1].strip()
        self.assertIsNotNone(port, "config.toml 没有声明端口")
        dockerfile = read("Dockerfile")
        self.assertIn(f"EXPOSE {port}", dockerfile)
        self.assertIn(f"PLANNER_PORT={port}", dockerfile)
        mapping = compose_ports_mapping()
        self.assertEqual(len(mapping), 1, f"compose 的端口映射应当只有一条，实有 {mapping}")
        # `${VAR:-默认}` 里就有冒号，先按 compose 的规则把默认值展开再拆三段
        expanded = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", mapping[0])
        host_ip, host_port, container_port = expanded.split(":")
        self.assertEqual(container_port, port, "compose 的容器端口与 config.toml 不一致")
        self.assertEqual(host_port, port, "宿主默认端口与 config.toml 不一致")
        # 端口映射行本身才是"对外暴露到什么接口"的证据；healthcheck 里的 127.0.0.1 不算，
        # 它探的是容器内部回环，写死成什么都不影响宿主暴露面。
        self.assertEqual(host_ip, "127.0.0.1",
                         f"默认把服务绑到了 {host_ip}（原行 {mapping[0]}），而这个应用没有身份认证")
        self.assertIn(f":{port}/_stcore/health", read("compose.yaml") + dockerfile)

    def test_the_app_listens_inside_the_container_only_after_the_entrypoint_says_so(self):
        text = read("docker-entrypoint.sh")
        self.assertIn(f'--server.port="${{PLANNER_PORT:-8501}}"', text)
        self.assertIn("--server.address=", text)

    def test_credentials_and_local_state_are_excluded_from_the_image(self):
        patterns = ignore_patterns()
        for must_ship in ["app.py", ".dockerignore", "Dockerfile", "compose.yaml",
                          "docker-entrypoint.sh", ".env.example", ".streamlit/config.toml",
                          "results_semantic_need/FROZEN_VERDICT.md",
                          "results_need_v2/FROZEN_VERDICT_V2.md",
                          "results_need_v2/_run_delivery_guards.sh",
                          "data/courses.csv", "data/students.csv", "data/simulation_truth.csv"]:
            self.assertFalse(excluded(must_ship, patterns), f"{must_ship} 被 .dockerignore 排除了，容器里将没有它")
            self.assertTrue(os.path.isfile(os.path.join(ROOT, must_ship)), f"{must_ship} 不在盘上")
        for must_drop in [".env", ".venv/lib/python3.12/site-packages/x.py", "logs/runs.jsonl",
                          "semantic_cache/llm_relevance.jsonl", "outputs/run/config.json",
                          ".streamlit/secrets.toml", "app.log"]:
            self.assertTrue(excluded(must_drop, patterns), f"{must_drop} 没有被排除，密钥或运行状态会进镜像")

    def test_the_dataset_the_app_seeds_from_is_the_one_in_the_image(self):
        dockerfile = read("Dockerfile")
        self.assertIn("COPY data/ /app/data_ship/", dockerfile)
        self.assertIn("/app/data_ship", read("docker-entrypoint.sh"))
        self.assertFalse(excluded("data/manifest.json", ignore_patterns()),
                        "COPY data/ 会因 .dockerignore 直接构建失败（本次实测踩过）")

    def test_entrypoint_referenced_by_the_dockerfile_exists_and_is_executable(self):
        dockerfile = read("Dockerfile")
        self.assertIn('ENTRYPOINT ["/app/docker-entrypoint.sh"]', dockerfile)
        path = os.path.join(ROOT, "docker-entrypoint.sh")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK), "docker-entrypoint.sh 没有可执行位，容器会起不来")
        # 丢位是可恢复的，但前提是镜像里补得上：COPY 会保留宿主模式，所以 Dockerfile 必须自己 chmod
        self.assertIn("chmod +x /app/docker-entrypoint.sh", dockerfile)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("exec streamlit run app.py", text)
        self.assertIn("--server.headless=true", text)

    def test_build_args_do_not_reinstall_dependencies(self):
        """Re-tagging a release must not require the network: only layers after COPY . may rebuild.

        Both halves of this were real build failures: an `ARG APP_VERSION` near the top
        invalidated the pip layer, and `${APT_PACKAGES:-default}` silently re-added the apt
        packages for someone who had asked for none.
        """
        dockerfile = read("Dockerfile")
        compose = read("compose.yaml")
        pip_layer = dockerfile.index("RUN pip install")
        self.assertGreater(dockerfile.index("ARG APP_VERSION"), pip_layer,
                           "APP_VERSION 声明在 pip 安装层之前，改版本号会重装依赖")
        self.assertGreater(dockerfile.index("ARG APT_PACKAGES"), pip_layer,
                           "apt 层排在 pip 之前，改系统包会重装依赖")
        self.assertIn("${APT_PACKAGES-libgomp1", compose,
                      "compose 用 ${VAR:-default} 会把显式空值当成未设置，apt 层跳不掉")
        self.assertNotIn("${APT_PACKAGES:-", compose)

    def test_requirements_are_the_single_source_of_truth_for_the_build(self):
        dockerfile = read("Dockerfile")
        self.assertIn("COPY requirements.txt", dockerfile)
        self.assertIn("-r /app/requirements.txt", dockerfile)
        pins = {}
        for line in read("requirements.txt").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, r"^[A-Za-z0-9._-]+==[A-Za-z0-9._-]+$",
                             f"依赖行 {line!r} 没有钉死，构建出来的镜像不可复现")
            name, _, version = line.partition("==")
            pins[name] = version
        for name in ("streamlit", "ortools", "pandas", "requests", "python-dotenv"):
            self.assertIn(name, pins, f"requirements.txt 里没有 {name}，镜像里它靠隐式安装")
            self.assertEqual(importlib.metadata.version(name), pins[name],
                             f"{name} 钉在 {pins[name]}，但跑测试的环境实装的是别的版本")


def compose_ports_mapping():
    """Read the `ports:` block of compose.yaml the way Docker does: a list of mappings."""
    out, inside = [], False
    for line in read("compose.yaml").splitlines():
        if line.startswith("    ports:"):
            inside = True
            continue
        if inside:
            if not line.startswith("      "):
                inside = False
                continue
            if line.strip().startswith("- "):
                out.append(line.strip()[2:].strip('"'))
    return out


def ignore_patterns():
    return [line.strip() for line in read(".dockerignore").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _pattern_regex(pattern: str):
    """Translate one .dockerignore line into a path regex with the same matching rules."""
    body = pattern[1:] if pattern.startswith("!") else pattern
    body = body.lstrip("/")
    directory = body.endswith("/")
    body = body.rstrip("/")
    out, i = "", 0
    while i < len(body):
        char = body[i]
        if body[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif char == "*":
            out += "[^/]*"
            i += 1
        elif char == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(char)
            i += 1
    tail = "(/.*)?" if directory else "/?"
    return re.compile("^" + out + tail + "$")


def excluded(relpath: str, patterns) -> bool:
    """Whether the build context path is excluded, i.e. the last matching line is not a `!`."""
    path = relpath[2:] if relpath.startswith("./") else relpath
    keep = False
    for pattern in patterns:
        if _pattern_regex(pattern).match(path):
            keep = not pattern.startswith("!")
    return keep


class ExperimentRunnerGuards(unittest.TestCase):
    """`run_experiments.py` must refuse to destroy landed data before it spends hours computing."""

    def _run(self, args, cwd=ROOT):
        return subprocess.run([sys.executable, os.path.join(ROOT, "run_experiments.py")] + args,
                              cwd=cwd, capture_output=True, text=True, timeout=180)

    def test_existing_raw_rows_blocks_a_fresh_run_without_touching_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            landed = os.path.join(tmp, "raw_rows.csv")
            with open(landed, "w", encoding="utf-8") as handle:
                handle.write("mechanism\n")
            result = self._run(["--seeds", "1", "--n-students", "20", "--out", tmp])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("拒绝静默覆盖", result.stdout + result.stderr)
            with open(landed, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "mechanism\n")

    def test_unwritable_output_is_reported_before_any_compute(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            open(blocker, "w").close()
            result = self._run(["--seeds", "1", "--n-students", "20",
                                "--out", os.path.join(blocker, "sub")])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("不可写", result.stdout + result.stderr)


class SecretResolution(unittest.TestCase):
    """密钥可以从环境变量来，也可以从只读文件来；两条路都不能把它写进任何产物。"""

    def test_environment_variable_wins_and_file_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = os.path.join(tmp, "openai_api_key.txt")
            with open(key_file, "w", encoding="utf-8") as handle:
                handle.write("  file-issued-key  \n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(llm_controller.api_key_from_env_or_file(), "")
                with mock.patch.dict(os.environ, {llm_controller.API_KEY_FILE_ENV: key_file}):
                    self.assertEqual(llm_controller.api_key_from_env_or_file(), "file-issued-key")
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "env-key",
                                                  llm_controller.API_KEY_FILE_ENV: key_file}):
                    self.assertEqual(llm_controller.api_key_from_env_or_file(), "env-key")
                with mock.patch.dict(os.environ, {llm_controller.API_KEY_FILE_ENV: os.path.join(tmp, "nope")}):
                    self.assertEqual(llm_controller.api_key_from_env_or_file(), "")

    def test_the_ui_reads_the_key_through_the_same_resolver(self):
        # 侧边栏自己 os.getenv("OPENAI_API_KEY") 会让"只给文件"的部署方式看起来像没配密钥
        app = read("app.py")
        self.assertIn("llm_controller.api_key_from_env_or_file()", app)
        self.assertNotIn('value=os.getenv("OPENAI_API_KEY"', app)

    def test_no_credential_literal_is_committed_in_the_deploy_files(self):
        for name in ("compose.yaml", "Dockerfile", "docker-entrypoint.sh", ".env.example",
                     ".streamlit/config.toml", "app.py"):
            text = read(name)
            for line in text.splitlines():
                if line.strip().startswith("#"):
                    continue
                self.assertNotIn("sk-", line, f"{name} 里出现了像真实密钥的字面量：{line[:60]}")


if __name__ == "__main__":
    unittest.main()
