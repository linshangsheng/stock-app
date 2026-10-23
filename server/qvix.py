"""A 股恐慌指数：期权隐含波动率 QVIX（期权论坛 optbbs.com 按 Cboe VIX 同样的方法，用 ETF 期权价格算出的「未来 30 天预期波动率」，年化 %）。
官方的中国波指 iVIX 2018 年已停止发布，这是目前能免费取到、方法与美股 VIX 一致的序列。取两条：
  * QVIX300：沪深300ETF 期权（2019-12-23 起）——代表 A 股大盘，对应美股 VIX 之于标普500；
  * QVIXCYB：创业板ETF 期权（2022-09-19 起）——与创业板指同一标的。
数据：http://1.optbbs.com/d/csv/d/k.csv（全部 QVIX 的日线开高低收，约 0.9 MB，每个交易日收盘后更新）。
实测（2026-10-16，本机网络）：这个站点对每个连接限速（约每秒几十～几百字节），但支持分段下载 → 第一次用 8 个连接分段并行取全量，
之后每天只取文件末尾几 KB（最近几行）。存进 A 股库 index_bar（symbol = QVIX300 / QVIXCYB），展示用，不进入选股与回测。"""
from __future__ import annotations

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd

from . import db, settings

log = logging.getLogger(__name__)
URL = "http://1.optbbs.com/d/csv/d/k.csv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
# 文件里的列位置（第 0 列是日期；每条序列占 4 列：开 / 高 / 低 / 收，但中间夹着几个只占 2 列的字段，不能按 4 列一组数）。
# 2026-10-16 核对：9~12 列从 2019-12-23（沪深300ETF 期权上市日）开始；71~74 列与当天创业板 QVIX 分时文件（vixcyb.csv）的开 / 最高 / 最低完全一致
SERIES = {
    "QVIX300": {"name": "沪深300", "cols": (9, 10, 11, 12), "since": "2019-12-23", "index": "sh.000300"},
    "QVIXCYB": {"name": "创业板", "cols": (71, 72, 73, 74), "since": "2022-09-19", "index": "sz.399006"},
}
CHUNK = 10_000
_lock = threading.Lock()


def _get(headers: dict, timeout: float):
    import requests
    r = requests.get(URL, headers={**UA, **headers}, timeout=timeout)
    r.raise_for_status()
    return r


def _size() -> int:
    import requests
    r = requests.head(URL, headers=UA, timeout=30)
    r.raise_for_status()
    return int(r.headers["Content-Length"])


def _parts_dir():
    d = settings.data_dir() / "cache" / "qvix_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ranges(start: int, end: int, chunk: int, workers: int, timeout: float, retries: int = 4, cache: bool = False) -> bytes:
    """分段并行取 [start, end)。cache=True：每段落盘（文件只在末尾追加，已取到的整段下次直接用），失败的段下次接着补。"""
    parts = [(x, min(x + chunk, end) - 1) for x in range(start, end, chunk)]
    pdir = _parts_dir() if cache else None

    def one(rng):
        f = pdir / f"{rng[0]}-{rng[1]}" if pdir else None
        if f is not None and f.exists() and f.stat().st_size == rng[1] - rng[0] + 1:
            return f.read_bytes()
        for k in range(retries):
            try:
                got = _get({"Range": f"bytes={rng[0]}-{rng[1]}"}, timeout).content
                if len(got) == rng[1] - rng[0] + 1:
                    if f is not None:
                        f.write_bytes(got)
                    return got
            except Exception as e:  # noqa: BLE001
                log.info("qvix part %s try %d failed: %s", rng, k, e)
        return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = list(ex.map(one, parts))
    miss = sum(1 for g in got if g is None)
    if miss:
        raise RuntimeError(f"QVIX 下载未完成：{len(parts) - miss}/{len(parts)} 段已取到（已保存，下次接着补）")
    return b"".join(got)


def download_full(workers: int = 8, timeout: float = 180) -> bytes:
    """分段并行下载全量文件（站点按连接限速，单连接取 0.9 MB 要几个小时）。整段（CHUNK 字节）落盘缓存，最后不足一段的尾巴每次重取。"""
    n = _size()
    full_end = n - n % CHUNK
    head = _ranges(0, full_end, CHUNK, workers, timeout, cache=True) if full_end else b""
    tail = _ranges(full_end, n, 1500, 6, timeout) if n > full_end else b""           # 尾巴（每天在变）拆成小段并行，单连接太慢
    return head + tail


def download_tail(nbytes: int = 6000, timeout: float = 300) -> bytes:
    """只取文件末尾（最近几行，每天的增量用），同样分段并行。"""
    n = _size()
    return _ranges(max(0, n - nbytes), n, 1500, 4, timeout)


def parse(raw: bytes, partial_head: bool = False) -> dict[str, pd.DataFrame]:
    """CSV -> {symbol: DataFrame(date, open, high, low, close)}。partial_head=True 表示 raw 是从文件中间截取的（丢掉第一行残行、没有表头）。"""
    txt = raw.decode("gbk", errors="replace")
    lines = txt.splitlines()
    if partial_head:
        lines = lines[1:]
    elif lines and not lines[0][:4].isdigit():
        lines = lines[1:]                                  # 表头是列号
    rows = [ln.split(",") for ln in lines if ln[:4].isdigit()]
    out = {}
    for sym, spec in SERIES.items():
        recs = []
        for r in rows:
            if len(r) <= spec["cols"][-1]:
                continue
            try:
                d = datetime.strptime(r[0].strip(), "%Y/%m/%d").date().isoformat()
            except ValueError:
                continue
            vals = [pd.to_numeric(r[i], errors="coerce") for i in spec["cols"]]
            o, h, lo, c = vals
            if not (c == c) or c <= 0:                     # #NAME? / 0 / 空 = 当天没有这条序列
                continue
            o = o if o == o and o > 0 else c
            h = max(x for x in (h, o, c) if x == x and x > 0)
            lo = min(x for x in (lo, o, c) if x == x and x > 0)
            recs.append((d, float(o), float(h), float(lo), float(c)))
        out[sym] = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close"]).drop_duplicates("date", keep="last")
    return out


def store(conn, data: dict[str, pd.DataFrame]) -> int:
    n = 0
    for sym, df in data.items():
        conn.executemany("INSERT OR REPLACE INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,NULL,NULL)",
                         [(sym, r.date, r.open, r.high, r.low, r.close) for r in df.itertuples()])
        n += len(df)
    conn.commit()
    return n


def refresh(conn, full: bool | None = None) -> dict:
    """库里没有历史（或不足一年）就分段下全量；否则只取文件末尾。失败不抛出（上游是第三方网页），返回原因。"""
    with _lock:
        have = conn.execute("SELECT COUNT(*), MAX(date) FROM index_bar WHERE symbol='QVIX300'").fetchone()
        full = (have[0] or 0) < 250 if full is None else full
        try:
            data = parse(download_full()) if full else parse(download_tail(), partial_head=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "full": full, "error": f"{type(e).__name__}: {str(e)[:150]}"}
        n = store(conn, data)
        last = {s: (df["date"].max() if len(df) else None) for s, df in data.items()}
        db.set_meta(conn, "qvix_refreshed", json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "full": full, "last": last}, ensure_ascii=False))
        conn.commit()
        return {"ok": True, "full": full, "rows": n, "last": last}


def load(conn, sym: str) -> pd.Series:
    df = pd.read_sql_query("SELECT date, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(sym,))
    return df.set_index("date")["close"].astype(float) if len(df) else pd.Series(dtype=float)


_bg: dict = {"running": False, "last": None}


def refresh_async() -> bool:
    """后台刷新（第一次全量要几分钟）：不阻塞盘后任务链和页面。返回是否新开了一个后台任务。"""
    if _bg["running"]:
        return False
    _bg["running"] = True

    def run():
        try:
            with settings.market_ctx("CN"), db.market_db("CN") as c:
                _bg["last"] = refresh(c)
        except Exception as e:  # noqa: BLE001
            _bg["last"] = {"ok": False, "error": str(e)[:150]}
        finally:
            _bg["running"] = False
    threading.Thread(target=run, name="qvix", daemon=True).start()
    return True


# ---- 分析（与美股 VIX 面板同结构） -------------------------------------------------------------------

EVIDENCE = settings.ROOT / "server" / "qvix_evidence.json"
ZONE_NAMES = ["平静", "正常", "略紧张", "紧张", "恐慌", "极度恐慌"]


def evidence() -> dict:
    try:
        return json.loads(EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def zone_of(v: float, cuts: list[float]) -> int:
    for i, c in enumerate(cuts):
        if v < c:
            return i
    return len(cuts)


def _pct(x):
    return "—" if x is None else f"{x:+.1%}"


def analyze_one(sym: str, s: pd.Series, ev: dict) -> dict:
    spec = SERIES[sym]
    e = (ev.get("series") or {}).get(sym) or {}
    cuts = e.get("cuts")
    last, v = s.index[-1], float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) > 1 else v
    out = {"symbol": sym, "name": spec["name"], "asof": last, "value": round(v, 2), "chg_1d": round(v - prev, 2),
           "chg_1d_pct": round(v / prev - 1, 4) if prev else None, "chg_5d": round(v - float(s.iloc[-6]), 2) if len(s) > 6 else None,
           "pct_all": round(float((s < v).mean()), 3), "median": round(float(s.median()), 2), "since": s.index[0],
           "hi_1y": round(float(s.tail(250).max()), 2), "lo_1y": round(float(s.tail(250).min()), 2),
           "history": [{"date": d, "v": round(float(x), 2)} for d, x in s.tail(260).items()]}
    if not cuts:
        return out
    zi = zone_of(v, cuts)
    bounds = [0, *cuts, None]
    out["zone"], out["zone_name"] = zi, ZONE_NAMES[zi]
    out["zones"] = [{"lo": bounds[i], "hi": bounds[i + 1], "name": ZONE_NAMES[i]} for i in range(len(ZONE_NAMES))]
    z = (e.get("zones") or [None] * 6)[zi]
    un = e.get("unconditional") or {}
    idx = spec["name"] + "指数" if sym == "QVIXCYB" else "沪深300"
    if z and z["fwd20"]["n"]:
        a, b = z["fwd20"], z["fwd60"]
        out["evidence_line"] = (f"{e.get('from')} 以来，{spec['name']}恐慌指数处在「{ZONE_NAMES[zi]}」区之后，{idx}接下来 20 个交易日平均 {_pct(a['mean'])}、上涨概率 {a['win']:.0%}；"
                                f"60 个交易日平均 {_pct(b['mean'])}、上涨概率 {(b['win'] or 0):.0%}"
                                + (f"，最差一次 {_pct(b['worst'])}" if b.get("worst") is not None else "")
                                + f"（进入过 {z['episodes']} 次；所有日子：60 日平均 {_pct((un.get('fwd60') or {}).get('mean'))}）。样本只有几年，参考价值有限。")
    out["evidence_all"], out["unconditional"] = e.get("zones"), un
    out["read"] = ["平静：期权市场预期的波动很小，情绪放松。不是卖出信号；只是遇到突发消息时波动会放大得比较快。",
                   "正常：没有明显的恐慌或自满，对操作没有额外提示，按指数的均线规则执行。",
                   "略紧张：预期波动在上升，留意，但还谈不上恐慌。",
                   "紧张：市场不安、波动大。先别把它当成抄底信号，按均线规则操作。",
                   "恐慌：预期波动处在历史高位（约前 10%）。美股的长历史显示恐慌之后平均表现好于平时，但 A 股这条序列只有几年，样本太少；想参与只用小钱分批。",
                   "极度恐慌：历史上最高的几个百分点。通常伴随急跌，波动极大；不要一次买满，也不要恐慌割肉。"][zi]
    return out


def build() -> dict:
    with settings.market_ctx("CN"), db.market_db("CN") as c:
        ser = {s: load(c, s) for s in SERIES}
        meta = db.get_meta(c, "qvix_refreshed")
    if all(len(s) < 60 for s in ser.values()):
        started = refresh_async()
        return {"status": "loading", "message": "第一次使用：正在从期权论坛下载 A 股恐慌指数（QVIX）的历史，约需几分钟，之后每天只取最新几天。"
                + ("" if started else "（下载进行中）"), "bg": _bg.get("last")}
    ev = evidence()
    info = json.loads(meta) if meta else {}
    stale = bool(info.get("at")) and (datetime.now() - datetime.fromisoformat(info["at"])).total_seconds() > 12 * 3600
    if stale or not info:
        refresh_async()                                    # 超过半天没刷新：后台取最新几行，下次打开就是新的
    return {"status": "ok", "series": [analyze_one(s, v, ev) for s, v in ser.items() if len(v) >= 60],
            "source": "期权论坛 QVIX（按 Cboe VIX 方法，由 ETF 期权价格计算）", "refreshed_at": info.get("at"), "stale": stale,
            "note": "A 股恐慌指数 = 期权市场预期的未来 30 天年化波动率（%），方法与美股 VIX 相同（官方的中国波指 iVIX 2018 年已停发）。"
                    "分区按每条序列自己的历史分位划分（与美股 VIX 的 15 / 20 / 25 / 30 / 40 在 VIX 长历史里的分位相同），不照搬美股的数值。"
                    "数据来自第三方网站，可能延迟或中断；只作参考，不进入选股与回测。"}


if __name__ == "__main__":                                 # python -m server.qvix  ：手动刷新（全量）
    import sys
    with settings.market_ctx("CN"), db.market_db("CN") as c:
        print(refresh(c, full="--tail" not in sys.argv))
    _ = date
