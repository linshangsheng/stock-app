"""构造数据的小工具：单只股票的手工价格路径，用于成交规则 / 口径的单测（6.7 回测单测）。"""
from __future__ import annotations

from datetime import date, timedelta

from server import db


def weekdays(start: str, n: int) -> list[str]:
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def bar(o, h=None, l=None, c=None, vol=1_000_000, status=1, st=0, factor=1.0):
    c = o if c is None else c
    h = max(o, c) if h is None else h
    l = min(o, c) if l is None else l
    return dict(open=o, high=h, low=l, close=c, volume=vol, amount=vol * (o + c) / 2, turnover=1.0, adj_factor=factor,
                trade_status=status, is_st=st)


def make_market(bars_by_symbol: dict[str, list[dict]], start="2024-01-01", board="main"):
    """把 {symbol: [bar,...]} 写入当前数据目录的 ashare.db，并建好日历 / 证券主表。返回日期列表。"""
    n = max(len(v) for v in bars_by_symbol.values())
    dates = weekdays(start, n)
    with db.market_db("CN") as c:
        c.executemany("INSERT OR REPLACE INTO market_calendar(date,is_open,is_half_day) VALUES(?,1,0)", [(d,) for d in dates])
        for sym, bars in bars_by_symbol.items():
            c.execute("INSERT OR REPLACE INTO securities(symbol,name,board,list_date,status,in_l1,sec_type) VALUES(?,?,?,?,?,1,'stock')",
                      (sym, sym, board, "2000-01-01", "active"))
            for d, b in zip(dates, bars):
                c.execute("INSERT OR REPLACE INTO daily_bar(symbol,date,open,high,low,close,volume,amount,turnover,adj_factor,"
                          "trade_status,is_st,source,is_temp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                          (sym, d, b["open"], b["high"], b["low"], b["close"], b["volume"], b["amount"], b["turnover"],
                           b["adj_factor"], b["trade_status"], b["is_st"], "test"))
        db.set_meta(c, "data_asof", dates[-1])
    return dates


def flat(n, price=10.0, **kw):
    return [bar(price, **kw) for _ in range(n)]
