"""盘后增量提速：腾讯批量报价通道（解析 / 写入 / 除权 / 停牌 / 降级）、抽查、日历不被兜底覆盖、单个指数失败不中断、
美股空结果重试、分步计时、补全后接着更新、低风险组合回测摘要按周缓存。离线（演示数据 + 伪造的报价）。"""
import json

import pandas as pd

from server import db, ingest, market_calendar as mc
from server.datasource_cn import get_source, parse_tencent
from server.throttle import CircuitOpen


def _raw(sym, name, close, prev, open_, vol, high, low, amount, ts="20261015161456", turn=1.0, status="", cap=100.0):
    """按腾讯 qt.gtimg.cn 的字段位置拼一行（第 0 列 symbol，之后是 ~ 分隔的原始字段）。"""
    f = ["1", name, sym[3:], str(close), str(prev), str(open_), str(vol)] + [""] * 23
    f += [ts, "0", "0", str(high), str(low), f"{close}/{vol}/{amount}", str(vol), "0", str(turn), "", status, "", "", "", str(cap), str(cap)]
    return [sym] + f + [""] * 40


def test_parse_tencent_units_suspension_and_sanity():
    q = parse_tencent([
        _raw("sh.600000", "浦发银行", 9.64, 9.65, 9.65, 685743, 9.70, 9.53, 658627633),          # 主板：量的单位是「手」
        _raw("sh.688981", "中芯国际", 104.44, 102.55, 99.56, 47571746, 104.58, 99.10, 4805901000),  # 科创板：单位是「股」
        _raw("sh.600293", "三峡新材", 3.93, 3.93, 0.0, 0, 0.0, 0.0, 0, status="S"),             # 停牌
        _raw("sz.000001", "坏数据", 10.0, 10.0, 10.0, 1000, 9.0, 11.0, 1000000),                 # 最高 < 最低
        _raw("bj.830799", "北交所", 10.0, 10.0, 10.0, 1000, 10.5, 9.5, 1000000),                 # 不在 A 股主板 / 创业板 / 科创板
    ]).set_index("symbol")
    assert list(q.index) == ["sh.600000", "sh.688981", "sh.600293", "sz.000001"]
    assert q.loc["sh.600000", "volume"] == 68574300 and q.loc["sh.688981", "volume"] == 47571746
    assert q.loc["sh.600000", "quote_date"] == "2026-10-15" and q.loc["sh.600000", "quote_time"] == "161456"
    assert bool(q.loc["sh.600293", "suspended"]) and bool(q.loc["sh.600293", "ok"]) and q.loc["sh.600293", "volume"] == 0
    assert bool(q.loc["sh.600000", "ok"]) and not bool(q.loc["sz.000001", "ok"])
    assert q.loc["sh.600000", "float_mktcap"] == 100.0 * 1e8


def _drop_last_day(c):
    asof = db.get_meta(c, "data_asof")
    prev = c.execute("SELECT MAX(date) FROM daily_bar WHERE date<?", (asof,)).fetchone()[0]
    rows = pd.read_sql("SELECT b.*, s.name FROM daily_bar b JOIN securities s ON s.symbol=b.symbol WHERE b.date=?", c, params=(asof,))
    c.execute("DELETE FROM daily_bar WHERE date=?", (asof,))
    c.commit()
    return asof, prev, rows


def _quotes_from(rows, prev_close, day):
    """把「真实的」当日日线变成报价结构（parse_tencent 的输出列）。"""
    return pd.DataFrame({"symbol": rows["symbol"], "name": rows["name"], "open": rows["open"], "high": rows["high"], "low": rows["low"],
                         "close": rows["close"], "prev_close": rows["symbol"].map(prev_close), "volume": rows["volume"], "amount": rows["amount"],
                         "turnover": rows["turnover"], "float_mktcap": 1e10, "pct": 0.0, "quote_date": day, "quote_time": "160000",
                         "suspended": False, "ok": True})


def test_quotes_path_writes_final_rows_handles_ex_rights_suspension_and_missing(demo_env, monkeypatch):
    src = get_source()
    monkeypatch.setattr(ingest, "quotes_settled", lambda *a, **k: True)
    with db.market_db("CN") as c:
        asof, prev, rows = _drop_last_day(c)
        rows = rows[rows["trade_status"] > 0].reset_index(drop=True)
        pc = dict(c.execute("SELECT symbol, close FROM daily_bar WHERE date=?", (prev,)).fetchall())
        q = _quotes_from(rows, pc, asof)
        x, y, z = q["symbol"].iloc[0], q["symbol"].iloc[1], q["symbol"].iloc[2]
        f0 = c.execute("SELECT adj_factor FROM daily_bar WHERE symbol=? AND date=?", (x, prev)).fetchone()[0]
        q.loc[q.symbol == x, "prev_close"] = pc[x] / 1.25                     # x 当天除权：交易所昨收比库里前收低 20%
        q.loc[q.symbol == y, ["suspended", "volume", "amount"]] = [True, 0.0, None]
        q.loc[q.symbol == y, "close"] = pc[y]
        q = q[q.symbol != z]                                                  # z 没有报价：交给 BaoStock（这里是演示源）
        calls = []
        out = ingest.update_incremental(c, src, "CN", asof, quotes_fn=lambda syms: calls.append(len(syms)) or q)
        assert out["mode"] == "quotes" and out["from_quotes"] == len(q) and out["ex_rights"] == 1 and out["suspended"] == 1
        assert out["baostock"] >= 1 and calls and calls[0] >= len(q)
        rx = c.execute("SELECT adj_factor, source, is_temp FROM daily_bar WHERE symbol=? AND date=?", (x, asof)).fetchone()
        assert abs(rx[0] / (f0 * 1.25) - 1) < 1e-9 and rx[1] == "tencent" and rx[2] == 0
        ry = c.execute("SELECT trade_status, volume, open, close FROM daily_bar WHERE symbol=? AND date=?", (y, asof)).fetchone()
        assert ry[0] == 0 and ry[1] is None and ry[2] == ry[3] == pc[y]
        assert c.execute("SELECT source FROM daily_bar WHERE symbol=? AND date=?", (z, asof)).fetchone()[0] == src.name
        same = c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND source='tencent'", (asof,)).fetchone()[0]
        assert same == len(q) and asof in json.loads(db.get_meta(c, "quote_days"))


def test_quotes_for_another_day_fall_back_to_history(demo_env, monkeypatch):
    src = get_source()
    monkeypatch.setattr(ingest, "quotes_settled", lambda *a, **k: True)
    with db.market_db("CN") as c:
        asof, prev, rows = _drop_last_day(c)
        pc = dict(c.execute("SELECT symbol, close FROM daily_bar WHERE date=?", (prev,)).fetchall())
        stale = _quotes_from(rows, pc, prev)                                  # 报价已经是别的交易日（例如新交易日开盘后）
        out = ingest.update_incremental(c, src, "CN", asof, quotes_fn=lambda syms: stale)
        assert out["mode"] == "history" and "quotes_skipped" in out and out["updated"] > 100
        assert c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND source='tencent'", (asof,)).fetchone()[0] == 0


def test_quotes_settled_waits_for_after_hours_trading():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    assert not ingest.quotes_settled("CN", "2026-10-15", datetime(2026, 10, 15, 15, 20, tzinfo=tz))
    assert ingest.quotes_settled("CN", "2026-10-15", datetime(2026, 10, 15, 15, 40, tzinfo=tz))
    assert ingest.quotes_settled("CN", "2026-10-14", datetime(2026, 10, 15, 8, 0, tzinfo=tz))


def test_spot_check_overwrites_mismatches_and_disables_channel(demo_env, monkeypatch):
    src = get_source()
    monkeypatch.setattr(ingest, "quotes_settled", lambda *a, **k: True)
    with db.market_db("CN") as c:
        asof, prev, rows = _drop_last_day(c)
        pc = dict(c.execute("SELECT symbol, close FROM daily_bar WHERE date=?", (prev,)).fetchall())
        q = _quotes_from(rows[rows["trade_status"] > 0], pc, asof)
        q["close"] = q["close"] * 1.03                                        # 报价被改坏：抽查应发现并暂停通道
        q["high"] = q[["high", "close"]].max(axis=1)
        ingest.update_incremental(c, src, "CN", asof, quotes_fn=lambda syms: q)
        nxt = (pd.Timestamp(asof) + pd.Timedelta(days=1)).date().isoformat()
        monkeypatch.setattr(ingest.mc, "today_str", lambda *a, **k: nxt)
        r = ingest.spot_check_quotes(c, src, n=20)
        assert r["date"] == asof and r["checked"] == 20 and r["diff"] == 20 and r["disabled_until"]
        assert not ingest.quotes_enabled(c)
        fixed = c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND source=?", (asof, src.name)).fetchone()[0]
        assert fixed == 20                                                    # 不一致的已用 BaoStock（演示源）覆盖
        assert ingest.spot_check_quotes(c, src, n=20) is None                 # 同一天不重复查


def test_calendar_is_kept_when_upstream_fails(demo_env):
    class Down:
        def trade_calendar(self, *a):
            raise CircuitOpen("baostock: 熔断中")
    with db.market_db("CN") as c:
        day = c.execute("SELECT MAX(date) FROM market_calendar WHERE is_open=1 AND date<?", (mc.today_str("CN"),)).fetchone()[0]
        c.execute("UPDATE market_calendar SET is_open=0 WHERE date=?", (day,))   # 当作节假日
        assert ingest.ensure_calendar(c, Down(), "CN") == "kept"
        assert c.execute("SELECT is_open FROM market_calendar WHERE date=?", (day,)).fetchone()[0] == 0


def test_index_failures_do_not_abort_and_quotes_fill_missing_day(demo_env, monkeypatch):
    class Down:
        def index_bars(self, *a):
            raise CircuitOpen("yfinance: 熔断中")

    class Q:
        def index_quotes(self, syms):
            return pd.DataFrame({"symbol": syms, "date": day, "time": "160000", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                                 "volume": 100.0, "amount": 1000.0})
    monkeypatch.setattr(ingest, "quotes_settled", lambda *a, **k: True)
    with db.market_db("CN") as c:
        day = mc.last_closed_trading_day(c, "CN")
        c.execute("DELETE FROM index_bar WHERE date=?", (day,))
        assert ingest.refresh_indices(c, Down(), "CN") == 0                    # 不再抛出熔断、中断整条任务链
        n = ingest.refresh_indices(c, Down(), "CN", quotes_src=Q())
        bench = c.execute("SELECT COUNT(*) FROM index_bar WHERE date=?", (day,)).fetchone()[0]
        assert n == bench > 0


def test_probe_uses_quotes_when_baostock_is_down(demo_env, monkeypatch):
    class Down:
        def index_bars(self, *a):
            raise CircuitOpen("baostock: 熔断中")

    class Q:
        def index_quotes(self, syms):
            return pd.DataFrame({"symbol": syms, "date": ["2026-10-15"] * len(syms)})
    monkeypatch.setattr(ingest, "quotes_settled", lambda *a, **k: True)
    assert ingest.probe_day_available(Down(), "CN", "2026-10-15", quotes_src=Q())
    assert not ingest.probe_day_available(Down(), "CN", "2026-10-16", quotes_src=Q())


def test_us_batch_retries_symbols_that_came_back_empty(us_env):
    from server.datasource_cn import get_source as gs
    src = gs("US")
    real = src.daily_bars_batch
    seen = []

    def flaky(symbols, start, end):                                           # 大批量时一半股票空（像被 Yahoo 限流），小批量正常
        got = real(symbols, start, end)
        seen.append(len(symbols))
        return {k: v for i, (k, v) in enumerate(got.items()) if len(symbols) <= 20 or i % 2 == 0}
    src.daily_bars_batch = flaky
    with db.market_db("US") as c:
        asof = db.get_meta(c, "data_asof")
        c.execute("DELETE FROM daily_bar WHERE date=?", (asof,))
        out = ingest.update_incremental(c, src, "US", asof)
        assert out.get("retried_empty", 0) > 0 and out["still_empty"] == 0, (out, seen)
        assert max(seen) > 20 and min(seen) <= 20
        missing = c.execute("SELECT COUNT(*) FROM securities s WHERE in_l1=1 AND status!='delisted' AND NOT EXISTS "
                            "(SELECT 1 FROM daily_bar b WHERE b.symbol=s.symbol AND b.date=?)", (asof,)).fetchone()[0]
        assert missing == 0


def test_daily_chain_records_step_timing_and_init_chains_update(demo_env):
    from server.jobs import JobManager
    jm = JobManager()
    jm._run("daily", market="CN", scan_day=None)
    last = jm.state["last"]["daily:CN"]
    labels = [x[0] for x in last["timing"]]
    assert last["ok"] and last["secs"] >= 0 and "更新个股日线" in labels and "选股扫描" in labels
    with db.market_db("CN") as c:
        log = c.execute("SELECT detail FROM job_log WHERE job='daily' AND status='ok' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "用时" in log and "选股扫描" in log
    jm._run("init", market="CN")                                              # 补全完成后接着更新 + 出清单
    res = jm.state["last"]["init:CN"]
    assert res["ok"] and "catch_up" in res["result"] and any(k == "拉取历史日线" for k, _ in res["timing"])


def test_factor_summary_cache_is_weekly():
    from server import factor_portfolio as fp
    assert fp.summary_key("2026-10-12", 1e5) == fp.summary_key("2026-10-16", 1e5)     # 同一周：不重算
    assert fp.summary_key("2026-10-16", 1e5) != fp.summary_key("2026-10-19", 1e5)     # 新的一周：重算
    assert fp.summary_key("2026-10-16", 1e5) != fp.summary_key("2026-10-16", 2e5)     # 资金变了：重算
