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
