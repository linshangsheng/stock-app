"""东财 push2 全市场快照：字段映射、分页、增量追加（临时数据）、时间保护、降级。"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from server import db, ingest, market_calendar as mc
from server.datasource_cn import EastmoneySource, parse_clist


def rows():
    return [{"f12": "600519", "f14": "贵州茅台", "f2": 1258.62, "f3": 1.86, "f5": 25000, "f6": 3.1e9, "f8": 0.2, "f15": 1260, "f16": 1240, "f17": 1245, "f18": 1235.58, "f21": 1.5e12},
            {"f12": "300750", "f14": "宁德时代", "f2": "-", "f3": "-", "f5": "-", "f6": "-", "f8": "-", "f15": "-", "f16": "-", "f17": "-", "f18": 286.8, "f21": 1.2e12},
            {"f12": "830799", "f14": "艾融软件", "f2": 20.0, "f3": 1, "f5": 100, "f6": 2e6, "f8": 1, "f15": 20, "f16": 19, "f17": 19.5, "f18": 19.8, "f21": 1e9},
            {"f12": "688981", "f14": "中芯国际", "f2": 100.0, "f3": 2, "f5": 1000, "f6": 1e8, "f8": 1, "f15": 101, "f16": 99, "f17": 99.5, "f18": 98, "f21": 5e11}]


def test_parse_clist_units_symbols_and_suspended():
    df = parse_clist(rows()).set_index("symbol")
    assert set(df.index) == {"sh.600519", "sz.300750", "sh.688981"}, "北交所（8 开头）不纳入"
    assert df.loc["sh.600519", "volume"] == 25000 * 100, "成交量由「手」换算为「股」"
    assert df.loc["sh.600519", "float_mktcap"] == 1.5e12 and df.loc["sh.600519", "amount"] == 3.1e9
    assert pd.isna(df.loc["sz.300750", "close"]) and df.loc["sz.300750", "prev_close"] == 286.8, "停牌股价格为空"


def test_snapshot_pagination_and_host_fallback(monkeypatch):
    src = EastmoneySource()
    calls = []
    pages = {1: {"total": 3, "diff": rows()[:2]}, 2: {"total": 3, "diff": [rows()[3]]}}

    def fake_page(host, pn):
        calls.append((host, pn))
        if host == EastmoneySource.HOSTS[0]:
            raise RuntimeError("blocked")                       # 首选节点不可用 -> 换备用节点
        return pages[pn]

    monkeypatch.setattr(src, "_page", fake_page)
    df = src.snapshot()
    assert len(df) == 3 and calls[-1][0] == EastmoneySource.HOSTS[1] and [c[1] for c in calls if c[0] == EastmoneySource.HOSTS[1]] == [1, 2]


def _drop_last_day(c):
    asof = db.get_meta(c, "data_asof")
    prev = c.execute("SELECT MAX(date) FROM daily_bar WHERE date<?", (asof,)).fetchone()[0]
    c.execute("DELETE FROM daily_bar WHERE date=?", (asof,))
    return asof, prev


def _snap_from_bars(c, day, scale=1.0):
    rows_ = c.execute("SELECT b.symbol, s.name, b.open, b.high, b.low, b.close, b.volume, b.amount FROM daily_bar b JOIN securities s ON s.symbol=b.symbol "
                      "WHERE b.date=(SELECT MAX(date) FROM daily_bar WHERE symbol=b.symbol)").fetchall()
    return pd.DataFrame([{"symbol": r[0], "name": r[1], "open": r[2], "high": r[3], "low": r[4], "close": r[5] * scale, "volume": r[6], "amount": r[7],
                          "turnover": 1.0, "float_mktcap": 1e10, "pct": 0.0} for r in rows_])


def test_incremental_uses_snapshot_marks_temp_then_reconcile_overwrites(demo_env, monkeypatch):
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        asof, prev = _drop_last_day(c)
        snap = _snap_from_bars(c, prev, scale=1.02)               # 快照 = 前一日 K 线 ×1.02（当作 asof 的收盘）
        monkeypatch.setattr(ingest, "snapshot_safe", lambda *a, **k: True)
        out = ingest.update_incremental(c, src, "CN", asof, snapshot_fn=lambda: snap)
        assert out["mode"] == "snapshot" and out["appended"] > 100
        assert c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND is_temp=1 AND source='eastmoney'", (asof,)).fetchone()[0] == out["appended"]
        r = ingest.reconcile_temp(c, src)                          # 次日用历史接口覆盖并对账
        assert r["checked"] > 100 and r["diff_rate"] > 0.5         # 快照被人为改动，偏差 > 0.5% 的应被记录
        assert c.execute("SELECT COUNT(*) FROM daily_bar WHERE is_temp=1").fetchone()[0] == 0


def test_incomplete_snapshot_falls_back_to_history(demo_env):
    from server.datasource_cn import get_source
    src = get_source()
    with db.market_db("CN") as c:
        asof, prev = _drop_last_day(c)
        small = _snap_from_bars(c, prev).head(5)                  # 快照明显不全：不能用
        import server.ingest as ig
        ig_snapshot_safe = ig.snapshot_safe
        ig.snapshot_safe = lambda *a, **k: True
        try:
            out = ingest.update_incremental(c, src, "CN", asof, snapshot_fn=lambda: small)
        finally:
            ig.snapshot_safe = ig_snapshot_safe
        assert out["mode"] == "history" and out["updated"] > 100


def test_snapshot_safe_blocks_once_new_session_started(demo_env):
    with db.market_db("CN") as c:
        asof = db.get_meta(c, "data_asof")
        nxt = mc.next_trading_day(c, asof)
        if nxt is None:
            c.execute("INSERT OR REPLACE INTO market_calendar(date,is_open,is_half_day) VALUES('2026-10-12',1,0)")
            nxt = "2026-10-12"
        y, m, d = map(int, nxt.split("-"))
        # 下一交易日 02:00 UTC = 北京时间 10:00：新交易日盘中，快照不再是上一日收盘数据
        mid = datetime(y, m, d, 2, 0, tzinfo=timezone.utc)
        import server.market_calendar as mcal
        orig = mcal.now_in_market
        mcal.now_in_market = lambda market, now=None: orig(market, mid)
        try:
            assert ingest.snapshot_safe(c, "CN", asof) is False
        finally:
            mcal.now_in_market = orig
