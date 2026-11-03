"""数据健康面板（server/data_health.py + /api/data/health）：新鲜度、当日覆盖与缺失名单、闸门、数据源、更新任务、备份，以及总结论。"""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from server import backup, data_health as dh, db, market_calendar as mc, settings, source_stats, throttle

GOOD_BACKUP = {"dir": "x", "full_dir": "y", "last": "2026-09-30", "age_days": 0, "count": 3, "stale": False, "items": []}


@pytest.fixture
def healthy(demo_env, monkeypatch):
    """把演示数据摆成「一切正常」：最近交易日 = 数据截止日，备份是新的。其余测试在这个基础上一次只弄坏一样。"""
    source_stats._path_loaded = None
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: asof)
    monkeypatch.setattr(backup, "backup_status", lambda: GOOD_BACKUP)
    return asof


def _add_calendar_days(n):
    """演示数据的交易日历正好到数据截止日：往后补 n 个工作日，用来模拟「数据落后」。返回补出来的日期列表。"""
    from datetime import date, timedelta
    with db.market_db("CN") as c:
        last = max(mc.trading_days(c))
        d, out = date.fromisoformat(last), []
        while len(out) < n:
            d += timedelta(days=1)
            if d.weekday() < 5:
                out.append(d.isoformat())
        c.executemany("INSERT OR REPLACE INTO market_calendar(date,is_open,is_half_day) VALUES(?,1,0)", [(x,) for x in out])
        c.commit()
    return out


def levels(r):
    return [i["level"] for i in r["issues"]]


def titles(r):
    return " | ".join(i["title"] for i in r["issues"])


def test_healthy_data_gets_ok_verdict(healthy):
    r = dh.build("CN")
    assert r["status"] == "ok" and "数据层没有发现问题" in r["headline"]
    assert "bad" not in levels(r) and "warn" not in levels(r)
    assert r["freshness"]["behind_days"] == 0 and r["coverage"]["rate"] == 1.0 and r["coverage"]["missing"] == 0
    assert r["gate"]["status"] == "PASS" and r["gate"]["checks"]


def test_stale_data_is_bad(demo_env, monkeypatch):
    monkeypatch.setattr(backup, "backup_status", lambda: GOOD_BACKUP)
    later = _add_calendar_days(3)[-1]                                          # 最近已收盘的交易日比数据截止日晚了 3 个交易日
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: later)
    r = dh.build("CN")
    assert r["freshness"]["behind_days"] == 3 and r["status"] == "bad" and "落后 3 个交易日" in titles(r) and "不可信" in r["headline"]


def test_one_day_behind_is_only_a_hint(demo_env, monkeypatch):
    monkeypatch.setattr(backup, "backup_status", lambda: GOOD_BACKUP)
    nxt = _add_calendar_days(1)[0]
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: nxt)
    r = dh.build("CN")
    assert r["freshness"]["behind_days"] == 1 and r["status"] == "ok" and "info" in levels(r)       # 收盘后到更新前的正常空档，不吓人


def test_missing_symbols_are_listed_with_gap(healthy):
    with db.market_db("CN") as c:
        sym = c.execute("SELECT symbol FROM daily_bar WHERE date=? ORDER BY symbol LIMIT 1", (healthy,)).fetchone()[0]
        c.execute("DELETE FROM daily_bar WHERE symbol=? AND date=?", (sym, healthy))
        c.commit()
    r = dh.build("CN")
    cov = r["coverage"]
    assert cov["missing"] == 1 and cov["expected"] == cov["have"] + 1 and cov["rate"] < 1
    assert cov["recent"][0]["symbol"] == sym and cov["recent"][0]["gap"] == 1 and cov["recent"][0]["last"] < healthy
    assert cov["long_stale"] == 0
    assert any("没拿到行情" in i["title"] for i in r["issues"])


def test_long_stale_symbols_are_counted_separately(healthy):
    with db.market_db("CN") as c:
        sym = c.execute("SELECT symbol FROM daily_bar WHERE date=? ORDER BY symbol LIMIT 1", (healthy,)).fetchone()[0]
        days = mc.trading_days(c, None, healthy)
        c.execute("DELETE FROM daily_bar WHERE symbol=? AND date>?", (sym, days[-40]))             # 最近 40 个交易日都没有
        c.commit()
    cov = dh.build("CN")["coverage"]
    assert cov["missing"] == 1 and cov["long_stale"] == 1 and cov["recent"] == []                     # 长期没数据的（多半退市 / 长期停牌）单独算，不混进「当日没拿到」


def test_gate_failure_is_bad_and_explained(healthy):
    with db.market_db("CN") as c:
        c.execute("DELETE FROM daily_bar WHERE date=? AND rowid IN (SELECT rowid FROM daily_bar WHERE date=? LIMIT 100)", (healthy, healthy))
        c.commit()
    r = dh.build("CN")
    assert r["gate"]["status"] == "INCOMPLETE" and r["status"] == "bad"
    bad = next(i for i in r["issues"] if "闸门没通过" in i["title"])
    assert bad["level"] == "bad" and "覆盖" in bad["hint"]


def test_source_failures_and_open_breaker(healthy):
    for _ in range(30):
        source_stats.record("baostock", False, "boom")
    for _ in range(5):
        source_stats.record("baostock", True)
    th = throttle.get("tencent")
    th._open_until = th.clock() + 120
    try:
        r = dh.build("CN")
    finally:
        th._open_until = 0.0
    bs = next(s for s in r["sources"] if s["name"] == "baostock")
    assert bs["ok7"] == 5 and bs["err7"] == 30 and abs(bs["rate7"] - 5 / 35) < 1e-9 and bs["errors"][0]["msg"] == "boom"
    tx = next(s for s in r["sources"] if s["name"] == "tencent")
    assert tx["breaker_open"] and tx["breaker_remaining_s"] > 0
    assert "熔断中" in titles(r) and "成功率只有" in titles(r) and r["status"] == "warn"
    assert [s["name"] for s in r["sources"]][:6] == [s["name"] for s in r["sources"] if s["mine"]][:6]      # 本市场的源排前面
    assert next(s for s in r["sources"] if s["name"] == "sec")["rate7"] is None                         # 没发过请求：成功率未知，不是 0


def test_recent_job_error_is_surfaced_until_a_later_success(healthy):
    with db.market_db("CN") as c:
        db.log_job(c, "daily", "error", "ConnectionError: baostock down")
        c.commit()
    r = dh.build("CN")
    assert "最近一次更新任务失败" in titles(r) and r["jobs"]["last_error"]["detail"].startswith("ConnectionError")
    import time
    time.sleep(1.1)                                                            # job_log.ts 精度是秒
    with db.market_db("CN") as c:
        db.log_job(c, "catch_up", "ok", "CN 用时 1 分 2 秒：更新 1 分")
        c.commit()
    assert "最近一次更新任务失败" not in titles(dh.build("CN"))                 # 之后又成功了，就不再报


def test_stale_backup_and_low_disk(healthy, monkeypatch):
    monkeypatch.setattr(backup, "backup_status", lambda: {**GOOD_BACKUP, "last": "2026-09-01", "age_days": 30, "stale": True})
    r = dh.build("CN")
    assert "备份已经超过 1 天" in titles(r) and r["status"] == "warn"
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": int(0.2 * 1073741824)})())
    r = dh.build("CN")
    assert r["status"] == "bad" and "磁盘只剩 0.2 GB" in titles(r)


def test_no_data_is_reported_not_crashed(fresh_env):
    r = dh.build("CN")
    assert r["has_data"] is False and r["status"] == "bad" and "初始化" in r["issues"][0]["hint"]


def test_process_info_has_memory_and_restart_flag(healthy, monkeypatch):
    p = dh.build("CN")["process"]
    assert p["pid"] and p["version"] and (p["rss_mb"] is None or p["rss_mb"] > 10)
    from server import main
    monkeypatch.setattr(main, "_build_id", lambda: "99999999999")             # 磁盘上的代码比运行中的新
    assert dh.build("CN")["process"]["needs_restart"] is True


def test_health_api_roundtrip(healthy):
    from fastapi.testclient import TestClient
    from server import main
    j = TestClient(main.app).get("/api/data/health").json()
    assert j["market"] == "CN" and j["status"] in ("ok", "warn", "bad") and j["headline"] and isinstance(j["issues"], list)
    assert {"freshness", "coverage", "gate", "sources", "jobs", "backup", "process"} <= set(j)
    assert settings.cfg() is not None


def test_waiting_spam_does_not_hide_last_success_and_is_collapsed(healthy):
    """真实数据里「等待上游数据」一天能有几十条：不能把真正的成功记录淹没，重复的要并成一行。"""
    with db.market_db("CN") as c:
        db.log_job(c, "catch_up", "ok", "CN 用时 1 分：更新 1 分")
        for i in range(200):
            db.log_job(c, "daily", "waiting", "上游尚未提供 2026-11-03 数据（探测基准指数）")
        db.log_job(c, "gate", "PASS", "")
        c.commit()
    j = dh.build("CN")["jobs"]
    assert j["last_ok"] and j["last_ok"]["job"] == "catch_up"                      # 找得到：不是只看最近 12 条
    texts = [(r["job"], r["text"], r["n"]) for r in j["recent"]]
    assert texts[0] == ("gate", "闸门通过", 1) and texts[1] == ("daily", "等待上游数据", 200)       # 200 条等待并成一行，并写明次数
    assert all(r["tone"] != "bad" for r in j["recent"] if r["status"] in ("waiting", "PASS"))   # 「等待」「通过」不能被当成失败
    assert len(j["recent"]) <= 12


def test_one_day_behind_while_waiting_for_upstream_says_so(demo_env, monkeypatch):
    monkeypatch.setattr(backup, "backup_status", lambda: GOOD_BACKUP)
    nxt = _add_calendar_days(1)[0]
    monkeypatch.setattr(mc, "last_closed_trading_day", lambda conn, market, now=None: nxt)
    with db.market_db("CN") as c:
        db.log_job(c, "daily", "waiting", f"上游尚未提供 {nxt} 数据（探测基准指数）")
        c.commit()
    r = dh.build("CN")
    assert r["status"] == "ok" and any("上游还没提供" in i["title"] and "10 分钟" in i["hint"] for i in r["issues"])


def test_waiting_count_is_marked_as_lower_bound_when_window_is_exceeded(healthy):
    with db.market_db("CN") as c:
        for i in range(dh.WINDOW + 40):
            db.log_job(c, "daily", "waiting", "上游尚未提供数据")
        c.commit()
    r = dh.build("CN")["jobs"]["recent"][0]
    assert r["status"] == "waiting" and r["n"] == dh.WINDOW and r["n_min"] is True        # 顶到窗口边界：只能说「至少这么多」
