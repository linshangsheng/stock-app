"""关键均线：创业板指 / 标普500 / 纳斯达克100 —— 每条常用均线（20 / 60 / 120 / 200 / 250 日）的位置、明天收盘到多少算站上 / 跌破，
以及按回测证据（tools/ma_research.py → server/key_ma_evidence.json）给出的操作建议。

证据结论（长历史、样本内 60% / 样本外 40%，判定标准事先定好：两段都 Sharpe ≥ 一直持有、最大回撤浅 10 个百分点以上）：
  * 标普500：200 / 250 日均线可操作，20 / 60 / 120 日不行（样本外变差）；主线 200 日
  * 纳斯达克100：只有 200 日均线勉强通过；主线 200 日
  * 创业板指：20 日均线 ±2%（就是软件 ETF 规则的趋势仓）最好，年线（250 日）和 60 日 ±2% 也通过
规则不使用杠杆：杠杆版本回撤 -70% ~ -95%，且 A 股账户买不到杠杆 ETF。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import db, settings

log = logging.getLogger(__name__)
MAS = (20, 60, 120, 200, 250)
NAMES_MA = {20: "月线", 60: "季线", 120: "半年线", 200: "200 日线", 250: "年线"}
EVIDENCE = settings.ROOT / "server" / "key_ma_evidence.json"
_lock = threading.Lock()
_fetch_locks: dict[str, threading.Lock] = {}


def km_cfg() -> dict:
    return settings.cfg().get("key_ma") or {}


def evidence() -> dict:
    try:
        return json.loads(EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"indices": {}}


# ---- 行情：A 股指数用本地库；美股指数从 yfinance 拉近 3 年、缓存（拉不到就用缓存 / 本地美股库） ---------------------------

def _cache_file(key: str):
    d = settings.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"key_ma_{key}.json"


def _fetch_us(symbol: str, start: str, end: str) -> pd.DataFrame:
    from .datasource_cn import get_source
    return get_source("US").index_bars(symbol, start, end)


def history(item: dict, fetch=None) -> tuple[pd.Series, dict]:
    """返回 (收盘价序列, 来源信息)。"""
    if item.get("market", "CN") == "CN":
        with db.market_db("CN") as c:
            df = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol=? ORDER BY date", c, params=(item["symbol"],))
        return df.set_index("date")["close"].astype(float).dropna(), {"source": "本地 A 股库", "stale": False}
    key, sym = item["key"], item["symbol"]
    f = _cache_file(key)
    cached = None
    if f.exists():
        try:
            cached = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            cached = None
    fresh_h = float(km_cfg().get("refresh_hours", 3))
    age_h = (time.time() - cached["fetched_at"]) / 3600 if cached else 1e9
    info = {"source": "yfinance", "stale": False}
    if age_h > fresh_h:
        lk = _fetch_locks.setdefault(key, threading.Lock())
        with lk:
            try:
                today = date.today()
                df = (fetch or _fetch_us)(sym, (today - timedelta(days=365 * 3)).isoformat(), today.isoformat())
                if df is not None and len(df) >= 260:
                    cached = {"fetched_at": time.time(), "rows": [[d, float(c)] for d, c in zip(df["date"], df["close"])]}
                    f.write_text(json.dumps(cached), encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - 上游失败：用缓存
                log.warning("key_ma fetch %s failed: %s", sym, e)
                info = {"source": "yfinance（本次更新失败，用缓存）", "stale": True}
    if cached and cached.get("rows"):
        s = pd.Series({d: c for d, c in cached["rows"]}, dtype=float).sort_index()
        info["fetched_at"] = datetime.fromtimestamp(cached["fetched_at"]).isoformat(timespec="minutes")
        return s, info
    try:                                                          # 兜底：本地美股库（若初始化过美股）
        with db.market_db("US") as c:
            df = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol=? ORDER BY date", c, params=(sym,))
        if len(df):
            return df.set_index("date")["close"].astype(float), {"source": "本地美股库", "stale": True}
    except Exception:  # noqa: BLE001
        pass
    return pd.Series(dtype=float), {"source": None, "stale": True}


# ---- 计算 -------------------------------------------------------------------------------------------

def trigger_prices(close: pd.Series, n: int, band: float = 0.0) -> dict:
    """明天收盘价到多少，就会跌破 / 站上（均线 ×(1∓band)）。明天的均线 = (最近 n-1 个收盘 + 明天收盘) / n。"""
    s = float(close.iloc[-(n - 1):].sum())
    return {"below": (1 - band) * s / (n - 1 + band), "above": (1 + band) * s / (n - 1 - band)}


def rule_state(close: pd.Series, n: int, band: float) -> tuple[bool, str | None]:
    """规则当前是否持有（带滞回），以及最近一次切换的日期。"""
    ma = close.rolling(n).mean()
    s = pd.Series(np.nan, index=close.index)
    s[close > ma * (1 + band)] = 1.0
    s[close < ma * (1 - band)] = 0.0
    s = s.ffill().fillna(0.0)
    ch = s.index[s.diff().fillna(0) != 0]
    return bool(s.iloc[-1] == 1.0), (ch[-1] if len(ch) else None)


def _ev_line(ev: dict, n: int, band: float) -> dict | None:
    return (ev.get("mas") or {}).get(f"{n}_{int(round(band * 100))}")


def _pct(x):
    """与前端 fmtPct 同样的四舍五入（2.25% → +2.3%），同一面板里不出现两个写法。"""
    return f"{round(x * 100 + (1e-9 if x >= 0 else -1e-9), 1):+.1f}%" if x is not None else "—"


def analyze(item: dict, close: pd.Series, ev: dict) -> dict:
    us = item.get("market") == "US"
    close = close.dropna()
    c, last = float(close.iloc[-1]), close.index[-1]
    chg = float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) > 1 else None
    prim = ev.get("primary") or {}
    hold = (ev.get("hold") or {})
    when = "下一个美股交易日收盘（北京时间第二天早上）" if us else "明天收盘"
    act_when = "当天 A 股开盘卖出对应的 QDII ETF" if us else "第二天开盘卖出"
    act_when_buy = "当天 A 股开盘买入对应的 QDII ETF" if us else "第二天开盘买入"
    lines = []
    for n in MAS:
        if len(close) < n + 1:
            continue
        ma = close.rolling(n).mean()
        m = float(ma.iloc[-1])
        evs = {b: _ev_line(ev, n, b) for b in (0.0, 0.02)}
        is_primary = prim.get("n") == n
        band = prim.get("band", 0.0) if is_primary else next((b for b in (0.0, 0.02) if evs[b] and evs[b]["actionable"]), 0.0)
        e = evs.get(band)
        role = "primary" if is_primary else ("actionable" if e and e["actionable"] else "reference")
        holding, since = rule_state(close, n, band)
        tp = trigger_prices(close, n, band)
        slope = float(m / ma.iloc[-21] - 1) if len(ma.dropna()) > 21 else None
        bt = ""
        if e:
            bt = (f"单独按它买卖：全程年化 {_pct(e['all']['cagr'])}、最大回撤 {e['all']['mdd']:.0%}；样本外 {_pct(e['oos']['cagr'])} / {e['oos']['mdd']:.0%}"
                  f"（一直持有：全程 {_pct(hold.get('all', {}).get('cagr'))} / {hold.get('all', {}).get('mdd', 0):.0%}；样本外 {_pct(hold.get('oos', {}).get('cagr'))} / {hold.get('oos', {}).get('mdd', 0):.0%}），"
                  f"每年换仓约 {e['switches_per_year']} 次")
        btxt = f"（均线 ×{1 - band:.2f}）" if band else ""
        btxt_up = f"（均线 ×{1 + band:.2f}）" if band else ""
        if role == "reference":
            advice = "仅参考，不单独操作：回测里单独按它买卖，没有同时做到「收益风险比不低于一直持有、回撤明显更小」。"
        elif holding:
            advice = (f"{'主线：' if is_primary else '辅助确认：'}在线上，持有。{when}低于 {tp['below']:,.2f}{btxt} 就算跌破 → {act_when}"
                      + ("。" if is_primary else "（主线没破就先不动，两条都破再卖更稳）。"))
        else:
            advice = (f"{'主线：' if is_primary else '辅助确认：'}在线下，空仓等待。{when}高于 {tp['above']:,.2f}{btxt_up} 就算站上 → {act_when_buy}"
                      + ("。" if is_primary else "（以主线为准）。"))
        lines.append({"n": n, "alias": NAMES_MA.get(n), "value": round(m, 2), "dist": round(c / m - 1, 4), "above": c > m,
                      "slope20": None if slope is None else round(slope, 4), "band": band, "role": role, "holding": holding,
                      "since": since, "trigger_below": round(tp["below"], 2), "trigger_above": round(tp["above"], 2),
                      "trigger_below_pct": round(tp["below"] / c - 1, 4), "trigger_above_pct": round(tp["above"] / c - 1, 4),
                      "advice": advice, "backtest": bt,
                      "evidence": {str(int(b * 100)): v for b, v in evs.items() if v}})
    p = next((x for x in lines if x["role"] == "primary"), None)
    # 距年线分区
    zone = None
    if len(close) >= 251:
        dev = c / float(close.rolling(250).mean().iloc[-1]) - 1
        z = next((z for z in ev.get("zones", []) if z["lo"] < dev <= z["hi"]), None)
        un = (ev.get("unconditional") or {}).get("fwd120") or {}
        if z and z["fwd120"]["n"]:
            f120, f60 = z["fwd120"], z["fwd60"]
            strong = f120["mean"] >= (un.get("mean") or 0) + 0.05 and f120["win"] >= 0.70 and z["episodes"] >= 10
            weak = f120["mean"] <= (un.get("mean") or 0) - 0.03 or f120["win"] <= 0.45
            tone = "偏强" if strong else ("偏弱" if weak else "接近平均")
            label = (f"距年线 {dev:+.1%}（{z['lo']:+.0%} ~ {z['hi']:+.0%} 区间）" if z["hi"] < 5 else f"距年线 {dev:+.1%}（高于 +20%）")
            text = (f"历史上处在这个区间之后 120 个交易日：平均 {_pct(f120['mean'])}、上涨概率 {f120['win']:.0%}"
                    f"（出现过 {z['episodes']} 次）；所有日子平均是 {_pct(un.get('mean'))} / {un.get('win', 0):.0%} —— {tone}。")
            extra = None
            if z["hi"] <= -0.20:
                extra = ("超跌区：历史上之后多数反弹。主线规则这时多半是空仓——两件事分开看：主线仓位照规则走；"
                         "想抄底只用小钱分批买，并接受可能继续下跌（2008 年这里之后还跌了约三成）。") if strong else \
                        "跌到年线 20% 以下，历史上没有明显的反弹优势（像 2000~2002 年会一路跌下去）：不抄底，等站上主线再买。"
            zone = {"dev": round(dev, 4), "label": label, "text": text, "tone": tone, "extra": extra,
                    "fwd120": f120, "fwd60": f60, "episodes": z["episodes"]}
    if p:
        if p["holding"]:
            head = f"持有：在主线（{p['n']} 日均线{'±2%' if p['band'] else ''}）上方 {p['dist']:+.1%}；收盘跌破 {p['trigger_below']:,.2f}（{p['trigger_below_pct']:+.1%}）就卖"
        else:
            head = f"空仓等待：在主线（{p['n']} 日均线{'±2%' if p['band'] else ''}）下方 {p['dist']:+.1%}；收盘站上 {p['trigger_above']:,.2f}（{p['trigger_above_pct']:+.1%}）再买"
    else:
        head = "没有通过检验的均线：只看不做"
    pe = _ev_line(ev, p["n"], p["band"]) if p else None
    hist = close.iloc[-520:]
    mas_hist = {n: close.rolling(n).mean().iloc[-520:] for n in MAS}
    return {"key": item["key"], "name": item["name"], "market": item.get("market", "CN"), "etf": item.get("etf"), "symbol": item["symbol"],
            "asof": last, "close": round(c, 2), "chg_1d": None if chg is None else round(chg, 4), "headline": head,
            "holding": None if not p else p["holding"], "primary": None if not p else {"n": p["n"], "band": p["band"]},
            "lines": lines, "zone": zone,
            "evidence": {"from": ev.get("from"), "to": ev.get("to"), "split": ev.get("split"), "hold": hold,
                         "primary": pe and {k: pe[k] for k in ("all", "is", "oos", "switches_per_year")}},
            "history": [{"date": d, "close": round(float(v), 2), **{f"ma{n}": (None if mas_hist[n].get(d) != mas_hist[n].get(d) else round(float(mas_hist[n][d]), 2)) for n in MAS}}
                        for d, v in hist.items()]}


def build(fetch=None) -> dict:
    ev_all = evidence()
    out = []
    for item in km_cfg().get("indices", []):
        try:
            s, info = history(item, fetch)
            if len(s) < 260:
                out.append({"key": item["key"], "name": item["name"], "status": "no_data",
                            "message": "数据不足（需要至少 260 个交易日）：A 股指数请先初始化数据；美股指数需要能连上 yfinance。"})
                continue
            r = analyze(item, s, (ev_all.get("indices") or {}).get(item["key"], {}))
            out.append({**r, "status": "ok", **info})
        except Exception as e:  # noqa: BLE001
            log.exception("key_ma %s failed", item.get("key"))
            out.append({"key": item.get("key"), "name": item.get("name"), "status": "error", "message": f"{type(e).__name__}: {e}"})
    return {"status": "ok", "indices": out, "evidence_generated": ev_all.get("generated"),
            "note": "规则输出，不构成投资建议。均线只看收盘价；不使用杠杆（杠杆版本历史回撤 -70% ~ -95%，A 股账户也买不到杠杆 ETF）。"}
