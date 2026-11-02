"""实盘成绩单：软件每天存档的信号，之后「真实发生」了什么，和不用这个信号时比。

和回测的区别：回测是拿历史数据反复调参数得到的，存档是当天收盘后就写下来、事后改不了的，所以是回测之外最无偏的证据。
这里只读存档、不调任何参数，也不替信号说好话：
- 观察清单：scan_outcomes（每次正式扫描之后，清单里的股票次日开盘买、第 5 / 20 个交易日收盘的真实涨跌），对比同期买沪深300 / SPY；
- 恐慌日超跌名单：panic_picks，口径同上，按「行情段」合并（同一段里的股票同涨同跌，不是独立样本）；
- 恐慌日买宽基 ETF：同一批恐慌日之后买指数的真实结果，对比「平时随便哪天买」；
- 宽基 ETF 规则：signal_log 里每天存下的目标仓位，用同一套成交规则（收盘定、次日开盘成）算出的「照着做」与「一直持有」；
- 低风险组合（A 股）：每个调仓日存下「影子名单」（只按规则、不看你实际持有什么），下一个调仓日结算这一期的等权收益，对比沪深300 / 中证500。
独立事件少于 10 个时只展示、不下结论。收益都未扣成本；指数是价格指数、不含分红，个股用前复权，所以对个股略有利。"""
from __future__ import annotations

import json
from bisect import bisect_right
from datetime import datetime

import pandas as pd

from . import db, market_calendar as mc, scanner, settings

BENCH = {"CN": ("sh.000300", "沪深300"), "US": ("SPY", "标普500（SPY）")}
HORIZONS = (5, 20)
PANIC_EXPECT = {"CN": "回测（约 28 段）：5 天平均 +2.7%、胜率 67%；20 天平均 +6.1%、胜率 68%。但比「同日随便买一只」只多一点点，统计上不显著。",
                "US": "回测（约 27 段）：5 天平均 +1.8%、胜率 65%；20 天平均 +3.5%、胜率 65%。相对随便买、相对直接买 SPY 都没有显著的额外好处。"}
MIN_SHOW, MIN_TRUST = 10, 30            # 独立事件数：少于 MIN_SHOW 不下结论；少于 MIN_TRUST 只作参考


# ---- 存档 -----------------------------------------------------------------

def record(market: str) -> int:
    """把今天宽基指数规则给出的目标仓位存档（同一天只存第一次，事后不改）。返回新写入的条数。"""
    from . import market_view
    d = market_view.build(market)
    if d.get("status") != "ok":
        return 0
    n = 0
    now = datetime.now().isoformat(timespec="seconds")
    with db.market_db(market) as c:
        for it in d.get("indices", []):
            detail = json.dumps({"name": it["name"], "trend": bool(it["trend"]["holding"]), "washout": bool(it["washout"]["holding"])}, ensure_ascii=False)
            cur = c.execute("INSERT OR IGNORE INTO signal_log(date, kind, key, value, detail, logged_at) VALUES(?,?,?,?,?,?)",
                            (it["date"], "etf_rule", it["symbol"], float(it["position"]), detail, now))
            n += cur.rowcount
    return n


# ---- 小工具 ---------------------------------------------------------------

def _bars(conn, symbol: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT date, open, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(symbol,))


def _fwd(bars: pd.DataFrame, day: str, k: int) -> float | None:
    """信号日 day 收盘后决定，次日开盘买，第 k 个交易日收盘卖（含买入当天），未扣成本。"""
    ds = bars["date"].tolist()
    i = bisect_right(ds, day)
    if i < len(ds) and bars["open"].iloc[i] > 0 and i + k - 1 < len(ds):
        return float(bars["close"].iloc[i + k - 1] / bars["open"].iloc[i] - 1)
    return None


def _segments(dates: list[str], tdays: list[str], gap: int = 5) -> list[list[str]]:
    """相邻事件日相隔不超过 gap 个交易日算同一段（与恐慌日回测的「行情段」同口径）。"""
    pos = {d: i for i, d in enumerate(tdays)}
    segs: list[list[str]] = []
    last = None
    for d in sorted(dates):
        p = pos.get(d)
        if not segs or last is None or p is None or p - last > gap:
            segs.append([])
        segs[-1].append(d)
        last = p if p is not None else last
    return segs


def _mean(v: list[float]) -> float | None:
    return sum(v) / len(v) if v else None


def _horizon(events: list[dict], k: int) -> dict:
    """events: [{date, r: {5, 20}, b: {5, 20}}]，每个是一个独立事件（扫描日 / 行情段）。"""
    done = [e for e in events if e["r"].get(k) is not None and e["b"].get(k) is not None]
    avg = _mean([e["r"][k] for e in done])
    bench = _mean([e["b"][k] for e in done])
    return {"h": k, "n": len(done), "pending": len(events) - len(done), "avg": avg, "bench": bench,
            "excess": (avg - bench) if avg is not None and bench is not None else None,
            "beat": sum(1 for e in done if e["r"][k] > e["b"][k]),
            "win": (sum(1 for e in done if e["r"][k] > 0) / len(done)) if done else None,
            "worst": min((e["r"][k] for e in done), default=None)}


def _verdict(n: int, what: str, wait: str = "买入后要满 5 个交易日才有结果") -> dict:
    if n == 0:
        return {"level": "none", "text": f"还没有成熟的{what}（{wait}）。"}
    if n < MIN_SHOW:
        return {"level": "none", "text": f"只有 {n} 个独立{what}，样本太少，只是记录，不下结论（至少要 {MIN_SHOW} 个才开始看，{MIN_TRUST} 个以上才有参考价值）。"}
    if n < MIN_TRUST:
        return {"level": "few", "text": f"{n} 个独立{what}，样本偏少，只作参考；{MIN_TRUST} 个以上才有一点说服力。"}
    return {"level": "ok", "text": f"{n} 个独立{what}，可以参考，但仍不是保证。"}


def _signal(sid: str, name: str, desc: str, expect: str, ev: str, events: list[dict], what: str, extra: dict | None = None,
            hz: tuple[int, ...] = HORIZONS) -> dict:
    n5 = len([e for e in events if e["r"].get(hz[0]) is not None])
    return {"id": sid, "name": name, "desc": desc, "expect": expect, "evidence": ev, "what": what, "horizons": list(hz),
            "since": min((e["date"] for e in events), default=None), "events": len(events),
            "rows": [_horizon(events, k) for k in hz], "verdict": _verdict(n5, what, "要等到下一个调仓日结算这一期，约 20 个交易日" if hz == (20,) else "买入后要满 5 个交易日才有结果"),
            "recent": [{"date": e["date"], "r5": e["r"].get(5), "r20": e["r"].get(20), "b5": e["b"].get(5), "b20": e["b"].get(20), "note": e.get("note")}
                       for e in sorted(events, key=lambda e: e["date"], reverse=True)[:8]], **(extra or {})}


def record_factor(market: str) -> int:
    """低风险组合的「影子名单」：调仓日（A 股）收盘后，按规则（不看用户实际持有什么，上一期的影子名单当作已持有）算出目标名单并存档。
    只在数据截止日恰好是调仓日、且还没存过时才算（要建全市场面板，比较重）。返回新写入的只数。"""
    if market != "CN":
        return 0
    from . import factor_portfolio as fp
    cfg = fp.fp_cfg()
    n, buffer, method = int(cfg["n"]), float(cfg.get("buffer", 2.0)), cfg["score"]
    with db.market_db(market) as c:
        day = db.get_meta(c, "data_asof") or c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        if not day or not fp.schedule(c, day)["is_rebalance_day"]:
            return 0
        if c.execute("SELECT 1 FROM signal_log WHERE kind='factor_target' AND date=? LIMIT 1", (day,)).fetchone():
            return 0
        prev = c.execute("SELECT MAX(date) FROM signal_log WHERE kind='factor_target' AND date<?", (day,)).fetchone()[0]
        held: list[str] = []
        if prev:
            days = fp.list_days(c, None, day)
            if len(days) >= 2 and days[-2] == prev:                           # 上一期紧挨着，影子名单才能当作「已持有」；中间断过就当重新开始
                held = [r[0] for r in c.execute("SELECT key FROM signal_log WHERE kind='factor_target' AND date=?", (prev,)).fetchall()]
    from . import main as _main                                                 # 与选股页同一份全市场面板（单次构建、有缓存）
    ctx, _ = _main._scan_ctx(market)
    if not ctx or ctx["panel"].dates[-1] != day:
        return 0
    F, l2 = ctx["feat"].f, ctx["l2"]
    Fl = {k: F[k].loc[[day]] for k in ("turnover_ma20", "atr_pct", "ret_20") if k in F}
    srow = fp.scores(Fl, l2.loc[[day]], method).iloc[0]
    syms = list(srow.index)
    order = [syms[j] for j in fp.rank_order(srow.to_numpy(dtype="float64"))]
    target, rank = fp.select_targets(order, held, n, buffer)
    names, now = ctx["names"], datetime.now().isoformat(timespec="seconds")
    with db.market_db(market) as c:
        for s in target:
            c.execute("INSERT OR IGNORE INTO signal_log(date, kind, key, value, detail, logged_at) VALUES(?,?,?,?,?,?)",
                      (day, "factor_target", s, float(rank.get(s, 0)), json.dumps({"name": names.get(s, s)}, ensure_ascii=False), now))
    return len(target)


def _open_adj(conn, day: str, syms: list[str]) -> dict[str, tuple[float, float]]:
    """某天的 (复权开盘价, 复权收盘价)；停牌 / 缺数据的不返回。"""
    if not syms:
        return {}
    q = ",".join("?" * len(syms))
    out = {}
    for s, o, cl, f, st in conn.execute(f"SELECT symbol, open, close, adj_factor, trade_status FROM daily_bar WHERE date=? AND symbol IN ({q})", (day, *syms)).fetchall():
        if o and cl and f and (st or 0) > 0:
            out[s] = (o * f, cl * f)
    return out


def _factor_live(conn, market: str) -> dict:
    from . import factor_portfolio as fp
    every = int(fp.fp_cfg()["rebalance_days"])
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM signal_log WHERE kind='factor_target' ORDER BY date").fetchall()]
    tdays = mc.trading_days(conn)
    pos = {d: i for i, d in enumerate(tdays)}
    asof = db.get_meta(conn, "data_asof") or (tdays[-1] if tdays else None)
    benches = {"沪深300": _bars(conn, "sh.000300").set_index("date"), "中证500": _bars(conn, "sh.000905").set_index("date")}

    def basket(day_in: str, day_out: str, names: list[str], use_close_out: bool) -> tuple[float | None, int]:
        a, b = _open_adj(conn, day_in, names), _open_adj(conn, day_out, names)
        rs = [b[s][1 if use_close_out else 0] / a[s][0] - 1 for s in names if s in a and s in b]
        return (sum(rs) / len(rs) if rs else None), len(rs)

    def bench_ret(name: str, day_in: str, day_out: str, use_close_out: bool) -> float | None:
        bd = benches[name]
        if day_in not in bd.index or day_out not in bd.index or not bd.loc[day_in, "open"] > 0:
            return None
        return float(bd.loc[day_out, "close" if use_close_out else "open"] / bd.loc[day_in, "open"] - 1)

    def names_of(d: str) -> dict[str, str]:
        return {r_[0]: json.loads(r_[1] or "{}").get("name", r_[0]) for r_ in conn.execute(
            "SELECT key, detail FROM signal_log WHERE kind='factor_target' AND date=? ORDER BY value", (d,)).fetchall()}

    events, current = [], None
    for i, d in enumerate(dates):
        cn = names_of(d)
        names = list(cn)
        if d not in pos or pos[d] + 1 >= len(tdays):
            continue
        entry = tdays[pos[d] + 1]                                              # 调仓日收盘出名单，次日开盘买
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt and nxt in pos and pos[nxt] - pos[d] == every and pos[nxt] + 1 < len(tdays):
            exit_day = tdays[pos[nxt] + 1]                                     # 下一个调仓日的次日开盘卖（换成新名单）
            r, used = basket(entry, exit_day, names, False)
            b300, b500 = bench_ret("沪深300", entry, exit_day, False), bench_ret("中证500", entry, exit_day, False)
            if r is not None and b300 is not None:
                events.append({"date": d, "r": {20: r}, "b": {20: b300},
                               "note": f"{used}/{len(names)} 只" + (f"；中证500 {b500 * 100:+.1f}%" if b500 is not None else "")})
        elif nxt is None:
            r, used = (basket(entry, asof, names, True) if asof and asof > entry else (None, 0))
            b300 = bench_ret("沪深300", entry, asof, True) if asof and asof > entry else None
            current = {"date": d, "entry": entry, "n": len(names), "used": used, "ret": r, "bench": b300,
                       "days": (pos[asof] - pos[entry] + 1) if asof in pos and asof > entry else 0, "names": [cn[s] for s in names]}
    return _signal("factor", "低风险组合（影子名单）", f"每 {every} 个交易日调仓：调仓日收盘后存下按规则算出的名单（不看你实际持有什么），次日开盘等权买入，到下一个调仓日的次日开盘换成新名单。",
                   "回测（2018~2026，¥3 万）：年化约 6%、最大回撤约 −22%，样本内 −1.5%、样本外 +16.8%，非常依赖市场风格；它的卖点是和大盘波动不同步，不是稳赚。",
                   "弱", events, "调仓周期",
                   {"bench_label": "同期买沪深300", "current": current,
                    "caveat": f"按「调仓周期」数，每期约 {every} 个交易日，攒够 30 期要两年多。未扣交易费用（回测里的收益是扣费后的）；影子名单只有调仓那天软件开着、数据更新了才会存档，漏掉的那期不补。"},
                   hz=(20,))


# ---- 三类「事后可核对」的信号 ---------------------------------------------------

def _watchlist(conn, market: str, bench: pd.DataFrame) -> dict:
    runs = {}
    for run_id, d, n in conn.execute("SELECT run_id, scan_date, n_candidates FROM scan_runs WHERE official=1 AND market=? "
                                     "ORDER BY scan_date, finished_at, rowid", (market,)).fetchall():
        runs[d] = (run_id, n or 0)                                              # 同一天重跑过，取最后一次
    events, empty = [], 0
    for d, (run_id, n) in runs.items():
        if n == 0:
            empty += 1
            continue
        o = conn.execute("SELECT AVG(ret_5d), AVG(ret_20d), COUNT(*) FROM scan_outcomes WHERE run_id=? AND filled=1", (run_id,)).fetchone()
        events.append({"date": d, "r": {5: o[0], 20: o[1]}, "b": {k: _fwd(bench, d, k) for k in HORIZONS}, "note": f"{n} 只"})
    stock = conn.execute("SELECT COUNT(ret_5d), SUM(ret_5d>0) FROM scan_outcomes o JOIN scan_runs r ON r.run_id=o.run_id "
                         "WHERE r.market=? AND r.official=1 AND o.filled=1", (market,)).fetchone()
    return _signal("watchlist", "观察清单（波动收缩突破）", "每次正式扫描后，清单里的股票次日开盘买入、等权。",
                   "回测里这类形态只比随机买入略好、统计上不显著；软件默认只观察、不分配资金。", "弱", events, "扫描日",
                   {"runs": len(runs), "empty_runs": empty, "stocks5": stock[0] or 0, "stock_win5": (stock[1] / stock[0]) if stock[0] else None,
                    "caveat": "相邻几天的清单常是同一批股票，独立样本比扫描天数更少。"})


def _panic_dates(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT date FROM panic_picks ORDER BY date").fetchall()]


def _panic_stocks(conn, market: str, bench: pd.DataFrame, tdays: list[str]) -> dict:
    per = {d: (a5, a20, n) for d, a5, a20, n in conn.execute("SELECT date, AVG(ret_5d), AVG(ret_20d), COUNT(*) FROM panic_picks GROUP BY date").fetchall()}
    events = []
    for seg in _segments(list(per), tdays):
        r, b = {}, {}
        for k, idx in ((5, 0), (20, 1)):
            rv = [per[d][idx] for d in seg if per[d][idx] is not None]
            bv = [_fwd(bench, d, k) for d in seg if per[d][idx] is not None]
            bv = [x for x in bv if x is not None]
            r[k] = _mean(rv) if rv and len(rv) == len(bv) else None
            b[k] = _mean(bv) if rv and len(rv) == len(bv) else None
        events.append({"date": seg[0], "r": r, "b": b, "note": f"{len(seg)} 个恐慌日、{sum(per[d][2] for d in seg)} 只次"})
    return _signal("panic_stocks", "恐慌日超跌名单", "恐慌日（A 股温度<15%，美股 VIX≥25）名单里的股票，次日开盘买入、等权；同一段恐慌合并成一个事件。",
                   PANIC_EXPECT.get(market, ""), "弱", events, "恐慌行情段",
                   {"caveat": "同一段恐慌里的股票同涨同跌，所以按「段」数，不按只数、也不按天数。"})


def _panic_etf(conn, market: str, tdays: list[str]) -> list[dict]:
    dates = _panic_dates(conn)
    out = []
    for it in scanner.panic_etf_evidence(market)["items"]:
        bars = _bars(conn, it["symbol"])
        if bars.empty:
            continue
        hold = it.get("hold") or {}
        events = []
        for seg in _segments(dates, tdays):
            r, b = {}, {}
            for k in HORIZONS:
                v = [x for x in (_fwd(bars, d, k) for d in seg) if x is not None]
                r[k] = _mean(v) if len(v) == len(seg) else None
                b[k] = (hold.get(str(k)) or {}).get("base_mean") if r[k] is not None else None
            events.append({"date": seg[0], "r": r, "b": b, "note": f"{len(seg)} 个恐慌日"})
        e5, e20 = hold.get("5") or {}, hold.get("20") or {}
        expect = (f"回测（{e5.get('segs', '?')} 段）：恐慌日买入，5 天平均 {e5['mean'] * 100:+.1f}%、胜率 {e5['win'] * 100:.0f}%（平时随便哪天买 {e5['base_mean'] * 100:+.1f}%、{e5['base_win'] * 100:.0f}%）；"
                  f"20 天平均 {e20['mean'] * 100:+.1f}%、胜率 {e20['win'] * 100:.0f}%。" if e5 and e20 else "")
        out.append(_signal("panic_etf_" + it["symbol"], f"恐慌日买{it['name']}", "同一批恐慌日之后，次日开盘买入这个指数（ETF 的近似，未算溢价和费用）；对比线 = 平时随便哪天买的平均。",
                           expect, "中", events, "恐慌行情段", {"bench_label": "平时"}))
    return out


def _rule_live(conn, market: str) -> dict:
    """宽基 ETF 规则（收盘定、次日开盘成）：用存档的每日目标仓位算「照着做」和「一直持有」。只统计前后两天的存档都有的交易日。"""
    from . import market_view
    cost = market_view.mv_cfg()["index_rule"]["cost_per_side"]
    names = {it["symbol"]: it["name"] for it in market_view.index_list(market)}
    rows, per_rule, per_hold = [], {}, {}
    first_all = conn.execute("SELECT MIN(date), COUNT(DISTINCT date) FROM signal_log WHERE kind='etf_rule'").fetchone()
    for sym, name in names.items():
        log = {d: v for d, v in conn.execute("SELECT date, value FROM signal_log WHERE kind='etf_rule' AND key=? ORDER BY date", (sym,)).fetchall()}
        if not log:
            continue
        bars = _bars(conn, sym).set_index("date")
        ds = list(bars.index)
        rr, rh = {}, {}
        for i in range(2, len(ds)):
            t, t1, t2 = ds[i], ds[i - 1], ds[i - 2]
            if t1 not in log or t2 not in log:
                continue
            o, c, pc = bars["open"].iloc[i], bars["close"].iloc[i], bars["close"].iloc[i - 1]
            if not (o > 0 and pc > 0):
                continue
            p_exec, p_prev = log[t1], log[t2]
            rr[t] = p_prev * (o / pc - 1) + p_exec * (c / o - 1) - abs(p_exec - p_prev) * cost
            rh[t] = c / pc - 1
        cur = log[max(log)]
        row = {"symbol": sym, "name": name, "since": min(log), "recorded": len(log), "days": len(rr), "pos": cur, "last": max(log),
               "rule": float((1 + pd.Series(rr)).prod() - 1) if rr else None, "hold": float((1 + pd.Series(rh)).prod() - 1) if rh else None}
        row["excess"] = (row["rule"] - row["hold"]) if rr else None
        rows.append(row)
        if rr:
            per_rule[sym], per_hold[sym] = pd.Series(rr), pd.Series(rh)
    port = None
    if per_rule:
        r = pd.concat(per_rule, axis=1).sort_index().mean(axis=1)
        h = pd.concat(per_hold, axis=1).sort_index().mean(axis=1)
        port = {"days": int(len(r)), "rule": float((1 + r).prod() - 1), "hold": float((1 + h).prod() - 1)}
        port["excess"] = port["rule"] - port["hold"]
    days = port["days"] if port else 0
    if days == 0:
        verdict = {"level": "none", "text": "刚开始记录：要至少连续 3 个交易日的存档才有第一个结果。"}
    elif days < 60:
        verdict = {"level": "none", "text": f"只记录了 {days} 个交易日。这个规则是慢节奏的（一次持有常常几个月），这点时间说明不了任何问题，只是在记录。"}
    elif days < 250:
        verdict = {"level": "few", "text": f"{days} 个交易日，不到一年，只作参考。这个规则的卖点是回撤小，不是收益高，要经过一轮下跌才看得出来。"}
    else:
        verdict = {"level": "ok", "text": f"{days} 个交易日，已覆盖一年以上，可以参考。"}
    return {"id": "etf_rule", "name": "宽基 ETF 规则（趋势仓 + 抄底仓）", "desc": "每天收盘后按规则存下目标仓位；这里按「收盘定、次日开盘成」、扣单边费用，算出照着做的收益，对比一直持有。",
            "expect": "回测（样本外）：回撤比一直持有小得多，收益相近或略高；不是更赚钱，是更好拿。", "evidence": "中",
            "since": first_all[0], "recorded": first_all[1] or 0, "rows": rows, "portfolio": port, "verdict": verdict}


# ---- 总入口 ---------------------------------------------------------------

def build(market: str) -> dict:
    try:
        record(market)                              # 打开成绩单时顺手补存今天的目标仓位（同一天只存第一次，不改历史）
    except Exception:  # noqa: BLE001 - 存档失败不影响展示已有的成绩
        pass
    sym, bname = BENCH[market]
    with db.market_db(market) as c:
        bench = _bars(c, sym)
        tdays = mc.trading_days(c)
        sigs = [_watchlist(c, market, bench), _panic_stocks(c, market, bench, tdays), *_panic_etf(c, market, tdays)]
        if market == "CN":
            sigs.append(_factor_live(c, market))
        rule = _rule_live(c, market)
        start = c.execute("SELECT MIN(d) FROM (SELECT MIN(scan_date) d FROM scan_runs WHERE official=1 UNION ALL SELECT MIN(date) FROM panic_picks "
                          "UNION ALL SELECT MIN(date) FROM signal_log)").fetchone()[0]
    return {"status": "ok", "bench": bname, "bench_symbol": sym, "archive_start": start, "signals": sigs, "rule": rule,
            "note": "这是软件自己存档、事后才核对的真实结果，不是回测。收益均未扣交易成本；指数是价格指数（不含分红），个股用前复权，所以对个股略有利。"
                    "几个月内的结果多半只是运气，请看「独立事件数」和结论那一行，不要只看数字。"}
