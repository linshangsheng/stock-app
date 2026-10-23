"""行情页顶部的合成图：指数日 K 线 + 市场温度 + 恐慌指数，三层共用一条时间轴，用来看三者的波动关系。
A 股：创业板指 + 全 A 市场温度（站上 20 日线的比例）+ 恐慌指数 QVIX（沪深300ETF、创业板ETF 期权）；
美股：纳斯达克100（^NDX）+ 美股市场温度 + VIX。
所有序列按指数的交易日对齐（缺的日子为空），前端三张图就能按同一个位置同步缩放和十字光标。展示用，不进入选股与回测。"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from . import db, market_calendar as mc, market_view, settings


def _cfg(market: str) -> dict:
    c = (settings.cfg().get("market_view") or {}).get("combo") or {}
    return {"symbol": c.get("us_index", "^NDX"), "name": "纳斯达克100"} if market == "US" else {"symbol": c.get("cn_index", "sz.399006"), "name": "创业板指"}


def _bars(conn, sym: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT date, open, high, low, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(sym,)).set_index("date")


def _ensure_us_index(conn, sym: str) -> None:
    """^NDX 是新加进辅助指数的：库里还没有就先取一次 10 年历史（之后每天随指数日线一起更新）。"""
    if conn.execute("SELECT COUNT(*) FROM index_bar WHERE symbol=?", (sym,)).fetchone()[0] >= 250:
        return
    from .datasource_cn import get_source
    src = get_source("US")
    last = mc.last_closed_trading_day(conn, "US") or date.today().isoformat()          # 只取到已收盘的交易日，不存盘中半根 K 线
    start = (date.fromisoformat(last) - timedelta(days=int(365.25 * int(settings.cfg()["history_years"])))).isoformat()
    df = src.index_bars(sym, start, last)
    df = df[df["date"] <= last]
    conn.executemany("INSERT OR REPLACE INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
                     [(sym, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount) for r in df.itertuples()])
    conn.commit()


def _r(x, d=2):
    return None if x is None or x != x else round(float(x), d)


def build(market: str) -> dict:
    ix = _cfg(market)
    with settings.market_ctx(market), db.market_db(market) as c:
        if market == "US":
            try:
                _ensure_us_index(c, ix["symbol"])
            except Exception:  # noqa: BLE001 - 取不到 ^NDX：退回库里已有的 QQQ（纳斯达克100 ETF）
                pass
            if c.execute("SELECT COUNT(*) FROM index_bar WHERE symbol=?", (ix["symbol"],)).fetchone()[0] < 250:
                ix = {"symbol": "QQQ", "name": "纳斯达克100（QQQ）"}
        bars = _bars(c, ix["symbol"])
        closed = mc.last_closed_trading_day(c, market)
        if closed:
            bars = bars[bars.index <= closed]
        br = market_view.load_breadth(c)
        panic = []
        if market == "US":
            from . import vix
            v, _, _ = vix.load_series()
            panic.append({"key": "VIX", "name": "VIX", "series": v, "lines": [{"v": 20, "label": "20 略紧张"}, {"v": 30, "label": "30 恐慌"}]})
        else:
            from . import qvix
            ev = qvix.evidence().get("series") or {}
            for sym, spec in qvix.SERIES.items():
                s = qvix.load(c, sym)
                cuts = (ev.get(sym) or {}).get("cuts")
                lines = [{"v": cuts[1], "label": f"{cuts[1]:g} 略紧张"}, {"v": cuts[3], "label": f"{cuts[3]:g} 恐慌"}] if cuts else []
                panic.append({"key": sym, "name": f"{spec['name']}恐慌指数", "series": s, "lines": lines})
            if all(len(p["series"]) < 60 for p in panic):
                qvix.refresh_async()
    if len(bars) < 60:
        return {"status": "no_data", "message": f"{ix['name']}日线不足：请先初始化数据。"}
    bars = bars[bars.index >= (br.index[0] if len(br) else bars.index[0])]          # 从有市场温度的那天开始
    dates = list(bars.index)
    b20 = br["b20"].reindex(dates) if len(br) else pd.Series(index=dates, dtype=float)
    cuts = market_view.mv_cfg()["zones"]
    return {"status": "ok", "index": ix, "dates": dates,
            "candles": [[_r(r.open), _r(r.high), _r(r.low), _r(r.close)] for r in bars.itertuples()],
            "temp": [_r(x * 100, 1) if x == x else None for x in b20],
            "temp_lines": [{"v": cuts[0] * 100, "label": f"{cuts[0]:.0%} 冰点"}, {"v": cuts[-1] * 100, "label": f"{cuts[-1]:.0%} 过热"}],
            "panic": [{"key": p["key"], "name": p["name"], "lines": p["lines"],
                       "values": [_r(x) for x in p["series"].reindex(dates)]} for p in panic],
            "note": ("市场温度 = 全部股票里站在 20 日均线上方的比例（%）；恐慌指数 = 期权市场预期的未来 30 天年化波动率（%），"
                     + ("A 股用期权论坛的 QVIX（沪深300ETF 2019-12 起、创业板ETF 2022-09 起）。" if market == "CN" else "美股用 Cboe 的 VIX。")
                     + "三张图共用时间轴：拖动 / 缩放任意一张，其余跟着走；鼠标指到某一天，三张图同时显示那天的数值。")}
