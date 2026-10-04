"""候选取舍规则（5.4）+ 风险预算仓位（5.8）的**纯函数**实现，盘后扫描与回测共用同一份。
可用空位 = 最大持仓数 - 现有持仓数；候选按综合分降序（同分优先 ATR% 更低、成交额更高），
依次检查组合约束（单行业上限、组合总风险上限、是否已持有），不满足则跳过取下一个，直到填满空位或候选用尽。"""
from __future__ import annotations

from . import execution, settings


def rank_candidates(cands: list[dict]) -> list[dict]:
    """综合分降序；同分优先 ATR% 更低、成交额更高。score 缺失排最后。"""
    def key(c):
        sc = c.get("score")
        return (0 if sc is not None else 1, -(sc if sc is not None else 0.0), c.get("atr_pct") or 0.0, -(c.get("adv20") or 0.0))
    return sorted(cands, key=key)


def plan_entry(c: dict, entry_mode: str) -> tuple[float, float, float]:
    """返回 (触发价, 入场参考价, 止损价)。stop_entry：触发价 = 信号日最高价 + 1 个最小价位；next_open：以信号日收盘作参考。"""
    trigger = round(c["high"] + execution.TICK, 2)
    ref = trigger if entry_mode == "stop_entry" else round(c["close"], 2)
    return trigger, ref, execution.initial_stop(ref, c["atr14"])


def select_and_size(cands: list[dict], *, slots: int, held_ind: dict, risk_used: float, risk_cap: float | None,
                    equity: float | None, risk_pct: float, top_n: int | None = None, entry_mode: str | None = None,
                    max_per_industry: int | None = None, max_adv_pct: float | None = None, rank: bool = True) -> list[dict]:
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
        trigger, ref, stop = plan_entry(c, entry_mode)
        rec = {**c, "trigger_price": trigger, "entry_ref": ref, "stop_price": stop,
               "stop_dist_pct": round((ref - stop) / ref, 4) if ref else None, "risk_amount": None, "shares": None,
               "risk_per_lot": round(execution.min_lot(c.get("board")) * (ref - stop), 2), "fit": True, "skip_reason": ""}
        sz = None
        if equity:
            sz = execution.size_by_risk(equity, risk_pct, ref, stop, c.get("board"), c.get("adv20"), max_adv_pct)
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
