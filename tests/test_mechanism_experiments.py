"""机制实验的预注册判据。决策模型只在这里和实验脚本里出现。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import run_mechanism_experiments as mech  # noqa: E402


def test_preregistered_mechanism_suite(tmp_path):
    summary = mech.run_suite(tmp_path, with_supply=False)
    assert set(summary["verdicts"]) >= {
        "control", "competition", "scale", "overlap", "priority",
        "paired_retune", "batch", "checkpoint",
    }
    assert all(verdict == "通过" for verdict in summary["verdicts"].values())

    assert summary["control"]["drops"] == 0
    assert summary["control"]["round_hashes_differ"] is False
    competition = summary["competition"]
    assert competition["drops"] > 0
    assert competition["round_hashes_differ"] is True
    assert competition["seat_rejections"] == competition["n"] - mech.HOT_CAP
    assert [row["seat_rejections"] for row in summary["scale"]] == [2, 6, 10]
    assert summary["overlap"]["preset_grant_overlap"]
    assert summary["priority"]["winners"] == ["S003"]

    paired = summary["paired_retune"]
    assert paired["seeds"] == 24
    assert paired["mean_delta"] >= mech.PAIRED_MIN_DELTA
    assert paired["seeds_delta_negative"] == 0
    assert paired["seeds_unmet_increased"] == 0
    assert paired["cross_seed_negative"] > 0
    assert paired["dose_mean_delta"] < paired["mean_delta"]
    assert summary["batch"]["single_calls"] == 4
    assert summary["batch"]["grouped_calls"] == 1
    assert summary["checkpoint"]["resume_log"] is True
    assert summary["checkpoint"]["preset_calls_on_resume"] == 2
