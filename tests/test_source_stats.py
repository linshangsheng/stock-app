"""数据源请求统计（server/source_stats.py）：按天成功 / 失败、成功率、上次成功时间、最近报错、落盘、限速层接入。"""
import json
from datetime import date, timedelta

import pytest

from server import source_stats as ss
from server import throttle


def _reset():
    ss._path_loaded = None          # 强制按当前数据目录重新读


def test_record_and_summary(fresh_env):
    _reset()
    for _ in range(8):
        ss.record("baostock", True)
    ss.record("baostock", False, "ConnectionError: boom")
    ss.record("baostock", False, "Timeout")
    s = ss.summary("baostock")
    assert (s["ok"], s["err"]) == (8, 2) and abs(s["rate"] - 0.8) < 1e-9
    assert s["last_ok"] and s["last_err"] and [e["msg"] for e in s["errors"]] == ["Timeout", "ConnectionError: boom"]    # 最近的在前
    assert s["today"] == [8, 2]
    none = ss.summary("sec")
    assert none["rate"] is None and none["ok"] == 0 and none["errors"] == []              # 没发过请求：成功率是「未知」，不是 0%


def test_errors_are_capped_and_messages_truncated(fresh_env):
    _reset()
    for i in range(9):
        ss.record("yfinance", False, f"E{i}" + "x" * 500)
    errs = ss.summary("yfinance")["errors"]
    assert len(errs) == ss.MAX_ERRORS and errs[0]["msg"].startswith("E8") and len(errs[0]["msg"]) <= 200


def test_persists_across_restart_and_prunes_old_days(fresh_env):
    _reset()
    ss.record("tencent", True)
    ss.flush()
    path = fresh_env / "source_stats.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d["sources"]["tencent"]["days"][(date.today() - timedelta(days=30)).isoformat()] = [99, 1]          # 一个月前的旧数据
    d["sources"]["tencent"]["days"][(date.today() - timedelta(days=3)).isoformat()] = [5, 5]
    path.write_text(json.dumps(d), encoding="utf-8")
    _reset()                                                                  # 模拟后端重启：从文件读回来
    s = ss.summary("tencent", days=7)
    assert (s["ok"], s["err"]) == (6, 5)                                      # 今天 1 + 3 天前 5/5；30 天前的不在 7 天窗口里
    ss.record("tencent", True)
    ss.flush()
    saved = json.loads(path.read_text(encoding="utf-8"))["sources"]["tencent"]["days"]
    assert (date.today() - timedelta(days=30)).isoformat() not in saved       # 超过 14 天的写盘时清掉


def test_data_dir_switch_does_not_leak_stats(fresh_env, tmp_path, monkeypatch):
    _reset()
    ss.record("cninfo", True)
    monkeypatch.setenv("STOCK_DATA_DIR", str(tmp_path / "other"))
    from server import settings
    settings.load_config(reload=True)
    (tmp_path / "other").mkdir(exist_ok=True)
    assert ss.summary("cninfo")["ok"] == 0                                    # 换了数据目录，不带着上一个目录的统计


def test_corrupt_file_is_ignored(fresh_env):
    (fresh_env / "source_stats.json").write_text("{not json", encoding="utf-8")
    _reset()
    ss.record("sec", True)                                                    # 不抛异常
    assert ss.summary("sec")["ok"] == 1


def test_throttle_call_records_success_and_failure(fresh_env):
    _reset()
    th = throttle.Throttle(name="unit_src", min_delay=0, max_delay=0, backoff_base=0, backoff_max=0, sleep=lambda s: None, breaker_failures=99)
    assert th.call(lambda: 42) == 42
    with pytest.raises(ValueError):
        th.call(lambda: (_ for _ in ()).throw(ValueError("nope")), retries=1)             # 失败 2 次（首次 + 重试 1 次）
    s = ss.summary("unit_src")
    assert (s["ok"], s["err"]) == (1, 2) and "ValueError: nope" in s["errors"][0]["msg"]


def test_open_breaker_rejection_is_not_counted(fresh_env):
    _reset()
    th = throttle.Throttle(name="unit_brk", min_delay=0, max_delay=0, sleep=lambda s: None)
    th._open_until = th.clock() + 100
    with pytest.raises(throttle.CircuitOpen):
        th.call(lambda: 1)
    assert ss.summary("unit_brk") ["ok"] == 0 and ss.summary("unit_brk")["err"] == 0     # 熔断中被直接拒绝：请求没发出去，不算成功也不算失败
