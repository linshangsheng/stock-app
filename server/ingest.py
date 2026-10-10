"""数据入库流程（3.26.2）：先过滤、再拉取、后增量。
  1) 证券名单（多个历史交易日并集，含已退市） -> L1 过滤
  2) 首次初始化：分批限速拉取 10 年日线 + 复权因子（fetch_state 断点续跑）
  3) 每个交易日收盘后增量；快照追加的当日 K 线标记 is_temp=1，次日由历史接口对账覆盖
全部写入由 INSERT OR REPLACE 保证幂等；只增不删。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd

from . import db, market_calendar as mc, markets, settings, universe
from .datasource_cn import BAR_COLS, get_source, EastmoneySource
from .throttle import CircuitOpen

ProgressCb = Callable[[str, int, int], None]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- 日历 ---------------------------------------------------------------------

def ensure_calendar(conn, src, market: str = "CN", years: int | None = None) -> str:
    years = years or int(settings.cfg()["history_years"])
    today = mc.today_str(market)
    start = (date.fromisoformat(today) - timedelta(days=365 * (years + 1))).isoformat()
    end = (date.fromisoformat(today) + timedelta(days=60)).isoformat()
    source = "source"
    try:
        rows = src.trade_calendar(start, end)
    except Exception:
        rows, source = [], "fallback"
    if not rows:
        rows, source = mc.weekday_fallback(start, end), "fallback"   # 节假日不识别，已在 meta 标注
    mc.store_calendar(conn, rows)
    db.set_meta(conn, "calendar_source", source)
    return source


# ---- 证券名单 -----------------------------------------------------------------

def _quarter_ends(start: date, end: date) -> list[date]:
    out = []
    y = start.year
    while y <= end.year:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, d)
            if start <= q <= end:
                out.append(q)
        y += 1
    return out


def refresh_securities(conn, src, market: str = "CN", years: int | None = None,
                       progress: ProgressCb | None = None, fast: bool = False) -> int:
    """证券列表：取多个历史交易日（半年一个，最近一个为最新收盘日）的并集，补回窗口期内已退市的 A 股（3.26.1-5）。
    BaoStock 的 query_all_stock 单次约 15~20 秒（M0 实测），因此每个日期查询后立即入库并记录进度（meta.sec_union_days），
    中断后只补未完成的日期；每周刷新只需查最新一天。"""
    if hasattr(src, "list_current_securities"):                 # 美股：交易所当前清单；历史上的退市股免费源补不回（幸存者偏差）
        return _refresh_securities_current(conn, src)
    years = years or int(settings.cfg()["history_years"])
    today = date.fromisoformat(mc.today_str(market))
    qs = _quarter_ends(today - timedelta(days=365 * years), today)
    qs = qs[::-1][::2][::-1]                                   # 半年一个（以最近的季末为锚）
    if fast:                                                   # 抽样 / 试用：只查最新交易日名单 1 次（不补已退市股票，省流量与时间）
        qs = []
    days = []
    for q in qs:                                               # 取该季末当日或之前最近的交易日
        td = mc.trading_days(conn, (q - timedelta(days=10)).isoformat(), q.isoformat())
        if td:
            days.append(td[-1])
    last = mc.last_closed_trading_day(conn, market)
    if last and last not in days:
        days.append(last)
    done = set(json.loads(db.get_meta(conn, "sec_union_days", "[]") or "[]"))
    n_new = 0
    current: set[str] | None = None
    todo = [d for d in days if d not in done or d == last]
    for i, d in enumerate(todo, 1):
        sec = src.list_securities_union([d])
        if d == last:
            current = set(sec["symbol"])
        for r in sec.itertuples():
            if conn.execute("SELECT 1 FROM securities WHERE symbol=?", (r.symbol,)).fetchone():
                conn.execute("UPDATE securities SET name=?, board=? WHERE symbol=?", (r.name, r.board, r.symbol))
            else:
                conn.execute("INSERT INTO securities(symbol,name,board,status,in_l1) VALUES(?,?,?,?,1)",
                             (r.symbol, r.name, r.board, "active"))
                n_new += 1
        done.add(d)
        db.set_meta(conn, "sec_union_days", json.dumps(sorted(done)))
        conn.commit()
        if progress:
            progress("securities_union", i, len(todo))
    # 仅出现在历史并集而不在最新列表里的，标记为已退市（精确的上市 / 退市日期由 security_basic 在拉取历史时补全）
    if current:
        conn.execute("UPDATE securities SET status='delisted' WHERE status='active' AND symbol NOT IN (%s)"
                     % ",".join("?" * len(current)), list(current))
        conn.execute("UPDATE securities SET status='active' WHERE status='delisted' AND delist_date IS NULL AND symbol IN (%s)"
                     % ",".join("?" * len(current)), list(current))
    conn.commit()
    return n_new


def _refresh_securities_current(conn, src) -> int:
    """美股名单：只增不删。清单里新出现的插入；已入库但不在当前清单里的标记 delisted（历史保留，3.26.1）。"""
    cur = src.list_current_securities()
    n_new = 0
    have = {r[0]: r[1] for r in conn.execute("SELECT symbol, status FROM securities")}
    for r in cur.itertuples():
        if r.symbol in have:
            conn.execute("UPDATE securities SET name=?, board='us', status='active' WHERE symbol=?", (r.name, r.symbol))
        else:
            conn.execute("INSERT INTO securities(symbol,name,board,status,in_l1,sec_type) VALUES(?,?,?,?,1,'stock')",
                         (r.symbol, r.name, "us", "active"))
            n_new += 1
    now_syms = set(cur["symbol"])
    for s_, st in have.items():
        if st == "active" and s_ not in now_syms:
            conn.execute("UPDATE securities SET status='delisted', delist_date=COALESCE(delist_date, "
                         "(SELECT MAX(date) FROM daily_bar WHERE symbol=?)) WHERE symbol=?", (s_, s_))
    db.set_meta(conn, "securities_refreshed", _now())
    conn.commit()
    return n_new


def choose_sample(conn, market: str, n: int, seed: int | None = None, resample: bool = False) -> list[str]:
    """初始化范围：从 L1 内的在市股票里随机抽 n 只，并把结果存进 meta（init_sample）。
    同样的 n 且未要求重新抽样时沿用已存的那一批——保证中断续跑、再次点击不会每次换一批、也不重复下载。"""
    import random as _r

    cur = db.get_meta(conn, "init_sample")
    if cur and not resample:
        d = json.loads(cur)
        if d.get("n") == n and d.get("symbols"):
            return d["symbols"]
    pool = [r[0] for r in conn.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted' AND sec_type='stock' ORDER BY symbol")]
    seed = seed if seed is not None else _r.SystemRandom().randrange(1, 10 ** 9)
    picked = sorted(_r.Random(seed).sample(pool, min(n, len(pool))))
    db.set_meta(conn, "init_sample", json.dumps({"n": n, "seed": seed, "symbols": picked, "pool": len(pool),
                                                 "at": _now()}, ensure_ascii=False))
    conn.commit()
    return picked


def clear_sample(conn) -> None:
    conn.execute("DELETE FROM meta WHERE key='init_sample'")
    conn.commit()


def refresh_industry_us(conn, src, limit: int | None = None, stop: Callable[[], bool] | None = None,
                        progress: ProgressCb | None = None, only: set[str] | None = None) -> int:
    """美股行业 / Sector（yfinance info，逐股接口，只对 L1 幸存者拉取，可断点续跑；当前快照，历史偏差同 3.14）。"""
    todo = [r[0] for r in conn.execute(
        "SELECT s.symbol FROM securities s LEFT JOIN industry_map m ON m.symbol=s.symbol "
        "WHERE s.in_l1=1 AND s.status!='delisted' AND m.symbol IS NULL ORDER BY s.symbol").fetchall()]
    if only is not None:
        todo = [x for x in todo if x in only]
    if limit:
        todo = todo[:limit]
    asof = date.today().isoformat()
    n = 0
    for i, sym in enumerate(todo, 1):
        if stop and stop():
            break
        try:
            ind, sec = src.industry(sym)
        except CircuitOpen:
            conn.commit()
            raise
        except Exception:  # noqa: BLE001
            continue
        if ind or sec:
            record_industry(conn, sym, ind or sec, sec, asof)
            n += 1
        if i % 50 == 0:
            conn.commit()
        if progress:
            progress("industry", i, len(todo))
    db.set_meta(conn, "industry_map_asof", asof)
    conn.commit()
    return n


def refresh_industry_us_bulk(conn, src, progress: ProgressCb | None = None) -> int:
    """美股行业 / Sector 批量版：Yahoo 筛选器按行业各查一次（约 150 个请求），代替逐股 info（每只 1 个请求，约 50 分钟）。
    只写 L1 内的股票；没覆盖到的（个别股票 Yahoo 无行业）留给逐股 refresh_industry_us 兜底。"""
    df = src.industry_map_screen(float(markets.universe_cfg("US")["l1"]["min_price"]))
    l1 = {r[0] for r in conn.execute("SELECT symbol FROM securities WHERE in_l1=1")}
    asof = date.today().isoformat()
    n = 0
    for r in df.itertuples():
        if r.symbol in l1:
            record_industry(conn, r.symbol, r.industry, r.sector, asof)
            n += 1
    db.set_meta(conn, "industry_map_asof", asof)
    conn.commit()
    if progress:
        progress("industry", n, n)
    return n


def record_industry(conn, symbol: str, industry: str | None, sector: str | None, asof: str) -> bool:
    """写当前行业映射；若与上一条记录不同（或首次出现）则追加一条带生效日期的历史快照（3.14：免费源只有当前快照，
    历史偏差只能靠「从现在起累积带生效日期的快照」逐步消除）。返回是否发生变化。"""
    last = conn.execute("SELECT industry, sector FROM industry_map_hist WHERE symbol=? ORDER BY asof DESC LIMIT 1", (symbol,)).fetchone()
    changed = last is None or last["industry"] != industry or last["sector"] != sector
    if changed:
        conn.execute("INSERT OR REPLACE INTO industry_map_hist(symbol,industry,sector,asof) VALUES(?,?,?,?)", (symbol, industry, sector, asof))
    conn.execute("INSERT INTO industry_map(symbol,industry,sector,asof) VALUES(?,?,?,?) "
                 "ON CONFLICT(symbol) DO UPDATE SET industry=excluded.industry, sector=excluded.sector, asof=excluded.asof",
                 (symbol, industry, sector, asof))
    return changed


def industry_asof(conn, symbol: str, day: str) -> str | None:
    """点时行业：day 当天或之前最近一次快照的行业；没有快照早于 day 时返回 None（不得用「今天的」回看更早的日子）。"""
    r = conn.execute("SELECT industry FROM industry_map_hist WHERE symbol=? AND asof<=? ORDER BY asof DESC LIMIT 1", (symbol, day)).fetchone()
    return r[0] if r else None


def industry_history_stats(conn) -> dict:
    r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT asof), MIN(asof), MAX(asof) FROM industry_map_hist").fetchone()
    changes = conn.execute("SELECT COUNT(*) FROM (SELECT symbol FROM industry_map_hist GROUP BY symbol HAVING COUNT(*)>1)").fetchone()[0]
    return {"rows": r[0], "snapshots": r[1], "first": r[2], "last": r[3], "symbols_with_changes": changes}


def refresh_industry(conn, src) -> int:
    if hasattr(src, "list_current_securities"):
        return refresh_industry_us(conn, src)
    df = src.industry_map()
    asof = date.today().isoformat()
    n = 0
    for r in df.itertuples():
        record_industry(conn, r.symbol, r.industry, None, asof)
        n += 1
    db.set_meta(conn, "industry_map_asof", asof)
    conn.commit()
    return n


# ---- 日线写入 -----------------------------------------------------------------

def save_bars(conn, symbol: str, bars: pd.DataFrame, source: str, is_temp: int = 0) -> int:
    if bars is None or bars.empty:
        return 0
    b = bars.copy()
    for c in BAR_COLS:
        if c not in b.columns:
            b[c] = None
    if "adj_close" not in b.columns:
        b["adj_close"] = None
    cols = BAR_COLS + ["adj_close"]
    recs = [(symbol, *[None if (isinstance(v, float) and v != v) else v for v in row], source, is_temp)
            for row in b[cols].itertuples(index=False)]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar(symbol,date,open,high,low,close,volume,amount,turnover,adj_factor,"
        "trade_status,is_st,adj_close,source,is_temp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", recs)
    save_actions(conn, symbol, b)
    return len(recs)


def save_actions(conn, symbol: str, bars: pd.DataFrame) -> None:
    """美股拆股 / 分红事件（必存：用于还原当时真实价，3.6.1 / 3.26.4）。"""
    if "split" in bars.columns:
        sp = bars[bars["split"] > 0]
        for d, r in zip(sp["date"], sp["split"]):
            conn.execute("INSERT OR REPLACE INTO corp_actions(symbol,ex_date,type,ratio_or_amount) VALUES(?,?,?,?)", (symbol, d, "split", float(r)))
    if "dividend" in bars.columns:
        dv = bars[bars["dividend"] > 0]
        for d, r in zip(dv["date"], dv["dividend"]):
            conn.execute("INSERT OR REPLACE INTO corp_actions(symbol,ex_date,type,ratio_or_amount) VALUES(?,?,?,?)", (symbol, d, "dividend", float(r)))


def set_state(conn, symbol: str, task: str, status: str, last_ok: str | None = None, fail: int | None = None):
    row = conn.execute("SELECT fail_count FROM fetch_state WHERE symbol=? AND task=?", (symbol, task)).fetchone()
    fc = (row[0] if row else 0) if fail is None else fail
    conn.execute("INSERT INTO fetch_state(symbol,task,last_ok_date,status,fail_count,updated_at) VALUES(?,?,?,?,?,?) "
                 "ON CONFLICT(symbol,task) DO UPDATE SET last_ok_date=COALESCE(excluded.last_ok_date,last_ok_date),"
                 "status=excluded.status, fail_count=excluded.fail_count, updated_at=excluded.updated_at",
                 (symbol, task, last_ok, status, fc, _now()))


BATCH = 100


def _fetch_many(src, symbols: list[str], start: str, end: str, stop=None, kw: dict | None = None):
    """逐批取日线：有批量接口（yfinance）则每批 BATCH 只一次请求，否则逐只。产出 (symbol, DataFrame | Exception)。"""
    kw = kw or {}
    if hasattr(src, "daily_bars_batch"):
        for i in range(0, len(symbols), BATCH):
            if stop and stop():
                return
            chunk = symbols[i:i + BATCH]
            try:
                got = src.daily_bars_batch(chunk, start, end)
            except CircuitOpen:
                raise
            except Exception as e:  # noqa: BLE001
                for sym in chunk:
                    yield sym, e
                continue
            for sym in chunk:
                yield sym, got.get(sym, pd.DataFrame())
    else:
        for sym in symbols:
            if stop and stop():
                return
            try:
                yield sym, src.daily_bars(sym, start, end, **kw.get(sym, {}))
            except CircuitOpen:
                raise
            except Exception as e:  # noqa: BLE001
                yield sym, e


def prefilter_l1(conn, src, market: str = "CN", progress: ProgressCb | None = None,
                 stop: Callable[[], bool] | None = None) -> dict:
    """无全市场快照（AkShare 不可用）时的 L1 预筛（3.26.2「先过滤、再拉取」）：
    每只在册股票只拉最近约 45 天日线（1 次请求），按 价格 / 20 日均成交额 粗筛，再只对幸存者拉 10 年历史。
    BaoStock 单只 10 年历史约 8~10 秒（M0 实测），不预筛则全量初始化要十几个小时。
    市值无法由日线得到，此处不筛（L2 的成交额门槛已间接约束小市值）；已退市股票一律保留；断点续跑（fetch_state: prefilter）。"""
    l1 = markets.universe_cfg(market)["l1"]
    last = mc.last_closed_trading_day(conn, market) or mc.today_str(market)
    start = (date.fromisoformat(last) - timedelta(days=45)).isoformat()
    todo = [r[0] for r in conn.execute(
        "SELECT s.symbol FROM securities s LEFT JOIN fetch_state f ON f.symbol=s.symbol AND f.task='prefilter' "
        "WHERE s.in_l1=1 AND s.status!='delisted' AND (f.status IS NULL OR f.status NOT IN ('ok','excluded')) ORDER BY s.symbol").fetchall()]
    kept = excluded = nodata = failed = 0
    kw = {sym: {"prev_factor": 1.0} for sym in todo}           # A 股增量模式：只 1 次请求，复权因子对预筛无关
    for i, (sym, bars) in enumerate(_fetch_many(src, todo, start, last, stop, kw), 1):
        if isinstance(bars, Exception):
            set_state(conn, sym, "prefilter", f"error: {str(bars)[:80]}")
            failed += 1
            continue
        live = bars[bars["trade_status"] > 0] if len(bars) else bars
        if live.empty:                                     # 近 45 天无交易：A 股（长期停牌等）无法判断，保留；美股无数据者多为权证 / 已摘牌，剔除
            if market == "US":
                conn.execute("UPDATE securities SET in_l1=0, l1_asof=? WHERE symbol=?", (last, sym))
                set_state(conn, sym, "prefilter", "excluded", last)
                excluded += 1
            else:
                set_state(conn, sym, "prefilter", "ok", last)
                nodata += 1
        else:
            px = float(live["close"].iloc[-1])
            adv = float(live["amount"].tail(20).mean())
            if px < l1["min_price"] or adv < l1["min_avg_amount20"] * 0.5:      # 阈值比 L2 宽松（L1 偏差见 3.26.1-6）
                conn.execute("UPDATE securities SET in_l1=0, l1_asof=? WHERE symbol=?", (last, sym))
                set_state(conn, sym, "prefilter", "excluded", last)
                excluded += 1
            else:
                set_state(conn, sym, "prefilter", "ok", last)
                kept += 1
        if i % 50 == 0:
            conn.commit()
        if progress:
            progress("prefilter_l1", i, len(todo))
    conn.commit()
    n_l1 = conn.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1").fetchone()[0]
    stats = {"checked": len(todo), "kept": kept, "excluded": excluded, "no_recent_data": nodata, "failed": failed, "l1_size": n_l1}
    db.set_meta(conn, "l1_prefilter", json.dumps({**stats, "asof": last}, ensure_ascii=False))
    return stats


def init_history(conn, src, market: str = "CN", limit: int | None = None, years: int | None = None,
                 progress: ProgressCb | None = None, stop: Callable[[], bool] | None = None,
                 only: set[str] | None = None) -> dict:
    """首次初始化 / 回补：对 L1 内尚未完成的股票拉取全部历史。断点续跑：只补 fetch_state 未 ok 的项。
    一次性开销（全量 A 股数小时），必须可中断续跑（3.26.2-8）。"""
    years = years or int(settings.cfg()["history_years"])
    last = mc.last_closed_trading_day(conn, market) or mc.today_str(market)
    start = (date.fromisoformat(last) - timedelta(days=int(365.25 * years))).isoformat()
    todo = [r[0] for r in conn.execute(
        "SELECT s.symbol FROM securities s LEFT JOIN fetch_state f ON f.symbol=s.symbol AND f.task='daily' "
        "WHERE s.in_l1=1 AND (f.status IS NULL OR f.status!='ok') ORDER BY s.symbol").fetchall()]
    if only is not None:
        todo = [x for x in todo if x in only]
    if limit:
        todo = todo[:limit]
    ok = fail = 0
    if hasattr(src, "daily_bars_batch"):                      # 美股：批量取（含拆股 / 分红），每批一次请求
        for i, (sym, bars) in enumerate(_fetch_many(src, todo, start, last, stop), 1):
            if isinstance(bars, Exception) or bars is None or bars.empty:
                set_state(conn, sym, "daily", f"error: {str(bars)[:80]}" if isinstance(bars, Exception) else "error: no data")
                fail += 1
            else:
                save_bars(conn, sym, bars, src.name)
                conn.execute("UPDATE securities SET list_date=COALESCE(list_date, ?) WHERE symbol=?", (bars["date"].min(), sym))
                set_state(conn, sym, "daily", "ok", bars["date"].max(), 0)
                ok += 1
            if i % BATCH == 0:
                conn.commit()
            if progress:
                progress("init_history", i, len(todo))
        conn.commit()
        return {"requested": len(todo), "ok": ok, "failed": fail}
    for i, sym in enumerate(todo, 1):
        if stop and stop():
            break
        try:
            delisted = conn.execute("SELECT status FROM securities WHERE symbol=?", (sym,)).fetchone()
            basic: dict = {}
            if delisted and delisted[0] == "delisted":      # 仅已退市股票需要精确的上市 / 退市日期（数据完整性闸门用）；在市股票省掉这次请求
                basic = src.security_basic(sym)
            ld, dd = basic.get("list_date"), basic.get("delist_date")
            s0 = max(start, ld) if ld else start
            s1 = min(last, dd) if dd else last
            bars = src.daily_bars(sym, s0, s1)
            save_bars(conn, sym, bars, src.name)
            ev = bars.attrs.get("adj_events")
            if ev is None:                                   # 数据源没带出复权因子事件（演示源等）：单独取一次
                ev = src.adj_factor_events(sym, "1990-01-01", s1)
            for e in ev.itertuples():
                conn.execute("INSERT OR REPLACE INTO corp_actions(symbol,ex_date,type,ratio_or_amount) VALUES(?,?,?,?)",
                             (sym, e.date, "adj_factor", e.factor))
            if not basic and (bars.empty or bars["date"].max() < (date.fromisoformat(last) - timedelta(days=10)).isoformat()):
                basic = src.security_basic(sym)                 # 近期没有日线（退市 / 长期停牌）：补查精确的上市 / 退市日期与状态
                ld, dd = basic.get("list_date"), basic.get("delist_date")
            if basic:
                conn.execute("UPDATE securities SET name=COALESCE(?,name), list_date=?, delist_date=?, status=?, sec_type=? WHERE symbol=?",
                             (basic.get("name"), ld, dd, basic.get("status", "active"), basic.get("sec_type", "stock"), sym))
            elif len(bars) and bars["date"].min() > (date.fromisoformat(start) + timedelta(days=10)).isoformat():
                conn.execute("UPDATE securities SET list_date=COALESCE(list_date, ?) WHERE symbol=?", (bars["date"].min(), sym))   # 窗口内才出现 = 新上市，首条日线即上市日
            set_state(conn, sym, "daily", "ok", bars["date"].max() if len(bars) else None, 0)
            ok += 1
        except CircuitOpen:
            conn.commit()
            raise
        except Exception as e:  # noqa: BLE001
            set_state(conn, sym, "daily", f"error: {str(e)[:100]}", None,
                      (conn.execute("SELECT fail_count FROM fetch_state WHERE symbol=? AND task='daily'", (sym,)).fetchone() or [0])[0] + 1)
            fail += 1
        if i % 20 == 0:
            conn.commit()
        if progress:
            progress("init_history", i, len(todo))
    conn.commit()
    return {"requested": len(todo), "ok": ok, "failed": fail}


def refresh_indices(conn, src, market: str = "CN", years: int | None = None) -> int:
    """指数日线（不受 L1 / L2 过滤）。"""
    years = years or int(settings.cfg()["history_years"])
    last = mc.last_closed_trading_day(conn, market) or mc.today_str(market)
    start = (date.fromisoformat(last) - timedelta(days=int(365.25 * years))).isoformat()
    n = 0
    for sym in markets.gate_benchmarks(market) + markets.aux_indices(market):
        row = conn.execute("SELECT MAX(date) FROM index_bar WHERE symbol=?", (sym,)).fetchone()
        s0 = (date.fromisoformat(row[0]) - timedelta(days=5)).isoformat() if row and row[0] else start
        try:
            df = src.index_bars(sym, s0, last)
        except CircuitOpen:
            raise
        except Exception:
            continue
        conn.executemany("INSERT OR REPLACE INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
                         [(sym, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount) for r in df.itertuples()])
        n += len(df)
    conn.commit()
    return n


# ---- 增量与对账 ---------------------------------------------------------------

def last_bar_date(conn) -> str | None:
    r = conn.execute("SELECT MAX(date) FROM daily_bar WHERE is_temp=0").fetchone()
    return r[0] if r and r[0] else None


def snapshot_safe(conn, market: str, day: str) -> bool:
    """快照只反映「最新一个交易日」：若新交易日的盘前 / 盘中已开始（今天是交易日、day 早于今天、且已过 09:00），
    快照就不再是 day 的收盘数据，不能用来追加 day 的日线。"""
    n = mc.now_in_market(market)
    today = n.date().isoformat()
    if mc.is_trading_day(conn, today) and day < today and n.hour >= 9:
        return False
    return day == (mc.last_closed_trading_day(conn, market) or day)


def update_incremental(conn, src, market: str = "CN", upto: str | None = None,
                       progress: ProgressCb | None = None, stop: Callable[[], bool] | None = None,
                       snapshot_fn: Callable[[], pd.DataFrame] | None = None) -> dict:
    """增量追加：对 L1 内每只股票，从库内最后日期之后拉到 upto（历史接口，上游为 BaoStock）。
    为避免每日数千次请求：若已有 AkShare 快照可用且只差 upto 当日，走快照追加（is_temp=1，次日对账覆盖）。"""
    upto = upto or mc.last_closed_trading_day(conn, market)
    if not upto:
        return {"mode": "none"}
    syms = [r[0] for r in conn.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted'").fetchall()]
    last_row = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT symbol, date, close, adj_factor FROM daily_bar WHERE (symbol, date) IN "
        "(SELECT symbol, MAX(date) FROM daily_bar GROUP BY symbol)").fetchall()}
    last_have = {s: v[0] for s, v in last_row.items()}
    # 只增量更新「已有历史」的股票；新进入 L1 的股票由 init_history 回补完整历史（避免只补一天造成历史残缺）
    need = [s for s in syms if s in last_have and last_have[s] < upto]
    if not need:
        return {"mode": "up_to_date", "updated": 0}
    if hasattr(src, "daily_bars_batch"):                       # 美股：批量增量 + 拆股重新调整检测
        return update_incremental_batch(conn, src, market, need, last_row, upto, progress, stop)
    gap_days = max(len(mc.trading_days(conn, last_have[s], upto)) for s in need[:50]) if need else 0
    if gap_days <= 2 and snapshot_fn is not None and snapshot_safe(conn, market, upto):
        try:
            snap = snapshot_fn()
            if len(snap) > 0.8 * len(need):                    # 快照明显不全（接口异常）则不用，回退到历史接口
                return {"mode": "snapshot", **append_snapshot(conn, snap, upto)}
        except CircuitOpen:
            pass
        except Exception:  # noqa: BLE001
            pass                                               # 快照失败 -> 回退到历史接口（BaoStock，每只 1 次请求）
    ok = fail = 0
    for i, sym in enumerate(need, 1):
        if stop and stop():
            break
        s0 = (date.fromisoformat(last_have[sym]) + timedelta(days=1)).isoformat()
        try:
            bars = src.daily_bars(sym, s0, upto, prev_close=last_row[sym][1], prev_factor=last_row[sym][2])
            save_bars(conn, sym, bars, src.name)
            set_state(conn, sym, "daily", "ok", bars["date"].max() if len(bars) else None, 0)
            ok += 1
        except CircuitOpen:
            conn.commit()
            raise
        except Exception as e:  # noqa: BLE001
            set_state(conn, sym, "daily", f"error: {str(e)[:100]}")
            fail += 1
        if i % 50 == 0:
            conn.commit()
        if progress:
            progress("update_incremental", i, len(need))
    conn.commit()
    return {"mode": "history", "updated": ok, "failed": fail}


def update_incremental_batch(conn, src, market: str, need: list[str], last_row: dict, upto: str,
                             progress: ProgressCb | None = None, stop: Callable[[], bool] | None = None) -> dict:
    """批量增量（美股）。yfinance 在发生拆股后会把**整段历史**重新调整，库里旧行就过期了，所以：
    每批从各自最后一日（含）起取，核对重叠日收盘价；对不上（或新窗口里出现拆股）的股票整只重拉历史并覆盖。
    重叠日本身也用新取到的数据覆盖：Yahoo 收盘后不久给出的当日 K 线是初步数据（开盘价常落在最高 / 最低价之外），
    第二天再取时已经修正——顺手覆盖掉，不让初步数据一直留在库里。"""
    ok = resync = fail = 0
    for i in range(0, len(need), BATCH):
        if stop and stop():
            break
        chunk = need[i:i + BATCH]
        start = min(last_row[s][0] for s in chunk)
        try:
            got = src.daily_bars_batch(chunk, start, upto)
        except CircuitOpen:
            conn.commit()
            raise
        except Exception as e:  # noqa: BLE001
            for s_ in chunk:
                set_state(conn, s_, "daily", f"error: {str(e)[:100]}")
            fail += len(chunk)
            continue
        for sym in chunk:
            bars = got.get(sym)
            if bars is None or bars.empty:
                continue
            last_d, last_close = last_row[sym][0], last_row[sym][1]
            overlap = bars[bars["date"] == last_d]
            drifted = len(overlap) and last_close and abs(float(overlap["close"].iloc[0]) / last_close - 1) > 0.002
            new = bars[bars["date"] > last_d]
            if drifted or (len(new) and (new["split"] > 0).any()):
                try:
                    full_start = (date.fromisoformat(upto) - timedelta(days=int(365.25 * int(settings.cfg()["history_years"])))).isoformat()
                    full = src.daily_bars(sym, full_start, upto)
                    if len(full):
                        save_bars(conn, sym, full, src.name)
                        resync += 1
                        set_state(conn, sym, "daily", "ok", full["date"].max(), 0)
                        continue
                except CircuitOpen:
                    conn.commit()
                    raise
                except Exception as e:  # noqa: BLE001
                    set_state(conn, sym, "daily", f"error: resync {str(e)[:80]}")
                    fail += 1
                    continue
            fresh = bars[bars["date"] >= last_d]                 # 含重叠日：用上游修正后的数据覆盖
            if len(fresh):
                save_bars(conn, sym, fresh, src.name)
            set_state(conn, sym, "daily", "ok", bars["date"].max(), 0)
            ok += 1
        conn.commit()
        if progress:
            progress("update_incremental", min(i + BATCH, len(need)), len(need))
    return {"mode": "batch", "updated": ok, "resynced_after_split": resync, "failed": fail}


def refetch_recent(conn, src, market: str, symbols: list[str], upto: str, days: int = 5) -> dict:
    """把指定股票最近几个交易日重新拉一遍并覆盖（只用于有批量接口的美股）。
    场景：收盘后不久取到的是 Yahoo 的初步 K 线（开盘价落在最高 / 最低价之外等），库里一旦存下，增量更新认为「已是最新」
    不会再取，完整性闸门就一直不过。闸门标出的异常股才重拉——一百来只只要一两次请求。"""
    if not symbols or not hasattr(src, "daily_bars_batch"):
        return {"refetched": 0}
    cal = mc.trading_days(conn, None, upto)
    if not cal:
        return {"refetched": 0}
    anchor = cal[-days] if len(cal) >= days else cal[0]
    q = "SELECT symbol, date, close, adj_factor FROM daily_bar WHERE date=? AND symbol IN (%s)" % ",".join("?" * len(symbols))
    rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(q, [anchor, *symbols]).fetchall()}
    need = [s for s in symbols if s in rows]
    if not need:
        return {"refetched": 0}
    r = update_incremental_batch(conn, src, market, need, rows, upto)
    return {"refetched": r.get("updated", 0) + r.get("resynced_after_split", 0), "failed": r.get("failed", 0), "from": anchor}


def append_snapshot(conn, snap: pd.DataFrame, day: str) -> dict:
    """用全市场快照追加当日 K 线（临时数据，is_temp=1）。复权因子沿用上一日；成交量已在字段映射层换算为股。"""
    last_fac = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol, adj_factor FROM daily_bar WHERE (symbol, date) IN "
        "(SELECT symbol, MAX(date) FROM daily_bar GROUP BY symbol)").fetchall()}
    st = {r[0]: r[1] for r in conn.execute("SELECT symbol, is_st FROM daily_bar WHERE (symbol,date) IN "
                                           "(SELECT symbol, MAX(date) FROM daily_bar GROUP BY symbol)").fetchall()}
    syms = set(r[0] for r in conn.execute("SELECT symbol FROM securities WHERE in_l1=1").fetchall())
    n = 0
    for r in snap.itertuples():
        if r.symbol not in syms or pd.isna(r.close):
            continue
        sus = 0 if (pd.isna(r.volume) or r.volume == 0) else 1
        is_st = 1 if "ST" in str(r.name).upper() else st.get(r.symbol, 0)
        conn.execute("INSERT OR IGNORE INTO daily_bar(symbol,date,open,high,low,close,volume,amount,turnover,adj_factor,"
                     "trade_status,is_st,source,is_temp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                     (r.symbol, day, r.open, r.high, r.low, r.close, r.volume, r.amount, r.turnover,
                      last_fac.get(r.symbol, 1.0), sus, is_st, "eastmoney"))
        n += 1
    db.set_meta(conn, "has_temp_rows", "1")
    conn.commit()
    return {"appended": n}


def reconcile_temp(conn, src, tol: float | None = None, progress: ProgressCb | None = None,
                   stop: Callable[[], bool] | None = None) -> dict:
    """次日用历史接口覆盖前一日的临时数据并对账（3.25.1）：收盘价相对偏差 > 0.5% 记差异（除权日除外）。"""
    tol = tol if tol is not None else settings.cfg()["gate"]["snapshot_recon_tol"]
    rows = conn.execute("SELECT symbol, MIN(date), MAX(date) FROM daily_bar WHERE is_temp=1 GROUP BY symbol").fetchall()
    n_rows = n_diff = 0
    for i, (sym, d0, d1) in enumerate(rows, 1):
        if stop and stop():
            break
        old = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT date, close, volume FROM daily_bar WHERE symbol=? AND is_temp=1", (sym,)).fetchall()}
        prev = conn.execute("SELECT close, adj_factor FROM daily_bar WHERE symbol=? AND date<? AND is_temp=0 ORDER BY date DESC LIMIT 1", (sym, d0)).fetchone()
        try:                                                    # 增量模式：1 次请求（除权日才补取复权因子）
            bars = src.daily_bars(sym, d0, d1, **({"prev_close": prev[0], "prev_factor": prev[1]} if prev else {}))
        except CircuitOpen:
            raise
        except Exception:
            continue
        for r in bars.itertuples():
            if r.date in old and old[r.date][0]:
                n_rows += 1
                if abs(r.close / old[r.date][0] - 1) > tol:
                    n_diff += 1
        save_bars(conn, sym, bars, src.name, is_temp=0)
        conn.execute("DELETE FROM daily_bar WHERE symbol=? AND is_temp=1 AND date<=?", (sym, d1))
        if progress:
            progress("reconcile", i, len(rows))
    conn.commit()
    rate = (n_diff / n_rows) if n_rows else 0.0
    db.set_meta(conn, "recon_diff_rate", round(rate, 5))
    return {"checked": n_rows, "diff": n_diff, "diff_rate": rate}


# ---- 财报日历（BaoStock 实际披露日 + 按去年同期推算 + 法定截止日兜底）-------------------

_QEND = [(3, 31), (6, 30), (9, 30), (12, 31)]
# 法定披露截止日（月, 日）：一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30
_DEADLINE = {1: (4, 30), 2: (8, 31), 3: (10, 31), 4: (4, 30)}


def latest_quarter_end(today: date) -> tuple[date, int]:
    """today 之前（含）最近的一个季末，及其季度序号 1~4。"""
    cands = [(date(y, m, d), q + 1) for y in (today.year - 1, today.year) for q, (m, d) in enumerate(_QEND)]
    return max(c for c in cands if c[0] <= today)


def _same_day_next_year(d: date) -> date:
    try:
        n = d.replace(year=d.year + 1)
    except ValueError:                                         # 2/29
        n = d.replace(year=d.year + 1, day=28)
    while n.weekday() >= 5:                                    # 落在周末则提前到周五（公司多在工作日披露）
        n -= timedelta(days=1)
    return n


def ensure_earnings(conn, src, symbols: list[str], today: date | None = None,
                    stop: Callable[[], bool] | None = None, market: str = "CN") -> dict:
    """按需补齐「漏斗候选 + 持仓」的财报日历（逐股接口不对全池拉取，3.17）。
    对每只股票：最近季末 S 的报告 ——
      * 已披露：存实际披露日（source=baostock，可用于回测点时过滤）；
      * 未披露：取去年同期披露日 +1 年作预计披露日（source=proj），无去年记录则用法定截止日（source=deadline）。
    预计日期有不确定性，风险剔除时对 proj / deadline 会放宽窗口（config.earnings.projection_margin_days）。
    结果记入 fetch_state（task=earnings:S），同一季度不重复请求；失败不影响主流程，返回失败数供界面标注。"""
    today = today or date.fromisoformat(mc.today_str(market))
    if hasattr(src, "earnings_dates"):                         # 美股：yfinance 财报日期（已披露 + 即将披露的预估）
        return _ensure_earnings_us(conn, src, symbols, today, stop, market)
    S, q = latest_quarter_end(today)
    task = f"earnings:{S.isoformat()}"
    done = {r[0] for r in conn.execute("SELECT symbol FROM fetch_state WHERE task=? AND status='ok'", (task,))}
    todo = [s for s in dict.fromkeys(symbols) if s not in done]
    now = _now()
    ok = failed = 0
    for sym in todo:
        if stop and stop():
            break
        try:
            prev = src.report_pub_date(sym, S.year - 1, q)
            cur = src.report_pub_date(sym, S.year, q) if (today - S).days >= 15 else None
        except CircuitOpen:
            conn.commit()
            raise
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        conn.execute("DELETE FROM events WHERE symbol=? AND event_type='EARNINGS' AND source IN ('proj','deadline')", (sym,))
        rows = []
        if prev:
            rows.append((prev, "baostock", "实际披露（去年同期）"))
        if cur:
            rows.append((cur, "baostock", "实际披露"))
        else:
            if prev:
                proj, src_tag, title = _same_day_next_year(date.fromisoformat(prev)), "proj", "预计披露（按去年同期）"
            else:
                m, d = _DEADLINE[q]
                proj, src_tag, title = date(S.year + (1 if q == 4 else 0), m, d), "deadline", "法定披露截止日（无历史记录）"
            if proj >= today:
                rows.append((proj.isoformat(), src_tag, title))
        for d, tag, title in rows:
            conn.execute("INSERT OR REPLACE INTO events(symbol,market,event_time,publish_time,ingested_at,event_type,source,title) "
                         "VALUES(?,?,?,?,?,?,?,?)", (sym, market, d, d if tag == "baostock" else None, now, "EARNINGS", tag, title))
        set_state(conn, sym, task, "ok", S.isoformat(), 0)
        ok += 1
    conn.commit()
    return {"quarter_end": S.isoformat(), "requested": len(todo), "ok": ok, "failed": failed, "cached": len(symbols) - len(todo)}


def _ensure_earnings_us(conn, src, symbols: list[str], today: date, stop, market: str) -> dict:
    """美股财报日历：yfinance get_earnings_dates。已披露的存实际日期（source=yfinance，可用于点时过滤）；
    即将披露的日期多为预估，存为 proj（风险剔除时放宽窗口）。每只股票每 7 天最多刷新一次。"""
    task = "earnings:us"
    fresh = {r[0] for r in conn.execute("SELECT symbol FROM fetch_state WHERE task=? AND status='ok' AND last_ok_date>=?",
                                        (task, (today - timedelta(days=7)).isoformat()))}
    todo = [s for s in dict.fromkeys(symbols) if s not in fresh]
    now = _now()
    ok = failed = 0
    for sym in todo:
        if stop and stop():
            break
        try:
            ds = src.earnings_dates(sym)
        except CircuitOpen:
            conn.commit()
            raise
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        conn.execute("DELETE FROM events WHERE symbol=? AND event_type='EARNINGS' AND source='proj'", (sym,))
        for d, reported in ds:
            tag, title = ("yfinance", "实际披露") if reported else ("proj", "预计披露")
            if not reported and d < today.isoformat():
                continue
            conn.execute("INSERT OR REPLACE INTO events(symbol,market,event_time,publish_time,ingested_at,event_type,source,title) "
                         "VALUES(?,?,?,?,?,?,?,?)", (sym, market, d, d if reported else None, now, "EARNINGS", tag, title))
        set_state(conn, sym, task, "ok", today.isoformat(), 0)
        ok += 1
    conn.commit()
    return {"quarter_end": None, "requested": len(todo), "ok": ok, "failed": failed, "cached": len(symbols) - len(todo)}


def refresh_earnings_bulk(conn, src, market: str = "CN", progress: ProgressCb | None = None,
                          stop: Callable[[], bool] | None = None) -> dict:
    """可选：为整个 L1 预取财报日历（每只 1~2 次请求，约 1~2 小时；可中断续跑）。日常只需按需拉取候选与持仓。"""
    syms = [r[0] for r in conn.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted'")]
    out = {"requested": 0, "ok": 0, "failed": 0}
    for i in range(0, len(syms), 50):
        if stop and stop():
            break
        r = ensure_earnings(conn, src, syms[i:i + 50], stop=stop, market=market)
        for k in out:
            out[k] += r[k]
        if progress:
            progress("earnings", min(i + 50, len(syms)), len(syms))
    return out


# ---- 事件流（M6）：只对「候选 + 持仓」按需拉取，缓存 6 小时 -----------------------------

def ensure_events(conn, provider, symbols: list[str], market: str = "CN", force: bool = False,
                  stop: Callable[[], bool] | None = None) -> dict:
    """按需拉取公告 / 新闻 / Insider 并入库（event_stream）。每只股票缓存 events.cache_hours 小时；force 强制刷新。"""
    from . import datasource_events as ev

    ttl = timedelta(hours=float(settings.cfg().get("events", {}).get("cache_hours", 6)))
    now = datetime.now()
    fresh = set()
    if not force:
        for sym, ts in conn.execute("SELECT symbol, last_ok_date FROM fetch_state WHERE task='events' AND status='ok'"):
            try:
                if ts and now - datetime.fromisoformat(ts) < ttl:
                    fresh.add(sym)
            except ValueError:
                pass
    todo = [s for s in dict.fromkeys(symbols) if s not in fresh]
    ok = failed = new = 0
    for sym in todo:
        if stop and stop():
            break
        try:
            items = provider.fetch(sym)
        except CircuitOpen:
            conn.commit()
            raise
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        for e in items:
            cur = conn.execute("INSERT OR IGNORE INTO event_stream(uid,symbol,market,event_time,publish_time,ingested_at,event_type,source,level,"
                               "title,summary,url,sentiment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ev.make_row(sym, market, e))
            new += cur.rowcount
        set_state(conn, sym, "events", "ok", now.isoformat(timespec="seconds"), 0)
        ok += 1
    conn.commit()
    return {"requested": len(todo), "ok": ok, "failed": failed, "new_events": new, "cached": len(symbols) - len(todo)}


# ---- 当日数据可用性探测 / 自学习（3.26.5：以「数据可用」为准，不以固定时刻为准）---------------

def probe_day_available(src, market: str, day: str) -> bool:
    """只查 1 个基准指数的当日日线：有返回即认为上游已提供当日数据（探测很便宜，可以频繁做）。"""
    sym = markets.gate_benchmarks(market)[0]
    try:
        df = src.index_bars(sym, day, day)
    except CircuitOpen:
        raise
    except Exception:  # noqa: BLE001
        return False
    return df is not None and len(df) > 0 and str(df["date"].iloc[-1]) == day


def record_ready_observation(conn, market: str, day: str, now=None) -> int | None:
    """首次探测到 day 的数据可用时，记录「收盘后多少分钟」（探测间隔内的上界）。只在 day 就是今天的市场日期时记录；
    保留最近 20 次，供 effective_ready_minutes 取中位数。"""
    from datetime import datetime as _dt

    n = mc.now_in_market(market, now)
    if n.date().isoformat() != day or db.get_meta(conn, f"ready_obs_day:{market}") == day:
        return None
    cfgm = mc.MARKETS[market]
    close_t = cfgm.get("half_close", cfgm["close"]) if (market == "US" and mc.is_half_day(conn, day)) else cfgm["close"]
    minutes = int((n - _dt.combine(n.date(), close_t, tzinfo=n.tzinfo)).total_seconds() // 60)
    if minutes < 0:
        return None
    obs = json.loads(db.get_meta(conn, f"ready_obs:{market}", "[]") or "[]")
    obs = (obs + [minutes])[-20:]
    db.set_meta(conn, f"ready_obs:{market}", json.dumps(obs))
    db.set_meta(conn, f"ready_obs_day:{market}", day)
    conn.commit()
    return minutes


def ready_stats(conn, market: str) -> dict:
    obs = json.loads(db.get_meta(conn, f"ready_obs:{market}", "[]") or "[]")
    if not obs:
        return {"n": 0, "median_min": None}
    s_ = sorted(obs)
    return {"n": len(obs), "median_min": s_[len(s_) // 2], "max_min": s_[-1], "last": obs[-1]}
