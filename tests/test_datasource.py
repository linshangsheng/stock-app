"""字段映射层单测：不联网，用伪造的 BaoStock 返回验证单位 / 复权因子 / 增量模式的请求数。"""
import pandas as pd
import pytest

from server import db, ingest
from server.datasource_cn import BaoStockSource, attach_adj_factor, board_of


class FakeBS(BaoStockSource):
    def __init__(self, kline: pd.DataFrame, factors: pd.DataFrame):
        super().__init__()
        self.kline, self.factors, self.calls = kline, factors, []

    def _query(self, fn_name, *a, **k):
        self.calls.append(fn_name)
        return self.kline if fn_name == "query_history_k_data_plus" else self.factors


def kline(rows):
    cols = ["date", "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "tradeStatus", "isST"]
    return pd.DataFrame([[str(x) for x in r] for r in rows], columns=cols)


FACTORS = pd.DataFrame({"dividOperateDate": ["2026-09-03"], "foreAdjustFactor": ["0.9"], "backAdjustFactor": ["1.1"], "adjustFactor": ["1.1"]})


def test_board_of():
    assert board_of("sh.600519") == "main" and board_of("sz.300750") == "chinext" and board_of("sh.688981") == "star"
    assert board_of("sh.000300") is None and board_of("sz.399001") is None and board_of("bj.830799") is None


def test_adj_factor_is_step_function_with_base_one():
    ev = pd.DataFrame({"date": ["2026-01-10", "2026-06-10"], "factor": [2.0, 2.5]})
    f = attach_adj_factor(pd.Series(["2026-01-05", "2026-01-10", "2026-03-01", "2026-06-10", "2026-09-01"]), ev)
    assert list(f) == [1.0, 2.0, 2.0, 2.5, 2.5]


def test_full_mode_uses_two_requests_and_keeps_units():
    k = kline([("2026-09-01", 10, 10.5, 9.9, 10.2, 10.0, 123456, 1250000.5, 0.5, 1, 0),
               ("2026-09-02", 10.2, 10.6, 10.1, 10.5, 10.2, 200000, 2100000, 0.7, 1, 0)])
    src = FakeBS(k, FACTORS)
    out = src.daily_bars("sh.600001", "2026-09-01", "2026-09-02")
    assert src.calls == ["query_history_k_data_plus", "query_adjust_factor"]
    assert out["volume"].iloc[0] == 123456 and out["amount"].iloc[0] == 1250000.5          # volume=股、amount=元，不做换算
    assert list(out["adj_factor"]) == [1.0, 1.0]                                          # 除权日 2026-09-03 之后才生效


def test_incremental_mode_one_request_when_no_ex_rights():
    k = kline([("2026-09-02", 10.2, 10.6, 10.1, 10.5, 10.2, 200000, 2100000, 0.7, 1, 0),
               ("2026-09-03", 10.5, 10.9, 10.4, 10.8, 10.5, 210000, 2200000, 0.7, 1, 0)])
    src = FakeBS(k, FACTORS)
    out = src.daily_bars("sh.600001", "2026-09-02", "2026-09-03", prev_close=10.2, prev_factor=1.3)
    assert src.calls == ["query_history_k_data_plus"], "增量模式无除权时只需 1 次请求"
    assert list(out["adj_factor"]) == [1.3, 1.3]


def test_incremental_mode_fetches_factors_on_ex_rights_day():
    # 9-03 除权：前收盘（已按除权调整）9.5 与库内前一日收盘 10.5 对不上
    k = kline([("2026-09-03", 9.6, 9.9, 9.5, 9.8, 9.5, 210000, 2200000, 0.7, 1, 0)])
    src = FakeBS(k, FACTORS)
    out = src.daily_bars("sh.600001", "2026-09-03", "2026-09-03", prev_close=10.5, prev_factor=1.0)
    assert src.calls == ["query_history_k_data_plus", "query_adjust_factor"]
    assert out["adj_factor"].iloc[0] == pytest.approx(1.1)


def test_snapshot_rows_are_marked_temp_and_overwritten_by_reconcile(demo_env):
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        sym = c.execute("SELECT symbol FROM securities WHERE status='active' LIMIT 1").fetchone()[0]
        last = c.execute("SELECT MAX(date) FROM daily_bar WHERE symbol=?", (sym,)).fetchone()[0]
        real = c.execute("SELECT close FROM daily_bar WHERE symbol=? AND date=?", (sym, last)).fetchone()[0]
        c.execute("DELETE FROM daily_bar WHERE symbol=? AND date=?", (sym, last))
        snap = pd.DataFrame([{"symbol": sym, "name": "x", "open": real, "high": real, "low": real, "close": real * 1.02,
                              "volume": 1000, "amount": 1e6, "turnover": 1, "float_mktcap": 1e10, "pct": 2}])
        ingest.append_snapshot(c, snap, last)
        assert c.execute("SELECT is_temp FROM daily_bar WHERE symbol=? AND date=?", (sym, last)).fetchone()[0] == 1
        r = ingest.reconcile_temp(c, src)
        assert r["checked"] == 1 and r["diff"] == 1 and r["diff_rate"] == 1.0         # 收盘价偏差 2% > 0.5%：记差异
        row = c.execute("SELECT close, is_temp FROM daily_bar WHERE symbol=? AND date=?", (sym, last)).fetchone()
        assert row["is_temp"] == 0 and row["close"] == pytest.approx(real)             # 次日用历史接口覆盖临时数据


def test_prefilter_l1_excludes_junk_and_history_only_for_survivors(fresh_env, monkeypatch):
    """无快照时的 L1 预筛：先拉近 45 天粗筛，再只对幸存者拉 10 年历史（3.26.2）；已退市一律保留；只改标记不删数据。"""
    from datetime import date, timedelta
    from server import market_calendar as mc, settings
    from server.datasource_cn import get_source
    src = get_source()
    th = settings.cfg()["universe"]["cn"]["l1"]
    monkeypatch.setitem(th, "min_price", 30.0)                       # 抬高价格门槛，让演示数据里有足够多被剔除的股票
    with db.market_db("CN") as c:
        ingest.ensure_calendar(c, src)
        ingest.refresh_securities(c, src)
        last = mc.last_closed_trading_day(c, "CN")
        start = (date.fromisoformat(last) - timedelta(days=45)).isoformat()
        expect_out = set()
        for (sym,) in c.execute("SELECT symbol FROM securities").fetchall():
            b = src.daily_bars(sym, start, last)
            live = b[b["trade_status"] > 0]
            if len(live) and (live["close"].iloc[-1] < th["min_price"] or live["amount"].tail(20).mean() < th["min_avg_amount20"] * 0.5):
                expect_out.add(sym)
        assert expect_out, "演示数据里应有被预筛剔除的低价 / 低成交额股票，否则测试没有意义"
        st = ingest.prefilter_l1(c, src)
        out = {r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=0")}
        assert out == expect_out and st["excluded"] == len(expect_out)
        ingest.init_history(c, src)
        have = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM daily_bar")}
        assert not (have & expect_out), "被预筛剔除的股票不应拉取 10 年历史"
        # 无快照的 L1 再应用不能把垃圾股翻回来
        from server import universe
        universe.apply_l1(c, None)
        assert {r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=0")} == expect_out


def test_l1_bias_experiment_runs_on_copy_and_reports(fresh_env, monkeypatch):
    """L1 快照偏差对比实验：在副本上抽样补拉被剔除股票的历史，比较含 / 不含的回测差异；正式库不被改动。"""
    from server import l1_bias, settings
    from server.datasource_cn import get_source
    src = get_source()
    monkeypatch.setitem(settings.cfg()["universe"]["cn"]["l1"], "min_price", 30.0)
    with db.market_db("CN") as c:
        ingest.ensure_calendar(c, src)
        ingest.refresh_securities(c, src)
        ingest.refresh_industry(c, src)
        from server import universe
        universe.apply_l1(c)
        ingest.prefilter_l1(c, src)
        ingest.init_history(c, src)
        ingest.refresh_indices(c, src)
        db.set_meta(c, "data_asof", c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0])
        excluded = c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=0").fetchone()[0]
        bars_before = c.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    assert excluded > 10
    rep = l1_bias.run("CN", sample=15, seed=3, start="2022-01-01")
    assert rep["sampled"] == 15 and rep["history_fetched"] == 15 and rep["with_extra"]["n"] >= rep["base"]["n"] * 0.5
    assert rep["verdict"] and isinstance(rep["material"], bool)
    with db.market_db("CN") as c:
        assert c.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] == bars_before, "实验不得改动正式库"
        assert c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=0").fetchone()[0] == excluded
    assert l1_bias.last_report("CN")["run_at"] == rep["run_at"]


def test_industry_history_snapshots_are_effective_dated(fresh_env):
    """行业映射只有当前快照 -> 从现在起累积带生效日期的快照；点时查询不得用「今天的」回看更早的日子（3.14）。"""
    with db.market_db("CN") as c:
        assert ingest.record_industry(c, "sh.600001", "银行", None, "2026-01-05") is True
        assert ingest.record_industry(c, "sh.600001", "银行", None, "2026-02-05") is False          # 未变化：不追加
        assert ingest.record_industry(c, "sh.600001", "非银金融", None, "2026-03-05") is True       # 行业调整：追加
        assert ingest.industry_asof(c, "sh.600001", "2026-01-20") == "银行"
        assert ingest.industry_asof(c, "sh.600001", "2026-03-10") == "非银金融"
        assert ingest.industry_asof(c, "sh.600001", "2025-12-31") is None, "快照之前的日期不能用后来的行业回填"
        st = ingest.industry_history_stats(c)
        assert st["rows"] == 2 and st["symbols_with_changes"] == 1
        assert c.execute("SELECT industry FROM industry_map WHERE symbol='sh.600001'").fetchone()[0] == "非银金融"
