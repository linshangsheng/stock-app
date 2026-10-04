"""股票池与完整性闸门（3.25.1 / 3.26.1）。"""
import pandas as pd

from server import db, market_calendar as mc, quality, universe
from server.panel import load_panel
from tests.helpers import bar, flat, make_market


def test_limit_pct_rules_by_board_date_and_st():
    dates = pd.Index(["2020-01-02", "2020-08-21", "2020-08-24", "2024-01-02"])
    boards = pd.Series({"a": "main", "b": "chinext", "c": "star"})
    st = pd.DataFrame(0, index=dates, columns=["a", "b", "c"])
    st.loc["2024-01-02", "a"] = 1
    m = universe.limit_pct_matrix(dates, pd.Index(["a", "b", "c"]), boards, st)
    assert m.loc["2020-01-02", "b"] == 0.10 and m.loc["2020-08-24", "b"] == 0.20      # 创业板 2020-08-24 起 20%
    assert m.loc["2020-01-02", "a"] == 0.10 and m.loc["2024-01-02", "a"] == 0.05      # 主板 ST 5%
    assert m.loc["2024-01-02", "c"] == 0.20


def test_oneword_limit_detection(fresh_env):
    bars = flat(60) + [bar(11, h=11, l=11, c=11, vol=500), bar(11.5, h=12, l=11.4, c=11.8)]
    make_market({"sh.600001": bars})
    with db.market_db("CN") as c:
        p = load_panel(c, float32=False)
    f = universe.limit_flags(p, pd.Series({"sh.600001": "main"}))
    assert bool(f["oneword"]["sh.600001"].iloc[60]) and bool(f["limit_up"]["sh.600001"].iloc[60])
    assert not bool(f["oneword"]["sh.600001"].iloc[61])


def test_l2_excludes_st_price_and_illiquid(fresh_env):
    n = 300
    make_market({"sh.600001": flat(n, 10.0, vol=2e7),                       # 正常：成交额 2 亿
                 "sh.600002": flat(n, 10.0, vol=2e7, st=1),                 # ST
                 "sh.600003": flat(n, 2.0, vol=2e8),                        # 低价 < 3 元
                 "sh.600004": flat(n, 10.0, vol=1e5)})                      # 成交额 < 1 亿
    with db.market_db("CN") as c:
        p = load_panel(c, float32=False)
        boards, ld, _ = universe.load_security_meta(c)
    m = universe.l2_mask(p, boards, ld)
    last = m.iloc[-1]
    assert last["sh.600001"] and not last["sh.600002"] and not last["sh.600003"] and not last["sh.600004"]


def test_gate_passes_on_clean_demo_data(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        g = quality.check_gate(c, "CN", asof)
    assert g["status"] == "PASS", g["reasons"]


def test_gate_fails_when_coverage_drops(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        syms = [r[0] for r in c.execute("SELECT symbol FROM daily_bar WHERE date=? LIMIT 30", (asof,))]
        c.executemany("DELETE FROM daily_bar WHERE symbol=? AND date=?", [(s, asof) for s in syms])
        g = quality.check_gate(c, "CN", asof)
    assert g["status"] == "INCOMPLETE" and any(not ch["ok"] and ch["name"] == "coverage" for ch in g["checks"])


def test_gate_fails_on_price_anomalies(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        syms = [r[0] for r in c.execute("SELECT symbol FROM daily_bar WHERE date=? AND trade_status=1 LIMIT 8", (asof,))]
        for s in syms:                                    # high < close：价格不合理
            c.execute("UPDATE daily_bar SET high=close*0.9 WHERE symbol=? AND date=?", (s, asof))
        g = quality.check_gate(c, "CN", asof)
    assert g["status"] == "INCOMPLETE" and set(syms) <= set(g["bad_symbols"])


def test_gate_fails_without_benchmark(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        c.execute("DELETE FROM index_bar WHERE date=? AND symbol='sh.000300'", (asof,))
        g = quality.check_gate(c, "CN", asof)
    assert g["status"] == "INCOMPLETE" and any(ch["name"] == "benchmark" and not ch["ok"] for ch in g["checks"])


def test_scan_refuses_official_archive_when_incomplete(demo_env):
    from server import scanner
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        c.execute("DELETE FROM index_bar WHERE date=?", (asof,))
    r = scanner.run_scan("CN", persist=True)
    assert r["official"] is False
    with db.market_db("CN") as c:
        assert c.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0] == 0, "INCOMPLETE 的运行不写入正式存档"
        assert c.execute("SELECT official FROM scan_runs").fetchone()[0] == 0


def test_calendar_uses_market_timezone(fresh_env):
    from datetime import datetime, timezone
    make_market({"sh.600001": flat(5)}, start="2024-01-01")
    with db.market_db("CN") as c:
        # 2024-01-03 06:30 UTC = 北京时间 14:30，A 股尚未收盘；07:30 UTC = 15:30 已收盘
        assert not mc.market_closed_at(c, "CN", "2024-01-03", datetime(2024, 1, 3, 6, 30, tzinfo=timezone.utc))
        assert mc.market_closed_at(c, "CN", "2024-01-03", datetime(2024, 1, 3, 7, 30, tzinfo=timezone.utc))
        assert mc.last_closed_trading_day(c, "CN", datetime(2024, 1, 3, 6, 30, tzinfo=timezone.utc)) == "2024-01-02"
