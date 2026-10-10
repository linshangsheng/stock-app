"""选股漏斗与盘后扫描（5.4）。
  L1 -> ① 交易池 L2 -> ② 市场环境闸门 + 行业强弱(打分) -> ③ RPS 强度(打分) -> ④ 形态扫描 -> ⑤ 风险剔除
     -> ⑥ 综合打分取 Top N + 候选取舍规则 -> 次日观察清单（触发价 / 止损位 / 风险金额 / 建议仓位）
每个运行写 scan_runs（含 config 快照、run_id、闸门状态）；闸门 INCOMPLETE 时不写正式存档（宁可不出结果）。"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from . import db, execution, features as feats, market_calendar as mc, markets, quality, selection, settings, setups, universe
from .panel import load_panel

LOOKBACK = 430          # MA200 / 252 日高点 / 120 日布林分位所需预热


def config_snapshot() -> dict:
    c = settings.cfg()
    return {k: c[k] for k in ("universe", "gate", "features", "regime", "funnel", "setups", "execution", "exits",
                              "portfolio", "limit_rules")} | {"features_version": feats.FEATURES_VERSION,
                                                              "market": settings.current_market()}


def load_bench(conn, symbol: str, upto: str) -> pd.Series | None:
    df = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol=? AND date<=? ORDER BY date",
                           conn, params=(symbol, upto))
    return df.set_index("date")["close"] if len(df) else None


def load_industry(conn) -> pd.Series:
    df = pd.read_sql_query("SELECT symbol, industry FROM industry_map", conn)
    return df.set_index("symbol")["industry"] if len(df) else pd.Series(dtype=object)


def build_context(conn, day: str, market: str = "CN", lookback: int = LOOKBACK):
    days = mc.trading_days(conn, None, day)[-lookback:]
    panel = load_panel(conn, days[0] if days else None, day, market=market)
    boards, list_dates, names = universe.load_security_meta(conn)
    l2, cond, flags = universe.l2_mask(panel, boards, list_dates, return_parts=True, market=market)
    bench = load_bench(conn, settings.cfg()["regime"]["benchmark"], day)
    vix = load_bench(conn, "^VIX", day) if market == "US" else None
    industry = load_industry(conn)
    feat = feats.compute_features(panel, l2, bench, industry, vix=vix)
    return {"panel": panel, "boards": boards, "names": names, "l2": l2, "cond": cond, "flags": flags,
            "feat": feat, "industry": industry, "days": days}


def _held_symbols(market: str) -> set[str]:
    with db.portfolio_db() as p:
        return {r["symbol"] for r in p.execute("SELECT symbol FROM positions WHERE market=? AND status='open'", (market,))}


def _account(market: str) -> dict | None:
    with db.portfolio_db() as p:
        r = p.execute("SELECT * FROM account WHERE market=?", (market,)).fetchone()
        return dict(r) if r else None


def _portfolio_state(market: str) -> dict:
    with db.portfolio_db() as p:
        pos = [dict(r) for r in p.execute("SELECT * FROM positions WHERE market=? AND status='open'", (market,))]
    return {"positions": pos}


def _earnings_blocked(conn, day: str, window: int, margin: int = 0) -> set[str]:
    """未来 window 个交易日内有财报披露的股票。实际披露日用 window；预计 / 法定截止日（不确定）额外放宽 margin 个交易日。"""
    nxt = mc.trading_days(conn, day, None)[1:window + margin + 1]
    if not nxt:
        return set()
    exact_end = nxt[min(window, len(nxt)) - 1]
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM events WHERE event_type='EARNINGS' AND event_time>=? AND "
        "((source IN ('baostock','yfinance') AND event_time<=?) OR (source IN ('proj','deadline') AND event_time<=?))",
        (nxt[0], exact_end, nxt[-1])).fetchall()
    return {r[0] for r in rows}


def _ensure_earnings_safe(conn, symbols: list[str], market: str = "CN") -> dict:
    """按需补齐财报日历；上游不可用时不阻塞扫描，返回失败数供界面标注。"""
    from . import ingest
    from .datasource_cn import get_source
    src = get_source(market)
    try:
        r = ingest.ensure_earnings(conn, src, symbols, market=market)
        return {"checked": r["ok"] + r["cached"], "failed": r["failed"]}
    except Exception:  # noqa: BLE001 - 熔断 / 网络错误
        return {"checked": 0, "failed": len(symbols)}
    finally:
        if hasattr(src, "close"):
            src.close()


def run_scan(market: str = "CN", scan_date: str | None = None, persist: bool = True,
             include_preview: bool = True) -> dict:
    with settings.market_ctx(market):                       # 按市场取配置：美股参数独立（2.1）
        return _run_scan(market, scan_date, persist, include_preview)


def _run_scan(market: str, scan_date: str | None, persist: bool, include_preview: bool) -> dict:
    started = datetime.now().isoformat(timespec="seconds")
    cfg = settings.cfg()
    fun = cfg["funnel"]
    with db.market_db(market) as conn:
        day = scan_date or db.get_meta(conn, "data_asof") or mc.last_closed_trading_day(conn, market)
        if not day:
            return {"error": "no_data", "message": "尚无行情数据，请先初始化数据（设置页或 python -m server.cli init）"}
        gate = quality.check_gate(conn, market, day)
        ctx = build_context(conn, day, market)
        panel, feat, l2 = ctx["panel"], ctx["feat"], ctx["l2"]
        if day not in panel.dates:
            return {"error": "no_bar", "message": f"{day} 无行情数据"}
        last = day
        F = feat.f

        # ④ 形态 + 冷却期
        sigs = setups.detect_all(feat)
        sigs = {k: setups.apply_cooldown(v & l2, fun["cooldown_days"]) for k, v in sigs.items()}
        any_sig, primary = setups.merge_setups(sigs)
        sig_today = any_sig.loc[last]
        names_en = list(sigs)

        regime = str(feat.regime.loc[last]) if feat.regime is not None else "UNKNOWN"
        reg_detail = {k: (None if pd.isna(v.loc[last]) else round(float(v.loc[last]), 4))
                      for k, v in feat.m.items() if k in ("breadth_ma20", "breadth_ma50", "breadth_ma200", "adv_ratio",
                                                          "bench_close", "bench_ma50", "bench_ma200", "pool_size", "new_high_cnt")}

        # ⑤ 风险剔除
        held = _held_symbols(market)
        flags = ctx["flags"]
        unfill = (flags["oneword"].loc[last] & flags["limit_up"].loc[last]) | flags["limit_up"].loc[last]
        earn = set()
        bad = set(gate["bad_symbols"])
        excl = {"held": 0, "earnings": 0, "limit_up": 0, "anomaly": 0}
        syms = [s for s in sig_today.index[sig_today.to_numpy()]]
        earn_info = {"checked": 0, "failed": 0}
        if fun["risk_exclusion"]["hard"] and syms:                 # 财报日历只对「漏斗候选」按需拉取（3.17）
            earn_info = _ensure_earnings_safe(conn, syms, market)
            earn = _earnings_blocked(conn, day, fun["risk_exclusion"]["earnings_window_days"],
                                     fun["risk_exclusion"].get("projection_margin_days", 0))
        keep = []
        for s in syms:
            if s in held:
                excl["held"] += 1
            elif s in bad:
                excl["anomaly"] += 1
            elif bool(unfill.get(s, False)):
                excl["limit_up"] += 1
            elif s in earn:
                excl["earnings"] += 1
            else:
                keep.append(s)

        # ⑥ 打分
        score = setups.score_frame(feat).loc[last]
        ind_of = ctx["industry"]
        rows = []
        close_raw, high_raw = panel.raw["close"].loc[last], panel.raw["high"].loc[last]
        for s in keep:
            prim = int(primary.loc[last, s])
            tags = [setups.SETUP_LABEL[n] for i, n in enumerate(names_en) if bool(sigs[n].loc[last, s])]
            rows.append({
                "symbol": s, "name": ctx["names"].get(s, s), "board": ctx["boards"].get(s),
                "industry": ind_of.get(s) if len(ind_of) else None,
                "setup": names_en[prim - 1], "setup_label": setups.SETUP_LABEL[names_en[prim - 1]], "all_setups": tags,
                "score": None if pd.isna(score.get(s)) else float(score[s]),
                "close": float(close_raw[s]), "high": float(high_raw[s]),
                "atr14": float(F["atr14"].loc[last, s]), "atr_pct": float(F["atr_pct"].loc[last, s]),
                "vol_ratio": _f(F["vol_ratio"].loc[last, s]), "close_pos": _f(F["close_pos"].loc[last, s]),
                "rps_20": _f(F["rps_20"].loc[last, s]), "rps_60": _f(F["rps_60"].loc[last, s]),
                "ind_rps_20": _f(F["ind_rps_20"].loc[last, s]) if "ind_rps_20" in F else None,
                "rs_20": _f(F["rs_20"].loc[last, s]) if "rs_20" in F else None,
                "ret_20": _f(F["ret_20"].loc[last, s]), "dist_52w_high": _f(F["dist_52w_high"].loc[last, s]),
                "adv20": _f(F["amt_ma20"].loc[last, s]),
                "struct_low": _f((F["ll5_incl"] if names_en[prim - 1] in ("pullback", "oversold") else F["struct_low_breakout"]).loc[last, s]),
            })
        cand = pd.DataFrame(rows)
        summary = {"universe_l2": int(l2.loc[last].sum()), "signals_today": int(sig_today.sum()),
                   "excluded": excl, "n_after_exclusion": len(keep), "regime": regime,
                   "l2_exclusions": universe.l2_exclusion_stats(ctx["cond"]),
                   "by_setup": {n: int(sigs[n].loc[last].sum()) for n in names_en},
                   "earnings_data": earn_info["failed"] == 0 and (earn_info["checked"] > 0 or not syms),
                   "earnings_failed": earn_info["failed"]}

        # ② 市场环境闸门（硬）
        port_factor, new_allowed = 1.0, True
        if regime == "DEFENSIVE":
            new_allowed = False
        elif regime in ("CAUTION", "UNKNOWN"):
            port_factor = cfg["portfolio"]["caution_position_factor"]
        results: list[dict] = []
        if len(cand):
            cand = cand.sort_values(["score", "atr_pct", "adv20"], ascending=[False, True, False], na_position="last") \
                .reset_index(drop=True)
            if new_allowed:
                results = select_and_size(cand, market, port_factor, regime, day, ctx["boards"])
            else:
                summary["suppressed_by_regime"] = len(cand)
        official = gate["status"] == "PASS"
        run_id = f"{market}-{day.replace('-', '')}-{settings.config_hash(config_snapshot())[:6]}-{datetime.now().strftime('%H%M%S')}"
        res_regime = {"state": regime, "detail": reg_detail, "position_factor": port_factor, "new_positions_allowed": new_allowed}
        out = {"run_id": run_id, "market": market, "scan_date": day, "data_asof": day, "entry_mode": cfg["execution"]["entry_mode"],
               "gate": {k: gate[k] for k in ("status", "checks", "reasons")}, "official": official, "regime": res_regime,
               "summary": summary, "candidates": results, "started_at": started,
               "valid_until": mc.next_trading_day(conn, day), "is_stale": _is_stale(conn, market, day)}
        if persist:
            conn.execute("INSERT INTO scan_runs(run_id,market,scan_date,config_hash,data_asof,data_gate_status,gate_detail,regime,"
                         "regime_detail,started_at,finished_at,official,universe_size,n_candidates,config,summary) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, market, day, settings.config_hash(config_snapshot()), day, gate["status"],
                          json.dumps(gate, ensure_ascii=False), regime, json.dumps(res_regime, ensure_ascii=False),
                          started, datetime.now().isoformat(timespec="seconds"), int(official), summary["universe_l2"],
                          len(results), json.dumps(config_snapshot(), ensure_ascii=False, default=str),
                          json.dumps(summary, ensure_ascii=False)))
            if official:
                for r in results:
                    conn.execute("INSERT INTO scan_results(run_id,symbol,setup,score,reasons,trigger_price,stop_price,risk_amount,shares,extra) "
                                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (run_id, r["symbol"], r["setup"], r["score"], json.dumps(r["reasons"], ensure_ascii=False),
                                  r["trigger_price"], r["stop_price"], r["risk_amount"], r["shares"],
                                  json.dumps({k: r[k] for k in ("name", "industry", "close", "atr14", "rps_20", "rps_60", "vol_ratio",
                                                                 "fit", "skip_reason", "entry_ref", "setup_label", "board",
                                                                 "stop_dist_pct", "risk_per_lot", "all_setups", "adv20", "atr_pct", "ind_rps_20")}, ensure_ascii=False)))
            db.set_meta(conn, "last_scan_run", run_id)
        if not official and not include_preview:
            out["candidates"] = []
        return out


def rescan_if_config_changed(market: str = "CN") -> dict | None:
    """最新一份正式清单若是用旧参数（配置哈希不同）算的，且就是当前数据日，则按现行参数重扫一次（升级 / 改参数后立即生效）。
    不同交易日的历史清单不动（那是当时真实给出的，历史回看要用）。"""
    with settings.market_ctx(market), db.market_db(market) as c:
        asof = db.get_meta(c, "data_asof")
        row = c.execute("SELECT config_hash, scan_date FROM scan_runs WHERE market=? AND official=1 ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1",
                        (market,)).fetchone()
        if not row or not asof or row["scan_date"] != asof or row["config_hash"] == settings.config_hash(config_snapshot()):
            return None
    return run_scan(market, scan_date=asof)


def _f(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(round(float(v), 4))


def _is_stale(conn, market: str, day: str) -> bool:
    """清单对应的「下一交易日」是否已过（补跑出的旧清单仅作记录，不再作为可执行清单，3.26.5）。"""
    nxt = mc.next_trading_day(conn, day)
    if not nxt:
        return False
    return mc.today_str(market) > nxt or (mc.today_str(market) == nxt and mc.market_closed_at(conn, market, nxt))


def select_and_size(cand: pd.DataFrame, market: str, port_factor: float, regime: str, day: str,
                    boards: pd.Series) -> list[dict]:
    """从账户 / 持仓读取状态，调用与回测共用的 selection.select_and_size。"""
    cfg = settings.cfg()
    pcfg = cfg["portfolio"]
    acct = _account(market)
    equity = acct["equity"] if acct and acct.get("equity") else None
    rpt = (acct.get("risk_per_trade") if acct else None) or pcfg["risk_per_trade"]
    pos = _portfolio_state(market)["positions"]
    max_pos = max(1, int(round(pcfg["max_positions"] * port_factor)))
    held_ind: dict = {}
    risk_used = 0.0
    with db.market_db(market) as conn:
        imap = load_industry(conn)
        for p in pos:
            ind = imap.get(p["symbol"])
            held_ind[ind] = held_ind.get(ind, 0) + 1
            if p.get("current_stop") and p.get("qty"):
                lastc = conn.execute("SELECT close FROM daily_bar WHERE symbol=? ORDER BY date DESC LIMIT 1", (p["symbol"],)).fetchone()
                if lastc:
                    risk_used += max(0.0, (lastc[0] - p["current_stop"]) * p["qty"])
    if regime in ("CAUTION", "UNKNOWN"):
        cand = cand.head(max(1, int(np.ceil(len(cand) * port_factor))))       # 只做最强候选
    risk_cap = (equity * pcfg["max_total_risk"] * port_factor) if equity else None
    out = selection.select_and_size(cand.to_dict("records"), slots=max_pos - len(pos), held_ind=held_ind,
                                    risk_used=risk_used, risk_cap=risk_cap, equity=equity, risk_pct=rpt,
                                    top_n=cfg["funnel"]["top_n"])
    for rec in out:
        rec["reasons"] = _reasons(rec)
    return out


def _reasons(r: dict) -> list[str]:
    t = [f"{r['setup_label']}"]
    if len(r.get("all_setups", [])) > 1:
        t.append("同时满足：" + "、".join(r["all_setups"]))
    if r.get("vol_ratio"):
        t.append(f"量比 {r['vol_ratio']:.1f}")
    if r.get("rps_20") is not None:
        t.append(f"RPS20 {r['rps_20']:.0f}")
    if r.get("ind_rps_20") is not None:
        t.append(f"行业强度 {r['ind_rps_20']:.0f}")
    if r.get("dist_52w_high") is not None:
        t.append(f"距52周高 {r['dist_52w_high'] * 100:.0f}%")
    return t


# ---- scan_outcomes 回填（T+1/3/5/10/20）--------------------------------------

def backfill_outcomes(market: str = "CN") -> dict:
    """对正式存档的清单，按期回填真实前向收益（5.4）。入场基准 = T+1 开盘价（next_open）或触发价（stop_entry）；
    ret_kd = 第 k 个交易日收盘 / 入场价 - 1；MAE / MFE 取入场后 20 日内的最低 / 最高相对入场价。是回测之外最无偏的证据。"""
    ks = (1, 3, 5, 10, 20)
    updated = 0
    with db.market_db(market) as conn:
        runs = conn.execute("SELECT run_id, scan_date FROM scan_runs WHERE official=1 AND market=?", (market,)).fetchall()
        mode = settings.cfg()["execution"]["entry_mode"]
        for run_id, d in runs:
            res = conn.execute("SELECT r.symbol, r.trigger_price, o.ret_20d FROM scan_results r LEFT JOIN scan_outcomes o "
                               "ON o.run_id=r.run_id AND o.symbol=r.symbol WHERE r.run_id=? AND (o.filled IS NULL OR o.filled=1)",
                               (run_id,)).fetchall()
            todo = [r for r in res if r[2] is None]
            if not todo:
                continue
            days = mc.trading_days(conn, d, None)
            fut = days[1:22]
            if not fut:
                continue
            for sym, trig, _ in todo:
                bars = pd.read_sql_query(
                    "SELECT date, open, high, low, close, adj_factor, trade_status FROM daily_bar WHERE symbol=? AND date>? AND date<=? ORDER BY date",
                    conn, params=(sym, d, fut[-1]))
                if bars.empty:
                    continue
                base = pd.read_sql_query("SELECT adj_factor FROM daily_bar WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                                         conn, params=(sym, d))
                f0 = base["adj_factor"].iloc[0] if len(base) else 1.0
                ratio = bars["adj_factor"] / f0                     # 以信号日因子为基准的复权比例
                o, h, l, c = (bars[k] * ratio for k in ("open", "high", "low", "close"))
                first = bars.iloc[0]
                filled = int(first["trade_status"] > 0)
                if mode == "stop_entry":
                    filled = int(filled and first["high"] >= trig)
                    entry = max(o.iloc[0], trig) if filled else np.nan
                else:
                    entry = o.iloc[0] if filled else np.nan
                if not filled or entry != entry:
                    conn.execute("INSERT OR REPLACE INTO scan_outcomes(run_id,symbol,filled) VALUES(?,?,0)", (run_id, sym))
                    continue
                vals = {}
                for k in ks:
                    vals[k] = float(c.iloc[k - 1] / entry - 1) if len(c) >= k else None
                win = slice(0, min(20, len(c)))
                mae = float(l.iloc[win].min() / entry - 1)
                mfe = float(h.iloc[win].max() / entry - 1)
                conn.execute("INSERT OR REPLACE INTO scan_outcomes(run_id,symbol,ret_1d,ret_3d,ret_5d,ret_10d,ret_20d,mae,mfe,filled) "
                             "VALUES(?,?,?,?,?,?,?,?,?,1)", (run_id, sym, vals[1], vals[3], vals[5], vals[10], vals[20], mae, mfe))
                updated += 1
    return {"updated": updated}
