"""各市场交易日历与时区（3.26.5）。
交易日 / 是否收盘 / 缺失交易日一律按市场时区判断，不使用本机时区。
模块命名为 market_calendar 而非 calendar，避免遮蔽标准库。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKETS = {
    "CN": {"tz": "Asia/Shanghai", "close": time(15, 0)},
    "US": {"tz": "America/New_York", "close": time(16, 0), "half_close": time(13, 0)},
}


def now_in_market(market: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(MARKETS[market]["tz"])
    if now is None:
        return datetime.now(tz)
    return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)


def today_str(market: str, now: datetime | None = None) -> str:
    return now_in_market(market, now).date().isoformat()


def store_calendar(conn, rows: list[tuple[str, int, int]]) -> None:
    conn.executemany(
        "INSERT INTO market_calendar(date,is_open,is_half_day) VALUES(?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET is_open=excluded.is_open, is_half_day=excluded.is_half_day", rows)


def weekday_fallback(start: str, end: str) -> list[tuple[str, int, int]]:
    """无外部日历时的兜底：周一至周五视为交易日（节假日不识别，会在 meta 中标注来源 fallback）。"""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    d = d0
    while d <= d1:
        out.append((d.isoformat(), 1 if d.weekday() < 5 else 0, 0))
        d += timedelta(days=1)
    return out


def trading_days(conn, start: str | None = None, end: str | None = None) -> list[str]:
    q = "SELECT date FROM market_calendar WHERE is_open=1"
    args: list = []
    if start:
        q += " AND date>=?"
        args.append(start)
    if end:
        q += " AND date<=?"
        args.append(end)
    q += " ORDER BY date"
    return [r[0] for r in conn.execute(q, args).fetchall()]


def is_trading_day(conn, d: str) -> bool:
    r = conn.execute("SELECT is_open FROM market_calendar WHERE date=?", (d,)).fetchone()
    return bool(r and r[0])


def is_half_day(conn, d: str) -> bool:
    r = conn.execute("SELECT is_half_day FROM market_calendar WHERE date=?", (d,)).fetchone()
    return bool(r and r[0])


def market_closed_at(conn, market: str, d: str, now: datetime | None = None) -> bool:
    """交易日 d 在 now 时刻（按市场时区）是否已收盘。"""
    n = now_in_market(market, now)
    cfg = MARKETS[market]
    close_t = cfg.get("half_close", cfg["close"]) if (market == "US" and is_half_day(conn, d)) else cfg["close"]
    close_dt = datetime.combine(date.fromisoformat(d), close_t, tzinfo=n.tzinfo)
    return n >= close_dt


def last_closed_trading_day(conn, market: str, now: datetime | None = None) -> str | None:
    """最近一个「已收盘」的交易日（不含数据是否已可用的判断）。"""
    n = now_in_market(market, now)
    today = n.date().isoformat()
    days = trading_days(conn, None, today)
    for d in reversed(days):
        if market_closed_at(conn, market, d, now):
            return d
    return None


def missing_days(conn, last_have: str | None, upto: str) -> list[str]:
    """(last_have, upto] 之间应有而未取得的交易日。last_have 为空则返回空（首次初始化走全量流程）。"""
    if not last_have:
        return []
    return [d for d in trading_days(conn, last_have, upto) if d > last_have]


def next_trading_day(conn, d: str) -> str | None:
    r = conn.execute("SELECT date FROM market_calendar WHERE is_open=1 AND date>? ORDER BY date LIMIT 1",
                     (d,)).fetchone()
    return r[0] if r else None
