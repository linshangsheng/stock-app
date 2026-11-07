"""宽基指数估值（市盈率）与「分批建仓」参考：A 股 5 个宽基指数 + 港股恒生指数 / 国企指数 / 恒生科技（独立的港股市场）+ 美股标普500 / 纳斯达克100。

数据：蛋卷基金的指数估值页（danjuanfunds.com，免登录的公开接口，第三方、没有官方保证，可能改版 / 限流 / 中断）：
  * /djapi/index_eva/dj                         一次返回 60 多个指数的当前市盈率 / 市净率 / 各自的历史分位 / 股息率；
  * /djapi/index_eva/pe_history/{代码}?day=all  近 10 年的市盈率历史（每周一个点）。
  这里的「市盈率」是指数成分股的整体滚动市盈率（总市值 / 总盈利），不是各只股票市盈率的简单平均。

规则（用户定的，参数在 config.yaml 的 valuation 段）：
  距离 = 当前市盈率 / 近 10 年最低市盈率 - 1。距离 <= 10% 就进入「分批建仓区」（例如近 10 年最低 20，则 22 以内）。
  进入之后按距离分阶梯、累计买到的批数不同（总共 5 批、每批 20%；第 5 批留作机动）：
    <=10% 第 1 批 · <=6% 累计 2 批 · <=3% 累计 3 批 · <=0.5%（几乎就是近 10 年最低）累计 4 批 · 第 5 批只在创新低后继续下探时再用。
  每批之间至少隔 10 个交易日，不要一天买完。这套分批方式是按一般做法设计的，没有拿历史回测验证过；估值低可以持续很久，所以才要分批。
只做展示和提醒，不进入选股、也不进入回测。

存储：整段历史存在 A 股库的 meta 表（valuation_cn），每次刷新与已有的合并（日期并集），接口失效时手里的历史还在。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import db, settings

log = logging.getLogger(__name__)
BASE = "https://danjuanfunds.com/djapi/index_eva"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
# 本软件的指数代码 -> 蛋卷的指数代码（美股只做标普500 和纳斯达克100，对应软件里的 SPY / QQQ；罗素2000 / 道琼斯不显示）
CODES_BY_MARKET = {
    "CN": {"sh.000016": "SH000016", "sh.000300": "SH000300", "sh.000905": "SH000905", "sh.000852": "SH000852", "sz.399006": "SZ399006"},
    "US": {"SPY": "SP500", "QQQ": "NDX"},
}
# 港股（独立市场，数据存在 hk.db）：三个指数。没有「市场温度 / 指数规则」，名字和对应 ETF 在这里写死。
HK_SPEC = {
    "HK.HSI": {"code": "HKHSI", "name": "恒生指数", "etf": "159920 恒生ETF", "style": "港股大盘"},
    "HK.HSCEI": {"code": "HKHSCEI", "name": "国企指数", "etf": "510900 恒生国企ETF（H股ETF）", "style": "港股国企"},
    "HK.HSTECH": {"code": "HKHSTECH", "name": "恒生科技", "etf": "513180 恒生科技ETF", "style": "港股科技"},
}
CODES_BY_MARKET["HK"] = {sym: v["code"] for sym, v in HK_SPEC.items()}
CODES = CODES_BY_MARKET["CN"]                              # 兼容旧名
METAS = {"CN": "valuation_cn", "US": "valuation_us", "HK": "valuation_hk"}
META = METAS["CN"]
STALE_SECONDS = 12 * 3600
_lock = threading.Lock()
_bg: dict = {m: {"running": False, "last": None} for m in ("CN", "US", "HK")}


def vcfg() -> dict:
    return settings.base_cfg().get("valuation") or {}


def _http(url: str, timeout: float = 25) -> dict:
    import requests
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if j.get("result_code") not in (0, None):
        raise RuntimeError(f"接口返回 result_code={j.get('result_code')}")
    return j["data"]


def _day(ts_ms: int) -> str:
    """毫秒时间戳 -> 北京时间的日期（接口的时间戳是北京时间 0 点）。"""
    return (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) + timedelta(hours=8)).date().isoformat()


def fetch_current() -> dict[str, dict]:
    """{蛋卷代码: {pe, pb, pe_pct, pb_pct, yield, date}}"""
    out = {}
    for it in _http(f"{BASE}/dj")["items"]:
        if it.get("pe") is None or not it.get("ts"):
            continue
        out[it["index_code"]] = {"pe": float(it["pe"]), "pb": it.get("pb"), "pe_pct": it.get("pe_percentile"), "pb_pct": it.get("pb_percentile"),
                                 "yield": it.get("yeild"), "date": _day(it["ts"])}
    return out


def fetch_history(code: str) -> list[list]:
    rows = _http(f"{BASE}/pe_history/{code}?day=all")["index_eva_pe_growths"]
    return [[_day(r["ts"]), round(float(r["pe"]), 4)] for r in rows if r.get("pe") and r.get("ts")]


def _load(conn, market: str = "CN") -> dict:
    try:
        return json.loads(db.get_meta(conn, METAS[market]) or "{}")
    except ValueError:
        return {}


def refresh(conn, market: str = "CN") -> dict:
    """联网取最新（各指数的历史 + 当前），与库里已有的历史合并后保存。任何一步失败都不抛出，返回原因（已有的数据原样保留）。"""
    codes = CODES_BY_MARKET[market]
    with _lock:
        old = _load(conn, market)
        series = {k: dict((d, v) for d, v in rows) for k, rows in (old.get("series") or {}).items()}
        current = dict(old.get("current") or {})
        errors = []
        cur = {}
        try:
            cur = fetch_current()
        except Exception as e:  # noqa: BLE001 - 第三方网页
            errors.append(f"当前估值：{type(e).__name__}: {str(e)[:100]}")
        for sym, code in codes.items():
            try:
                for d, v in fetch_history(code):
                    series.setdefault(sym, {})[d] = v
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym} 历史：{type(e).__name__}: {str(e)[:100]}")
            if code in cur:
                current[sym] = cur[code]
        ok = bool(series) and not (len(errors) >= len(codes) + 1)
        if ok:
            db.set_meta(conn, METAS[market], json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                                "series": {k: sorted([d, v] for d, v in s.items()) for k, s in series.items()},
                                                "current": current}, ensure_ascii=False))
            conn.commit()
        return {"ok": ok, "errors": errors}


def refresh_async(market: str = "CN") -> bool:
    """后台刷新（不阻塞页面和盘后任务链）。返回是否新开了一个后台任务。"""
    bg = _bg[market]
    if bg["running"]:
        return False
    bg["running"] = True

    def run():
        try:
            with settings.market_ctx(market), db.market_db(market) as c:
                bg["last"] = refresh(c, market)
        except Exception as e:  # noqa: BLE001
            bg["last"] = {"ok": False, "errors": [str(e)[:150]]}
        finally:
            bg["running"] = False
    threading.Thread(target=run, name=f"valuation-{market}", daemon=True).start()
    return True


def is_demo(market: str = "CN") -> bool:
    """演示数据模式不联网。"""
    return (settings.base_cfg().get("datasource") or {}).get(market.lower()) == "demo"


# ---- 分析 ----------------------------------------------------------------------------------------

def plan_for(gap: float | None, cfg: dict | None = None) -> dict:
    """距近 10 年最低的幅度 -> 现在累计应买到第几批。gap 为 None 或在建仓区之外 = 0 批。"""
    c = cfg or vcfg()
    tiers = list(c.get("tiers") or [0.10, 0.06, 0.03, 0.005])
    total = int(c.get("batches", 5))
    n = 0 if gap is None else sum(1 for t in tiers if gap <= t + 1e-12)
    return {"batches_total": total, "batches_now": n, "per_batch_pct": round(1 / total, 4) if total else None,
            "tiers": [{"gap": t, "batches": k + 1} for k, t in enumerate(tiers)], "min_days": int(c.get("min_days_between", 10))}


def analyze_one(sym: str, name: str, rows: list, cur: dict | None, cfg: dict | None = None) -> dict | None:
    """rows = [[日期, 市盈率], ...]（按日期升序）；cur = 当前估值（可为 None，则用历史最后一个点）。"""
    c = cfg or vcfg()
    if not rows and not cur:
        return None
    pts = [(d, float(v)) for d, v in rows if v and v > 0]
    last_d = pts[-1][0] if pts else None
    if cur and (last_d is None or cur["date"] >= last_d):
        pts = [p for p in pts if p[0] != cur["date"]] + [(cur["date"], float(cur["pe"]))]
    if len(pts) < 30:
        return None
    asof, pe = pts[-1]
    years = int(c.get("window_years", 10))
    start = (datetime.fromisoformat(asof) - timedelta(days=int(365.25 * years))).date().isoformat()
    win = [p for p in pts if p[0] >= start]
    span = (datetime.fromisoformat(asof) - datetime.fromisoformat(win[0][0])).days / 365.25
    wtext = f"近 {years} 年" if span >= years - 0.6 else f"{win[0][0][:7]} 以来（约 {span:.1f} 年）"          # 有的指数（如恒生科技）没有 10 年历史，如实写
    low_d, low = min(win, key=lambda p: p[1])
    hi_d, hi = max(win, key=lambda p: p[1])
    vals = sorted(v for _, v in win)
    gap = pe / low - 1
    enter = float(c.get("enter_gap", 0.10))
    plan = plan_for(gap, c)
    own_pct = sum(1 for v in vals if v <= pe) / len(vals)
    out = {"symbol": sym, "name": name, "asof": asof, "pe": round(pe, 2), "low": round(low, 2), "low_date": low_d, "high": round(hi, 2), "high_date": hi_d,
           "median": round(vals[len(vals) // 2], 2), "gap": round(gap, 4), "enter_price": round(low * (1 + enter), 2),
           "window_text": wtext, "span_years": round(span, 1), "pct": round(own_pct, 3), "pct_src": cur.get("pe_pct") if cur else None, "pb": cur.get("pb") if cur else None, "pb_pct": cur.get("pb_pct") if cur else None,
           "dividend_yield": cur.get("yield") if cur else None, "since": win[0][0], "points": len(win),
           "in_zone": gap <= enter + 1e-9, **plan}
    if gap <= enter + 1e-9:
        n = plan["batches_now"]
        nxt = plan["tiers"][n]["gap"] if n < len(plan["tiers"]) else None
        out["advice"] = (f"进入分批建仓区（距{wtext}最低 {gap:+.1%}）：累计买到第 {n} 批（共 {plan['batches_total']} 批，每批 {1 / plan['batches_total']:.0%}）。"
                         + (f"市盈率再降到 {low * (1 + nxt):.2f} 以内（距最低 {nxt:+.1%}）再加一批。" if nxt is not None
                            else f"已经基本是{wtext}最低，累计 {n} 批；最后 1 批留作机动，只在创新低后继续明显下探时再用。")
                         + f"每批之间至少隔 {plan['min_days']} 个交易日。")
    else:
        out["advice"] = f"还没到：距{wtext}最低 {gap:+.1%}（进入建仓区要降到 {low * (1 + enter):.2f} 以内，现在 {pe:.2f}）。"
    return out


def view_items(conn, market: str = "CN") -> dict:
    """库里已有的估值结果（不联网）：{status, items: {指数代码: 分析结果}, refreshed_at, stale}"""
    from . import market_view
    d = _load(conn, market)
    if not d.get("series"):
        return {"status": "empty", "items": {}, "refreshed_at": None, "stale": True}
    if market == "HK":
        names = {sym: v["name"] for sym, v in HK_SPEC.items()}
    else:
        with settings.market_ctx(market):
            names = {i["symbol"]: i["name"] for i in market_view.index_list(market)}
    items = {}
    for sym in CODES_BY_MARKET[market]:
        if sym not in d["series"]:
            continue
        r = analyze_one(sym, names.get(sym, sym), d["series"][sym], (d.get("current") or {}).get(sym))
        if r:
            items[sym] = {**r, "etf": HK_SPEC[sym]["etf"], "style": HK_SPEC[sym]["style"]} if market == "HK" else r
    at = d.get("at")
    stale = not at or (datetime.now() - datetime.fromisoformat(at)).total_seconds() > STALE_SECONDS
    stale = stale or not set(CODES_BY_MARKET[market]) <= set(d["series"])      # 新增了指数而库里还没有：马上去取，不等 12 小时
    return {"status": "ok" if items else "empty", "items": items, "refreshed_at": at, "stale": stale}


def build(market: str = "CN") -> dict:
    """/api/market/valuation：宽基指数（A 股 5 个 / 美股标普500 + 纳斯达克100）的估值与分批建仓参考。"""
    if is_demo(market):
        return {"status": "demo", "message": "演示数据模式不联网，没有指数估值。", "items": []}
    with settings.market_ctx(market), db.market_db(market) as c:
        v = view_items(c, market)
    started = False
    if v["status"] != "ok" or v["stale"]:
        started = refresh_async(market)
    if v["status"] != "ok":
        return {"status": "loading", "message": "第一次使用：正在联网取指数估值历史（几秒钟），稍后刷新。" + ("" if started else "（取数进行中）"), "items": [], "bg": _bg[market].get("last")}
    c2 = vcfg()
    order = list(CODES_BY_MARKET[market])
    return {"status": "ok", "items": [v["items"][s] for s in order if s in v["items"]], "refreshed_at": v["refreshed_at"], "stale": v["stale"],
            "rule": {"enter_gap": float(c2.get("enter_gap", 0.10)), "window_years": int(c2.get("window_years", 10)), **plan_for(None, c2)},
            "source": "蛋卷基金·指数估值（成分股整体滚动市盈率，每周一个点的近 10 年历史）",
            "note": NOTE}


def for_view(market: str = "CN") -> dict | None:
    """给 /api/market/view 用：只读库里已有的结果，过期就在后台刷新；演示模式 / 没有数据返回 None。"""
    if is_demo(market) or not vcfg().get("enabled", True):
        return None
    with settings.market_ctx(market), db.market_db(market) as c:
        v = view_items(c, market)
    if v["status"] != "ok" or v["stale"]:
        refresh_async(market)
    return v if v["status"] == "ok" else None


NOTE = ("市盈率低不等于马上涨：低估值可以持续一两年，所以要分批、拉长时间买，不要一次买完。「近 10 年最低」按每周一个点的历史计算，真正的日最低可能略低；"
        "指数只有近 10 年的历史，样本里只有几轮真正的低位。盈利会骗人（周期行业景气顶点时市盈率偏低、衰退时偏高），建议同时看市净率分位。"
        "这套分批方式是按一般做法设计的，没有用历史回测验证过；数据来自第三方网站，可能延迟或中断。只作参考，不是投资建议，不进入选股与回测。")


if __name__ == "__main__":                                 # python -m server.valuation [US|HK] ：手动刷新
    import sys
    mk = next((a.upper() for a in sys.argv[1:] if a.upper() in ("US", "HK")), "CN")
    with settings.market_ctx(mk), db.market_db(mk) as c:
        print(refresh(c, mk))
