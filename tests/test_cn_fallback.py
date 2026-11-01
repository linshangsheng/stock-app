"""A 股多数据源：BaoStock 连不上时自动换腾讯 / 东方财富（切换顺序、熔断后跳过、复权因子按交易所公式、增量按比例接续、
备用名单不误标退市、日历用上交所日历、ST 按名称补）。离线：伪造的数据源与报文。"""
import pandas as pd
import pytest

from server import db, ingest, throttle
from server.datasource_cn_alt import (CnMultiSource, _factor_from_events, _factor_from_preclose, _finish, _parse_fh, _r2)


def test_exchange_formula_factor_matches_hand_calculation():
    assert _parse_fh({"fh_sh": "1", "FHcontent": "10送3股, 10转12股, 10派1元"}) == (0.1, 1.5, 0.0, 0.0)
    assert _parse_fh({"fh_sh": "25.2", "FHcontent": "10转8股, 10派25.2元"})[:2] == (2.52, 0.8)
    assert _parse_fh({}) is None and _r2(6.335) == 6.34 and _r2(6.334) == 6.33
    raw = pd.DataFrame({"date": ["2024-01-02", "2024-01-03", "2024-01-04"], "close": [10.0, 6.4, 6.5]})
    f = _factor_from_events(raw, {"2024-01-03": (0.5, 0.5, 0.0, 0.0)})          # 10 派 5 元、10 送 5 股：参考价 (10 − 0.5) ÷ 1.5 = 6.33
    assert f.tolist() == [1.0, round(10 / 6.33, 6), round(10 / 6.33, 6)]
    em = pd.DataFrame({"close": [10.0, 9.6, 9.7], "preclose": [9.9, 9.5, 9.6]})  # 第二天交易所前收 9.5 ≠ 前一天收盘 10：除权
    assert _factor_from_preclose(em).tolist() == [1.0, round(10 / 9.5, 6), round(10 / 9.5, 6)]


def test_incremental_continues_from_stored_factor():
    raw = pd.DataFrame({"date": ["2024-01-02", "2024-01-03", "2024-01-04"], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                        "amount": 1.0, "turnover": 1.0})
    out = _finish(raw, pd.Series([7.0, 7.0, 7.7]), "2024-01-03", "2024-01-04", prev_factor=2.0, is_st=0)
    assert out["date"].tolist() == ["2024-01-03", "2024-01-04"] and out["adj_factor"].tolist() == pytest.approx([2.0, 2.2])


class _Fake:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, 0

    def daily_bars(self, symbol, start, end, prev_close=None, prev_factor=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        return pd.DataFrame({"date": [start], "close": [1.0]})

    def trade_calendar(self, start, end):
        raise throttle.CircuitOpen("baostock: 熔断中")

    def close(self):
        pass


def _multi(*fakes):
    m = CnMultiSource(bs=fakes[0])
    m.sources = {f.name: f for f in fakes}
    m.order = [f.name for f in fakes]
    return m


def test_fallback_order_and_skip_after_circuit_open():
    bs, tx, em = _Fake("baostock", throttle.CircuitOpen("baostock: 连续失败触发熔断")), _Fake("tencent"), _Fake("eastmoney_hist")
    m = _multi(bs, tx, em)
    m.daily_bars("sh.600000", "2024-01-02", "2024-01-05")
    assert m.name == "tencent" and "baostock" in m.down and em.calls == 0
    m.daily_bars("sh.600001", "2024-01-02", "2024-01-05")
    assert bs.calls == 1 and tx.calls == 2                                      # 熔断过的源本次任务里不再去试
    bad = _multi(_Fake("baostock", RuntimeError("这只没数据")), _Fake("tencent"))
    bad.daily_bars("sh.600000", "2024-01-02", "2024-01-05")
    assert "baostock" not in bad.down                                           # 普通失败只换源，不判定整个源不可用
    dead = _multi(_Fake("baostock", throttle.CircuitOpen("x")), _Fake("tencent", throttle.CircuitOpen("y")))
    with pytest.raises(throttle.CircuitOpen, match="都不可用"):
        dead.daily_bars("sh.600000", "2024-01-02", "2024-01-05")
    pr = _multi(_Fake("baostock", throttle.CircuitOpen("x")), _Fake("tencent")).probe()
    assert [r["ok"] for r in pr] == [False, True]


def test_calendar_and_security_list_fallbacks(fresh_env):
    m = _multi(_Fake("baostock"), _Fake("tencent"))
    with db.market_db("CN") as c:
        assert ingest.ensure_calendar(c, m, "CN") == "xshg"                    # BaoStock 熔断 → 上交所日历
        assert c.execute("SELECT COUNT(*) FROM market_calendar WHERE is_open=1").fetchone()[0] > 1000
        c.execute("INSERT INTO securities(symbol,name,board,status,in_l1) VALUES('sh.600111','北方稀土','main','active',1)")   # 已有、但不在备用名单里（例如长期停牌）
        c.commit()
        m.down["baostock"] = "熔断"
        m._sec_cache = pd.DataFrame({"symbol": ["sh.600000", "sz.000001"], "name": ["浦发银行", "*ST样例"], "board": ["main", "main"]})
        ingest.refresh_securities(c, m, "CN")
        st = dict(c.execute("SELECT symbol, status FROM securities").fetchall())
        assert st["sh.600111"] == "active" and st["sh.600000"] == "active"      # 备用名单不能把已有股票误标为退市
        assert db.get_meta(c, "securities_fallback") and db.get_meta(c, "sec_union_days", "[]") == "[]"   # 历史名单留给 BaoStock 恢复后补
        m.last_source = "tencent"
        bars = pd.DataFrame({"date": ["2024-01-02"], "is_st": [0]})
        assert ingest._fix_st(c, "sz.000001", bars, m)["is_st"].tolist() == [1]  # 备用源没有逐日 ST：按名称补


def test_baostock_incremental_factor_is_relative(monkeypatch):
    """BaoStock 增量遇到除权：新因子 = 库里上一天的因子 × BaoStock 的相对变化（历史可能来自别的源，基准不同）。"""
    from server.datasource_cn import BaoStockSource
    src = BaoStockSource()

    def fake(fn, *a, **k):
        if fn == "query_history_k_data_plus":
            return pd.DataFrame({"date": ["2024-07-18"], "open": ["9"], "high": ["9"], "low": ["9"], "close": ["9"], "preclose": ["9.5"],
                                 "volume": ["1"], "amount": ["1"], "turn": ["1"], "tradeStatus": ["1"], "isST": ["0"]})
        return pd.DataFrame({"dividOperateDate": ["2020-01-02", "2024-07-18"], "backAdjustFactor": ["5.0", "5.5"]})
    monkeypatch.setattr(src, "_query", fake)
    b = src.daily_bars("sh.600000", "2024-07-18", "2024-07-18", prev_close=10.0, prev_factor=2.0)
    assert b["adj_factor"].tolist() == pytest.approx([2.2])


def test_daily_update_covers_all_active_a_shares_not_only_l1(demo_env):
    """市场温度按全 A 统计：每周刷新入库池（L1）后被粗筛剔出的股票，每天也要继续更新，否则温度悄悄变成只算 L1（2026-10-22 的问题）。"""
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        sym = c.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted' AND board IS NOT NULL LIMIT 1").fetchone()[0]
        c.execute("UPDATE securities SET in_l1=0 WHERE symbol=?", (sym,))
        c.execute("DELETE FROM daily_bar WHERE date=?", (asof,))
        c.commit()
        assert sym in ingest._update_universe(c, "CN")
        ingest.update_incremental(c, src, "CN", asof)
        assert c.execute("SELECT 1 FROM daily_bar WHERE symbol=? AND date=?", (sym, asof)).fetchone()


def test_us_scan_universe_limited_to_index_members(us_env, monkeypatch):
    """美股观察清单只在标普500 ∪ 纳斯达克100 里选：名单取不到时不限制（不能因此什么都扫不出）；写法统一成库里的 BRK-B 形式。"""
    from server import scanner, settings, us_index
    assert us_index.norm("brk.b") == "BRK-B"
    with db.market_db("US") as c:
        day = db.get_meta(c, "data_asof")
        settings.cfg()["universe"]["us"]["index_members_only"] = False
        base = scanner.build_context(c, day, market="US")
        n_all = int(base["l2"].iloc[-1].sum())
        pick = set(list(base["l2"].columns[base["l2"].iloc[-1]])[:5])
        settings.cfg()["universe"]["us"]["index_members_only"] = True
        monkeypatch.setattr(us_index, "members", lambda: pick)
        ctx = scanner.build_context(c, day, market="US")
        assert set(ctx["l2"].columns[ctx["l2"].iloc[-1]]) == pick and ctx["cond"]["index_member"].iloc[-1].sum() == 5
        monkeypatch.setattr(us_index, "members", lambda: set())
        assert int(scanner.build_context(c, day, market="US")["l2"].iloc[-1].sum()) == n_all


def test_panic_list_only_on_low_temperature_days(demo_env):
    """恐慌日超跌清单：平时为空；市场温度 < 阈值时才列出站上 200 日线且 RSI14<30 的股票。"""
    from server import scanner
    with db.market_db("CN") as c:
        day = db.get_meta(c, "data_asof")
        c.execute("INSERT OR REPLACE INTO market_breadth(date,n,b20) VALUES(?,?,?)", (day, 1000, 0.5))
        c.commit()
    calm = scanner.run_scan("CN", persist=False)["summary"]["panic"]
    assert calm["active"] is False and calm["rows"] == []
    with db.market_db("CN") as c:
        c.execute("UPDATE market_breadth SET b20=0.05 WHERE date=?", (day,))
        c.commit()
    p = scanner.run_scan("CN", persist=False)["summary"]["panic"]
    assert p["active"] is True and len(p["rows"]) <= p["total"]
    assert all({"symbol", "name", "close", "rsi14"} <= set(r) for r in p["rows"])


def test_panic_picks_archived_and_outcomes_backfilled(demo_env):
    """恐慌日清单要能事后检验：正式扫描触发时存档；之后按「次日开盘买入」回填 1/3/5/10 日收益，并出现在历史回看接口里。"""
    from fastapi.testclient import TestClient
    from server import main, scanner
    with db.market_db("CN") as c:
        days = [r[0] for r in c.execute("SELECT date FROM market_calendar WHERE is_open=1 ORDER BY date DESC LIMIT 15")]
        d0, sym = days[12], c.execute("SELECT symbol FROM daily_bar WHERE date=? AND trade_status=1 LIMIT 1", (days[12],)).fetchone()[0]
        c.execute("INSERT INTO panic_picks(date,symbol,name,close,b20) VALUES(?,?,?,?,?)", (d0, sym, "样例", 10.0, 0.1))
        c.commit()
    scanner.backfill_outcomes("CN")
    with db.market_db("CN") as c:
        r = c.execute("SELECT ret_1d, ret_5d, ret_10d, filled FROM panic_picks WHERE symbol=?", (sym,)).fetchone()
    assert r["filled"] == 1 and r["ret_1d"] is not None and r["ret_5d"] is not None
    hist = TestClient(main.app).get("/api/scan/history?market=CN").json()
    assert hist["panic"][0]["date"] == d0 and hist["panic_overall"]["n"] == 1 and hist["panic_overall"]["d5"]["days"] >= 0


def test_us_panic_day_is_triggered_by_vix(us_env):
    """美股恐慌日看 VIX（>= 25），不看温度；A 股看温度。"""
    from server import scanner
    with db.market_db("US") as c:
        day = db.get_meta(c, "data_asof")
        c.execute("INSERT OR REPLACE INTO market_breadth(date,n,b20) VALUES(?,?,?)", (day, 1000, 0.05))      # 温度再低也不算
        c.execute("INSERT OR REPLACE INTO index_bar(symbol,date,close) VALUES('^VIX',?,?)", (day, 18.0))
        c.commit()
    calm = scanner.run_scan("US", persist=False)["summary"]["panic"]
    assert calm["active"] is False and calm["trigger"] == "vix" and calm["vix"] == 18.0
    with db.market_db("US") as c:
        c.execute("UPDATE index_bar SET close=31 WHERE symbol='^VIX' AND date=?", (day,))
        c.commit()
    p = scanner.run_scan("US", persist=False)["summary"]["panic"]
    assert p["active"] is True and p["vix"] == 31.0 and len(p["rows"]) <= p["total"]
