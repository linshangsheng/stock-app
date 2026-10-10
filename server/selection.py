"""候选取舍规则（5.4）+ 风险预算仓位（5.8）的**纯函数**实现，盘后扫描与回测共用同一份。
可用空位 = 最大持仓数 - 现有持仓数；候选按综合分降序（同分优先 ATR% 更低、成交额更高），
依次检查组合约束（单行业上限、组合总风险上限、是否已持有），不满足则跳过取下一个，直到填满空位或候选用尽。"""
from __future__ import annotations

from . import execution, settings


def operation_plan(ref: float, stop: float, board: str | None, equity: float, risk_pct: float,
                   adv: float | None = None, max_adv_pct: float | None = None, gaps=(0.0, 0.005, 0.01, 0.02, 0.03, 0.05)) -> dict | None:
    """次日开盘买的操作计算（与回测 execution.resize_on_fill 同一口径）：
    最多亏 = 账户资金 × 单笔风险比例；计划股数按参考价（信号日收盘）算；开盘价更高时按「最多亏 ÷（开盘价 − 止损价）」
    重算股数（只少不多，整手向下取整）；放弃价 = 开盘价高于它时连一手的风险都超预算。"""
    if not (ref and stop and equity and risk_pct) or ref <= stop:
        return None
    risk = equity * risk_pct
    plan = execution.size_by_risk(equity, risk_pct, ref, stop, board, adv, max_adv_pct)
    planned = int(plan["shares"])
    lot = execution.min_lot(board)
    scen = []
    for g in gaps:
        op = round(ref * (1 + g), 2)
        sh = min(planned, execution.lot_round(board, risk / (op - stop))) if op > stop else 0
        if sh < lot:
            sh = 0
        scen.append({"gap": g, "open": op, "shares": sh, "cost": round(sh * op, 2), "max_loss": round(sh * (op - stop), 2)})
    tp_pct = settings.cfg()["exits"].get("take_profit_pct") or 0
    return {"take_profit_pct": tp_pct, "take_profit": round(ref * (1 + tp_pct), 2) if tp_pct else None,
            "risk_budget": round(risk, 2), "planned_shares": planned, "planned_cost": round(planned * ref, 2),
            "planned_loss": round(planned * (ref - stop), 2), "give_up_above": round(stop + risk / lot, 2),
            "capped_by_adv": bool(plan.get("capped_by_adv")), "scenarios": scen,
            "note": "" if planned else plan.get("reason", "")}


def open_plan(ref: float, stop: float, board: str | None, op: dict | None = None, market: str = "CN") -> list[dict]:
    """明天开盘的各种情况怎么做（与回测成交规则一致：次日开盘买；开盘 ≤ 止损价放弃；涨停开盘买不到；
    高开按「最多亏 ÷（开盘价 − 止损价）」少买、只少不多；低开风险更小、按计划股数买、不加）。
    每行：case / open（价格区间文字）/ open_px / action / shares / cost / max_loss；op 为空（未填资金）时 shares 等为 None。"""
    if not (ref and stop) or ref <= stop:
        return []
    tick = execution.TICK
    if op and not op.get("planned_shares"):
        return [{"case": "任何开盘价", "open": "—", "open_px": None, "action": f"不买：按你的资金，「最多亏」连一手的风险都不够（{op.get('note') or '止损距离太大'}）",
                 "shares": 0, "cost": 0, "max_loss": 0}]
    lim = None
    if market == "CN":
        rules = settings.cfg().get("limit_rules", {})
        lim = (rules.get(board or "main") or rules.get("main") or [{"pct": 0.10}])[-1]["pct"]
    up_px = round(ref * (1 + lim), 2) if lim else None
    planned = (op or {}).get("planned_shares")
    risk = (op or {}).get("risk_budget")

    def row(case, open_txt, px, action, shares):
        r = {"case": case, "open": open_txt, "open_px": px, "action": action, "shares": shares, "cost": None, "max_loss": None}
        if shares is not None and px:
            r["cost"], r["max_loss"] = round(shares * px, 2), round(shares * max(px - stop, 0), 2)
        return r

    def at(px):                                   # 某个开盘价下该买的股数（只少不多）
        if not op:
            return None
        n = min(planned, execution.lot_round(board, risk / (px - stop))) if px > stop else 0
        return n if n >= execution.min_lot(board) else 0

    rows = [row("低开到止损价以下", f"≤ {stop:.2f}", stop, "不买：一开盘就在止损价下方，买了马上就该卖", 0 if op else None),
            row("低开", f"{stop + tick:.2f} ~ {ref - tick:.2f}", None, "照计划买，股数不加；离止损更近，风险比计划还小", planned),
            row("平开", f"≈ {ref:.2f}", ref, "照计划买", planned)]
    for g in (0.005, 0.01, 0.02, 0.03, 0.05):
        px = round(ref * (1 + g), 2)
        if up_px and px >= up_px:
            break
        n = at(px)
        act = ("少买" if op and n and n < planned else "照计划买" if op and n else "不买：止损太远，一手风险超过「最多亏」" if op
               else "少买：股数 = 最多亏 ÷（开盘价 − 止损价），按整手向下取整")
        rows.append(row(f"高开 {g * 100:g}%", f"≈ {px:.2f}", px, act, n))
    if up_px:
        rows.append(row("涨停开盘", f"= {up_px:.2f}", up_px, "不买：涨停开盘基本买不到，也不追（回测按买不到处理）", 0 if op else None))
    return rows


def rank_candidates(cands: list[dict]) -> list[dict]:
    """综合分降序；同分优先 ATR% 更低、成交额更高。score 缺失排最后。"""
    def key(c):
        sc = c.get("score")
        return (0 if sc is not None else 1, -(sc if sc is not None else 0.0), c.get("atr_pct") or 0.0, -(c.get("adv20") or 0.0))
    return sorted(cands, key=key)


def plan_entry(c: dict, entry_mode: str, stop_mode: str | None = None, exits: dict | None = None) -> tuple[float, float, float]:
    """返回 (触发价, 入场参考价, 止损价)。stop_entry：触发价 = 信号日最高价 + 1 个最小价位；next_open：以信号日收盘作参考。
    止损：atr = 入场价 - k×ATR（默认）；structure = 形态低点下方一个价位（结构化止损，5.4.1 对照），低点不在入场价之下时回退到 ATR；
    两种都受硬止损（相对入场价最大亏损）约束。"""
    trigger = round(c["high"] + execution.TICK, 2)
    ref = trigger if entry_mode == "stop_entry" else round(c["close"], 2)
    ex = exits or settings.cfg()["exits"]                         # 回测传入策略自己的 exits，否则用配置
    stop_mode = stop_mode or ex.get("stop_mode", "atr")
    k, hard_pct = ex.get("stop_atr_k"), ex.get("hard_stop_pct")
    sl = c.get("struct_low")
    if stop_mode == "structure" and sl is not None and sl == sl and sl < ref:
        hard = hard_pct or 0
        stop = round(max(sl - execution.TICK, ref * (1 - hard) if hard else 0.0), 2)
        if stop < ref:
            return trigger, ref, stop
    return trigger, ref, execution.initial_stop(ref, c["atr14"], k, hard_pct)


def size_position(sizing: str, *, equity, risk_pct, ref, stop, board, adv, max_adv_pct, max_positions, atr_pct, fixed_pct=0.10, vol_ref=0.03) -> dict:
    """仓位方式（5.8）。risk：按单笔风险倒推（推荐，与 ATR 止损配套）；equal：净值 / 最大持仓数；
    fixed_pct：固定比例；vol_inverse：等权基础上按 参考ATR% / ATR% 缩放（波动越大仓位越小）。
    价值法的股数同样按最小单位取整并受单笔容量限制；返回字段与 execution.size_by_risk 一致。"""
    if sizing in (None, "", "risk"):
        return execution.size_by_risk(equity, risk_pct, ref, stop, board, adv, max_adv_pct)
    if sizing == "equal":
        value = equity / max(1, max_positions)
    elif sizing == "fixed_pct":
        value = equity * fixed_pct
    elif sizing == "vol_inverse":
        k = min(2.0, max(0.25, vol_ref / atr_pct)) if atr_pct and atr_pct == atr_pct else 1.0
        value = equity / max(1, max_positions) * k
    else:
        raise ValueError(f"unknown sizing {sizing}")
    shares = execution.lot_round(board, value / ref)
    capped = False
    if adv and max_adv_pct:
        cap_sh = execution.lot_round(board, adv * max_adv_pct / ref)
        if shares > cap_sh:
            shares, capped = cap_sh, True
    if shares < execution.min_lot(board):
        shares = 0
    per = max(ref - stop, 0.0)
    return {"shares": int(shares), "risk_amount": round(shares * per, 2), "risk_per_lot": round(execution.min_lot(board) * per, 2),
            "capped_by_adv": capped, "reason": "" if shares else "资金/容量不足一手，放弃"}


def select_and_size(cands: list[dict], *, slots: int, held_ind: dict, risk_used: float, risk_cap: float | None,
                    equity: float | None, risk_pct: float, top_n: int | None = None, entry_mode: str | None = None,
                    max_per_industry: int | None = None, max_adv_pct: float | None = None, rank: bool = True,
                    sizing: str | None = None, max_positions: int | None = None, stop_mode: str | None = None,
                    exits: dict | None = None) -> list[dict]:
    cfg = settings.cfg()
    entry_mode = entry_mode or cfg["execution"]["entry_mode"]
    max_per_industry = max_per_industry or cfg["portfolio"]["max_per_industry"]
    max_adv_pct = cfg["execution"]["max_adv_pct"] if max_adv_pct is None else max_adv_pct
    ordered = rank_candidates(cands) if rank else list(cands)
    if top_n:
        ordered = ordered[:top_n]
    ind_count = dict(held_ind)
    taken = 0
    out = []
    for c in ordered:
        trigger, ref, stop = plan_entry(c, entry_mode, stop_mode, exits)
        rec = {**c, "trigger_price": trigger, "entry_ref": ref, "stop_price": stop,
               "stop_dist_pct": round((ref - stop) / ref, 4) if ref else None, "risk_amount": None, "shares": None,
               "risk_per_lot": round(execution.min_lot(c.get("board")) * (ref - stop), 2), "fit": True, "skip_reason": ""}
        sz = None
        if equity:
            pcfg = cfg["portfolio"]
            sz = size_position(sizing or pcfg.get("sizing", "risk"), equity=equity, risk_pct=risk_pct, ref=ref, stop=stop, board=c.get("board"),
                               adv=c.get("adv20"), max_adv_pct=max_adv_pct, max_positions=max_positions or pcfg["max_positions"],
                               atr_pct=c.get("atr_pct"), fixed_pct=pcfg.get("fixed_pct", 0.10), vol_ref=pcfg.get("vol_ref_atr_pct", 0.03))
            rec.update(shares=sz["shares"], risk_amount=sz["risk_amount"], risk_per_lot=sz["risk_per_lot"])
        ind = c.get("industry")
        if slots - taken <= 0:
            rec.update(fit=False, skip_reason="已无空位")
        elif ind and ind_count.get(ind, 0) >= max_per_industry:
            rec.update(fit=False, skip_reason=f"行业持仓已达上限（{max_per_industry} 只）")
        elif sz is not None and sz["shares"] == 0:
            rec.update(fit=False, skip_reason=sz["reason"])
        elif risk_cap is not None and sz is not None and risk_used + sz["risk_amount"] > risk_cap:
            rec.update(fit=False, skip_reason="超出组合总风险上限")
        else:
            taken += 1
            ind_count[ind] = ind_count.get(ind, 0) + 1
            if sz is not None:
                risk_used += sz["risk_amount"]
        out.append(rec)
    return out
