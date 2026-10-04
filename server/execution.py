"""日线成交规则的共用函数（5.5.1）。回测、观察清单与持仓体检共用同一份实现，口径只此一份。"""
from __future__ import annotations

import math

from . import settings

TICK = 0.01


def lot_round(board: str | None, shares: float) -> int:
    """最小交易单位向下取整：A 股整手（100 股）；科创板 200 股起、之后按 1 股递增；美股整股（5.5.1）。"""
    shares = int(math.floor(max(shares, 0)))
    if board == "us":
        return shares
    if board == "star":
        return 0 if shares < 200 else shares
    return (shares // 100) * 100


def min_lot(board: str | None) -> int:
    return 1 if board == "us" else 200 if board == "star" else 100


def lot_step(board: str | None) -> int:
    """降仓时一次减少的股数（A 股一手 / 科创板 1 股 / 美股 1 股）。"""
    return 100 if board not in ("star", "us") else 1


def trade_fees(side: str, price: float, qty: int, costs: dict | None = None, market: str = "CN") -> float:
    """费用：费率读配置，不在代码里写死（5.5.1-4）。
    A 股：佣金（双向，含最低）+ 印花税（仅卖出）+ 过户费（双向）。
    美股：佣金（按股，默认零佣金）+ 卖出的 SEC 规费（按成交额）与 FINRA TAF（按股数、单笔封顶）。"""
    sell = side.lower() in ("sell", "s")
    notional = price * qty
    if market == "US":
        c = costs or settings.cfg()["costs"]["us"]
        fee = c.get("commission_per_share", 0.0) * qty
        if sell:
            fee += notional * c.get("sec_fee_sell", 0.0) + min(qty * c.get("finra_taf_per_share", 0.0), c.get("finra_taf_cap", 8.3))
        return fee
    c = costs or settings.cfg()["costs"]["cn"]
    fee = max(notional * c["commission_rate"], c["commission_min"]) + notional * c["transfer_fee"]
    if sell:
        fee += notional * c["stamp_duty_sell"]
    return fee


def slip(price: float, side: str, slippage: float) -> float:
    """在成交价上按不利方向加滑点。"""
    return price * (1 + slippage) if side == "buy" else price * (1 - slippage)


def initial_stop(entry: float, atr: float, k: float | None = None, hard_pct: float | None = None) -> float:
    """初始止损 = 入场价 - k × ATR14，且不低于硬止损（单笔最大亏损上限，5.7）。"""
    ex = settings.cfg()["exits"]
    k = ex["stop_atr_k"] if k is None else k
    hard_pct = ex["hard_stop_pct"] if hard_pct is None else hard_pct
    s = entry - k * atr
    if hard_pct:
        s = max(s, entry * (1 - hard_pct))
    return round(s, 2)


def trailing_stop(prev_stop: float, highest_close: float, atr: float, ma10: float | None = None,
                  mode: str | None = None, k: float | None = None) -> float:
    """移动止损（T 日收盘后更新，T+1 生效；只上移不下移；不允许用当日最高价更新当日止损，5.5.1-3）。"""
    ex = settings.cfg()["exits"]
    mode = mode or ex["trail"]
    k = ex["trail_atr_k"] if k is None else k
    cand = prev_stop
    if mode == "atr" and atr == atr:
        cand = highest_close - k * atr
    elif mode == "ma10" and ma10 is not None and ma10 == ma10:
        cand = ma10
    return round(max(prev_stop, cand), 2)


def size_by_risk(equity: float, risk_pct: float, entry: float, stop: float, board: str | None,
                 adv_amount: float | None = None, max_adv_pct: float | None = None) -> dict:
    """风险预算仓位（5.8）：股数 = 单笔风险金额 /（入场价 - 止损价），按最小单位向下取整；
    取整后单笔风险仍须不超上限，否则降一手，降到 0 放弃；成交额受 20 日均成交额 × 比例限制。"""
    risk_amt = equity * risk_pct
    per_share = entry - stop
    if per_share <= 0:
        return {"shares": 0, "risk_amount": 0.0, "reason": "止损价不低于入场价"}
    raw = risk_amt / per_share
    lot = min_lot(board)
    shares = lot_round(board, raw)
    while shares > 0 and shares * per_share > risk_amt * 1.0000001:
        shares = lot_round(board, shares - lot_step(board))
    capped = False
    if adv_amount and max_adv_pct:
        cap_sh = lot_round(board, adv_amount * max_adv_pct / entry)
        if shares > cap_sh:
            shares, capped = cap_sh, True
    if shares < lot and shares != 0:
        shares = 0
    return {"shares": int(shares), "risk_amount": round(shares * per_share, 2), "risk_per_lot": round(lot * per_share, 2),
            "capped_by_adv": capped, "reason": "" if shares else "资金/容量不足一手，放弃"}
