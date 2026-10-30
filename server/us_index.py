"""美股指数成分股：标普500（维基百科表格）+ 纳斯达克100（纳斯达克官方接口 api.nasdaq.com），合并去重后约 520~530 只
（两个指数重叠很多：苹果、微软、英伟达等都在两边）。用途：美股「观察清单」只在这些股票里选（config universe.us.index_members_only）。

名单每月更新一次（成分股调整本来就不频繁），存到 data/cache/us_index_members.json；联网失败就用上次的缓存；第一次运行又没有网络时用软件自带的快照
（server/us_index_members.json，发版时更新）。代码统一成库里的写法（BRK.B → BRK-B）。
注意：只有「当前」成分，没有历史进出记录——只用于今天的扫描；回测不用它（否则会只选到后来才进入指数的赢家，结果偏乐观）。"""
from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path

from . import settings

log = logging.getLogger(__name__)
SEED = settings.ROOT / "server" / "us_index_members.json"
TTL_DAYS = 30
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
MIN_SP500, MIN_NDX = 450, 90                      # 取回的名单少于这个数 = 页面改版 / 被截断，不采用


def norm(sym: str) -> str:
    return str(sym).strip().upper().replace(".", "-")


def _cache_file() -> Path:
    d = settings.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "us_index_members.json"


def fetch_sp500() -> list[str]:
    import pandas as pd
    import requests
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=30).text
    t = next(x for x in pd.read_html(io.StringIO(html)) if "Symbol" in x.columns)
    return sorted({norm(s) for s in t["Symbol"]})


def fetch_ndx() -> list[str]:
    import requests
    j = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers=UA, timeout=30).json()
    return sorted({norm(r["symbol"]) for r in j["data"]["data"]["rows"]})


def _read(f: Path) -> dict | None:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if d.get("sp500") and d.get("ndx") else None
    except (OSError, ValueError):
        return None


def refresh(force: bool = False) -> dict:
    """返回 {sp500, ndx, fetched_at, source}。过期（> 30 天）或 force 时联网更新；失败用缓存，再失败用自带快照。"""
    f = _cache_file()
    cached = _read(f)
    if cached and not force and time.time() - cached.get("fetched_at", 0) < TTL_DAYS * 86400:
        return {**cached, "source": "cache"}
    got, errs = {}, []
    for key, fn, lo in (("sp500", fetch_sp500, MIN_SP500), ("ndx", fetch_ndx, MIN_NDX)):
        try:
            lst = fn()
            if len(lst) < lo:
                raise RuntimeError(f"只取到 {len(lst)} 只，少于 {lo}，疑似页面改版")
            got[key] = lst
        except Exception as e:  # noqa: BLE001 - 联网失败不影响扫描：用旧名单
            errs.append(f"{key}: {type(e).__name__}: {str(e)[:80]}")
            log.warning("us index %s fetch failed: %s", key, e)
    base = cached or _read(SEED) or {"sp500": [], "ndx": []}
    out = {"sp500": got.get("sp500", base["sp500"]), "ndx": got.get("ndx", base["ndx"]),
           "fetched_at": time.time() if len(got) == 2 else base.get("fetched_at", 0)}
    if got:
        f.write_text(json.dumps({**out, "errors": errs}, ensure_ascii=False), encoding="utf-8")
    out["source"] = "network" if len(got) == 2 else ("partial" if got else ("cache" if cached else "seed"))
    out["errors"] = errs
    return out


def members() -> set[str]:
    """标普500 ∪ 纳斯达克100（库里的代码写法）。"""
    d = refresh()
    return set(d["sp500"]) | set(d["ndx"])


def summary() -> dict:
    d = refresh()
    s, n = set(d["sp500"]), set(d["ndx"])
    return {"sp500": len(s), "ndx": len(n), "both": len(s & n), "total": len(s | n), "source": d.get("source"),
            "updated": time.strftime("%Y-%m-%d", time.localtime(d["fetched_at"])) if d.get("fetched_at") else None}
