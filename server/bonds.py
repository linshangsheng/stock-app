"""10 年期国债收益率（股债性价比用）：中国 / 美国。港股对照美国（港元联系汇率下港元利率跟随美元，所以港股拿美国 10 年期国债当参照）。

数据：东方财富数据中心的「中美国债收益率」（reportName=RPTA_WEB_TREASURYYIELD，日频，1990 年起；这里只取 2015-10 起）：
  EMM00166466 = 中国国债收益率 10 年，EMG00001310 = 美国国债收益率 10 年（与同一行的 2 年期、10 年-2 年利差互相对得上，2026-11-06：中国 1.5868、美国 4.86）。
  接口每页最多 500 行、实测每页要约 20 秒，所以第一次取 10 年（6 页）用几个线程并行，之后每次只取最新一页（约 2 年）合并进去。
存储：各市场库 meta 表的 bond_yield_cn / bond_yield_us / bond_yield_hk（港股存的是美国的那条）。
股债利差 = 指数盈利收益率（1 ÷ 滚动市盈率）− 10 年期国债收益率；利差越大，股票相对国债越便宜。只做展示：A 股样本里这个关系并不强（见 README），不进入选股和回测。"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import db, settings

log = logging.getLogger(__name__)
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Referer": "https://data.eastmoney.com/"}
COL = {"CN": "EMM00166466", "US": "EMG00001310"}
SERIES_OF = {"CN": "CN", "US": "US", "HK": "US"}                 # 市场 -> 用哪条国债
NAME = {"CN": "中国10年期国债", "US": "美国10年期国债"}
SINCE = "2015-10-01"
PAGE = 500
STALE_SECONDS = 12 * 3600
METAS = {"CN": "bond_yield_cn", "US": "bond_yield_us", "HK": "bond_yield_hk"}
_lock = threading.Lock()
_bg: dict = {m: {"running": False, "last": None} for m in METAS}
_mem: dict = {"at": 0.0, "rows": None}                              # 同一时刻三个市场都要刷新时只联网一次


def _page(n: int, size: int = PAGE, timeout: float = 90) -> dict:
    import requests
    last = None
    for _ in range(3):
        try:
            r = requests.get(URL, params={"reportName": "RPTA_WEB_TREASURYYIELD", "columns": f"SOLAR_DATE,{COL['CN']},{COL['US']}", "pageNumber": n, "pageSize": size,
                                          "sortColumns": "SOLAR_DATE", "sortTypes": -1, "source": "WEB", "client": "WEB", "filter": f"(SOLAR_DATE>='{SINCE}')"},
                             headers=UA, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if not j.get("success", True) or not j.get("result"):
                raise RuntimeError(str(j.get("message"))[:80])
            return j["result"]
        except Exception as e:  # noqa: BLE001 - 第三方网页
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:100]}")


def _rows(res: dict) -> dict[str, list]:
    out = {"CN": [], "US": []}
    for r in res.get("data") or []:
        d = str(r["SOLAR_DATE"])[:10]
        for k in ("CN", "US"):
            v = r.get(COL[k])
            if isinstance(v, (int, float)) and 0 < v < 30:
                out[k].append([d, round(float(v), 4)])
    return out


def fetch(full: bool) -> dict[str, list]:
    """full = 取 2015-10 起的全部（多页并行）；否则只取最新一页。返回 {'CN': [[日期, 收益率%]...], 'US': [...]}（日期升序）。"""
    first = _page(1)
    out = _rows(first)
    pages = int(first.get("pages") or 1)
    if full and pages > 1:
        with ThreadPoolExecutor(max_workers=3) as ex:
            for part in ex.map(_page, range(2, pages + 1)):
                for k, v in _rows(part).items():
                    out[k] += v
    return {k: [[d, y] for d, y in sorted({d: y for d, y in v}.items())] for k, v in out.items()}


def load(conn, market: str) -> dict:
    try:
        return json.loads(db.get_meta(conn, METAS[market]) or "{}")
    except ValueError:
        return {}


def refresh(conn, market: str) -> dict:
    """联网取最新并合并进库（港股存美国的那条）。失败不抛出，已有数据原样保留。"""
    series = SERIES_OF[market]
    with _lock:
        old = load(conn, market)
        have = {d: y for d, y in (old.get("rows") or [])}
        full = len(have) < 500
        try:
            if _mem["rows"] is not None and time.time() - _mem["at"] < 600 and (not full or _mem["full"]):
                got = _mem["rows"]
            else:
                got = fetch(full)
                _mem.update({"at": time.time(), "rows": got, "full": full})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        for d, y in got[series]:
            have[d] = y
        if not have:
            return {"ok": False, "error": "没有取到国债收益率"}
        db.set_meta(conn, METAS[market], json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "series": series, "rows": sorted([d, y] for d, y in have.items())}))
        conn.commit()
        return {"ok": True, "rows": len(have), "last": max(have)}


def refresh_async(market: str) -> bool:
    bg = _bg[market]
    if bg["running"]:
        return False
    bg["running"] = True

    def run():
        try:
            with settings.market_ctx(market), db.market_db(market) as c:
                bg["last"] = refresh(c, market)
        except Exception as e:  # noqa: BLE001
            bg["last"] = {"ok": False, "error": str(e)[:150]}
        finally:
            bg["running"] = False
    threading.Thread(target=run, name=f"bond-{market}", daemon=True).start()
    return True


def stale(d: dict) -> bool:
    if not d.get("rows") or not d.get("at"):
        return True
    return (datetime.now() - datetime.fromisoformat(d["at"])).total_seconds() > STALE_SECONDS


def yield_on(rows: list, day: str, max_gap_days: int = 10) -> float | None:
    """day 当天或之前最近一个交易日的收益率（假期 / 周末往前找，最多往前 max_gap_days 天）。rows 按日期升序。"""
    lo, hi = 0, len(rows)
    while lo < hi:                                            # 二分：最后一个 日期 <= day
        mid = (lo + hi) // 2
        if rows[mid][0] <= day:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    d, y = rows[lo - 1]
    gap = (datetime.fromisoformat(day) - datetime.fromisoformat(d)).days
    return y if gap <= max_gap_days else None


def info(rows: list, market: str) -> dict | None:
    if not rows:
        return None
    last_d, last_y = rows[-1]
    ago = rows[-251][1] if len(rows) > 251 else None
    out = {"name": NAME[SERIES_OF[market]], "yield": last_y, "asof": last_d, "chg_1y": round(last_y - ago, 2) if ago is not None else None, "series": SERIES_OF[market]}
    if market == "HK":
        out["note"] = "港元实行联系汇率，港元利率跟随美元，所以港股对照美国 10 年期国债（不是港元国债的精确收益率）。"
    return out


def add_spread(item: dict, pe_points: list, bond_rows: list) -> None:
    """给一条指数估值结果加上股债利差：盈利收益率 − 10 年期国债。pe_points = [(日期, 市盈率)]（近 10 年的周点）。"""
    if not bond_rows or not item.get("pe"):
        return
    y = yield_on(bond_rows, item["asof"], max_gap_days=15)
    if y is None:
        return
    ey = 100.0 / item["pe"]
    spread = ey - y
    hist = []
    for d, pe in pe_points:
        yy = yield_on(bond_rows, d)
        if yy is not None and pe > 0:
            hist.append(100.0 / pe - yy)
    item.update({"ey": round(ey, 2), "bond_yield": y, "spread": round(spread, 2)})
    if len(hist) >= 52:                                       # 至少一年的点才给分位
        hs = sorted(hist)
        pct = sum(1 for s in hs if s <= spread) / len(hs)
        item.update({"spread_pct": round(pct, 3), "spread_median": round(hs[len(hs) // 2], 2), "spread_min": round(hs[0], 2), "spread_max": round(hs[-1], 2), "spread_points": len(hs),
                     "spread_label": "股票相对便宜" if pct >= 0.8 else "股票相对偏贵" if pct <= 0.2 else "中等"})
