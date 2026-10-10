"""回测层（第六章）：自研轻量逐日回测循环（6.7：可控、透明、易排错）+ 单因子检验（6.6.1）+ 形态事件研究（6.6.2）
+ 漏斗消融（6.6.3）+ 分状态 / 分年度评估（6.6.4）。

设计要点：
  * 与盘后扫描共用 features.py / setups.py / selection.py / execution.py —— 口径只此一份（6.4）。
  * 成交严格按 5.5.1：信号 T 日收盘后生成，入场最早 T+1；跳空、止损优先于止盈（同日取最坏）、移动止损 T+1 生效、
    A 股 T+1（买入日止损不生效）、涨停买不进 / 跌停卖不出顺延、整手、单笔容量、费用与滑点参数化。
  * 单笔交易的退出路径只依赖价格序列（trade_path 纯函数）；组合层负责资金 / 空位 / 行业 / 总风险约束。
  * 每次运行写入 backtest_runs：run_id、config_hash、数据截止日、试验次数、代码版本。
价格口径：在复权（前复权）价上运行，分红再投资隐含在复权价中；涨跌停 / 整手 / 股数取整用不复权价（5.5.1-5）。
幸存者偏差：A 股库内保留退市股且 L2 逐日点时；行业映射为当前快照，结果带轻微未来信息（3.14），均在结果中标注。
"""
from __future__ import annotations

import json
import math
import uuid
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

from . import db, execution, features as feats, market_calendar as mc, markets, selection, settings, setups, universe
from .panel import load_panel
from .scanner import load_bench, load_industry

US_BIAS_NOTE = ("美股：免费源（yfinance）不含已退市股票的历史，回测存在**幸存者偏差**，结果只作上界参考；"
                "真正无偏的证据只有 scan_outcomes（每日扫描之后的真实前向收益，5.4）。")

BIAS_NOTES = [
    "行业映射为当前快照（3.14）：历史上的行业调整无法还原，「行业强弱」判断带轻微未来信息。",
    "A 股：已退市股票保留且股票池逐日点时过滤；L1 入库粗筛基于入库当日快照，可能使结果偏乐观（3.26.1-6）。",
    "回测在复权价上运行，分红再投资隐含在复权价中；A 股红利税第一阶段忽略。",
    "本回测不是收益预测，而是对历史规律的统计检验；参数请只用样本外结果确认（6.6）。",
]


def default_strategy() -> dict:
    c = settings.cfg()
    return {
        "id": "default", "market": "CN", "start": None, "end": None,
        "setups": list(c["setups"]["enabled"]), "setup_params": {},
        "entry_mode": c["execution"]["entry_mode"], "stop_entry_valid_days": c["execution"]["stop_entry_valid_days"],
        "max_gap_atr": c["execution"].get("max_gap_atr", 0), "resize_on_fill": c["execution"].get("resize_on_fill", False),
        "slippage": c["execution"]["slippage"], "max_adv_pct": c["execution"]["max_adv_pct"],
        "exits": dict(c["exits"]), "portfolio": dict(c["portfolio"]),
        "funnel": {"risk_exclusion": True, "regime_gate": True, "industry_score": True, "rps_score": True,
                   "rps_min": 0, "top_n": c["funnel"]["top_n"], "cooldown_days": c["funnel"]["cooldown_days"]},
        "cost_mult": 1.0, "slip_mult": 1.0, "oos_start": None, "initial_equity": c["portfolio"]["initial_equity"],
    }


def merge_strategy(over: dict | None) -> dict:
    return settings.deep_merge(default_strategy(), over or {})


class BtContext:
    """一次准备、多次运行（特征与基础信号只算一遍，供多个策略变体 / 消融 / 参数网格复用）。"""

    def __init__(self, conn, market: str = "CN", start: str | None = None, end: str | None = None, warmup: int = 300,
                 symbols: list[str] | None = None):
        self.market = market
        days_all = mc.trading_days(conn, None, end)
        data_last = conn.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        self.data_asof = db.get_meta(conn, "data_asof") or data_last
        if end is None:
            end = data_last
        days_all = [d for d in days_all if d <= end]
        if start:
            i0 = max(0, next((i for i, d in enumerate(days_all) if d >= start), len(days_all)) - warmup)
        else:
            i0 = 0
        load_from = days_all[i0] if days_all else None
        with settings.market_ctx(market):
            self.panel = load_panel(conn, load_from, end, market=market, symbols=symbols)      # symbols：抽样回测（内存不够跑全市场时）
            P = self.panel
            self.boards, self.list_dates, self.names = universe.load_security_meta(conn)
            self.l2, self.cond, self.flags = universe.l2_mask(P, self.boards, self.list_dates, return_parts=True, market=market)
            self.bench = load_bench(conn, settings.cfg()["regime"]["benchmark"], end)
            vix = load_bench(conn, "^VIX", end) if market == "US" else None
            self.industry = load_industry(conn)
            self.feat = feats.compute_features(P, self.l2, self.bench, self.industry, vix=vix)
        self.t1 = markets.t_plus_1(market)
        self.dates = list(P.dates)
        self.syms = list(P.symbols)
        self.T, self.N = len(self.dates), len(self.syms)
        self.start = start or (self.dates[0] if self.dates else None)
        self.end = end
        a = lambda df: df.to_numpy(dtype=np.float64)  # noqa: E731
        self.O, self.H, self.L, self.C = (a(P.adj[k]) for k in ("open", "high", "low", "close"))
        self.rO, self.rH, self.rL, self.rC = (a(P.raw[k]) for k in ("open", "high", "low", "close"))
        self.S = self.rC / self.C                                    # 原始价 / 复权价（每日每股比例）
        self.status = P.status.to_numpy()
        self.ol_up = self.flags["open_limit_up"].to_numpy()
        self.ol_dn = self.flags["open_limit_down"].to_numpy()
        self.lim_up = self.flags["limit_up"].to_numpy()
        self.oneword = self.flags["oneword"].to_numpy()
        F = self.feat.f
        self.atr = a(F["atr14"])
        self.atr_pct = a(F["atr_pct"])
        self.ma10, self.ma20 = a(F["ma10"]), a(F["ma20"])
        self.adv20 = a(F["amt_ma20"])
        self.rps20 = a(F["rps_20"])
        self.ll5_incl, self.struct_low_b = a(F["ll5_incl"]), a(F["struct_low_breakout"])
        self.regime = self.feat.regime.reindex(P.dates).fillna("UNKNOWN").to_numpy()
        self.ind_arr = np.array([self.industry.get(s) if len(self.industry) else None for s in self.syms], dtype=object)
        self.board_arr = [self.boards.get(s) for s in self.syms]
        self.l2_arr = self.l2.to_numpy()
        self.has_earnings = False
        self.earn_block = np.zeros((self.T, self.N), dtype=bool)
        self._load_earnings(conn)
        self._sig_cache: dict[str, tuple] = {}
        self.date_idx = {d: i for i, d in enumerate(self.dates)}
        self.trials = 0

    def _load_earnings(self, conn):
        """财报窗口（点时）：实际披露日按 window 排除；预计 / 法定截止日按 window + margin 放宽。
        注意：财报日历只对「扫描候选与持仓」按需拉取，回测只能用库里已有的事件——覆盖不全时结果标注 earnings_data=False 的局限。"""
        rows = conn.execute("SELECT symbol, event_time, source FROM events WHERE event_type='EARNINGS'").fetchall()
        if not rows:
            return
        self.has_earnings = True
        sidx = {s: j for j, s in enumerate(self.syms)}
        fcfg = settings.cfg()["funnel"]["risk_exclusion"]
        win, margin = fcfg["earnings_window_days"], fcfg.get("projection_margin_days", 0)
        darr = np.array(self.dates)
        for sym, d, src in rows:
            j = sidx.get(sym)
            if j is None:
                continue
            k = int(np.searchsorted(darr, d))
            w = win if src == "baostock" else win + margin
            self.earn_block[max(0, k - w):k, j] = True

    # ---- 信号 ----
    def signals(self, st: dict):
        key = json.dumps({"s": st["setups"], "p": st["setup_params"], "cd": st["funnel"]["cooldown_days"]}, sort_keys=True)
        if key not in self._sig_cache:
            params = settings.deep_merge(setups.default_params(), st["setup_params"])
            sigs = setups.detect_all(self.feat, params, st["setups"])
            sigs = {k: setups.apply_cooldown(v & self.l2, st["funnel"]["cooldown_days"]) for k, v in sigs.items()}
            any_sig, primary = setups.merge_setups(sigs)
            self._sig_cache[key] = (sigs, any_sig.to_numpy(), primary.to_numpy(), list(sigs))
        return self._sig_cache[key]

    def score_arr(self, st: dict):
        f = st["funnel"]
        if not f["industry_score"] and not f["rps_score"]:
            return None
        w = {"rps": 1.0 if f["rps_score"] else 0.0, "industry": 1.0 if f["industry_score"] else 0.0}
        return setups.score_frame(self.feat, w).to_numpy(dtype=np.float64)

    # ---- 单笔交易退出路径（纯函数）----
    def trade_path(self, j: int, e: int, fill: float, stop0: float, st: dict, with_stops: bool = False) -> dict:
        """从入场日 e（成交价 fill，复权价）出发，按 5.5.1 推演到退出。返回退出日 / 价 / 原因 / MAE / MFE。"""
        ex = st["exits"]
        slip = st["slippage"] * st["slip_mult"]
        O, H, L, C = self.O[:, j], self.H[:, j], self.L[:, j], self.C[:, j]
        stat = self.status[:, j]
        stop, highest = stop0, fill
        pending, reason = False, ""
        mae, mfe = 0.0, 0.0
        stops: dict[int, float] = {}
        T = self.T
        risk0 = fill - stop0
        tp = None                                      # 固定目标位（仅作对照，5.6）
        if ex.get("take_profit_pct"):
            tp = fill * (1 + ex["take_profit_pct"])
        elif ex.get("take_profit_r"):
            tp = fill + ex["take_profit_r"] * risk0
        ptarget = fill + ex["partial_r"] * risk0 if ex.get("partial_r") else None
        partial = None                                 # (t, 价格(复权, 已含滑点), 比例)
        for t in range(e, T):
            if t > e or not self.t1:                   # A 股 T+1：买入日不可卖出；美股 T+0：买入日止损即时生效
                if t > e and stat[t] == 0:             # 停牌：无价格行为
                    continue
                if pending:
                    if self.ol_dn[t, j]:               # 跌停开盘卖不出：顺延
                        continue
                    return self._exit(t, O[t] * (1 - slip), reason, fill, mae, mfe, e, stops if with_stops else None, partial)
                if L[t] <= stop:                       # 止损（5.5.1-3：止损与止盈同日取最坏——此处无固定止盈）
                    if self.ol_dn[t, j]:
                        pending, reason = True, "止损(跌停顺延)"
                        mae = min(mae, L[t] / fill - 1)
                        continue
                    px = O[t] if O[t] <= stop else stop      # 跳空低开：按开盘价成交（劣于止损价）
                    tag = "止损(跳空)" if O[t] <= stop else "止损"
                    mae = min(mae, L[t] / fill - 1)
                    mfe = max(mfe, H[t] / fill - 1)
                    return self._exit(t, px * (1 - slip), tag, fill, mae, mfe, e, stops if with_stops else None, partial)
                # 止损优先于止盈：同日同时触及，日线无法判断先后，一律按最坏情形（先止损，5.5.1-3）——上面的止损判断已先行
                if tp is not None and H[t] >= tp:
                    px = O[t] if O[t] >= tp else tp
                    mfe = max(mfe, H[t] / fill - 1)
                    mae = min(mae, L[t] / fill - 1)
                    return self._exit(t, px * (1 - slip), "固定止盈", fill, mae, mfe, e, stops if with_stops else None, partial)
                if ptarget is not None and partial is None and H[t] >= ptarget:
                    ppx = (O[t] if O[t] >= ptarget else ptarget) * (1 - slip)
                    partial = (t, ppx, float(ex.get("partial_fraction", 0.5)))
                    if ex.get("partial_move_stop_be"):
                        stop = max(stop, fill)
            if stat[t] == 0:
                continue
            mae = min(mae, L[t] / fill - 1)
            mfe = max(mfe, H[t] / fill - 1)
            highest = max(highest, C[t])
            s_t = self.S[t, j]
            if s_t == s_t:                             # 移动止损在原始价空间计算（ATR 乘比例还原），再换回复权价；T+1 生效
                m10 = self.ma10[t, j] * s_t if self.ma10[t, j] == self.ma10[t, j] else None
                stop = execution.trailing_stop(stop * s_t, highest * s_t, self.atr[t, j] * s_t, m10,
                                               ex["trail"], ex["trail_atr_k"]) / s_t
            stops[t] = stop
            if t == e and self.t1 and C[t] < stop0:    # A 股 T+1：买入日止损不生效；收盘已破位则次日开盘卖出
                pending, reason = True, "止损(买入日破位)"
            if not pending:
                if ex.get("exit_below_ma") and self.ma20[t, j] == self.ma20[t, j] and C[t] < self.ma20[t, j]:
                    pending, reason = True, f"收盘跌破MA{ex['exit_below_ma']}"
                elif t - e >= ex["max_hold_days"]:
                    pending, reason = True, "时间退出"
        # 数据结束仍持有：按最后有效收盘估值（不计入已平仓统计）
        last = T - 1
        while last > e and stat[last] == 0:
            last -= 1
        return {"exit_t": None, "exit_adj": C[last], "reason": "持仓中", "mae": mae, "mfe": mfe, "last_t": last,
                "stops": stops if with_stops else None, "partial": partial}

    def _exit(self, t, px, reason, fill, mae, mfe, e, stops, partial=None):
        return {"exit_t": t, "exit_adj": px, "reason": reason, "mae": mae, "mfe": mfe, "last_t": t, "stops": stops, "partial": partial}

    # ---- 入场成交解析（5.5.1-1/2）----
    def resolve_entry(self, j: int, ts: int, trigger_raw: float, stop_adj: float, st: dict):
        """信号日 ts 收盘后下单 -> (成交日, 成交价(复权, 已含滑点), 放弃原因)。"""
        slip = st["slippage"] * st["slip_mult"]
        mode = st["entry_mode"]
        T = self.T
        if mode == "next_open":
            for k in range(ts + 1, min(T, ts + 6)):
                if self.status[k, j] == 0:
                    continue                           # 停牌顺延
                if self.ol_up[k, j]:
                    return None, None, "涨停开盘无法成交"
                gap_k = st.get("max_gap_atr") or 0
                if gap_k and self.atr[ts, j] == self.atr[ts, j] and self.O[k, j] > self.C[ts, j] + gap_k * self.atr[ts, j]:
                    return None, None, "高开过多放弃"            # 开盘比信号日收盘高出 k×ATR 以上：不追
                fill = self.O[k, j] * (1 + slip)
                if fill <= stop_adj:
                    return None, None, "跳空跌破止损位"
                return k, fill, ""
            return None, None, "停牌/无后续数据"
        trig = trigger_raw / self.S[ts, j]
        v = int(st["stop_entry_valid_days"])
        seen = 0
        for k in range(ts + 1, min(T, ts + 6)):
            if self.status[k, j] == 0:
                continue
            seen += 1
            if self.O[k, j] >= trig:
                if self.ol_up[k, j]:
                    return None, None, "涨停开盘无法成交"
                fill = self.O[k, j] * (1 + slip)
            elif self.H[k, j] >= trig:
                fill = trig * (1 + slip)
            else:
                if seen >= v:
                    break
                continue
            if fill <= stop_adj:
                return None, None, "跳空跌破止损位"
            return k, fill, ""
        return None, None, "触发价未触及(订单作废)"

    # ---- 组合回测 ----
    def run(self, over: dict | None = None, detail: bool = True) -> dict:
        st = merge_strategy(over)
        st["start"] = st["start"] or self.start
        c = settings.cfg()
        costs = {k: v * st["cost_mult"] for k, v in markets.cost_cfg(self.market).items()}
        P = st["portfolio"]
        f = st["funnel"]
        sigs, any_sig, primary, names = self.signals(st)
        score = self.score_arr(st)
        i0 = self.date_idx.get(st["start"]) if st["start"] in self.date_idx else \
            int(np.searchsorted(np.array(self.dates), st["start"] or self.dates[0]))
        end = st["end"] or self.dates[-1]
        i1 = int(np.searchsorted(np.array(self.dates), end, side="right")) - 1
        cash = float(st["initial_equity"])
        positions: dict[int, dict] = {}
        pending: dict[int, list] = {}                  # 成交日 -> 订单列表
        n_pending = 0
        trades, curve = [], []
        counters = {"signals": 0, "after_exclusion": 0, "regime_blocked": 0, "ordered": 0, "filled": 0}
        abandoned: dict[str, int] = {}
        bump = lambda k: abandoned.__setitem__(k, abandoned.get(k, 0) + 1)  # noqa: E731
        last_px: dict[int, float] = {}
        peak = float(st["initial_equity"])
        ddc = (P.get("dd_cut") or {}) if isinstance(P.get("dd_cut"), dict) else {}
        for t in range(max(i0, 1), i1 + 1):
            # 1) 入场成交（先于当日退出，对资金保守）
            for od in pending.pop(t, []):
                n_pending -= 1
                j = od["j"]
                fill_raw = od["fill"] * self.S[t, j]
                shares = od["shares"]
                if st.get("resize_on_fill"):                    # 高开：按实际成交价重算股数，单笔风险不超过计划（只减不加）
                    per = fill_raw - od["stop_adj"] * self.S[t, j]
                    if per > 0 and od["risk_amount"] > 0:
                        shares = min(shares, execution.lot_round(self.board_arr[j], od["risk_amount"] / per))
                    if shares <= 0:
                        bump("高开后风险超预算，放弃")
                        continue
                fee_b = execution.trade_fees("buy", fill_raw, shares, costs, self.market)
                while shares > 0 and shares * fill_raw + execution.trade_fees("buy", fill_raw, shares, costs, self.market) > cash:
                    shares = execution.lot_round(self.board_arr[j], shares - execution.lot_step(self.board_arr[j]))
                if shares <= 0:
                    bump("资金不足")
                    continue
                fee_b = execution.trade_fees("buy", fill_raw, shares, costs, self.market)
                cash -= shares * fill_raw + fee_b
                path = self.trade_path(j, t, od["fill"], od["stop_adj"], st, with_stops=True)
                positions[j] = {**od, "e": t, "shares": shares, "init_shares": shares, "s": self.S[t, j], "fee_b": fee_b, "path": path,
                                "entry_raw": fill_raw, "planned_risk": (fill_raw - od["stop_adj"] * self.S[t, j]) * shares,
                                "realized": 0.0, "fees_partial": 0.0, "partial_done": False}
                last_px[j] = od["fill"]
                counters["filled"] += 1
            # 2a) 分批止盈：卖出一部分（整手取整），其余继续持有
            for j, p in positions.items():
                pt = p["path"].get("partial")
                if pt and pt[0] == t and not p["partial_done"] and p["path"]["exit_t"] != t:
                    q = execution.lot_round(self.board_arr[j], p["shares"] * pt[2])
                    if 0 < q < p["shares"]:
                        px_raw = pt[1] * p["s"]
                        fee_p = execution.trade_fees("sell", px_raw, q, costs, self.market)
                        cash += q * px_raw - fee_p
                        p["realized"] += (px_raw - p["entry_raw"]) * q
                        p["fees_partial"] += fee_p
                        p["shares"] -= q
                    p["partial_done"] = True
            # 2b) 当日退出
            for j in [j for j, p in positions.items() if p["path"]["exit_t"] == t]:
                p = positions.pop(j)
                px_raw = p["path"]["exit_adj"] * p["s"]
                fee_s = execution.trade_fees("sell", px_raw, p["shares"], costs, self.market)
                cash += p["shares"] * px_raw - fee_s
                pnl = p["realized"] + (px_raw - p["entry_raw"]) * p["shares"] - p["fee_b"] - fee_s - p["fees_partial"]
                trades.append(self._trade_rec(p, t, px_raw, pnl, fee_s + p["fees_partial"]))
            # 3) 盯市
            mv = 0.0
            for j, p in positions.items():
                cj = self.C[t, j]
                if cj == cj:
                    last_px[j] = cj
                mv += p["shares"] * last_px.get(j, p["fill"]) * p["s"]
            equity = cash + mv
            peak = max(peak, equity)
            curve.append((self.dates[t], equity, mv, cash, str(self.regime[t])))
            # 4) 生成次日订单
            if t >= i1:
                continue
            idx = np.nonzero(any_sig[t])[0]
            counters["signals"] += len(idx)
            if not len(idx):
                continue
            cands = []
            for j in idx:
                if j in positions or any(o["j"] == j for ods in pending.values() for o in ods):
                    continue
                if f["risk_exclusion"] and (self.oneword[t, j] or self.lim_up[t, j] or self.earn_block[t, j]):
                    bump("风险剔除(涨停/财报)")
                    continue
                if f["rps_min"] and not (self.rps20[t, j] >= f["rps_min"]):
                    bump("RPS 硬过滤")
                    continue
                s_t = self.S[t, j]
                cands.append({"j": j, "symbol": self.syms[j], "board": self.board_arr[j], "industry": self.ind_arr[j],
                              "close": self.rC[t, j], "high": self.rH[t, j], "atr14": self.atr[t, j] * s_t,
                              "atr_pct": self.atr_pct[t, j], "adv20": self.adv20[t, j],
                              "score": None if score is None or score[t, j] != score[t, j] else float(score[t, j]),
                              "setup": names[int(primary[t, j]) - 1],
                              "struct_low": self._struct_low(t, j, names[int(primary[t, j]) - 1])})
            counters["after_exclusion"] += len(cands)
            if not cands:
                continue
            reg = str(self.regime[t])
            factor = 1.0
            if f["regime_gate"]:
                if reg == "DEFENSIVE":
                    counters["regime_blocked"] += len(cands)
                    continue
                if reg in ("CAUTION", "UNKNOWN"):
                    factor = P["caution_position_factor"]
            max_pos = max(1, int(round(P["max_positions"] * factor)))
            held_ind: dict = {}
            risk_used = 0.0
            for j, p in positions.items():
                held_ind[self.ind_arr[j]] = held_ind.get(self.ind_arr[j], 0) + 1
                sp = p["path"]["stops"].get(t)
                cj = last_px.get(j, p["fill"])
                risk_used += max(0.0, (cj - (sp if sp is not None else p["stop_adj"])) * p["shares"] * p["s"])
            for ods in pending.values():
                for o in ods:
                    held_ind[self.ind_arr[o["j"]]] = held_ind.get(self.ind_arr[o["j"]], 0) + 1
                    risk_used += o["risk_amount"]
            risk_cap = equity * P["max_total_risk"] * factor
            ranked = selection.rank_candidates(cands)
            if f["regime_gate"] and factor < 1:
                ranked = ranked[:max(1, int(math.ceil(len(ranked) * factor)))]
            sel = selection.select_and_size(ranked, slots=max_pos - len(positions) - n_pending, held_ind=held_ind,
                                            risk_used=risk_used, risk_cap=risk_cap, equity=equity, risk_pct=P["risk_per_trade"] * (ddc.get("factor", 1.0) if ddc.get("threshold") and equity <= peak * (1 - ddc["threshold"]) else 1.0),
                                            top_n=f["top_n"] or None, entry_mode=st["entry_mode"],
                                            max_per_industry=P["max_per_industry"], max_adv_pct=st["max_adv_pct"], rank=False,
                                            sizing=P.get("sizing", "risk"), max_positions=max_pos, stop_mode=st["exits"].get("stop_mode", "atr"),
                                            exits=st["exits"])
            for r in sel:
                if not r["fit"]:
                    bump(r["skip_reason"])
                    continue
                j = r["j"]
                s_t = self.S[t, j]
                stop_adj = r["stop_price"] / s_t
                k, fill, why = self.resolve_entry(j, t, r["trigger_price"], stop_adj, st)
                if k is None:
                    bump(why)
                    continue
                counters["ordered"] += 1
                pending.setdefault(k, []).append({"j": j, "fill": fill, "stop_adj": stop_adj, "shares": r["shares"],
                                                  "risk_amount": r["risk_amount"], "setup": r["setup"], "signal_t": t,
                                                  "regime": reg, "score": r["score"]})
                n_pending += 1
        # 数据末尾仍未平仓的持仓
        open_pos = []
        for j, p in positions.items():
            open_pos.append({"symbol": self.syms[j], "entry_date": self.dates[p["e"]], "entry_px": round(p["entry_raw"], 3),
                             "shares": p["shares"], "setup": p["setup"]})
        res = self._summarize(st, curve, trades, counters, abandoned, i0, i1, open_pos)
        self.trials += 1
        return res

    def _struct_low(self, t, j, setup):
        """结构化止损所用的形态低点（原始价）：回踩 = 近 5 日最低（含当日）；突破 / 波动收缩 = 近 10 日整理区低点（不含当日）。"""
        arr = self.ll5_incl if setup in ("pullback", "oversold") else self.struct_low_b
        v = arr[t, j] * self.S[t, j]
        return float(v) if v == v else None

    def _trade_rec(self, p, t, px_raw, pnl, fee_s):
        j = p["j"]
        risk = p["planned_risk"]
        return {"symbol": self.syms[j], "name": self.names.get(self.syms[j], ""), "setup": p["setup"], "regime": p["regime"],
                "signal_date": self.dates[p["signal_t"]], "entry_date": self.dates[p["e"]], "exit_date": self.dates[t],
                "entry_px": round(p["entry_raw"], 3), "exit_px": round(px_raw, 3), "shares": p.get("init_shares", p["shares"]),
                "pnl": round(pnl, 2), "fees": round(p["fee_b"] + fee_s, 2), "ret": round(px_raw / p["entry_raw"] - 1, 4),
                "r": round(pnl / risk, 3) if risk > 0 else None, "hold_days": t - p["e"],
                "mae": round(p["path"]["mae"], 4), "mfe": round(p["path"]["mfe"], 4), "exit_reason": p["path"]["reason"],
                "notional": round(p["entry_raw"] * p.get("init_shares", p["shares"]), 2), "score": p.get("score"),
                "partial": bool(p.get("partial_done"))}

    # ---- 汇总 ----
    def _summarize(self, st, curve, trades, counters, abandoned, i0, i1, open_pos) -> dict:
        eq = pd.Series([c[1] for c in curve], index=[c[0] for c in curve], dtype=float)
        mv = pd.Series([c[2] for c in curve], index=eq.index, dtype=float)
        reg = pd.Series([c[4] for c in curve], index=eq.index)
        bench = None
        if self.bench is not None and len(eq):
            bench = self.bench.reindex(eq.index).ffill()
        tr = pd.DataFrame(trades)
        metrics = perf_metrics(eq, mv, tr, bench, counters, abandoned)
        seg = {"by_year": {}, "by_regime": {}}
        oos = None
        if len(tr):
            tr["year"] = tr["entry_date"].str[:4]
            for y, g in tr.groupby("year"):
                seg["by_year"][y] = trade_stats(g)
            for rg, g in tr.groupby("regime"):
                seg["by_regime"][rg] = trade_stats(g)
            if st.get("oos_start"):
                a, b = tr[tr["entry_date"] < st["oos_start"]], tr[tr["entry_date"] >= st["oos_start"]]
                oos = {"oos_start": st["oos_start"], "in_sample": trade_stats(a), "out_of_sample": trade_stats(b)}
        out = {"strategy": {k: v for k, v in st.items() if k != "setup_params"} | {"setup_params": st["setup_params"]},
               "period": {"start": self.dates[max(i0, 1)] if self.dates else None, "end": self.dates[i1] if self.dates else None},
               "metrics": metrics, "segments": seg, "oos": oos, "open_positions": open_pos,
               "notes": ([US_BIAS_NOTE] if self.market == "US" else []) + list(BIAS_NOTES),
               "earnings_data": self.has_earnings,
               "equity_curve": [{"date": d, "equity": round(e, 2), "regime": r} for d, e, r in
                                zip(eq.index[::max(1, len(eq) // 600)], eq.values[::max(1, len(eq) // 600)], reg.values[::max(1, len(eq) // 600)])],
               "drawdown_curve": drawdown_curve(eq, max(1, len(eq) // 600)),
               "benchmark_curve": None if bench is None else
               [{"date": d, "value": round(float(v / bench.iloc[0] * eq.iloc[0]), 2)} for d, v in zip(bench.index[::max(1, len(eq) // 600)], bench.values[::max(1, len(eq) // 600)])]}
        out["trades"] = trades
        return out


# ---- 指标 ---------------------------------------------------------------------

def trade_stats(g: pd.DataFrame) -> dict:
    n = len(g)
    if not n:
        return {"n": 0}
    r = g["r"].dropna()
    wins, losses = g[g["pnl"] > 0], g[g["pnl"] <= 0]
    gw, gl = wins["pnl"].sum(), -losses["pnl"].sum()
    return {"n": n, "win_rate": round(len(wins) / n, 3), "expectancy_r": round(float(r.mean()), 3) if len(r) else None,
            "avg_ret": round(float(g["ret"].mean()), 4),
            "payoff": round(float(wins["pnl"].mean() / -losses["pnl"].mean()), 2) if len(wins) and len(losses) and losses["pnl"].mean() < 0 else None,
            "profit_factor": round(gw / gl, 2) if gl > 0 else None, "pnl": round(float(g["pnl"].sum()), 2),
            "avg_hold_days": round(float(g["hold_days"].mean()), 1)}


def drawdown_curve(eq: pd.Series, step: int) -> list[dict]:
    if not len(eq):
        return []
    dd = eq / eq.cummax() - 1
    return [{"date": d, "dd": round(float(v), 4)} for d, v in zip(dd.index[::step], dd.values[::step])]


def perf_metrics(eq: pd.Series, mv: pd.Series, tr: pd.DataFrame, bench: pd.Series | None, counters: dict, abandoned: dict) -> dict:
    m: dict = {}
    n = len(eq)
    if n < 2:
        return {"error": "区间过短"}
    ret = eq.pct_change().dropna()
    years = n / 252
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else None
    dd = eq / eq.cummax() - 1
    mdd = float(dd.min())
    # 回撤持续时间：最长一段「低于前高」的天数
    under = (eq < eq.cummax()).astype(int)
    longest = cur = 0
    for v in under:
        cur = cur + 1 if v else 0
        longest = max(longest, cur)
    down = ret[ret < 0]
    m.update({"total_return": round(float(total), 4), "cagr": None if cagr is None else round(float(cagr), 4),
              "max_drawdown": round(mdd, 4), "max_dd_days": int(longest),
              "sharpe": round(float(ret.mean() / ret.std() * np.sqrt(252)), 2) if ret.std() > 0 else None,
              "sortino": round(float(ret.mean() / down.std() * np.sqrt(252)), 2) if len(down) > 1 and down.std() > 0 else None,
              "calmar": round(float(cagr / abs(mdd)), 2) if cagr is not None and mdd < 0 else None,
              "exposure": round(float((mv / eq).mean()), 3), "days": n})
    if len(tr):
        s = trade_stats(tr)
        pnl_sign = (tr["pnl"] > 0).tolist()
        mc_, cur = 0, 0
        for w in pnl_sign:
            cur = 0 if w else cur + 1
            mc_ = max(mc_, cur)
        turnover = tr["notional"].sum() * 2 / eq.mean() / max(years, 1e-9)
        m.update({k: v for k, v in s.items() if k != "pnl"})
        m.update({"avg_mae": round(float(tr["mae"].mean()), 4), "avg_mfe": round(float(tr["mfe"].mean()), 4),
                  "max_consec_losses": int(mc_), "turnover_per_year": round(float(turnover), 2),
                  "loss_over_1_5r": int((tr["r"].dropna() < -1.5).sum()), "total_fees": round(float(tr["fees"].sum()), 2)})
    else:
        m.update({"n": 0})
    if bench is not None and bench.notna().sum() > 30:
        br = bench.pct_change().dropna()
        j = pd.concat([ret, br], axis=1, join="inner").dropna()
        if len(j) > 30 and j.iloc[:, 1].var() > 0:
            beta = float(np.cov(j.iloc[:, 0], j.iloc[:, 1])[0, 1] / j.iloc[:, 1].var())
            alpha = float((j.iloc[:, 0].mean() - beta * j.iloc[:, 1].mean()) * 252)
            btot = float(bench.iloc[-1] / bench.iloc[0] - 1)
            m.update({"alpha": round(alpha, 4), "beta": round(beta, 2), "benchmark_return": round(btot, 4),
                      "excess_return": round(float(total - btot), 4)})
    m["sample"] = {**counters, "abandoned": abandoned, "abandoned_total": int(sum(abandoned.values()))}
    if len(ret) > 30 and ret.std() > 0:                           # Deflated Sharpe 所需的收益分布矩（run_job 结合试验次数计算）
        m["ret_moments"] = {"sr_daily": float(ret.mean() / ret.std()), "skew": float(ret.skew()), "kurt": float(ret.kurt() + 3), "T": int(len(ret))}
    return m


# ---- 单因子检验（6.6.1）-------------------------------------------------------

FACTORS = ["rps_10", "rps_20", "rps_60", "ret_5", "ret_10", "ret_20", "vol_ratio", "atr_pct", "dist_52w_high",
           "risk_adj_mom20", "close_ma20", "trend_r2", "rs_20", "ind_rps_20", "bb_pct120", "rsi14", "close_pos"]


def _row_corr(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    m = ~(np.isnan(a) | np.isnan(b))
    a = np.where(m, a, np.nan)
    b = np.where(m, b, np.nan)
    n = m.sum(1)
    am = np.nanmean(a, 1, keepdims=True)
    bm = np.nanmean(b, 1, keepdims=True)
    da, db_ = a - am, b - bm
    num = np.nansum(da * db_, 1)
    den = np.sqrt(np.nansum(da ** 2, 1) * np.nansum(db_ ** 2, 1))
    out = np.where((den > 0) & (n >= 30), num / den, np.nan)
    return out


def single_factor_test(ctx: BtContext, factors: list[str] | None = None, horizons=(5, 10, 20), n_groups: int = 5,
                       start: str | None = None, end: str | None = None) -> dict:
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return _single_factor_test(ctx, factors, horizons, n_groups, start, end)


def factor_verdict(row: dict, horizons) -> dict:
    """按 config.validation.factor 判定：至少 min_horizons_pass 个周期同时满足 |IC|、IR、t 值、分层单调性且方向一致。"""
    v = settings.cfg()["validation"]["factor"]
    passed, why = 0, []
    signs = set()
    for h in horizons:
        x = row["horizons"].get(h) or {}
        if x.get("ic_mean") is None:
            continue
        ok = (abs(x["ic_mean"]) >= v["min_abs_ic"] and abs(x.get("ic_ir") or 0) >= v["min_ic_ir"]
              and abs(x.get("t_stat_eff") or 0) >= v["min_t_eff"] and abs(x.get("monotonic") or 0) >= v["min_abs_monotonic"]
              and (x["ic_mean"] > 0) == ((x.get("long_short") or 0) > 0))
        if ok:
            passed += 1
            signs.add(x["ic_mean"] > 0)
    ok = passed >= v["min_horizons_pass"] and len(signs) == 1
    if not ok:
        why.append(f"仅 {passed} 个周期达标（需 ≥ {v['min_horizons_pass']}）" if len(signs) <= 1 else "各周期方向不一致")
    return {"pass": ok, "horizons_passed": passed, "direction": ("正向" if True in signs else "反向") if signs else None,
            "reason": "；".join(why)}


def _single_factor_test(ctx, factors, horizons, n_groups, start, end) -> dict:
    """Rank IC（因子截面排名与未来 h 日收益排名的 Spearman 相关）、IC IR、分层收益、方向确认。
    未来收益按 5.5.1 成交假设：T 日收盘出信号，T+1 开盘入场，持有 h 日后收盘。只在当日交易池 L2 内检验。"""
    factors = [f for f in (factors or FACTORS) if f in ctx.feat.f]
    O, C, l2 = ctx.O, ctx.C, ctx.l2_arr
    T = ctx.T
    i0 = int(np.searchsorted(np.array(ctx.dates), start or ctx.dates[0]))
    i1 = int(np.searchsorted(np.array(ctx.dates), end or ctx.dates[-1], side="right"))
    out = []
    fwd = {}
    for h in horizons:
        r = np.full((T, ctx.N), np.nan)
        r[:T - h - 1] = C[h + 1:] / O[1:T - h] - 1 if T > h + 1 else r[:0]
        fwd[h] = np.where(l2, r, np.nan)
    for name in factors:
        X = ctx.feat.f[name].to_numpy(dtype=np.float64)
        X = np.where(l2, X, np.nan)
        Xr = pd.DataFrame(X).rank(axis=1).to_numpy()
        row = {"factor": name, "horizons": {}}
        for h in horizons:
            Yr = pd.DataFrame(fwd[h]).rank(axis=1).to_numpy()
            ic = _row_corr(Xr[i0:i1], Yr[i0:i1])
            ic = ic[~np.isnan(ic)]
            if len(ic) < 30:
                row["horizons"][h] = {"n_days": int(len(ic)), "note": "样本不足"}
                continue
            # 前瞻窗口重叠 -> IC 序列自相关，t 值按有效样本 N/h 折算
            mean, sd = float(ic.mean()), float(ic.std(ddof=1))
            t_eff = mean / (sd / np.sqrt(max(len(ic) / h, 1))) if sd > 0 else None
            # 分层收益
            q = pd.DataFrame(X[i0:i1]).rank(axis=1, pct=True)
            grp = np.ceil(q.to_numpy() * n_groups).clip(1, n_groups)
            fr = fwd[h][i0:i1]
            g_ret = []
            for g in range(1, n_groups + 1):
                sel = np.where(grp == g, fr, np.nan)
                dm = np.nanmean(sel, 1)
                g_ret.append(float(np.nanmean(dm)))
            mono = float(pd.Series(g_ret).corr(pd.Series(range(n_groups)), method="spearman")) if len(set(g_ret)) > 1 else None
            row["horizons"][h] = {"n_days": int(len(ic)), "ic_mean": round(mean, 4), "ic_std": round(sd, 4),
                                  "ic_ir": round(mean / sd, 3) if sd > 0 else None,
                                  "t_stat_eff": None if t_eff is None else round(float(t_eff), 2),
                                  "ic_pos_ratio": round(float((ic > 0).mean()), 3),
                                  "group_ret": [round(x, 5) for x in g_ret], "monotonic": None if mono is None else round(mono, 2),
                                  "long_short": round(g_ret[-1] - g_ret[0], 5),
                                  "direction": "正向（因子越大越好）" if mean > 0 else "反向（因子越小越好）"}
        row["verdict"] = factor_verdict(row, horizons)
        out.append(row)
    return {"kind": "single_factor", "factors": out, "horizons": list(horizons), "groups": n_groups,
            "note": "通过标准（IC 均值、IR、单调性门槛）实施前请与你确认（6.6.1 / 附录 B.2-6）；方向不预设，以检验结果为准。",
            "caveat": "t 值已按前瞻窗口重叠折算有效样本数；因子是否进入买入条件 / 打分，以样本外结论为准。"}


# ---- 形态事件研究（6.6.2）-----------------------------------------------------

def _event_one(ctx: BtContext, st: dict, t: int, j: int, cost_rate: float) -> dict | None:
    """单个 (信号日, 股票) 独立推演一笔「单位风险」交易（无资金约束）：按 5.5.1 成交规则入场，按同一套出场规则退出。"""
    ex = st["exits"]
    s_t = ctx.S[t, j]
    atr = ctx.atr[t, j] * s_t
    if not (atr == atr):
        return None
    close_raw, high_raw = ctx.rC[t, j], ctx.rH[t, j]
    trig = round(high_raw + execution.TICK, 2)
    ref = trig if st["entry_mode"] == "stop_entry" else round(close_raw, 2)
    stop_raw = execution.initial_stop(ref, atr, ex["stop_atr_k"], ex["hard_stop_pct"])
    stop_adj = stop_raw / s_t
    k, fill, why = ctx.resolve_entry(j, t, trig, stop_adj, st)
    if k is None:
        return {"t": t, "j": j, "filled": False, "why": why}
    path = ctx.trade_path(j, k, fill, stop_adj, st)
    if path["exit_t"] is None:
        return None
    risk = fill - stop_adj
    net = (path["exit_adj"] - fill) - cost_rate * fill
    rec = {"t": t, "j": j, "filled": True, "r": net / risk, "ret": net / fill, "hold": path["exit_t"] - k,
           "mae": path["mae"], "mfe": path["mfe"], "reason": path["reason"], "entry_t": k}
    for h in (3, 5, 10, 20):
        kk = k + h - 1
        rec[f"ret_{h}d"] = (ctx.C[kk, j] / fill - 1) if kk < ctx.T and ctx.C[kk, j] == ctx.C[kk, j] else np.nan
    return rec


def _cost_rate(st: dict, market: str = "CN") -> float:
    """往返比例成本（用于事件研究的单位风险交易）。美股：卖出规费按成交额；佣金 / TAF 按股数，对万元级以上持仓可忽略。"""
    cc = markets.cost_cfg(market)
    if market == "US":
        return cc.get("sec_fee_sell", 0.0) * st["cost_mult"]
    return (cc["commission_rate"] * 2 + cc["transfer_fee"] * 2 + cc["stamp_duty_sell"]) * st["cost_mult"]


def _event_trades(ctx: BtContext, st: dict, cells: list[tuple[int, int]], setup: str | None = None) -> pd.DataFrame:
    cr = _cost_rate(st, ctx.market)
    rows = [r for r in (_event_one(ctx, st, t, j, cr) for t, j in cells) if r is not None]
    return pd.DataFrame(rows)


def _day_agg(df: pd.DataFrame, col: str) -> pd.Series:
    """按信号日聚合（同日信号高度相关，不能当独立样本，6.6.2）。"""
    return df[df["filled"]].groupby("t")[col].mean()


def block_bootstrap_ci(x: np.ndarray, block: int = 10, n: int = 2000, seed: int = 1) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    L = len(x)
    if L < block * 2:
        return (float("nan"), float("nan"))
    nb = int(np.ceil(L / block))
    means = np.empty(n)
    for i in range(n):
        starts = rng.integers(0, L - block + 1, nb)
        means[i] = np.concatenate([x[s:s + block] for s in starts])[:L].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def event_verdict(res: dict) -> tuple[str, bool]:
    """按 config.validation.event 判定；独立信号日过少不下结论。"""
    v = settings.cfg()["validation"]["event"]
    if res["independent_signal_days"] < v["min_independent_days"]:
        return f"独立信号日过少（< {v['min_independent_days']}），不下结论", False
    fails = []
    p = res.get("random_baseline", {}).get("p_value")
    if p is None or p > v["max_p"]:
        fails.append(f"p 值 {p} > {v['max_p']}（未显著优于随机入场）")
    if res["mean_r_by_day"] <= v["min_mean_r"]:
        fails.append("期望 R 非正")
    lo = (res.get("mean_r_ci95") or [None])[0]
    if v["ci_lower_gt_zero"] and (lo is None or lo <= 0):
        fails.append("95% 置信区间下限 ≤ 0")
    yrs = [y for y in res.get("by_year", {}).values() if y["n"] >= v["min_year_n"]]
    if yrs and sum(1 for y in yrs if y["mean_r"] > 0) / len(yrs) < v["min_positive_year_ratio"]:
        fails.append("只靠少数年份（正期望年份占比不足）")
    if fails:
        return "未通过：" + "；".join(fails) + " → 保持「仅标注、不进入综合打分」", False
    return "通过：显著优于随机入场且分年度稳定，可评估升格进入综合打分（仍需样本外确认）", True


def event_study(ctx: BtContext, over: dict | None = None, n_random: int = 200, seed: int = 7,
                setup_names: list[str] | None = None) -> dict:
    """每个形态单独做事件研究：触发后收益分布、随机基线 p 值、交易质量、信号聚集处理、分段稳定性、样本量。
    随机基线 = 同一天、同一交易池内随机抽取相同数量的标的，**只替换入场条件，出场 / 仓位 / 成交规则不变**。"""
    st = merge_strategy(over)
    rng = np.random.default_rng(seed)
    names = setup_names or st["setups"]
    out = []
    i0 = int(np.searchsorted(np.array(ctx.dates), st["start"] or ctx.dates[0]))
    for nm in names:
        st1 = merge_strategy({**(over or {}), "setups": [nm]})
        sigs, any_sig, _, _ = ctx.signals(st1)
        cells = [(int(t), int(j)) for t, j in zip(*np.nonzero(any_sig)) if t >= max(i0, 1) and t < ctx.T - 2]
        ev = _event_trades(ctx, st1, cells)
        res = {"setup": nm, "label": setups.SETUP_LABEL.get(nm, nm), "n_signals": len(cells),
               "n_filled": int(ev["filled"].sum()) if len(ev) else 0}
        if not len(ev) or res["n_filled"] == 0:
            res["verdict"] = "无成交样本"
            out.append(res)
            continue
        f = ev[ev["filled"]]
        by_day_r = _day_agg(ev, "r")
        res["independent_signal_days"] = int(len(by_day_r))
        res.update({"mean_r": round(float(f["r"].mean()), 3), "median_r": round(float(f["r"].median()), 3),
                    "mean_r_by_day": round(float(by_day_r.mean()), 3), "win_rate": round(float((f["r"] > 0).mean()), 3),
                    "payoff": round(float(f[f["r"] > 0]["r"].mean() / -f[f["r"] <= 0]["r"].mean()), 2)
                    if (f["r"] <= 0).any() and (f["r"] > 0).any() else None,
                    "profit_factor": round(float(f[f["r"] > 0]["r"].sum() / -f[f["r"] <= 0]["r"].sum()), 2) if (f["r"] <= 0).any() and f[f["r"] <= 0]["r"].sum() < 0 else None,
                    "avg_hold": round(float(f["hold"].mean()), 1), "avg_mae": round(float(f["mae"].mean()), 4),
                    "avg_mfe": round(float(f["mfe"].mean()), 4)})
        # 触发后收益分布 + 相对基准超额
        post = {}
        bench_ret = None
        if ctx.bench is not None:
            b = ctx.bench.reindex(ctx.dates).ffill().to_numpy()
        for h in (3, 5, 10, 20):
            v = f[f"ret_{h}d"].dropna()
            ex_ = None
            if ctx.bench is not None and len(v):
                br = []
                for _, r in f.dropna(subset=[f"ret_{h}d"]).iterrows():
                    k, kk = int(r["entry_t"]), int(r["entry_t"]) + h - 1
                    if kk < ctx.T and b[k] == b[k] and b[kk] == b[kk]:
                        br.append(b[kk] / b[k] - 1)
                    else:
                        br.append(np.nan)
                ex_ = float(np.nanmean(v.to_numpy()[:len(br)] - np.array(br))) if br else None
            post[h] = {"mean": round(float(v.mean()), 4), "median": round(float(v.median()), 4), "excess_vs_bench": None if ex_ is None else round(ex_, 4)}
        res["post_returns"] = post
        # block bootstrap 置信区间（按信号日序列）
        vals = by_day_r.sort_index().to_numpy()
        lo, hi = block_bootstrap_ci(vals, block=max(1, int(round(f["hold"].mean() if len(f) else 10))))
        res["mean_r_ci95"] = [None if lo != lo else round(lo, 3), None if hi != hi else round(hi, 3)]
        # 随机基线
        member_days = {t: np.nonzero(ctx.l2_arr[t])[0] for t in set(ev["t"])}
        cnt_by_day = f.groupby("t").size().to_dict()
        base_means = []
        cache: dict[tuple, float] = {}
        for b_ in range(n_random):
            per_day = []
            for t, kcnt in cnt_by_day.items():
                pool = member_days.get(t)
                if pool is None or not len(pool):
                    continue
                pick = rng.choice(pool, size=min(kcnt, len(pool)), replace=False)
                cr = _cost_rate(st1, ctx.market)
                for j in pick:
                    key = (t, int(j))
                    if key not in cache:
                        e2 = _event_one(ctx, st1, t, int(j), cr)
                        cache[key] = e2["r"] if e2 and e2["filled"] else np.nan
                rs = [cache[(t, int(j))] for j in pick]
                rs = [x for x in rs if x == x]
                if rs:
                    per_day.append(np.mean(rs))
            if per_day:
                base_means.append(float(np.mean(per_day)))
        if base_means:
            obs = float(by_day_r.mean())
            bm = np.array(base_means)
            res["random_baseline"] = {"n_draws": len(bm), "mean_r": round(float(bm.mean()), 3),
                                      "p5": round(float(np.percentile(bm, 5)), 3), "p95": round(float(np.percentile(bm, 95)), 3),
                                      "p_value": round(float(((bm >= obs).sum() + 1) / (len(bm) + 1)), 4)}
        # 分段稳定性
        f2 = f.copy()
        f2["year"] = [ctx.dates[int(t)][:4] for t in f2["t"]]
        f2["regime"] = [str(ctx.regime[int(t)]) for t in f2["t"]]
        res["by_year"] = {y: {"n": int(len(g)), "mean_r": round(float(g["r"].mean()), 3)} for y, g in f2.groupby("year")}
        res["by_regime"] = {y: {"n": int(len(g)), "mean_r": round(float(g["r"].mean()), 3)} for y, g in f2.groupby("regime")}
        res["verdict"], res["pass"] = event_verdict(res)
        out.append(res)
    return {"kind": "event_study", "setups": out, "n_random": n_random,
            "note": "显著性按信号日聚合并用 block bootstrap（块长 ≥ 持仓周期）；通过标准实施前请与你确认（6.6.2 / 附录 B.2-6）。",
            "caveat": "随机基线抽样次数默认 200（可调高，规范建议 ≥ 1000，运行时间随之增加）。"}


# ---- 漏斗消融（6.6.3）+ 闸门价值（6.6.4）---------------------------------------

def ablation(ctx: BtContext, over: dict | None = None) -> dict:
    """逐层加入漏斗条件，比较样本量与期望值变化；同时给出「带闸门 / 不带闸门」对比。全部走同一套成交规则与成本。"""
    base = {"funnel": {"risk_exclusion": False, "regime_gate": False, "industry_score": False, "rps_score": False, "top_n": 0}}
    steps = [
        ("基线：L2 内仅形态信号（④）", {}),
        ("+ ⑤ 风险剔除", {"funnel": {"risk_exclusion": True}}),
        ("+ ② 市场环境闸门", {"funnel": {"risk_exclusion": True, "regime_gate": True}}),
        ("+ ② 行业强弱（打分）", {"funnel": {"risk_exclusion": True, "regime_gate": True, "industry_score": True}}),
        ("+ ③ RPS 排名（打分）", {"funnel": {"risk_exclusion": True, "regime_gate": True, "industry_score": True, "rps_score": True}}),
        ("+ ⑥ 综合打分取 Top N", {"funnel": {"risk_exclusion": True, "regime_gate": True, "industry_score": True, "rps_score": True,
                                           "top_n": settings.cfg()["funnel"]["top_n"]}}),
    ]
    rows = []
    for label, ov in steps:
        r = ctx.run(settings.deep_merge(settings.deep_merge(over or {}, base), ov))
        m = r["metrics"]
        rows.append({"step": label, "n_trades": m.get("n", 0), "expectancy_r": m.get("expectancy_r"), "win_rate": m.get("win_rate"),
                     "max_drawdown": m.get("max_drawdown"), "turnover": m.get("turnover_per_year"),
                     "total_return": m.get("total_return"), "sharpe": m.get("sharpe"),
                     "signals": m.get("sample", {}).get("signals")})
    gate_on = ctx.run(settings.deep_merge(over or {}, {"funnel": {"regime_gate": True}}))
    gate_off = ctx.run(settings.deep_merge(over or {}, {"funnel": {"regime_gate": False}}))
    keep = None
    a, b = gate_on["metrics"], gate_off["metrics"]
    if a.get("max_drawdown") is not None and b.get("max_drawdown") is not None:
        dd_better = a["max_drawdown"] > b["max_drawdown"]          # 回撤为负数，越接近 0 越好
        exp_ok = (a.get("expectancy_r") or 0) >= (b.get("expectancy_r") or 0) - 0.05
        keep = bool(dd_better and exp_ok)
    # 候选取舍：Top N 与全量候选 / 风险预算与等权
    return {"kind": "ablation", "steps": rows,
            "gate_value": {"with_gate": {k: a.get(k) for k in ("max_drawdown", "expectancy_r", "total_return", "n")},
                           "without_gate": {k: b.get(k) for k in ("max_drawdown", "expectancy_r", "total_return", "n")},
                           "keep_gate": keep,
                           "rule": "闸门只有在降低回撤且不显著牺牲期望值时才保留（6.6.4）"},
            "note": "某层由「打分项」升格为「硬过滤」须在样本外显著提升期望值且信号数量不过度收缩（6.6.3）。"}


def param_grid(ctx: BtContext, setup: str, param: str, values: list, over: dict | None = None) -> dict:
    """参数稳健性：单个自由参数网格（5.4.1，每形态最多 3 个自由参数）；每个点计入试验次数。"""
    if param not in setups.FREE_PARAMS.get(setup, []):
        raise ValueError(f"{setup} 的自由参数仅限 {setups.FREE_PARAMS.get(setup)}")
    rows = []
    for v in values:
        r = ctx.run(settings.deep_merge(over or {}, {"setups": [setup], "setup_params": {setup: {param: v}}}))
        m = r["metrics"]
        rows.append({"value": v, "n": m.get("n", 0), "expectancy_r": m.get("expectancy_r"), "win_rate": m.get("win_rate"),
                     "max_drawdown": m.get("max_drawdown"), "total_return": m.get("total_return")})
    exps = [r["expectancy_r"] for r in rows if r["expectancy_r"] is not None]
    stable = bool(exps) and (max(exps) - min(exps) < max(0.3, abs(np.mean(exps)) * 1.0))
    return {"kind": "param_grid", "setup": setup, "param": param, "rows": rows, "stable": stable,
            "note": "参数小幅变化时结果不应剧烈波动（6.6）；每个网格点都计入试验次数。"}


# ---- 方案对比（5.5.1 入场模式对比 / 5.6 移动止盈 vs 固定目标 / 5.7 止损形态 / 5.8 仓位方式 / 6.6 成本敏感性）----

def compare_variants(ctx: BtContext, over: dict | None = None) -> dict:
    """同一策略、同一数据下，逐项对比关键执行假设。每个变体都走同一套成交规则与成本，并计入试验次数。
    用于回答：入场用次日开盘还是触发价？移动止盈是否优于固定目标？结构化止损是否优于 ATR 止损？风险预算是否优于等权？成本加倍后还成立吗？"""
    base = settings.deep_merge(default_strategy(), over or {})
    variants = [
        ("基准（默认配置）", "基准", {}),
        ("入场：次日开盘 next_open", "入场模式", {"entry_mode": "next_open"}),
        ("入场：触发价 stop_entry", "入场模式", {"entry_mode": "stop_entry"}),
        ("止盈：ATR 移动止盈（默认）", "止盈方式", {"exits": {"trail": "atr"}}),
        ("止盈：跟踪 MA10", "止盈方式", {"exits": {"trail": "ma10"}}),
        ("止盈：不移动止损", "止盈方式", {"exits": {"trail": "none"}}),
        ("止盈：固定目标 +15%（对照）", "止盈方式", {"exits": {"trail": "none", "take_profit_pct": 0.15}}),
        ("止盈：固定目标 2R（对照）", "止盈方式", {"exits": {"trail": "none", "take_profit_r": 2.0}}),
        ("止盈：分批（1.5R 卖一半 + 移动止盈）", "止盈方式", {"exits": {"partial_r": 1.5, "partial_fraction": 0.5}}),
        ("止损：ATR × 1.5", "止损", {"exits": {"stop_atr_k": 1.5}}),
        ("止损：ATR × 2（默认）", "止损", {"exits": {"stop_atr_k": 2.0}}),
        ("止损：ATR × 3", "止损", {"exits": {"stop_atr_k": 3.0}}),
        ("止损：结构化（形态低点）", "止损", {"exits": {"stop_mode": "structure"}}),
        ("仓位：风险预算（默认）", "仓位", {"portfolio": {"sizing": "risk"}}),
        ("仓位：等权", "仓位", {"portfolio": {"sizing": "equal"}}),
        ("仓位：固定比例 10%", "仓位", {"portfolio": {"sizing": "fixed_pct"}}),
        ("仓位：波动率反比", "仓位", {"portfolio": {"sizing": "vol_inverse"}}),
        ("候选：Top N", "候选取舍", {"funnel": {"top_n": settings.cfg()["funnel"]["top_n"]}}),
        ("候选：全量候选", "候选取舍", {"funnel": {"top_n": 0}}),
        ("成本：费用与滑点 ×2", "成本敏感性", {"cost_mult": 2.0, "slip_mult": 2.0}),
        ("成本：费用与滑点 ×4", "成本敏感性", {"cost_mult": 4.0, "slip_mult": 4.0}),
    ]
    rows = []
    for label, group, ov in variants:
        r = ctx.run(settings.deep_merge(over or {}, ov))
        m = r["metrics"]
        rows.append({"group": group, "variant": label, "n": m.get("n", 0), "expectancy_r": m.get("expectancy_r"), "win_rate": m.get("win_rate"),
                     "payoff": m.get("payoff"), "profit_factor": m.get("profit_factor"), "avg_hold": m.get("avg_hold_days"),
                     "max_drawdown": m.get("max_drawdown"), "total_return": m.get("total_return"), "sharpe": m.get("sharpe"),
                     "calmar": m.get("calmar"), "avg_mae": m.get("avg_mae"), "avg_mfe": m.get("avg_mfe")})
    return {"kind": "compare", "rows": rows, "trials_added": len(variants),
            "note": "各变体共用同一信号与成交规则；差异只来自被对比的那一项。固定目标位仅作对照（5.6：移动止盈为主）。"
                    "差异不大时不要据此改参数——这些对比都在同一段历史上，只有样本外与前向收益能证明改动有效。"}


# ---- Walk-forward 滚动前推 + 隔离期（6.6）----

def walk_forward(ctx: BtContext, over: dict | None = None, setup: str = "breakout", grid: dict | None = None,
                 train_years: float = 3.0, test_months: int = 6, embargo_days: int | None = None, min_trades: int = 30) -> dict:
    """滚动前推：每个窗口在训练段内从参数网格里选期望值最高的一组（要求成交笔数 ≥ min_trades），
    再在**隔离期之后**的测试段上用这组参数评估；训练段与测试段之间留出不少于持仓周期的隔离期（embargo），避免信息泄漏。
    汇总所有测试段（样本外）的成绩，并报告「样本外 / 样本内」效率与参数稳定性。每个网格点 × 每个窗口都计入试验次数。"""
    import itertools

    st0 = merge_strategy(over)
    grid = grid or default_wf_grid(setup)
    names = list(grid)
    combos = [dict(zip(names, vals)) for vals in itertools.product(*[grid[k] for k in names])]
    if len(names) > 3:
        raise ValueError("每个形态最多 3 个自由参数参与网格搜索（5.4.1）")
    embargo = int(embargo_days if embargo_days is not None else max(st0["exits"]["max_hold_days"], 5))
    dates = ctx.dates
    i_start = int(np.searchsorted(np.array(dates), st0["start"] or ctx.start))
    train_len = int(round(train_years * 252))
    test_len = int(round(test_months * 21))
    windows, trials = [], 0
    ts = i_start
    while ts + train_len + embargo + test_len <= ctx.T:
        te = ts + train_len - 1
        xs = te + 1 + embargo
        xe = xs + test_len - 1
        scores = []
        for cmb in combos:
            ov = settings.deep_merge(over or {}, {"setups": [setup], "setup_params": {setup: cmb}, "start": dates[ts], "end": dates[te]})
            m = ctx.run(ov)["metrics"]
            trials += 1
            scores.append((m.get("expectancy_r") if (m.get("n", 0) >= min_trades and m.get("expectancy_r") is not None) else None, m.get("n", 0), cmb))
        valid = [x for x in scores if x[0] is not None]
        if not valid:
            windows.append({"train": [dates[ts], dates[te]], "test": [dates[xs], dates[xe]], "chosen": None, "note": "训练段成交笔数不足，跳过"})
        else:
            best = max(valid, key=lambda x: x[0])
            ov_t = settings.deep_merge(over or {}, {"setups": [setup], "setup_params": {setup: best[2]}, "start": dates[xs], "end": dates[xe]})
            rt = ctx.run(ov_t)
            trials += 1
            windows.append({"train": [dates[ts], dates[te]], "embargo_days": embargo, "test": [dates[xs], dates[xe]], "chosen": best[2],
                            "train_expectancy_r": round(best[0], 3), "train_n": best[1],
                            "test_expectancy_r": rt["metrics"].get("expectancy_r"), "test_n": rt["metrics"].get("n", 0),
                            "test_win_rate": rt["metrics"].get("win_rate"), "test_return": rt["metrics"].get("total_return"),
                            "test_max_drawdown": rt["metrics"].get("max_drawdown"), "_trades": rt["trades"]})
        ts += test_len
    done = [w for w in windows if w.get("chosen")]
    all_tr = [t for w in done for t in w.pop("_trades", [])]
    for w in windows:
        w.pop("_trades", None)
    oos = trade_stats(pd.DataFrame(all_tr)) if all_tr else {"n": 0}
    is_vals = [w["train_expectancy_r"] for w in done]
    oos_vals = [w["test_expectancy_r"] for w in done if w["test_expectancy_r"] is not None]
    keyfun = lambda w: json.dumps(w["chosen"], sort_keys=True)  # noqa: E731
    from collections import Counter
    cnt = Counter(keyfun(w) for w in done)
    most, mc_ = (cnt.most_common(1)[0] if cnt else (None, 0))
    eff = (float(np.mean(oos_vals)) / float(np.mean(is_vals))) if is_vals and oos_vals and np.mean(is_vals) > 0 else None
    return {"kind": "walk_forward", "setup": setup, "grid": grid, "embargo_days": embargo, "train_years": train_years, "test_months": test_months,
            "windows": windows, "oos": oos, "n_windows": len(done), "positive_windows": sum(1 for v in oos_vals if v > 0),
            "mean_is_expectancy_r": round(float(np.mean(is_vals)), 3) if is_vals else None,
            "mean_oos_expectancy_r": round(float(np.mean(oos_vals)), 3) if oos_vals else None, "efficiency": None if eff is None else round(eff, 2),
            "param_stability": None if not done else round(mc_ / len(done), 2), "most_chosen": None if most is None else json.loads(most),
            "trials_added": trials,
            "note": "只有测试段（样本外）的成绩才算数；样本内期望明显高于样本外 = 过拟合的典型特征。训练段与测试段之间留有不少于持仓周期的隔离期。"}


def default_wf_grid(setup: str) -> dict:
    return {"breakout": {"vol_ratio_min": [1.2, 1.5, 1.8], "close_pos_min": [0.6, 0.7]},
            "pullback": {"rps60_min": [70, 80, 90], "vol_ratio_max": [0.7, 0.9]},
            "vcp": {"atr_ratio_max": [0.7, 0.8, 0.9], "vol_ratio_min": [1.2, 1.5]},
            "oversold": {"rps60_min": [60, 70, 80], "rsi_max": [30, 35, 40]}}[setup]


def deflated_sharpe(moments: dict, trials: int) -> dict:
    """Deflated Sharpe Ratio（Bailey & López de Prado）：在尝试过 N 次参数 / 规则后，观测到的最好 Sharpe 有多大概率只是运气。
    返回 DSR（概率，越接近 1 越可信）与「期望最大 Sharpe 门槛」（日频）。试验次数为 1 时即概率性夏普（PSR，对 0 检验）。"""
    from statistics import NormalDist
    nd = NormalDist()
    sr, skew, kurt, T = moments["sr_daily"], moments["skew"], moments["kurt"], moments["T"]
    var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr * sr) / max(T - 1, 1)
    sd_sr = math.sqrt(max(var_sr, 1e-12))
    gamma = 0.5772156649
    sr0 = 0.0
    if trials > 1:
        sr0 = sd_sr * ((1 - gamma) * nd.inv_cdf(1 - 1 / trials) + gamma * nd.inv_cdf(1 - 1 / (trials * math.e)))
    dsr = nd.cdf((sr - sr0) / sd_sr)
    return {"dsr": round(dsr, 4), "sr_daily": round(sr, 5), "sr_annual": round(sr * math.sqrt(252), 2), "benchmark_sr_daily": round(sr0, 5),
            "trials": trials, "interpretation": "≥0.95：扣除多次尝试的运气成分后仍显著；< 0.5：很可能只是运气 / 过拟合"}


# ---- 入口 + 存档 --------------------------------------------------------------

def count_trials(conn, market: str, strategy_id: str) -> int:
    r = conn.execute("SELECT COUNT(*) FROM backtest_runs WHERE market=? AND strategy_id=?", (market, strategy_id)).fetchone()
    return int(r[0])


def save_run(conn, kind: str, market: str, strategy: dict, result: dict, data_asof: str, trials: int) -> str:
    run_id = f"BT-{kind[:3].upper()}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
    chash = settings.config_hash(strategy)
    metrics = result.get("metrics") or {}
    slim = dict(result)
    conn.execute("INSERT INTO backtest_runs(run_id,kind,strategy_id,market,config_hash,data_asof,metrics,trial_count,code_version,"
                 "created_at,config,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, kind, strategy.get("id", "default"), market, chash, data_asof,
                  json.dumps(metrics, ensure_ascii=False, default=str), trials, settings.git_commit(),
                  datetime.now().isoformat(timespec="seconds"), json.dumps(strategy, ensure_ascii=False, default=str),
                  json.dumps(slim, ensure_ascii=False, default=str)))
    return run_id


def run_job(market: str, kind: str, params: dict | None = None) -> dict:
    """POST /api/backtest 的后端入口：kind = strategy | single_factor | event_study | ablation | param_grid。"""
    params = params or {}
    with settings.market_ctx(market), db.market_db(market) as conn:
        st = merge_strategy(params.get("strategy"))
        ctx = BtContext(conn, market, st["start"], st["end"])
        asof = ctx.data_asof
        if not ctx.dates:
            return {"error": "no_data", "message": "尚无行情数据"}
        sid = st["id"]
        trials = count_trials(conn, market, sid) + 1
        if kind == "strategy":
            res = ctx.run(params.get("strategy"))
            res["kind"] = "strategy"
            mom = res["metrics"].get("ret_moments")
            if mom:
                res["metrics"]["deflated_sharpe"] = deflated_sharpe(mom, trials)
        elif kind == "single_factor":
            res = single_factor_test(ctx, params.get("factors"), tuple(params.get("horizons", (5, 10, 20))),
                                     start=st["start"], end=st["end"])
        elif kind == "event_study":
            res = event_study(ctx, params.get("strategy"), int(params.get("n_random", 200)), setup_names=params.get("setups"))
        elif kind == "ablation":
            res = ablation(ctx, params.get("strategy"))
            trials += 7
        elif kind == "param_grid":
            res = param_grid(ctx, params["setup"], params["param"], params["values"], params.get("strategy"))
            trials += len(params["values"])
        elif kind == "compare":
            res = compare_variants(ctx, params.get("strategy"))
            trials += res["trials_added"]
        elif kind == "walk_forward":
            res = walk_forward(ctx, params.get("strategy"), params.get("setup", "breakout"), params.get("grid"),
                               float(params.get("train_years", 3)), int(params.get("test_months", 6)), params.get("embargo_days"))
            trials += res["trials_added"]
        else:
            raise ValueError(f"unknown kind {kind}")
        res["market"], res["data_asof"] = market, asof
        res["trial_count"] = trials
        res["run_id"] = save_run(conn, kind, market, st, res, asof, trials)
        res["config_hash"] = settings.config_hash(st)
        res["code_version"] = settings.git_commit()
        return res


def _in_market(fn):
    """回测各入口都在 ctx 所属市场的配置上下文里运行（美股参数独立于 A 股，2.1）。"""
    import functools

    @functools.wraps(fn)
    def wrapped(ctx, *a, **k):
        with settings.market_ctx(ctx.market):
            return fn(ctx, *a, **k)
    return wrapped


for _n in ("single_factor_test", "event_study", "ablation", "param_grid", "compare_variants", "walk_forward"):
    globals()[_n] = _in_market(globals()[_n])
_run_inner = BtContext.run
BtContext.run = lambda self, *a, **k: _in_market(_run_inner)(self, *a, **k)
