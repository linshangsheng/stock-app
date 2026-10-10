"""初始化提速：美股筛选器快照 / 行业映射解析、A 股初始化少发请求（离线，不联网）。"""
import pandas as pd

from server import db, ingest, universe
from server.datasource_us import parse_screen_quotes


def test_parse_screen_quotes():
    q = [{"symbol": "AAPL", "regularMarketPrice": 200.0, "averageDailyVolume3Month": 5e7, "marketCap": 3e12},
         {"symbol": "XYZ", "regularMarketPrice": 5.0, "averageDailyVolume3Month": 2e5},                      # 无市值
         {"symbol": "BAD", "regularMarketPrice": None, "averageDailyVolume3Month": 1e6},                     # 缺价格：丢弃
         {"symbol": "AAPL", "regularMarketPrice": 200.0, "averageDailyVolume3Month": 5e7, "marketCap": 3e12}]  # 重复
    df = parse_screen_quotes(q)
    assert list(df["symbol"]) == ["AAPL", "XYZ"]
    assert df.loc[0, "amount"] == 200.0 * 5e7 and pd.isna(df.loc[1, "float_mktcap"])


def test_apply_l1_exclude_missing(us_env):
    with db.market_db("US") as c:
        syms = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE status!='delisted' ORDER BY symbol LIMIT 3")]
        snap = pd.DataFrame({"symbol": syms[:2], "close": [50.0, 50.0], "amount": [5e8, 5e8], "float_mktcap": [5e9, 5e9]})
        st = universe.apply_l1(c, snap, market="US", exclude_missing=True)
        kept = {r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted'")}
        assert kept == set(syms[:2]) and st["not_in_snapshot"] >= 1


def test_cn_init_reuses_adj_events(monkeypatch, demo_env):
    """初始化：在市股票不再请求 security_basic，复权因子事件不重复请求。"""
    from server.datasource_cn import BaoStockSource
    calls = {"basic": 0, "events": 0}
    src = BaoStockSource.__new__(BaoStockSource)

    def basic(sym):
        calls["basic"] += 1
        return {}

    def ev(sym, a, b):
        calls["events"] += 1
        return pd.DataFrame({"date": ["2020-01-02"], "factor": [1.0]})

    def q(fn, *a, **k):
        return pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "open": ["1", "1"], "high": ["1", "1"], "low": ["1", "1"],
                             "close": ["1", "1"], "preclose": ["1", "1"], "volume": ["1", "1"], "amount": ["1", "1"], "turn": ["1", "1"],
                             "tradeStatus": ["1", "1"], "isST": ["0", "0"]})

    src._query = q
    src.security_basic = basic
    src.adj_factor_events = ev
    bars = src.daily_bars("sh.600000", "2024-01-01", "2024-01-31")
    assert calls["events"] == 1 and bars.attrs["adj_events"] is not None
