"""持仓风险总览：不是每只股票各自的体检，而是「这些持仓放在一起」的风险——集中在哪几个行业、哪几只其实同涨同跌、
大盘跌 10% 大概亏多少、按现在的持仓把过去一年重放一遍最差会怎样。只用日线收盘价，不改任何买卖规则，不接入选股 / 回测。
提醒线（行业 40%、单只 30%、相关系数 0.8 等）是常识性的经验值，不是回测出来的。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import daily_view, db, market_calendar as mc, portfolio, settings
from .panel import load_panel

WINDOW = 250            # 重放 / 相关性看过去约一年的日线
BETA_DAYS = 120         # 对大盘的敏感度（beta）看最近约半年
MIN_DAYS = 60           # 少于这么多天重叠数据的持仓，不参与相关性 / 重放（单独标出来）
CORR_FLAG = 0.8
IND_FLAG = 0.40
ONE_FLAG = 0.30
SHOCK = 0.10            # 假设大盘下跌 10%


def _max_dd(s: pd.Series) -> float:
    eq = (1 + s).cumprod()
    return float((eq / eq.cummax() - 1).min())


def analyze(adj: pd.DataFrame, values: dict[str, float], industry: dict[str, str | None], names: dict[str, str],
            bench: pd.Series | None = None, equity: float | None = None) -> dict:
    """adj：日期 × 股票 的复权收盘价（最近约 250 天）；values：每只持仓的当前市值；bench：基准指数收盘（日期索引）。"""
    syms = [s for s in values if values[s] and values[s] > 0]
    total = float(sum(values[s] for s in syms))
    if not syms or total <= 0:
        return {"status": "empty"}
    w = pd.Series({s: values[s] / total for s in syms})
    rets = adj.reindex(columns=syms).pct_change(fill_method=None).iloc[1:]
    enough = [s for s in syms if rets[s].notna().sum() >= MIN_DAYS]
    short = [s for s in syms if s not in enough]
    br = bench.reindex(rets.index.union(bench.index)).ffill().reindex(rets.index).pct_change(fill_method=None) if bench is not None and len(bench) else None

    beta: dict[str, float | None] = {}
    for s in syms:
        beta[s] = None
        if s in enough and br is not None:
            j = pd.concat([rets[s], br], axis=1).dropna().tail(BETA_DAYS)
            if len(j) >= MIN_DAYS and j.iloc[:, 1].var() > 0:
                beta[s] = float(j.iloc[:, 0].cov(j.iloc[:, 1]) / j.iloc[:, 1].var())

    ind: dict[str, float] = {}
    for s in syms:
        k = industry.get(s) or "其他"
        ind[k] = ind.get(k, 0.0) + float(w[s])
    ind_rows = sorted(({"name": k, "share": round(v, 3)} for k, v in ind.items()), key=lambda r: -r["share"])
    holdings = sorted(({"symbol": s, "name": names.get(s, s), "industry": industry.get(s), "weight": round(float(w[s]), 3),
                        "value": round(values[s], 2), "beta": None if beta[s] is None else round(beta[s], 2),
                        "enough": s in enough} for s in syms), key=lambda r: -r["weight"])

    out: dict = {"status": "ok", "n": len(syms), "value": round(total, 2), "equity": equity,
                 "exposure": None if not equity else round(total / equity, 3),
                 "holdings": holdings, "industry": ind_rows, "short": short}

    # 哪几只同涨同跌 + 实际相当于几只独立的持仓
    out["pairs"], out["eff_n"] = [], None
    if len(enough) >= 2:
        R = rets[enough]
        C = R.corr(min_periods=MIN_DAYS)
        pairs = [{"a": a, "b": b, "a_name": names.get(a, a), "b_name": names.get(b, b), "corr": round(float(C.loc[a, b]), 2)}
                 for i, a in enumerate(enough) for b in enough[i + 1:] if C.loc[a, b] == C.loc[a, b]]
        out["pairs"] = sorted([p for p in pairs if p["corr"] >= 0.7], key=lambda p: -p["corr"])[:6]
        wu = w[enough] / w[enough].sum()
        cov = R.fillna(0).cov()
        sig = np.sqrt(np.diag(cov.to_numpy()))
        pv = float(wu.to_numpy() @ cov.to_numpy() @ wu.to_numpy())
        if pv > 0:
            dr = float((wu.to_numpy() * sig).sum() / np.sqrt(pv))          # 分散化比；等波动、互不相关的 n 只 = √n，完全同涨同跌 = 1
            out["eff_n"] = round(dr * dr, 1)

    # 大盘跌 10% 的估算 + 按现在的持仓重放过去一年
    bs = [(float(w[s]), beta[s]) for s in syms if beta[s] is not None]
    cover = sum(x[0] for x in bs)
    out["beta"] = round(sum(x[0] * x[1] for x in bs) / cover, 2) if cover >= 0.5 else None
    out["shock"] = None
    if out["beta"] is not None:
        loss = out["beta"] * SHOCK
        out["shock"] = {"market_drop": SHOCK, "loss_pct": round(loss, 3), "loss_amount": round(total * loss, 2),
                        "loss_of_equity": None if not equity else round(total * loss / equity, 3), "covered": round(cover, 2)}
    out["replay"] = None
    if len(enough) >= 1:
        wu = w[enough] / w[enough].sum()
        pr = (rets[enough].fillna(0) * wu).sum(axis=1)
        pr = pr[rets[enough].notna().any(axis=1)]
        if len(pr) >= MIN_DAYS:
            worst = pr.idxmin()
            rp = {"days": int(len(pr)), "worst_day": round(float(pr.min()), 4), "worst_day_date": str(worst), "max_dd": round(_max_dd(pr), 4),
                  "ann_vol": round(float(pr.std() * np.sqrt(252)), 3), "covered": round(float(w[enough].sum()), 2), "bench_max_dd": None}
            if br is not None:
                b = br.reindex(pr.index).dropna()
                if len(b) >= MIN_DAYS:
                    rp["bench_max_dd"] = round(_max_dd(b), 4)
            out["replay"] = rp

    # 提醒（常识性经验值，不是回测出来的）
    flags: list[dict] = []
    if len(syms) >= 3 and ind_rows and ind_rows[0]["share"] >= IND_FLAG and ind_rows[0]["name"] != "其他":
        flags.append({"level": "warn", "text": f"「{ind_rows[0]['name']}」行业占持仓的 {ind_rows[0]['share'] * 100:.0f}%，这个行业走弱时会一起拖累。"})
    if len(syms) >= 3 and holdings[0]["weight"] >= ONE_FLAG:
        flags.append({"level": "warn", "text": f"{holdings[0]['name']}一只就占 {holdings[0]['weight'] * 100:.0f}%。"})
    hi = [p for p in out["pairs"] if p["corr"] >= CORR_FLAG]
    if hi:
        flags.append({"level": "warn", "text": "这几只过去一年几乎同涨同跌：" + "；".join(f"{p['a_name']} 和 {p['b_name']}（相关 {p['corr']:.2f}）" for p in hi[:3]) + "。买两只不等于分散了风险。"})
    if out["eff_n"] is not None and len(enough) >= 4 and out["eff_n"] < 0.5 * len(enough):
        flags.append({"level": "warn", "text": f"{len(enough)} 只持仓，风险上只相当于约 {out['eff_n']:.1f} 只互不相关的股票——分散得并不充分。"})
    if short:
        flags.append({"level": "info", "text": "上市 / 有数据不到 3 个月的持仓没有算进相关性和重放：" + "、".join(names.get(s, s) for s in short) + "。"})
    out["flags"] = flags
    return out


def build(market: str = "CN") -> dict:
    with settings.market_ctx(market):
        pos = portfolio.list_positions(market, "open")
        if not pos:
            return {"status": "empty", "market": market}
        acct = portfolio.get_account(market)
        with db.market_db(market) as c:
            day = db.get_meta(c, "data_asof") or mc.last_closed_trading_day(c, market)
            if not day:
                return {"status": "empty", "market": market}
            days = mc.trading_days(c, None, day)[-(WINDOW + 1):]
            syms = [p["symbol"] for p in pos]
            panel = load_panel(c, days[0], day, symbols=syms, only_l1=False, float32=False, market=market)
            imap = pd.read_sql_query("SELECT symbol, industry FROM industry_map", c).set_index("symbol")["industry"].to_dict()
            nm = {r[0]: r[1] for r in c.execute("SELECT symbol, name FROM securities WHERE symbol IN (%s)" % ",".join("?" * len(syms)), syms)}
            from . import scanner
            bsym = settings.cfg()["regime"]["benchmark"]
            bench = scanner.load_bench(c, bsym, day)
        values: dict[str, float] = {}
        for p in pos:
            s = p["symbol"]
            if s in panel.symbols:
                px = panel.raw["close"][s].dropna()
                if len(px):
                    values[s] = values.get(s, 0.0) + float(px.iloc[-1]) * p["qty"]
        res = analyze(panel.adj["close"], values, imap, nm, bench, acct.get("equity") or None)
    if res.get("status") != "ok":
        return {**res, "market": market}
    return {**res, "market": market, "date": day, "bench_name": daily_view.BENCH_NAME.get(bsym, bsym)}
