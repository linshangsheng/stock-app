"""美股（M5）：拆股还原口径、名单过滤、T+0、美股费用、日历、批量增量 + 拆股重新调整检测、独立参数。"""
import pandas as pd
import pytest

from server import backtest, db, execution, ingest, scanner, settings, universe
from server.datasource_us import UsSource, get_us_source, parse_nasdaq_lists, standardize_yf
from server.panel import load_panel
from tests.helpers import bar, flat, make_market, weekdays

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust|G|N|N|100|Y|N
ZTST|Test Co|Q|Y|N|100|N|N
XYZW|XYZ Corp - Warrant|S|N|N|100|N|N
SPAK|Foo Acquisition Corp. - Class A Ordinary Shares|S|N|N|100|N|N
BADC|Bad Co|Q|N|D|100|N|N
File Creation Time: 1003202612:00||||||
"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|100|N|BRK.B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
ABC$D|ABC Preferred Series D|N|ABC$D|N|100|N|ABC$D
GME|GameStop Corp. Common Stock|N|GME|N|100|N|GME
File Creation Time: 1003202612:00||||||
"""


# ---- 纯函数 / 解析 ----------------------------------------------------------

def test_parse_nasdaq_lists_filters_non_common_stock():
    df = parse_nasdaq_lists(NASDAQ, OTHER)
    assert set(df["symbol"]) == {"AAPL", "BRK-B", "GME"}, "剔除 ETF / 测试标的 / 权证 / SPAC / 优先股 / 异常财务状态，并把 BRK.B 转为 BRK-B"


def test_standardize_yf_amount_is_adjusted_price_times_adjusted_volume():
    idx = pd.to_datetime(["2024-06-07", "2024-06-10", "2024-06-11"])
    raw = pd.DataFrame({"Open": [120, 121, 120], "High": [122, 123, 122], "Low": [119, 120, 119], "Close": [120.9, 121.8, 120.9],
                        "Adj Close": [120.5, 121.4, 120.5], "Volume": [4e8, 3e8, float("nan")], "Dividends": [0, 0, 0],
                        "Stock Splits": [0, 10, 0]}, index=idx)
    out = standardize_yf(raw)
    assert out["amount"].iloc[1] == pytest.approx(121.8 * 3e8) and out["split"].iloc[1] == 10 and out["volume"].iloc[2] == 0
    assert (out["adj_factor"] == 1.0).all() and list(out.columns[-3:]) == ["adj_close", "dividend", "split"]


def test_us_calendar_holidays_and_early_close():
    rows = {d: (o, h) for d, o, h in UsSource().trade_calendar("2026-04-01", "2026-12-31")}
    assert rows["2026-07-03"][0] == 0           # 独立日（7/4 周六）休市顺延到周五
    assert rows["2026-04-03"][0] == 0           # 耶稣受难日
    assert rows["2026-11-27"] == (1, 1)         # 感恩节次日：提前收盘
    assert rows["2026-11-26"][0] == 0


def test_us_fees_and_lots():
    assert execution.trade_fees("buy", 100.0, 1000, market="US") == 0.0
    assert execution.trade_fees("sell", 100.0, 1000, market="US") > 0
    big = execution.trade_fees("sell", 100.0, 100_000, market="US")
    cc = settings.cfg()["costs"]["us"]
    assert big == pytest.approx(100.0 * 100_000 * cc["sec_fee_sell"] + cc["finra_taf_cap"]), "FINRA TAF 单笔封顶"
    assert execution.lot_round("us", 1234) == 1234 and execution.min_lot("us") == 1


def test_market_configs_are_independent():
    with settings.market_ctx("US"):
        assert settings.cfg()["execution"]["slippage"] == 0.0005 and settings.cfg()["regime"]["benchmark"] == "SPY"
    assert settings.cfg()["execution"]["slippage"] == 0.001 and settings.cfg()["regime"]["benchmark"] == "sh.000300"


# ---- 拆股还原 / L2 价格 / T+0（构造数据）-------------------------------------

def _make_us(bars_by_symbol, splits=()):
    """直接写 us.db：日历 + 证券 + 日线（拆股调整口径）+ 拆股事件。"""
    n = max(len(v) for v in bars_by_symbol.values())
    dates = weekdays("2024-01-01", n)
    with db.market_db("US") as c:
        c.executemany("INSERT OR REPLACE INTO market_calendar(date,is_open,is_half_day) VALUES(?,1,0)", [(d,) for d in dates])
        for sym, bars in bars_by_symbol.items():
            c.execute("INSERT OR REPLACE INTO securities(symbol,name,board,list_date,status,in_l1,sec_type) VALUES(?,?,?,?,?,1,'stock')",
                      (sym, sym, "us", None, "active"))
            for d, b in zip(dates, bars):
                c.execute("INSERT OR REPLACE INTO daily_bar(symbol,date,open,high,low,close,volume,amount,turnover,adj_factor,trade_status,"
                          "is_st,source,is_temp) VALUES(?,?,?,?,?,?,?,?,?,1.0,1,0,'t',0)",
                          (sym, d, b["open"], b["high"], b["low"], b["close"], b["volume"], b["amount"], None))
        for sym, idx, ratio in splits:
            c.execute("INSERT INTO corp_actions(symbol,ex_date,type,ratio_or_amount) VALUES(?,?,'split',?)", (sym, dates[idx], ratio))
        db.set_meta(c, "data_asof", dates[-1])
    return dates


def test_split_restoration_and_l2_price_uses_real_price(fresh_env):
    n = 300
    # 存储价（拆股调整）恒为 3 美元；第 200 日发生 4 拆 1：拆股前真实价 = 3 × 4 = 12，拆股后 = 3
    _make_us({"AAA": flat(n, 3.0, vol=3e7)}, splits=[("AAA", 200, 4.0)])
    with db.market_db("US") as c:
        p = load_panel(c, market="US", float32=False)
        boards, ld, _ = universe.load_security_meta(c)
        assert p.raw["close"]["AAA"].iloc[100] == pytest.approx(12.0) and p.raw["close"]["AAA"].iloc[250] == pytest.approx(3.0)
        assert p.adj["close"]["AAA"].iloc[100] == pytest.approx(3.0), "特征用拆股调整价（序列连续）"
        m = universe.l2_mask(p, boards, ld, market="US")
    assert bool(m["AAA"].iloc[199]) and not bool(m["AAA"].iloc[250]), "股价门槛（≥$5）用还原后的当时价：拆股前 12 美元通过，拆股后 3 美元不通过"


def test_us_stop_active_on_entry_day_but_not_in_cn(fresh_env):
    bars = flat(80) + [bar(10, h=10.1, l=8.5, c=9.6), bar(9.7, h=9.8, l=9.5, c=9.7)] + flat(3, 9.7)
    _make_us({"AAA": bars})
    make_market({"sh.600001": bars})
    with db.market_db("US") as c:
        us = backtest.BtContext(c, "US")
    with db.market_db("CN") as c:
        cn = backtest.BtContext(c, "CN")
    ex = {"exits": {"exit_below_ma": 0, "trail": "none", "max_hold_days": 50}}
    p_us = us.trade_path(0, 80, 10.0, 9.0, backtest.merge_strategy(ex))
    p_cn = cn.trade_path(0, 80, 10.0, 9.0, backtest.merge_strategy(ex))
    assert p_us["exit_t"] == 80 and "止损" in p_us["reason"], "美股 T+0：买入当日盘中触及止损即刻生效"
    assert p_cn["exit_t"] is None or p_cn["exit_t"] > 80, "A 股 T+1：买入日止损不生效"


# ---- 完整流程（演示数据）----------------------------------------------------

def test_us_pipeline_gate_scan_backtest(us_env):
    from server import quality
    with db.market_db("US") as c:
        asof = db.get_meta(c, "data_asof")
        assert c.execute("SELECT COUNT(*) FROM corp_actions WHERE type='split'").fetchone()[0] > 0
        assert {r[0] for r in c.execute("SELECT DISTINCT board FROM securities")} == {"us"}
        g = quality.check_gate(c, "US", asof)
        assert g["status"] == "PASS", g["reasons"]
        assert any(ch["name"] == "benchmark" and ch["value"] == 7 for ch in g["checks"]), "美股基准含 SPY / QQQ / IWM / DIA / ^GSPC / ^IXIC / ^VIX"
    r = scanner.run_scan("US")
    assert r["official"] and r["market"] == "US" and r["run_id"].startswith("US-")
    with db.market_db("US") as c:
        ctx = backtest.BtContext(c, "US", "2022-01-01", None)
    res = ctx.run()
    assert res["metrics"]["n"] > 50 and any("幸存者偏差" in n for n in res["notes"])


def test_batch_incremental_detects_resplit_and_resyncs(us_env):
    src = get_us_source()
    with db.market_db("US") as c:
        sym = c.execute("SELECT symbol FROM securities WHERE status='active' LIMIT 1").fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM daily_bar WHERE symbol=?", (sym,)).fetchone()[0]
        prev = c.execute("SELECT date FROM daily_bar WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1", (sym, last)).fetchone()[0]
        real = c.execute("SELECT close FROM daily_bar WHERE symbol=? AND date=?", (sym, prev)).fetchone()[0]
        # 模拟：上游发生拆股后把整段历史重新调整 —— 库里旧行（含前一日）仍是旧口径，与上游对不上
        c.execute("UPDATE daily_bar SET close=close*2, open=open*2, high=high*2, low=low*2 WHERE symbol=? AND date<=?", (sym, prev))
        c.execute("DELETE FROM daily_bar WHERE symbol=? AND date=?", (sym, last))
        out = ingest.update_incremental(c, src, "US", last)
        assert out["mode"] == "batch" and out["resynced_after_split"] >= 1
        fixed = c.execute("SELECT close FROM daily_bar WHERE symbol=? AND date=?", (sym, prev)).fetchone()[0]
        assert fixed == pytest.approx(real), "检测到重叠日收盘价对不上：整只重拉历史并覆盖"


def test_us_earnings_calendar_on_demand(us_env):
    from datetime import date
    src = get_us_source()
    with db.market_db("US") as c:
        syms = [r[0] for r in c.execute("SELECT symbol FROM securities LIMIT 3")]
        r = ingest.ensure_earnings(c, src, syms, today=date(2026, 10, 3), market="US")
        assert r["ok"] == 3
        ev = c.execute("SELECT source, COUNT(*) n FROM events WHERE symbol=? GROUP BY source", (syms[0],)).fetchall()
        assert {e["source"] for e in ev} == {"yfinance", "proj"}
        assert ingest.ensure_earnings(c, src, syms, today=date(2026, 10, 3), market="US")["cached"] == 3


def test_us_api_endpoints(us_env):
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient
    from server import main
    cl = TestClient(main.app)
    assert cl.post("/api/scan/run", params={"market": "US"}).status_code == 200
    got = cl.get("/api/scan", params={"market": "US"}).json()
    assert got["run"]["market"] == "US" and got["run"]["official"]
    items = cl.get("/api/search", params={"q": "Demo Corp A", "market": "US"}).json()["items"]
    assert items and items[0]["code"] == items[0]["symbol"]
    k = cl.get("/api/kline", params={"symbol": items[0]["symbol"], "market": "US"}).json()
    assert len(k["bars"]) > 200 and k["board"] == "us"
    assert cl.get("/api/universe", params={"market": "US"}).json()["l2"] > 50
    assert cl.put("/api/account", params={"market": "US"}, json={"equity": 100000, "cash": 100000, "risk_per_trade": 0.005}).json()["account"]["equity"] == 100000
    assert cl.put("/api/settings", params={"market": "US"}, json={"markets": {"US": {"execution": {"slippage": 0.0007}}}}).status_code == 200
    with settings.market_ctx("US"):
        assert settings.cfg()["execution"]["slippage"] == 0.0007
    assert settings.cfg()["execution"]["slippage"] == 0.001, "改美股参数不影响 A 股"
