"""日线成交规则的共用函数（5.5.1）。回测、观察清单与持仓体检共用同一份实现，口径只此一份。"""
from __future__ import annotations

import math

from . import settings

TICK = 0.01


def hk_lot(board: str | None) -> int | None:
    """港股的「板块」写成 hk<每手股数>（如 hk400）：每手股数因股票而异（腾讯 100、汇丰 400、小米 200），查到了就带上；没查到写 hk（当 1 股算，
    股数不取整，界面上标「每手股数没查到」）。不是港股返回 None。"""
    if isinstance(board, str) and board.startswith("hk"):
        n = board[2:]
        return int(n) if n.isdigit() and int(n) > 0 else 1
    return None


def lot_round(board: str | None, shares: float) -> int:
    """最小交易单位向下取整：A 股整手（100 股）；科创板 200 股起、之后按 1 股递增；美股整股；港股按每手股数（5.5.1）。"""
    shares = int(math.floor(max(shares, 0)))
    hl = hk_lot(board)
    if hl is not None:
        return (shares // hl) * hl
    if board == "us":
        return shares
    if board == "star":
        return 0 if shares < 200 else shares
    return (shares // 100) * 100


def min_lot(board: str | None) -> int:
    hl = hk_lot(board)
    return hl if hl is not None else 1 if board == "us" else 200 if board == "star" else 100


def lot_step(board: str | None) -> int:
    """降仓时一次减少的股数（A 股一手 / 科创板 1 股 / 美股 1 股 / 港股一手）。"""
    hl = hk_lot(board)
    return hl if hl is not None else 100 if board not in ("star", "us") else 1


# 港交所价位最小变动（按价格分档；价格 < 上限用这一档）。报价必须落在这个格子上（400 港元的股票没有 400.01 这个价）。
# 现行（2026-08-03 起，港交所「缩减最小价位」第二阶段）：< 0.25 → 0.001；< 10 → 0.005；< 20 → 0.01；< 50 → 0.02；< 100 → 0.05；< 200 → 0.1；
# < 500 → 0.2；< 1000 → 0.5；< 2000 → 1；< 5000 → 2；其余 5。适用于股票（ETF、债券、牛熊证等不适用）。
# 用 2026-09 起全部股票的开高低收做过对照：1~10 港元 0.005、10~20 0.01、20~50 0.02、50~100 0.05、100~200 0.1、200~500 0.2、500~1000 0.5，和这张表一致。
# 这张表是「现在」的：2025-08 之前 0.5~10 是 0.01、10~20 是 0.02、20~100 是 0.05，之后分两个阶段降低。只用于给「明天」的价格取整；以后做港股回测要按日期取当时的价位表。
HK_TICKS = ((0.25, 0.001), (10.0, 0.005), (20.0, 0.01), (50.0, 0.02), (100.0, 0.05), (200.0, 0.1), (500.0, 0.2), (1000.0, 0.5), (2000.0, 1.0), (5000.0, 2.0))


def hk_tick(price: float) -> float:
    for upper, t in HK_TICKS:
        if price < upper:
            return t
    return 5.0


def tick(board: str | None, price: float) -> float:
    """最小变动价位：港股看价格分档，A 股 / 美股固定 0.01。"""
    return hk_tick(price) if hk_lot(board) is not None else TICK


def snap(board: str | None, price: float, direction: str = "nearest") -> float:
    """把价格落到合法价位上（direction = up / down / nearest）。A 股 / 美股就是保留 2 位小数，和以前一样。"""
    if hk_lot(board) is None:
        return round(price, 2)
    t = hk_tick(price)
    n = price / t
    k = math.ceil(n - 1e-9) if direction == "up" else math.floor(n + 1e-9) if direction == "down" else round(n)
    return round(k * t, 4)


def trade_fees(side: str, price: float, qty: int, costs: dict | None = None, market: str = "CN") -> float:
    """费用：费率读配置，不在代码里写死（5.5.1-4）。
    A 股：佣金（双向，含最低）+ 印花税（仅卖出）+ 过户费（双向）。
    美股：佣金（按股，默认零佣金）+ 卖出的 SEC 规费（按成交额）与 FINRA TAF（按股数、单笔封顶）。"""
    sell = side.lower() in ("sell", "s")
    notional = price * qty
    if market == "HK":
        c = costs or settings.cfg()["costs"]["hk"]
        fee = max(notional * c["commission_rate"], c["commission_min"]) + notional * c["stamp_duty"]                      # 佣金（含最低）+ 印花税（买卖双向）
        fee += notional * (c["trading_fee"] + c["sfc_levy"] + c["frc_levy"])                                                  # 交易费 + 证监会征费 + 财汇局征费
        return fee + min(max(notional * c["settlement_rate"], c["settlement_min"]), c["settlement_max"])                      # 结算费（有最低和封顶）
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
