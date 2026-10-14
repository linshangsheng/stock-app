"""VIX 恐慌指数（美股行情页）：当前水平、分区、历史分位、期限结构（VIX ÷ VIX3M）、近一年走势，
以及按长历史研究（tools/vix_research.py → server/vix_evidence.json）给出的「处在这个区之后标普500 的历史表现」。

数据：本地美股库的 ^VIX 日线（10 年，每天盘后更新）+ yfinance 实时补最新一天与 ^VIX3M（缓存 3 小时，失败用缓存 / 本地库）。
官方来源是 Cboe（https://www.cboe.com/tradable_products/vix/vix_historical_data/）；Yahoo 的 ^VIX 就是 Cboe 发布的值，这里沿用已有的 yfinance 通道。
展示用，不进入选股与回测。"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

from . import db, settings

log = logging.getLogger(__name__)
EVIDENCE = settings.ROOT / "server" / "vix_evidence.json"
ZONE_NAMES = {(0, 15): "平静", (15, 20): "正常", (20, 25): "略紧张", (25, 30): "紧张", (30, 40): "恐慌", (40, 999): "极度恐慌"}


def evidence() -> dict:
    try:
        return json.loads(EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_file():
    d = settings.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "vix_live.json"


def _fetch_yf(symbol: str, start: str, end: str) -> pd.DataFrame:
    from .datasource_cn import get_source
    return get_source("US").index_bars(symbol, start, end)


def load_series(fetch=None) -> tuple[pd.Series, pd.Series, dict]:
    """返回 (VIX 收盘, VIX3M 收盘, 来源信息)。"""
    with db.market_db("US") as c:
        loc = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol='^VIX' ORDER BY date", c)
    vix = loc.set_index("date")["close"].astype(float) if len(loc) else pd.Series(dtype=float)
    f = _cache_file()
    cached = None
    if f.exists():
        try:
            cached = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            cached = None
    hours = float((settings.cfg().get("key_ma") or {}).get("refresh_hours", 3))
    age = (time.time() - cached["fetched_at"]) / 3600 if cached else 1e9
    info = {"source": "本地美股库", "stale": False}
    if age > hours:
        try:
            start = (date.today() - timedelta(days=420)).isoformat()
            rows = {}
            for sym in ("^VIX", "^VIX3M"):
                df = (fetch or _fetch_yf)(sym, start, date.today().isoformat())
                rows[sym] = [[d, float(v)] for d, v in zip(df["date"], df["close"])]
            cached = {"fetched_at": time.time(), "rows": rows}
            f.write_text(json.dumps(cached), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - 上游失败：用缓存 / 本地库
            log.warning("vix live fetch failed: %s", e)
            info = {"source": "本地美股库（实时更新失败）", "stale": True}
    v3 = pd.Series(dtype=float)
    if cached and cached.get("rows"):
        live = pd.Series({d: v for d, v in cached["rows"].get("^VIX", [])}, dtype=float)
        if len(live):
            vix = pd.concat([vix[~vix.index.isin(live.index)], live]).sort_index()
            info = {**info, "source": "yfinance（Cboe 发布的 VIX）", "fetched_at": datetime.fromtimestamp(cached["fetched_at"]).isoformat(timespec="minutes")}
        v3 = pd.Series({d: v for d, v in cached["rows"].get("^VIX3M", [])}, dtype=float).sort_index()
    return vix.dropna(), v3.dropna(), info


def _pct(x):
    return f"{x:+.1%}" if x is not None else "—"


def _zone(v: float) -> tuple[int, str]:
    for i, ((lo, hi), name) in enumerate(ZONE_NAMES.items()):
        if lo <= v < hi:
            return i, name
    return len(ZONE_NAMES) - 1, "极度恐慌"


def analyze(vix: pd.Series, v3: pd.Series, ev: dict) -> dict:
    last, v = vix.index[-1], float(vix.iloc[-1])
    prev = float(vix.iloc[-2]) if len(vix) > 1 else v
    zi, zname = _zone(v)
    ten = vix.tail(2520)
    pct10 = float((ten < v).mean())
    y1 = vix.tail(252)
    ratio = None
    if len(v3) and last in v3.index:
        ratio = v / float(v3[last])
    elif len(v3):
        ratio = v / float(v3.iloc[-1])
    ez = (ev.get("zones") or [])[zi] if ev.get("zones") and zi < len(ev["zones"]) else None
    un = ev.get("unconditional") or {}
    lines = []
    if ez:
        a, b = ez["fwd20"], ez["fwd60"]
        lines.append(f"1990 年以来 VIX 处在「{ZONE_NAMES_BY_IDX[zi]}」区间（{ez['lo']}~{ez['hi'] if ez['hi'] < 900 else '∞'}）之后，标普500 接下来 20 个交易日平均 {_pct(a['mean'])}、"
                     f"上涨概率 {a['win']:.0%}；60 个交易日平均 {_pct(b['mean'])}、上涨概率 {b['win']:.0%}，最差一次 {_pct(b['worst'])}"
                     f"（出现过 {ez['episodes']} 次；所有日子的平均：60 日 {_pct((un.get('fwd60') or {}).get('mean'))}、上涨 {((un.get('fwd60') or {}).get('win') or 0):.0%}）。")
    if zi >= 4:
        read = ("恐慌区：历史上这之后的平均表现好于平时，但「平均」不等于「马上见底」——最差的一次仍然是大跌。想参与只用小钱分批，不要一次买满；"
                "这是统计倾向，不是买入信号。")
    elif zi == 3:
        read = "紧张区：市场不安，波动大。历史上这之后略好于平时，但先别把它当成抄底信号；按指数的均线规则（关键均线）操作。"
    elif zi == 0:
        read = "平静区：市场波动小、情绪放松。历史上这时之后的表现和平时差不多，不是卖出信号——趋势还在就继续按均线规则持有；只是遇到突发消息，波动会放大得比较快。"
    else:
        read = "正常区：没有明显的恐慌或自满，VIX 对操作没有额外的提示，按指数的均线规则执行即可。"
    ts = None
    if ratio is not None:
        tz = 0 if ratio < 0.9 else 1 if ratio < 1.0 else 2
        et = (ev.get("term_zones") or [None, None, None])[tz]
        ts = {"ratio": round(ratio, 3), "zone": tz, "name": ["期限结构正常（远高于近）", "近端接近远端", "倒挂（近端比远端更恐慌）"][tz],
              "text": ["市场对短期没有特别担心。", "短期担忧在上升，留意。", "短期恐慌超过中期：这是市场正处于「急性恐慌」的特征，通常伴随大幅波动。"][tz]}
        if et:
            ts["evidence"] = f"历史上处在这个状态之后 60 个交易日，标普500 平均 {_pct(et['fwd60']['mean'])}、上涨概率 {et['fwd60']['win']:.0%}（{et['episodes']} 次）。"
    hist = vix.tail(260)
    h3 = v3.reindex(hist.index).ffill() if len(v3) else None
    return {"asof": last, "value": round(v, 2), "chg_1d": round(v - prev, 2), "chg_1d_pct": round(v / prev - 1, 4) if prev else None,
            "chg_5d": round(v - float(vix.iloc[-6]), 2) if len(vix) > 6 else None,
            "zone": zi, "zone_name": zname, "zones": [{"lo": lo, "hi": hi if hi < 900 else None, "name": n} for (lo, hi), n in ZONE_NAMES.items()],
            "pct_10y": round(pct10, 3), "hi_1y": round(float(y1.max()), 2), "lo_1y": round(float(y1.min()), 2), "median_10y": round(float(ten.median()), 2),
            "term": ts, "read": read, "evidence_line": lines[0] if lines else None, "evidence": ez, "evidence_all": ev.get("zones"),
            "unconditional": un,
            "evidence_range": {"from": ev.get("vix_from"), "to": ev.get("to")},
            "history": [{"date": d, "vix": round(float(x), 2), "vix3m": (None if h3 is None or h3.get(d) != h3.get(d) else round(float(h3[d]), 2))} for d, x in hist.items()]}


ZONE_NAMES_BY_IDX = list(ZONE_NAMES.values())


def build(fetch=None) -> dict:
    vix, v3, info = load_series(fetch)
    if len(vix) < 260:
        return {"status": "no_data", "message": "VIX 数据不足：请先初始化美股数据，或检查网络（需要能连上 yfinance）。"}
    return {"status": "ok", **analyze(vix, v3, evidence()), **info,
            "note": "VIX = 市场预期的未来 30 天标普500 年化波动率，常叫「恐慌指数」。它衡量的是不安程度，不预测方向；历史统计仅供参考，不构成投资建议。"}
