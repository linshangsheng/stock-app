"""持仓估值、盘后体检（5.10）、交易日志与复盘统计（5.11）。
系统不接券商下单：持仓与交易由用户手动录入；体检只用日线，给出的是**次日参数**，不是盘中盯盘。"""
from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import db, execution, features as feats, market_calendar as mc, settings
from .panel import load_panel


# ---- 账户 / 持仓 / 交易 -------------------------------------------------------

def get_account(market: str) -> dict:
    with db.portfolio_db() as p:
        r = p.execute("SELECT * FROM account WHERE market=?", (market,)).fetchone()
    base = {"market": market, "asof": None, "equity": None, "cash": None,
            "risk_per_trade": settings.cfg()["portfolio"]["risk_per_trade"]}
    return {**base, **dict(r)} if r else base


def set_account(market: str, equity: float | None, cash: float | None, risk_per_trade: float | None) -> dict:
    with db.portfolio_db() as p:
        p.execute("INSERT INTO account(market,asof,equity,cash,risk_per_trade) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(market) DO UPDATE SET asof=excluded.asof, equity=excluded.equity, cash=excluded.cash, "
                  "risk_per_trade=excluded.risk_per_trade",
                  (market, date.today().isoformat(), equity, cash, risk_per_trade))
    return get_account(market)


def list_positions(market: str, status: str | None = "open") -> list[dict]:
    q, a = "SELECT * FROM positions WHERE market=?", [market]
    if status:
        q += " AND status=?"
        a.append(status)
    with db.portfolio_db() as p:
        return [dict(r) for r in p.execute(q + " ORDER BY open_date DESC, id DESC", a)]


def upsert_position(market: str, d: dict) -> dict:
    """手工新增 / 修改持仓（不经交易日志时使用）。"""
    f = ("symbol", "open_date", "qty", "avg_cost", "initial_stop", "current_stop", "setup", "note", "signal_run_id")
    with db.portfolio_db() as p:
        if d.get("id"):
            sets = ",".join(f"{k}=?" for k in f if k in d)
            p.execute(f"UPDATE positions SET {sets} WHERE id=? AND market=?", [d[k] for k in f if k in d] + [d["id"], market])
            pid = d["id"]
        else:
            qty = int(d["qty"])
            cur = p.execute("INSERT INTO positions(market,symbol,open_date,qty,avg_cost,initial_stop,current_stop,setup,note,"
                            "init_qty,signal_run_id,entry_value) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (market, d["symbol"], d.get("open_date") or date.today().isoformat(), qty, d["avg_cost"],
                             d.get("initial_stop"), d.get("current_stop") or d.get("initial_stop"), d.get("setup"),
                             d.get("note"), qty, d.get("signal_run_id"), qty * d["avg_cost"]))
            pid = cur.lastrowid
        return dict(p.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())


def delete_position(market: str, pid: int) -> None:
    with db.portfolio_db() as p:
        p.execute("DELETE FROM positions WHERE id=? AND market=?", (pid, market))


EXIT_REASONS = ("止损", "移动止盈", "时间退出", "信号反转", "事件", "主观")


def record_trade(market: str, t: dict) -> dict:
    """录入一笔真实交易：买入建立 / 加仓，卖出减仓；卖出必须填出场原因（选「主观」须写备注，5.11）。"""
    side = t["side"].lower()
    if side not in ("buy", "sell"):
        raise ValueError("side 须为 buy / sell")
    sym, d, price, qty = t["symbol"], t["date"], float(t["price"]), int(t["qty"])
    if qty <= 0 or price <= 0:
        raise ValueError("价格与数量须为正")
    fee = t.get("fee")
    fee = float(fee) if fee not in (None, "") else execution.trade_fees(side, price, qty, market=market)
    if side == "sell":
        if t.get("exit_reason") not in EXIT_REASONS:
            raise ValueError(f"卖出必须选择出场原因：{'/'.join(EXIT_REASONS)}")
        if t["exit_reason"] == "主观" and not (t.get("note") or "").strip():
            raise ValueError("出场原因选「主观」须写备注")
    with db.portfolio_db() as p:
        pos = p.execute("SELECT * FROM positions WHERE market=? AND symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
                        (market, sym)).fetchone()
        if side == "buy":
            if pos:
                nq = pos["qty"] + qty
                avg = (pos["avg_cost"] * pos["qty"] + price * qty) / nq
                p.execute("UPDATE positions SET qty=?, avg_cost=?, init_qty=COALESCE(init_qty,0)+?, fees=fees+?, "
                          "entry_value=COALESCE(entry_value,0)+? WHERE id=?", (nq, avg, qty, fee, price * qty, pos["id"]))
                pid = pos["id"]
            else:
                if t.get("initial_stop") in (None, ""):
                    raise ValueError("买入请填写初始止损价（用于计算 R 倍数）")
                cur = p.execute(
                    "INSERT INTO positions(market,symbol,open_date,qty,avg_cost,initial_stop,current_stop,setup,init_qty,fees,"
                    "signal_run_id,regime,planned_trigger,entry_value,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (market, sym, d, qty, price, t["initial_stop"], t["initial_stop"], t.get("setup"), qty, fee,
                     t.get("signal_run_id"), t.get("regime"), t.get("planned_trigger"), price * qty, t.get("note")))
                pid = cur.lastrowid
        else:
            if not pos or pos["qty"] < qty:
                raise ValueError("卖出数量超过持仓")
            pnl = (price - pos["avg_cost"]) * qty - fee
            nq = pos["qty"] - qty
            if nq == 0:
                p.execute("UPDATE positions SET qty=0, status='closed', close_date=?, close_price=?, "
                          "realized_pnl=realized_pnl+?, fees=fees+?, exit_reason=? WHERE id=?",
                          (d, price, pnl, fee, t["exit_reason"], pos["id"]))
            else:
                p.execute("UPDATE positions SET qty=?, realized_pnl=realized_pnl+?, fees=fees+? WHERE id=?",
                          (nq, pnl, fee, pos["id"]))
            pid = pos["id"]
        cur = p.execute(
            "INSERT INTO trades(market,symbol,side,date,price,qty,fee,signal_run_id,setup,regime,initial_stop,planned_trigger,"
            "exit_reason,note,position_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (market, sym, side, d, price, qty, fee, t.get("signal_run_id"), t.get("setup"), t.get("regime"),
             t.get("initial_stop"), t.get("planned_trigger"), t.get("exit_reason"), t.get("note"), pid))
        return dict(p.execute("SELECT * FROM trades WHERE trade_id=?", (cur.lastrowid,)).fetchone())


def list_trades(market: str, start: str | None = None, end: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM trades WHERE market=?", [market]
    if start:
        q += " AND date>=?"
        a.append(start)
    if end:
        q += " AND date<=?"
        a.append(end)
    with db.portfolio_db() as p:
        return [dict(r) for r in p.execute(q + " ORDER BY date DESC, trade_id DESC", a)]


def delete_trade(market: str, trade_id: int) -> None:
    with db.portfolio_db() as p:
        p.execute("DELETE FROM trades WHERE trade_id=? AND market=?", (trade_id, market))


# ---- 盘后体检 -----------------------------------------------------------------

def _latest(conn, sym: str) -> dict | None:
    r = conn.execute("SELECT date, close, trade_status FROM daily_bar WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
    return dict(r) if r else None


def health(market: str = "CN", day: str | None = None, save: bool = True) -> dict:
    with settings.market_ctx(market):
        return _health(market, day, save)


def _health(market: str, day: str | None, save: bool) -> dict:
    """每个交易日扫描之后，对每只持仓做体检：R 倍数、移动止损位（次日生效）、退出信号、临近事件、异常；
    并输出组合层风险占用与「必须处理 / 关注 / 正常」分级，以及次日执行参数清单。"""
    cfg = settings.cfg()
    pcfg, ex = cfg["portfolio"], cfg["exits"]
    positions = list_positions(market, "open")
    acct = get_account(market)
    with db.market_db(market) as conn:
        day = day or db.get_meta(conn, "data_asof") or mc.last_closed_trading_day(conn, market)
        if not day:
            return {"error": "no_data"}
        regime_row = conn.execute("SELECT regime FROM scan_runs WHERE scan_date=? AND market=? ORDER BY finished_at DESC LIMIT 1",
                                  (day, market)).fetchone()
        regime = regime_row[0] if regime_row else "UNKNOWN"
        out_pos, risk_total, mv_total = [], 0.0, 0.0
        ind_value: dict[str, float] = {}
        if positions:
            syms = [p["symbol"] for p in positions]
            days = mc.trading_days(conn, None, day)[-330:]
            panel = load_panel(conn, days[0], day, symbols=syms, only_l1=False, market=market)
            F = feats.compute_features(panel).f if len(panel.symbols) else {}
            imap = pd.read_sql_query("SELECT symbol, industry FROM industry_map", conn).set_index("symbol")["industry"]
            nxt_days = mc.trading_days(conn, day, None)[1:6]
            from . import scanner as _sc
            _sc._ensure_earnings_safe(conn, syms, market)                      # 持仓的财报日历按需补齐
            earn = _sc._earnings_blocked(conn, day, 5, cfg["funnel"]["risk_exclusion"].get("projection_margin_days", 0)) if nxt_days else set()
            for p in positions:
                out_pos.append(_health_one(conn, p, day, panel, F, imap, earn, ex))
            for o in out_pos:
                if o.get("price"):
                    mv_total += o["market_value"]
                    risk_total += o["risk_to_stop"]
                    ind_value[o["industry"] or "其他"] = ind_value.get(o["industry"] or "其他", 0) + o["market_value"]
            if save:
                with db.portfolio_db() as pdb:
                    for o in out_pos:
                        if o.get("new_stop") is not None:
                            pdb.execute("UPDATE positions SET current_stop=? WHERE id=?", (o["new_stop"], o["id"]))
    equity = acct.get("equity") or None
    factor = 1.0 if regime == "NORMAL" else (0.0 if regime == "DEFENSIVE" else pcfg["caution_position_factor"])
    risk_cap = equity * pcfg["max_total_risk"] * (factor if factor else 1) if equity else None
    max_pos = max(1, int(round(pcfg["max_positions"] * (factor or 1))))
    levels = {"must": 0, "watch": 0, "ok": 0}
    for o in out_pos:
        levels[o["level"]] += 1
    summary = {
        "market_value": round(mv_total, 2), "risk_to_stop": round(risk_total, 2), "risk_cap": None if risk_cap is None else round(risk_cap, 2),
        "risk_used_pct": None if not risk_cap else round(risk_total / risk_cap, 3),
        "industry_share": {k: round(v / mv_total, 3) for k, v in ind_value.items()} if mv_total else {},
        "slots_free": max(0, max_pos - len(positions)), "max_positions": max_pos, "regime": regime,
        "regime_note": {"NORMAL": "正常开仓", "CAUTION": "谨慎：仓位上限减半，只做最强候选", "DEFENSIVE": "防守：不开新仓",
                        "UNKNOWN": "基准数据缺失：按谨慎处理"}.get(regime, ""),
        "levels": levels,
    }
    exec_list = [{"symbol": o["symbol"], "name": o["name"], "action": o["action"], "stop_price": o["new_stop"] or o["current_stop"],
                  "qty": o["qty"], "level": o["level"]} for o in out_pos if o["level"] != "ok" or o.get("new_stop")]
    rank = {"must": 0, "watch": 1, "ok": 2}
    out_pos.sort(key=lambda o: rank[o["level"]])
    return {"market": market, "date": day, "data_asof": day, "positions": out_pos, "summary": summary,
            "next_day_params": exec_list, "account": acct}


def _health_one(conn, p: dict, day: str, panel, F: dict, imap, earn: set, ex: dict) -> dict:
    sym = p["symbol"]
    name = (conn.execute("SELECT name FROM securities WHERE symbol=?", (sym,)).fetchone() or [sym])[0]
    base = {"id": p["id"], "symbol": sym, "name": name, "qty": p["qty"], "avg_cost": p["avg_cost"], "setup": p["setup"],
            "open_date": p["open_date"], "industry": imap.get(sym) if len(imap) else None,
            "initial_stop": p["initial_stop"], "current_stop": p["current_stop"], "reasons": [], "flags": [],
            "level": "ok", "action": "无需动作", "new_stop": None}
    if sym not in panel.symbols or day not in panel.dates:
        base.update(level="watch", flags=["无行情数据"], action="检查数据 / 是否停牌或退市")
        return base
    i = panel.dates.get_loc(day)
    raw_c, adj_c = float(panel.raw["close"][sym].iloc[i]), panel.adj["close"][sym].iloc[i]
    status = int(panel.status[sym].iloc[i])
    if status == 0 or raw_c != raw_c:
        base.update(level="watch", flags=["停牌"], action="停牌中：复牌前无法操作，关注复牌公告", price=raw_c)
        return base
    atr = float(F["atr14"][sym].iloc[i]) if "atr14" in F else float("nan")
    ma10, ma20, ma50 = (float(F[k][sym].iloc[i]) for k in ("ma10", "ma20", "ma50"))
    price = raw_c
    qty, cost = p["qty"], p["avg_cost"]
    init_stop = p["initial_stop"] or execution.initial_stop(cost, atr)
    risk_per_share = cost - init_stop
    r_mult = (price - cost) / risk_per_share if risk_per_share > 0 else None
    # 持有天数（交易日）
    held = int(panel.dates[(panel.dates >= p["open_date"]) & (panel.dates <= day)].size) - 1 if p["open_date"] else 0
    # 移动止损（T 日收盘后更新，T+1 生效，只上移）
    since = panel.dates >= p["open_date"]
    highest = float(panel.raw["close"][sym][since].max()) if since.any() else price
    prev_stop = p["current_stop"] or init_stop
    new_stop = execution.trailing_stop(prev_stop, highest, atr, ma10, ex["trail"], ex["trail_atr_k"])
    raised = new_stop > (p["current_stop"] or 0) + 0.004
    dist_atr = (price - new_stop) / atr if atr == atr and atr > 0 else None
    low_today = float(panel.raw["low"][sym].iloc[i])
    o = {**base, "price": round(price, 2), "market_value": round(price * qty, 2), "pnl": round((price - cost) * qty, 2),
         "pnl_pct": round(price / cost - 1, 4), "r_multiple": None if r_mult is None else round(r_mult, 2),
         "hold_days": held, "atr14": round(atr, 3) if atr == atr else None,
         "stop_dist_atr": None if dist_atr is None else round(dist_atr, 2),
         "stop_dist_pct": round((price - new_stop) / price, 4), "risk_to_stop": round(max(0.0, (price - new_stop) * qty), 2),
         "new_stop": new_stop if raised else None, "current_stop": new_stop}
    must, watch, reasons = [], [], []
    action = "无需动作"
    # 退出信号
    hard = cost * (1 - ex["hard_stop_pct"]) if ex.get("hard_stop_pct") else None
    if low_today <= prev_stop or (hard and price <= hard):
        must.append(f"今日触及止损位 {prev_stop:.2f}" if low_today <= prev_stop else f"触发硬止损 {hard:.2f}")
        action = "次日开盘卖出（止损；若开盘已低于止损价按开盘价，A 股跌停可能卖不出）"
    elif ex.get("exit_below_ma") and price < {20: ma20, 50: ma50}.get(ex["exit_below_ma"], ma20):
        must.append(f"收盘跌破 MA{ex['exit_below_ma']}（趋势信号失效）")
        action = "次日开盘价卖出（收盘类退出信号，5.5.1）"
    elif held >= ex["max_hold_days"]:
        must.append(f"时间退出到期（持有 {held} 日 ≥ {ex['max_hold_days']}）")
        action = f"次日开盘价卖出（时间退出）"
    if raised and not must:
        must.append(f"止损位上移：{prev_stop:.2f} → {new_stop:.2f}")
        action = f"在券商端把条件单止损价改为 {new_stop:.2f}（次日生效）"
    if must:
        o["level"] = "must"
    # 关注
    if o["level"] != "must":
        if dist_atr is not None and dist_atr < 1.0:
            watch.append(f"接近止损（仅 {dist_atr:.1f} ATR）")
        if sym in earn:
            watch.append("未来 5 个交易日内有财报披露")
        if watch:
            o["level"] = "watch"
    elif sym in earn:
        watch.append("未来 5 个交易日内有财报披露")
    # 异常标记
    vr = float(F["vol_ratio"][sym].iloc[i]) if "vol_ratio" in F else float("nan")
    if vr == vr and vr >= 4:
        o["flags"].append(f"成交量异常（量比 {vr:.1f}）")
    ret1 = float(F["ret_1"][sym].iloc[i])
    if ret1 == ret1 and abs(ret1) >= 0.095:
        o["flags"].append("涨跌停附近")
    o["reasons"] = must + watch
    o["action"] = action
    return o


# ---- 交易复盘统计 -------------------------------------------------------------

def _round_trips(market: str) -> pd.DataFrame:
    """已平仓的持仓视为一个 round trip：实际 R = 实际盈亏 ÷ 计划风险（5.7 / 5.11）。MAE / MFE 由日线现算。"""
    with db.portfolio_db() as p:
        rows = [dict(r) for r in p.execute("SELECT * FROM positions WHERE market=? AND status='closed'", (market,))]
    if not rows:
        return pd.DataFrame()
    out = []
    with db.market_db(market) as conn:
        for r in rows:
            sym, d0, d1 = r["symbol"], r["open_date"], r["close_date"]
            init_qty = r["init_qty"] or r["qty"] or 1
            risk = (r["avg_cost"] - (r["initial_stop"] or r["avg_cost"])) * init_qty
            pnl = r["realized_pnl"] or 0.0
            bars = pd.read_sql_query("SELECT date, high, low FROM daily_bar WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
                                     conn, params=(sym, d0, d1))
            mae = float(bars["low"].min() / r["avg_cost"] - 1) if len(bars) else None
            mfe = float(bars["high"].max() / r["avg_cost"] - 1) if len(bars) else None
            hold = len(mc.trading_days(conn, d0, d1)) - 1
            regime = r.get("regime") or _regime_on(conn, market, d0)
            plan_dev = None
            if r.get("planned_trigger"):
                plan_dev = r["avg_cost"] / r["planned_trigger"] - 1
            out.append({"id": r["id"], "symbol": sym, "setup": r["setup"] or "未标注", "regime": regime or "UNKNOWN",
                        "pnl": pnl, "risk": risk, "r": (pnl / risk) if risk > 0 else None, "hold_days": hold,
                        "mae": mae, "mfe": mfe, "exit_reason": r["exit_reason"], "entry_dev": plan_dev,
                        "open_date": d0, "close_date": d1})
    return pd.DataFrame(out)


def _regime_on(conn, market: str, d: str) -> str | None:
    row = conn.execute("SELECT regime FROM scan_runs WHERE market=? AND scan_date<=? ORDER BY scan_date DESC LIMIT 1", (market, d)).fetchone()
    return row[0] if row else None


def _stats(df: pd.DataFrame) -> dict:
    n = len(df)
    r = df["r"].dropna()
    wins, losses = df[df["pnl"] > 0], df[df["pnl"] <= 0]
    gross_w, gross_l = wins["pnl"].sum(), -losses["pnl"].sum()
    return {"n": n, "win_rate": round(len(wins) / n, 3) if n else None,
            "payoff": round((wins["pnl"].mean() / -losses["pnl"].mean()), 2) if len(wins) and len(losses) and losses["pnl"].mean() < 0 else None,
            "expectancy_r": round(float(r.mean()), 3) if len(r) else None,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
            "avg_hold_days": round(float(df["hold_days"].mean()), 1) if n else None,
            "avg_mae": round(float(df["mae"].mean()), 4) if df["mae"].notna().any() else None,
            "avg_mfe": round(float(df["mfe"].mean()), 4) if df["mfe"].notna().any() else None,
            "worst_r": round(float(r.min()), 2) if len(r) else None,
            "loss_over_1_5r": int((r < -1.5).sum()),
            "small_sample": n < 20}


def journal_stats(market: str = "CN", group_by: str = "setup,regime") -> dict:
    """复盘统计：按形态 × 市场状态分组（样本 < 20 笔标注「样本不足，仅供参考」）；对比实盘与回测期望；执行偏差影响。"""
    df = _round_trips(market)
    if df.empty:
        return {"overall": None, "groups": [], "note": "尚无已平仓记录"}
    keys = [k for k in group_by.split(",") if k in ("setup", "regime", "exit_reason")]
    groups = []
    for vals, g in df.groupby(keys or ["setup"]):
        vals = vals if isinstance(vals, tuple) else (vals,)
        groups.append({"key": dict(zip(keys or ["setup"], vals)), **_stats(g)})
    groups.sort(key=lambda x: -x["n"])
    # 执行偏差：主观离场 / 追高入场 的交易单独统计
    dev = {}
    subj = df[df["exit_reason"] == "主观"]
    if len(subj):
        dev["subjective_exit"] = _stats(subj)
        dev["planned_exit"] = _stats(df[df["exit_reason"] != "主观"]) if len(df[df["exit_reason"] != "主观"]) else None
    chase = df[df["entry_dev"].fillna(0) > 0.01]
    if len(chase):
        dev["chased_entry"] = _stats(chase)
    # 实盘 vs 回测
    bt = {}
    with db.market_db(market) as conn:
        for r in conn.execute("SELECT strategy_id, metrics FROM backtest_runs WHERE kind='strategy' ORDER BY created_at DESC LIMIT 50"):
            try:
                m = json.loads(r["metrics"])
            except Exception:
                continue
            bt.setdefault(r["strategy_id"], m.get("expectancy_r"))
    live_by_setup = {s: _stats(g)["expectancy_r"] for s, g in df.groupby("setup")}
    return {"overall": _stats(df), "groups": groups, "execution_deviation": dev,
            "live_vs_backtest": [{"setup": s, "live_expectancy_r": v, "backtest_expectancy_r": bt.get(s)} for s, v in live_by_setup.items()],
            "min_sample": 20}


def export_all(market: str = "CN") -> dict:
    """持仓与交易日志导出（File API 的第二份人工可读备份，3.26.6）。"""
    return {"exported_at": datetime.now().isoformat(timespec="seconds"), "market": market,
            "account": get_account(market), "positions": list_positions(market, None), "trades": list_trades(market)}
