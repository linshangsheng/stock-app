"""港股选股（第 4 步）：价位表 / 每手股数取整 / 费用 / 触发价止损价取整 / 港交所日历 + 行情校正 / 每手股数查询 / 整条扫描 / 接口。不联网。"""
import warnings

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import db, execution, hk_scan, hk_stocks, market_calendar as mc, scanner, selection, settings, setups  # noqa: E402


# ---------------------------------------------------------------- 价位表 / 每手 / 费用
def test_hk_tick_table_boundaries():
    t = execution.hk_tick
    assert [t(0.1), t(0.249), t(0.25), t(9.99), t(10.0), t(19.99), t(20.0), t(49.99), t(50.0), t(99.99), t(100.0), t(199.9), t(200.0), t(499.9), t(500.0), t(999.5), t(1000.0), t(1999.0), t(2000.0), t(4999.0), t(5000.0)] \
        == [0.001, 0.001, 0.005, 0.005, 0.01, 0.01, 0.02, 0.02, 0.05, 0.05, 0.1, 0.1, 0.2, 0.2, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0, 5.0]


def test_snap_rounds_to_the_grid_and_keeps_a_share_and_us_behaviour():
    assert execution.snap("hk", 15.681, "up") == 15.69 and execution.snap("hk", 15.681, "down") == 15.68 and execution.snap("hk400", 15.686) == 15.69
    assert execution.snap("hk", 53.51, "up") == 53.55 and execution.snap("hk", 53.54, "down") == 53.5
    assert execution.snap("hk", 3.801, "up") == 3.805 and execution.snap("hk", 3.799, "down") == 3.795
    assert execution.snap("hk", 15.68, "up") == 15.68 and execution.snap("hk", 15.68, "down") == 15.68          # 已经在格子上的不动（浮点误差不能把它推到下一格）
    assert execution.snap("main", 15.6849) == 15.68 and execution.snap("us", 15.6851) == 15.69 and execution.snap(None, 3.14159) == 3.14      # A 股 / 美股：保留 2 位，和以前一样
    assert execution.tick("main", 400.0) == 0.01 and execution.tick("hk", 400.0) == 0.2


def test_lot_rounding_by_board_hk_lot_known_or_unknown_and_other_markets_unchanged():
    assert execution.lot_round("hk400", 1234) == 1200 and execution.min_lot("hk400") == 400 and execution.lot_step("hk400") == 400
    assert execution.lot_round("hk", 1234) == 1234 and execution.min_lot("hk") == 1                       # 每手没查到：不取整、不猜
    assert execution.hk_lot("hk400") == 400 and execution.hk_lot("hk") == 1 and execution.hk_lot("main") is None and execution.hk_lot("us") is None and execution.hk_lot(None) is None
    assert execution.lot_round("main", 1234) == 1200 and execution.lot_round("star", 150) == 0 and execution.lot_round("star", 201) == 201 and execution.lot_round("us", 17.9) == 17


def test_hk_trade_fees_charge_stamp_duty_both_sides():
    buy = execution.trade_fees("buy", 100.0, 1000, None, "HK")                  # 成交额 10 万港元
    sell = execution.trade_fees("sell", 100.0, 1000, None, "HK")
    # 佣金 max(30, 3) = 30；印花税 100；交易费 5.65；证监会 2.7；财汇局 0.15；结算费 max(2, 2) = 2
    assert buy == pytest.approx(30 + 100 + 5.65 + 2.7 + 0.15 + 2) and sell == pytest.approx(buy)
    assert execution.trade_fees("buy", 1.0, 100, None, "HK") == pytest.approx(3 + 0.1 + 0.00565 + 0.0027 + 0.00015 + 2)           # 小额：佣金 / 结算费有最低收费


# ---------------------------------------------------------------- 触发价 / 止损价取整
def _cand(board, high=15.681, close=15.6, atr=0.5, **kw):
    return {"board": board, "high": high, "close": close, "atr14": atr, **kw}


def test_plan_entry_hk_trigger_goes_up_and_stop_goes_down_onto_the_grid():
    for high in (15.681, 3.796, 53.51, 400.05, 0.2301):
        for mode in ("stop_entry", "next_open"):
            trig, ref, stop = selection.plan_entry(_cand("hk", high=high, close=high * 0.99, atr=high * 0.03), mode)
            tk = execution.hk_tick(trig)
            assert abs(trig / tk - round(trig / tk)) < 1e-6 and trig > high, (high, trig)                       # 触发价在价位格子上、而且在最高价上方
            tk = execution.hk_tick(stop)
            assert abs(stop / tk - round(stop / tk)) < 1e-6 and stop < ref, (high, stop, ref)                    # 止损价在格子上、而且在入场价下方
    assert selection.plan_entry(_cand("main", high=15.681, close=15.6, atr=0.5), "stop_entry")[0] == 15.69      # A 股：和以前一样（最高价 + 0.01）


def test_hk_stop_never_moves_above_the_unrounded_stop():
    trig, ref, stop = selection.plan_entry(_cand("hk", high=15.681, close=15.6, atr=0.37), "stop_entry")
    raw = execution.initial_stop(ref, 0.37, settings.cfg()["exits"]["stop_atr_k"], settings.cfg()["exits"].get("hard_stop_pct"))
    assert stop <= raw + 1e-9 and raw - stop < execution.hk_tick(raw)         # 向下取整：最多差一个价位，而且只会更低（风险偏保守）


def test_open_plan_hk_shows_three_decimals_below_ten_dollars():
    rows = selection.open_plan(3.805, 3.505, "hk", None, "HK")
    assert any("3.805" in r["open"] for r in rows) and rows[0]["open"] == "≤ 3.505"
    assert all(r["open"] != "= " for r in rows) and not any(r["case"] == "涨停开盘" for r in rows)                # 港股没有涨停
    cn = selection.open_plan(15.68, 14.9, "main", None, "CN")
    assert any(r["case"] == "涨停开盘" for r in cn) and cn[0]["open"] == "≤ 14.90"


# ---------------------------------------------------------------- 交易日历
def _hsi(conn, dates):
    conn.executemany("INSERT OR REPLACE INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES('HK.HSI',?,1,1,1,1,0,0)", [(d,) for d in dates])
    conn.commit()


def test_calendar_comes_from_xhkg_and_is_corrected_by_data(fresh_env):
    with db.market_db("HK") as c:
        _hsi(c, ["2026-09-07", "2026-09-08", "2026-09-10", "2026-09-11", "2026-09-12"])                         # 周三 09-09 没有行情（模拟台风休市）；周六 09-12 有行情（以行情为准：开市）
        info = hk_scan.ensure_calendar(c)
        assert info["source"] == "xhkg+data" and "2026-09-09" in info["diff"]
        assert not mc.is_trading_day(c, "2026-09-09") and mc.is_trading_day(c, "2026-09-12") and mc.is_trading_day(c, "2026-09-10")
        assert not mc.is_trading_day(c, "2025-12-25") and not mc.is_trading_day(c, "2026-01-01") and not mc.is_trading_day(c, "2026-02-01")        # 行情覆盖之外：圣诞 / 元旦 / 周日不开市
        assert mc.is_trading_day(c, "2026-03-03")
        assert db.get_meta(c, "calendar_source") == "xhkg+data"


def test_hk_close_time_and_half_day():
    assert mc.MARKETS["HK"]["tz"] == "Asia/Hong_Kong"
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Hong_Kong")

    class C:
        def execute(self, q, a):
            class R:
                def fetchone(s):
                    return (1,)
            return R()
    assert mc.market_closed_at(C(), "HK", "2026-02-16", datetime(2026, 2, 16, 12, 30, tzinfo=tz)) is True          # 半日市 12:10 收
    assert mc.market_closed_at(C(), "HK", "2026-02-16", datetime(2026, 2, 16, 12, 0, tzinfo=tz)) is False


# ---------------------------------------------------------------- 每手股数
def test_attach_lots_uses_cache_then_fast_lookup_and_stops_after_two_failures(fresh_env, monkeypatch):
    calls = []

    def fake(path, params, timeout=40, tries=2):
        calls.append((params["secid"], timeout, tries))
        raise RuntimeError("东财限流")
    monkeypatch.setattr(hk_stocks, "_get_json", fake)
    with db.market_db("HK") as c:
        db.set_meta(c, hk_stocks.META_LOTS, '{"hk.00001": 500}')
        cand = pd.DataFrame({"symbol": [f"hk.{i:05d}" for i in range(1, 8)], "score": range(7, 0, -1)})
        out = hk_scan.attach_lots(c, cand)
    assert list(out["board"]) == ["hk500"] + ["hk"] * 6
    assert len(calls) == 2 and all(t == 8 and n == 1 for _, t, n in calls)               # 快速查：8 秒超时、只试一次；连续两只失败就不再试
    assert hk_scan.lot_of("hk500") == 500 and hk_scan.lot_of("hk") is None and hk_scan.lot_of("main") is None


def test_attach_lots_caches_what_it_finds(fresh_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", lambda path, params, timeout=40, tries=2: {"f602": 200})
    with db.market_db("HK") as c:
        out = hk_scan.attach_lots(c, pd.DataFrame({"symbol": ["hk.00700", "hk.00005"]}))
        assert list(out["board"]) == ["hk200", "hk200"] and hk_stocks._lots(c) == {"hk.00700": 200, "hk.00005": 200}


# ---------------------------------------------------------------- 整条扫描
N_STOCKS, N_DAYS, LAST = 60, 520, "2026-11-06"


def _seed(conn, with_index=True):
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=LAST, periods=N_DAYS)
    dates = [d.strftime("%Y-%m-%d") for d in idx]
    conn.executemany("INSERT INTO securities(symbol,name,board,status,in_l1,sec_type) VALUES(?,?,'hk','active',1,'stock')", [(f"hk.{700 + i:05d}", f"测试股{i}") for i in range(N_STOCKS)])
    rows = []
    for i in range(N_STOCKS):
        ret = rng.normal(0.0006, 0.012, N_DAYS)
        close = np.round(20 * (1 + i % 7) * np.exp(np.cumsum(ret)), 2)
        open_ = np.round(close * (1 + rng.normal(0, 0.003, N_DAYS)), 2)
        hi, lo = np.round(np.maximum(open_, close) * 1.006, 2), np.round(np.minimum(open_, close) * 0.994, 2)
        vol = np.round(rng.uniform(2e6, 4e6, N_DAYS))
        for k, d in enumerate(dates):
            rows.append((f"hk.{700 + i:05d}", d, float(open_[k]), float(hi[k]), float(lo[k]), float(close[k]), float(vol[k]), float(vol[k] * close[k]), 1.0, float(close[k]), 1, "test"))
    conn.executemany("INSERT INTO daily_bar(symbol,date,open,high,low,close,volume,amount,adj_factor,adj_close,trade_status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if with_index:
        base = 20000 * np.exp(np.cumsum(np.full(N_DAYS, 0.0006)))
        for sym in ("HK.HSI", "HK.HSCEI"):
            conn.executemany("INSERT INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,0,0)",
                             [(sym, d, float(b), float(b), float(b), float(b)) for d, b in zip(dates, base)])
    conn.commit()
    return dates


@pytest.fixture
def hk_env(fresh_env, monkeypatch):
    monkeypatch.setattr(hk_stocks, "_get_json", lambda path, params, timeout=40, tries=2: {"f602": 400 if params["secid"].endswith("00700") else 100})
    with db.market_db("HK") as c:
        _seed(c)
    return fresh_env


def _force_signals(monkeypatch, symbols):
    """合成数据里很难自然出信号：把「突破」形态在最后一天强行设为这几只（打分、止损、取整、每手都是真实代码）。"""
    real = setups.detect_all

    def fake(feat):
        base = real(feat)["breakout"]
        m = base & False
        m.loc[LAST, symbols] = True
        return {"breakout": m}
    monkeypatch.setattr(setups, "detect_all", fake)


def test_scan_runs_end_to_end_and_marks_hk_specifics(hk_env, monkeypatch):
    _force_signals(monkeypatch, ["hk.00700", "hk.00701", "hk.00702"])
    res = hk_scan.run()
    assert res["market"] == "HK" and res["scan_date"] == LAST and res["official"] is True, res.get("gate")
    s = res["summary"]
    assert s["earnings_skipped"] is True and s["earnings_data"] is False and s["panic"]["unsupported"] is True and s["panic"]["active"] is False
    assert s["universe_l2"] > 30 and s["signals_today"] == 3 and res["regime"]["state"] in ("NORMAL", "CAUTION")
    cands = res["candidates"]
    assert 1 <= len(cands) <= 3
    for c in cands:
        assert c["board"].startswith("hk")
        tk = execution.hk_tick(c["trigger_price"])
        assert abs(c["trigger_price"] / tk - round(c["trigger_price"] / tk)) < 1e-6 and c["trigger_price"] > c["high"]
        tk = execution.hk_tick(c["stop_price"])
        assert abs(c["stop_price"] / tk - round(c["stop_price"] / tk)) < 1e-6 and c["stop_price"] < c["trigger_price"]
    by = {c["symbol"]: c for c in cands}
    assert by["hk.00700"]["board"] == "hk400" and all(c["board"] == "hk100" for sym, c in by.items() if sym != "hk.00700")            # 每手股数查到了：写进板块（假接口：00700 是 400，其余 100）


def test_scan_saves_to_scan_tables(hk_env, monkeypatch):
    _force_signals(monkeypatch, ["hk.00700", "hk.00703"])
    hk_scan.run()
    with settings.market_ctx("HK"), db.market_db("HK") as c:
        assert c.execute("SELECT COUNT(*) FROM scan_runs WHERE market='HK' AND official=1").fetchone()[0] == 1
        assert db.get_meta(c, "data_asof") == LAST
        rows = c.execute("SELECT symbol, extra FROM scan_results").fetchall()
    assert {r["symbol"] for r in rows} >= {"hk.00700", "hk.00703"}


def test_scan_without_library_or_with_missing_index_is_explained_not_crashing(fresh_env):
    r = hk_scan.run()
    assert r["error"] == "no_library" and "初始化" in r["message"]


def test_scan_gate_blocks_when_benchmark_missing(fresh_env):
    with db.market_db("HK") as c:
        _seed(c, with_index=False)
    res = hk_scan.run()
    assert res["official"] is False and res["gate"]["status"] != "PASS"
    with db.market_db("HK") as c:
        assert c.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0] == 0                         # 闸门没过：不存正式清单


def test_scan_only_one_at_a_time(hk_env):
    assert hk_scan._run_lock.acquire(blocking=False)
    try:
        assert hk_scan.run()["error"] == "busy"
    finally:
        hk_scan._run_lock.release()


def test_data_day_is_the_last_day_with_nearly_all_stocks(hk_env):
    with db.market_db("HK") as c:
        c.execute("DELETE FROM daily_bar WHERE date=? AND symbol NOT IN (SELECT symbol FROM securities LIMIT 10)", (LAST,))      # 最后一天只有 10 / 60 只：还没下载完
        c.commit()
        assert hk_scan.data_day(c) < LAST


def test_us_and_cn_behaviour_unchanged_by_hk_changes(fresh_env):
    assert execution.lot_round("main", 1250) == 1200 and execution.trade_fees("sell", 10.0, 1000, None, "CN") > 0
    from server import markets
    assert markets.board_of("sh.600000", "CN") == "main" and markets.board_of("AAPL", "US") == "us" and markets.board_of("hk.00700", "HK") == "hk"
    assert markets.gate_benchmarks("HK") == ["HK.HSI", "HK.HSCEI"] and markets.aux_indices("HK") == [] and "SPY" in markets.gate_benchmarks("US")


# ---------------------------------------------------------------- 接口：港股用 A 股 / 美股同一套 /api/scan /api/kline /api/account
def test_api_scan_kline_account_work_for_hk_and_other_endpoints_still_refuse(hk_env, monkeypatch):
    _force_signals(monkeypatch, ["hk.00700", "hk.00701"])
    from server import main
    cl = TestClient(main.app)
    d = cl.get("/api/scan", params={"market": "HK"}).json()
    assert d["run"] is None and d["market"] == "HK"
    r = cl.post("/api/scan/run", params={"market": "HK"}).json()
    assert r["run"]["scan_date"] == LAST and r["run"]["official"] is True and r["candidates"]
    d = cl.get("/api/scan", params={"market": "HK"}).json()
    run = d["run"]
    assert d["market"] == "HK" and run["scan_date"] == LAST and run["official"] and run["summary"]["earnings_skipped"] is True
    assert run["regime"]["market_name"] == "港股" and run["regime"]["benchmark_name"] == "恒生指数"
    by = {c["symbol"]: c for c in d["candidates"]}
    c0 = by["hk.00700"]
    assert c0["board"] == "hk400" and c0["lot"] == 400 and c0["lot_cost"] == pytest.approx(400 * (c0["entry_ref"] or c0["close"]), abs=0.01)
    assert by["hk.00701"]["lot"] == 100 and c0["open_plan"] and not any(x["case"] == "涨停开盘" for x in c0["open_plan"])
    assert cl.get("/api/scan", params={"market": "HK", "date": "2020-01-01"}).json()["run"] is None
    assert cl.get("/api/scan/history", params={"market": "HK"}).status_code == 200
    # 填账户资金后：股数按每手取整
    assert cl.put("/api/account", params={"market": "HK"}, json={"equity": 1000000, "cash": 1000000, "risk_per_trade": 0.0075}).json()["account"]["equity"] == 1000000
    c0 = {c["symbol"]: c for c in cl.get("/api/scan", params={"market": "HK"}).json()["candidates"]}["hk.00700"]
    assert c0["op"]["planned_shares"] % 400 == 0 and c0["op"]["planned_shares"] > 0
    k = cl.get("/api/kline", params={"market": "HK", "symbol": "hk.00700"}).json()
    assert k["bars"] and k["levels"]["scan"]["lot"] == 400 and k["levels"]["scan"]["open_plan"]
    # 港股的自选 / 持仓 / 回测还没做：这些接口仍然拒绝港股（不会悄悄按 A 股处理）
    assert cl.get("/api/watchlist", params={"market": "HK"}).status_code == 400
    assert cl.get("/api/positions", params={"market": "HK"}).status_code == 400
