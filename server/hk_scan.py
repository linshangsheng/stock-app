"""港股选股（港股第 4 步）：个股观察清单，和 A 股 / 美股共用同一套漏斗（server/scanner.py），这里只放港股独有的部分。

流程（和 A 股一样）：股票池（成交额前 800 → 价格 / 成交额 / 停牌过滤）→ 市场环境闸门（恒生指数 + 站上 50 日线的股票比例）→ 形态（默认只开「波动收缩突破」）
→ 打分（低换手 + 低波动优先）→ 观察清单：明天的触发价 / 止损价 / 止损距离 / 每手股数。

港股和 A 股不一样的地方，这里逐条处理：
  * T+0、没有涨跌停：一字板、涨停买不到这类规则不适用（markets.py）；
  * 报价必须落在港交所的价位格子上（400 港元的股票没有 400.01 这个价）：触发价向上、止损价向下取整到价位（execution.snap）；
  * 每手股数因股票而异（腾讯 100、汇丰 400、小米 200）：只有东方财富查得到，而且一次一只、很慢。扫描时只给排名靠前的候选查（查到的存进缓存），
    查不到就老实标「每手股数没查到」，股数不按整手取整——不猜；
  * 没有行业数据：行业强弱不参与打分，「同行业最多持有几只」不起作用；
  * 没有财报日历数据源：不做「财报前后回避」，界面上标明；
  * 交易日历：用港交所日历（exchange_calendars XHKG），再用行情数据校正（指数 / 个股有行情的日子一定是交易日，台风、黑色暴雨导致休市的日子日历不知道）。

重要：港股还没有回测。形态参数、止损倍数、风险参数都是沿用 A 股 / 美股的默认值，没有在港股上校准过，所以这份清单没有历史证据支持，界面上写明「证据强度：无」。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta

import pandas as pd

from . import db, execution, hk_stocks, market_calendar as mc, settings

log = logging.getLogger(__name__)
LOT_TOP = 30                       # 只给得分最高的这么多只候选查每手股数（清单最多 15 只）
LOT_BUDGET_S = 40                  # 查每手股数最多花这么多秒；连续 2 只查不到（数据源被限流）就不再试
MIN_STOCKS_PER_DAY = 30            # 一天里至少这么多只股票有行情，才算「个股行情证明这天开市」


def ensure_calendar(conn) -> dict:
    """港股交易日历：港交所日历（exchange_calendars XHKG）+ 行情数据校正。数据覆盖的区间里，有行情的日子才是交易日（以行情为准）；
    数据覆盖之外（未来、更早）用港交所日历。返回 {source, days, diff}：diff = 日历和行情不一致的天数（日历说开市但没有行情，常见原因是台风 / 黑雨休市）。"""
    import exchange_calendars as xc
    years = int(settings.cfg()["history_years"])
    today = date.fromisoformat(mc.today_str("HK"))
    start, end = today - timedelta(days=365 * (years + 1)), today + timedelta(days=60)
    cal = xc.get_calendar("XHKG")
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    lo, hi = max(s, cal.first_session), min(e, cal.last_session)
    sessions = {d.date().isoformat() for d in cal.sessions_in_range(lo, hi)}
    half = {d.date().isoformat() for d in cal.early_closes if lo <= d <= hi}
    truth = {r[0] for r in conn.execute("SELECT date FROM index_bar WHERE symbol='HK.HSI'")}
    truth |= {r[0] for r in conn.execute("SELECT date FROM daily_bar WHERE symbol LIKE 'hk.%' GROUP BY date HAVING COUNT(*)>=?", (MIN_STOCKS_PER_DAY,))}
    t_lo, t_hi = (min(truth), max(truth)) if truth else (None, None)
    rows, diff, d = [], [], start
    while d <= end:
        iso = d.isoformat()
        cal_open = iso in sessions
        if t_lo and t_lo <= iso <= t_hi:
            is_open = iso in truth
            if cal_open and not is_open:
                diff.append(iso)
        else:
            is_open = cal_open
        rows.append((iso, 1 if is_open else 0, 1 if iso in half else 0))
        d += timedelta(days=1)
    mc.store_calendar(conn, rows)
    db.set_meta(conn, "calendar_source", "xhkg+data")
    conn.commit()
    return {"source": "xhkg+data", "days": sum(r[1] for r in rows), "diff": diff[-10:], "n_diff": len(diff)}


def data_day(conn) -> str | None:
    """行情最新的交易日：个股行情齐全（至少池里 90% 的股票有这一天）的最后一天。今天收盘了但个股日线还没下载，就是上一个交易日。"""
    pool = conn.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1 AND symbol LIKE 'hk.%'").fetchone()[0]
    if not pool:
        return None
    last = conn.execute("SELECT MAX(date) FROM daily_bar WHERE symbol LIKE 'hk.%'").fetchone()[0]
    if not last:
        return None
    r = conn.execute("SELECT b.date FROM daily_bar b JOIN securities s ON s.symbol=b.symbol WHERE s.in_l1=1 AND b.symbol LIKE 'hk.%' AND b.date>=date(?, '-30 day') "
                     "GROUP BY b.date HAVING COUNT(*)>=? ORDER BY b.date DESC LIMIT 1", (last, int(pool * 0.9))).fetchone()
    return r[0] if r else None


def attach_lots(conn, cand: pd.DataFrame, top: int = LOT_TOP, budget: float = LOT_BUDGET_S) -> pd.DataFrame:
    """给候选加上「板块」：查到每手股数的写成 hk400、没查到的写成 hk（execution.hk_lot 据此取整 / 不取整）。cand 已按得分从高到低排好。"""
    cand = cand.copy()
    cache = hk_stocks._lots(conn)
    t0, fails, boards = time.monotonic(), 0, []
    for i, sym in enumerate(cand["symbol"]):
        lot = cache.get(sym)
        if lot is None and i < top and fails < 2 and time.monotonic() - t0 < budget:
            lot = hk_stocks.lot_size(conn, sym, fast=True).get("lot")
            fails = 0 if lot else fails + 1
        boards.append(f"hk{int(lot)}" if lot else "hk")
    cand["board"] = boards
    return cand


_run_lock = threading.Lock()


def run(scan_date: str | None = None) -> dict:
    """运行一次港股选股（写入 scan_runs / scan_results，和 A 股 / 美股同一套）。个股行情库没初始化时返回 error；已经有一次在跑时不再开第二次。"""
    if not _run_lock.acquire(blocking=False):
        return {"error": "busy", "message": "港股正在扫描中，请稍等几秒再看。"}
    try:
        return _run(scan_date)
    finally:
        _run_lock.release()


def _run(scan_date: str | None) -> dict:
    from . import scanner
    with settings.market_ctx("HK"), db.market_db("HK") as c:
        if not hk_stocks.library_ready(c):
            return {"error": "no_library", "message": "港股个股行情库还没初始化（设置 → 数据更新 → 初始化港股个股行情库）。"}
        ensure_calendar(c)
        day = scan_date or data_day(c)
        if not day:
            return {"error": "no_data", "message": "港股个股日线还不全（需要池里 90% 以上的股票有最近一个交易日的行情），请先更新港股数据。"}
        if not scan_date:
            db.set_meta(c, "data_asof", day)
            c.commit()
    res = scanner.run_scan("HK", scan_date=day)
    if res.get("official"):
        try:                                                           # 和 A 股一样：回填以前清单的真实前向收益（T+1/3/5/10/20），历史回看用
            scanner.backfill_outcomes("HK")
        except Exception:  # noqa: BLE001 - 回填失败不影响清单
            log.exception("hk backfill failed")
    return res


def lot_of(board: str | None) -> int | None:
    """板块 hk400 → 400；hk（没查到）→ None。"""
    hl = execution.hk_lot(board)
    return hl if hl and hl > 1 else None
