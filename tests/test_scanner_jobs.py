"""扫描存档、scan_outcomes 回填、盘后任务链与启动补跑（5.4 / 3.26.5）。"""
import json

import pytest

from server import db, market_calendar as mc, scanner
from server.jobs import JobManager


def _days_back(n):
    with db.market_db("CN") as c:
        return mc.trading_days(c)[-n]


def test_scan_archive_is_reproducible_and_complete(demo_env):
    day = _days_back(40)
    r = scanner.run_scan("CN", scan_date=day)
    assert r["official"] and r["scan_date"] == day and r["data_asof"] == day
    with db.market_db("CN") as c:
        run = dict(c.execute("SELECT * FROM scan_runs WHERE run_id=?", (r["run_id"],)).fetchone())
        n = c.execute("SELECT COUNT(*) FROM scan_results WHERE run_id=?", (r["run_id"],)).fetchone()[0]
    assert run["config_hash"] and json.loads(run["config"])["setups"], "每次扫描记录 config 快照与哈希，保证可复现（1.6）"
    assert run["data_gate_status"] == "PASS" and n == len(r["candidates"])


def test_scan_uses_only_data_up_to_scan_date(demo_env):
    """历史日扫描不得使用之后的数据：删掉扫描日之后的全部行情，结果应完全一致。"""
    day = _days_back(25)
    a = scanner.run_scan("CN", scan_date=day, persist=False)
    with db.market_db("CN") as c:
        c.execute("DELETE FROM daily_bar WHERE date>?", (day,))
        c.execute("DELETE FROM index_bar WHERE date>?", (day,))
    b = scanner.run_scan("CN", scan_date=day, persist=False)
    assert [(x["symbol"], x["setup"], round(x["score"] or 0, 6)) for x in a["candidates"]] == \
           [(x["symbol"], x["setup"], round(x["score"] or 0, 6)) for x in b["candidates"]]
    assert a["regime"]["state"] == b["regime"]["state"] and len(a["candidates"]) > 0


def _day_with_candidates(start_back: int = 40, max_back: int = 240, step: int = 3) -> str:
    """合成数据按「今天」生成，某个固定的回看日是否恰好有信号随日期变化：向前找第一个有候选的扫描日（保证测试与运行日期无关）。"""
    for n in range(start_back, max_back, step):
        d = _days_back(n)
        if scanner.run_scan("CN", scan_date=d, persist=False)["candidates"]:
            return d
    raise AssertionError("合成数据里找不到有候选的扫描日")


def test_outcomes_backfill_matches_manual_calculation(demo_env):
    day = _day_with_candidates()
    r = scanner.run_scan("CN", scan_date=day)
    out = scanner.backfill_outcomes("CN")
    assert out["updated"] > 0
    with db.market_db("CN") as c:
        row = c.execute("SELECT o.*, s.trigger_price FROM scan_outcomes o JOIN scan_results s ON s.run_id=o.run_id AND s.symbol=o.symbol "
                        "WHERE o.run_id=? AND o.filled=1 LIMIT 1", (r["run_id"],)).fetchone()
        assert row is not None and row["ret_20d"] is not None
        bars = c.execute("SELECT date, open, close, adj_factor FROM daily_bar WHERE symbol=? AND date>? ORDER BY date LIMIT 21",
                         (row["symbol"], day)).fetchall()
        base = c.execute("SELECT adj_factor FROM daily_bar WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1", (row["symbol"], day)).fetchone()[0]
        entry = bars[0]["open"] * bars[0]["adj_factor"] / base                       # T+1 开盘价（next_open），以信号日因子为基准复权
        exp5 = bars[4]["close"] * bars[4]["adj_factor"] / base / entry - 1
        assert row["ret_5d"] == pytest.approx(exp5, rel=1e-6)
    again = scanner.backfill_outcomes("CN")                                           # 幂等：重复回填不产生重复行
    with db.market_db("CN") as c:
        assert c.execute("SELECT COUNT(*) FROM scan_outcomes WHERE run_id=?", (r["run_id"],)).fetchone()[0] == \
               c.execute("SELECT COUNT(DISTINCT symbol) FROM scan_outcomes WHERE run_id=?", (r["run_id"],)).fetchone()[0]
    assert again["updated"] >= 0


def test_daily_chain_runs_end_to_end(demo_env):
    jm = JobManager()
    res = jm.daily_chain("CN")
    assert res["gate"]["status"] == "PASS" and res["scan"]["official"] is True
    assert res["backup"]["files"]["portfolio.db"] is not None and "health" in res and "outcomes" in res


def test_chain_stops_after_failed_gate(demo_env, monkeypatch):
    from server import ingest
    monkeypatch.setattr(ingest, "refresh_indices", lambda *a, **k: 0)         # 上游补不回来（否则任务链会自愈）
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        c.execute("DELETE FROM index_bar WHERE date=?", (asof,))
    res = JobManager().daily_chain("CN")
    assert res["gate"]["status"] == "INCOMPLETE" and res["scan"]["official"] is False
    assert "health" not in res and "outcomes" not in res, "闸门不通过：后续步骤（体检 / 回填）不执行并告警"


def test_catch_up_backfills_skipped_days(demo_env):
    last_scan_day = _days_back(4)
    scanner.run_scan("CN", scan_date=last_scan_day)                                    # 应用关闭期间错过了其后的交易日
    res = JobManager().catch_up("CN")
    got = {x["date"] for x in res["backfilled_scans"]}
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        expected = {d for d in mc.trading_days(c, last_scan_day, asof) if last_scan_day < d < asof}
        official = {r[0] for r in c.execute("SELECT scan_date FROM scan_runs WHERE official=1")}
    assert got == expected and expected <= official and asof in official


def test_earnings_projection_from_last_year_and_deadline_fallback(demo_env):
    from datetime import date
    from server import ingest
    from server.datasource_cn import get_source
    src = get_source()
    today = date(2026, 10, 3)                                   # 最近季末 2026-09-30（三季报），尚未披露
    with db.market_db("CN") as c:
        syms = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE status='active' LIMIT 3")]
        r = ingest.ensure_earnings(c, src, syms, today=today)
        assert r["ok"] == 3 and r["failed"] == 0
        ev = c.execute("SELECT * FROM events WHERE symbol=? AND event_type='EARNINGS' ORDER BY event_time", (syms[0],)).fetchall()
        actual = [e for e in ev if e["source"] == "baostock"]
        proj = [e for e in ev if e["source"] == "proj"]
        assert actual and proj, "应有去年同期实际披露日 + 今年预计披露日"
        exp = ingest._same_day_next_year(date.fromisoformat(actual[0]["event_time"]))
        assert proj[0]["event_time"] == exp.isoformat() and exp.weekday() < 5
        assert ingest.ensure_earnings(c, src, syms, today=today)["cached"] == 3          # 同一季度不重复请求
    assert ingest.latest_quarter_end(date(2026, 2, 1)) == (date(2025, 12, 31), 4)
    assert ingest._same_day_next_year(date(2024, 2, 29)) == date(2025, 2, 28)


def test_scan_excludes_candidates_inside_earnings_window(demo_env):
    day = _day_with_candidates()
    base = scanner.run_scan("CN", scan_date=day, persist=False)
    assert base["candidates"], "需要至少一个候选"
    victim = base["candidates"][0]["symbol"]
    with db.market_db("CN") as c:
        nxt = mc.trading_days(c, day, None)[1:8]
        # 预计披露日落在「窗口 3 日 + 放宽 3 日」内的第 5 个交易日 -> 被剔除；实际披露日在第 5 个交易日 -> 不剔除（超出 3 日窗口）
        from datetime import date
        from server import ingest
        ingest.set_state(c, victim, f"earnings:{ingest.latest_quarter_end(date.fromisoformat(mc.today_str('CN')))[0].isoformat()}", "ok")  # 已查过，不再覆盖手工事件
        c.execute("INSERT INTO events(symbol,market,event_time,event_type,source) VALUES(?,?,?,?,?)", (victim, "CN", nxt[4], "EARNINGS", "proj"))
    r = scanner.run_scan("CN", scan_date=day, persist=False)
    assert victim not in {x["symbol"] for x in r["candidates"]} and r["summary"]["excluded"]["earnings"] >= 1
    with db.market_db("CN") as c:
        c.execute("UPDATE events SET source='baostock' WHERE symbol=?", (victim,))
        c.execute("DELETE FROM events WHERE symbol=? AND source IN ('proj','deadline')", (victim,))
    r2 = scanner.run_scan("CN", scan_date=day, persist=False)
    assert victim in {x["symbol"] for x in r2["candidates"]}


def test_probe_waits_for_upstream_and_learns_ready_time(demo_env, monkeypatch):
    from datetime import datetime, timezone
    from server import ingest
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        assert ingest.probe_day_available(src, "CN", asof) is True
        monkeypatch.setattr(src, "index_bars", lambda *a, **k: __import__("pandas").DataFrame())
        assert ingest.probe_day_available(src, "CN", asof) is False, "上游还没有当日数据：继续等，不白白发几千次请求"
        monkeypatch.undo()
        y, m, d = map(int, asof.split("-"))
        now = datetime(y, m, d, 8, 0, tzinfo=timezone.utc)                 # 北京时间 16:00，收盘后 60 分钟
        assert ingest.record_ready_observation(c, "CN", asof, now) == 60
        assert ingest.record_ready_observation(c, "CN", asof, now) is None, "同一天只记录首次可用时间"
        assert ingest.ready_stats(c, "CN") == {"n": 1, "median_min": 60, "max_min": 60, "last": 60}


def test_auto_chain_returns_waiting_when_probe_fails(demo_env, monkeypatch):
    from server import ingest
    monkeypatch.setattr(ingest, "probe_day_available", lambda *a, **k: False)
    res = JobManager().daily_chain("CN")
    assert res["status"] == "waiting_data" and "scan" not in res
