"""港股个股行情库（港股第 3 步）：名单（按成交额取前 N 只）+ 10 年日线入库 + 每日增量 + K 线数据 + 每手股数。

数据：
  * 名单：东方财富港股行情列表（push2 clist，主板 + 创业板，按成交额从大到小，每页 100 行）。港股有近 3000 只证券，大量是几乎没人交易的，
    所以只取成交额最高的前 N 只（默认 800）作为「港股股票池」；名单每周刷新一次，掉出前 N 名的标记为不在池（日线保留）。
  * 日线：雅虎财经 yfinance 批量下载（代码 0700.HK，不复权 + 复权价；一批 40 只，几秒钟）。与东财的收盘价逐只核对过（腾讯 419.80 一致）。
  * 每手股数：东方财富个股详情的 f602（腾讯 100、汇丰 400、小米 200、中国平安 500，抽查 8 只与已知一致）。这个接口一次只能查一只，
    而且慢（实测每只约 6 秒），所以只在需要时（看某只股票时）查一次并缓存，不对整个名单批量查。
存储：hk.db 的 securities / daily_bar / fetch_state / meta（与 A 股、美股库同一套表）。symbol = hk.00700（5 位代码）。
这一步只有「行情库 + K 线」：选股扫描、持仓、回测还没做（港股 T+0、无涨跌停、每手股数因股票而异、双边印花税，参数要单独校准）。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta

import pandas as pd

from . import db, pinyin_search, settings, throttle

log = logging.getLogger(__name__)
HOSTS = ("82.push2.eastmoney.com", "push2.eastmoney.com")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Referer": "https://quote.eastmoney.com/"}
UNIVERSE_TOP = 800
PAGE = 100
BATCH = 40
UNIVERSE_DAYS = 7                       # 名单多久刷新一次
META_UNIVERSE, META_LOTS, META_STATE = "hk_universe", "hk_lots", "hk_stocks_state"
_lock = threading.Lock()
_job: dict = {"running": False, "kind": None, "total": 0, "done": 0, "message": "", "error": None, "started": None, "finished": None, "stop": False, "errors": []}


# ---- 代码换算 ------------------------------------------------------------------------------------

def sym_of(code: str) -> str:
    return "hk." + str(code).zfill(5)


def code_of(symbol: str) -> str:
    return symbol.split(".")[-1]


def yahoo_ticker(symbol: str) -> str:
    """hk.00700 -> 0700.HK；hk.09988 -> 9988.HK；hk.80737 -> 80737.HK（雅虎对 5 位以上的代码不补零）。"""
    n = int(code_of(symbol))
    return f"{n:04d}.HK"


# ---- 名单 ----------------------------------------------------------------------------------------

def _get_json(path: str, params: dict, timeout: float = 40) -> dict:
    import requests
    last = None
    for host in HOSTS:
        for _ in range(2):
            try:
                r = requests.get(f"https://{host}{path}", params=params, headers=UA, timeout=timeout)
                r.raise_for_status()
                return r.json().get("data") or {}
            except Exception as e:  # noqa: BLE001 - 第三方网页：换节点 / 重试
                last = e
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:100]}")


def fetch_list(top: int = UNIVERSE_TOP, stop=None, progress=None) -> list[dict]:
    """东财港股列表：按成交额从大到小取前 top 只。中途某页失败就用已取到的（返回的名单会短一些，调用方可以看长度）。"""
    out, pages = [], (top + PAGE - 1) // PAGE
    for p in range(1, pages + 1):
        if stop and stop():
            break
        try:
            d = _get_json("/api/qt/clist/get", {"pn": p, "pz": PAGE, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f6", "fs": "m:128+t:3,m:128+t:4",
                                                  "fields": "f2,f3,f5,f6,f12,f14,f21"})
        except RuntimeError as e:
            log.info("hk list page %d failed: %s", p, e)
            break
        rows = d.get("diff") or []
        for r in rows:
            try:
                price, amount = float(r["f2"]), float(r["f6"])
            except (TypeError, ValueError):                      # 停牌的股票价格 / 成交额是 "-"
                continue
            if price > 0 and amount > 0:
                out.append({"symbol": sym_of(r["f12"]), "name": r["f14"], "price": price, "pct": r.get("f3") if isinstance(r.get("f3"), (int, float)) else None,
                            "volume": r.get("f5"), "amount": amount, "mcap": r.get("f21") if isinstance(r.get("f21"), (int, float)) else None})
        if progress:
            progress(p, pages)
        if len(rows) < PAGE:
            break
    return out[:top]


def refresh_universe(conn, top: int = UNIVERSE_TOP, stop=None, progress=None) -> dict:
    items = fetch_list(top, stop, progress)
    if not items:
        raise RuntimeError("没有取到港股名单（网络或数据源暂时不可用）")
    today = date.today().isoformat()
    conn.execute("UPDATE securities SET in_l1=0 WHERE symbol LIKE 'hk.%'")
    conn.executemany("INSERT INTO securities(symbol,name,board,status,in_l1,l1_asof,sec_type) VALUES(?,?,?,?,1,?,'stock') "
                     "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, in_l1=1, l1_asof=excluded.l1_asof, status='active'",
                     [(i["symbol"], i["name"], "hk", "active", today) for i in items])
    db.set_meta(conn, META_UNIVERSE, json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "top": top, "n": len(items),
                                                 "items": [{k: i[k] for k in ("symbol", "name", "price", "pct", "amount", "mcap")} for i in items]}, ensure_ascii=False))
    conn.commit()
    return {"n": len(items), "partial": len(items) < min(top, 500)}


def universe_meta(conn) -> dict:
    try:
        return json.loads(db.get_meta(conn, META_UNIVERSE) or "{}")
    except ValueError:
        return {}


# ---- 日线 ----------------------------------------------------------------------------------------

def _download(tickers: list[str], start: str | None, period: str | None):
    """yfinance 批量下载（一个测试里可以换成假的）。返回 group_by=ticker 的 DataFrame。"""
    import yfinance as yf
    th = throttle.get("yfinance")

    def once():
        kw = {"start": start} if start else {"period": period or "10y"}
        df = yf.download(tickers, interval="1d", auto_adjust=False, group_by="ticker", progress=False, threads=True, **kw)
        if df is None or df.empty:
            raise throttle.RateLimited("返回空数据")
        return df
    return th.call(once, retries=2)


def _frame_for(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    d = df[ticker] if isinstance(df.columns, pd.MultiIndex) else df
    d = d.dropna(subset=["Close"])
    return d[d["Close"] > 0]


def store_bars(conn, symbol: str, d: pd.DataFrame) -> int:
    if d.empty:
        return 0
    close, vol = d["Close"].astype(float), d["Volume"].fillna(0).astype(float)
    adj = d["Adj Close"].astype(float) if "Adj Close" in d else close
    rows = [(symbol, ix.strftime("%Y-%m-%d"), float(o), float(h), float(l), float(c), float(v), float(c * v), float(a / c) if c else 1.0, float(a), 1 if v > 0 else 0, "yfinance")
            for ix, o, h, l, c, v, a in zip(d.index, d["Open"], d["High"], d["Low"], close, vol, adj)]
    conn.executemany("INSERT OR REPLACE INTO daily_bar(symbol,date,open,high,low,close,volume,amount,adj_factor,adj_close,trade_status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def download_bars(conn, symbols: list[str], start: str | None = None, period: str | None = "10y", stop=None, progress=None) -> dict:
    """批量下载并入库；一批失败不影响其他批。返回 {ok, empty, failed: [symbol]}。"""
    ok = empty = 0
    failed: list[str] = []
    for i in range(0, len(symbols), BATCH):
        if stop and stop():
            break
        chunk = symbols[i:i + BATCH]
        by_t = {yahoo_ticker(s): s for s in chunk}
        try:
            df = _download(list(by_t), start, period)
        except Exception as e:  # noqa: BLE001
            log.info("hk bars batch failed: %s", e)
            failed += chunk
            if progress:
                progress(min(i + BATCH, len(symbols)), len(symbols))
            continue
        for t, s in by_t.items():
            try:
                n = store_bars(conn, s, _frame_for(df, t))
            except KeyError:
                n = 0
            if n:
                ok += 1
                last = conn.execute("SELECT MAX(date) FROM daily_bar WHERE symbol=?", (s,)).fetchone()[0]
                conn.execute("INSERT OR REPLACE INTO fetch_state(symbol,task,last_ok_date,status,fail_count,updated_at) VALUES(?,?,?,?,0,?)",
                             (s, "bars_hk", last, "ok", datetime.now().isoformat(timespec="seconds")))
            else:
                empty += 1
        conn.commit()
        if progress:
            progress(min(i + BATCH, len(symbols)), len(symbols))
    return {"ok": ok, "empty": empty, "failed": failed}


def _symbols(conn, only_missing: bool = False) -> list[str]:
    q = "SELECT s.symbol FROM securities s WHERE s.in_l1=1 AND s.symbol LIKE 'hk.%'"
    if only_missing:
        q += " AND NOT EXISTS (SELECT 1 FROM daily_bar b WHERE b.symbol=s.symbol)"
    return [r[0] for r in conn.execute(q + " ORDER BY s.symbol")]


def library_ready(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM daily_bar WHERE symbol LIKE 'hk.%' LIMIT 1").fetchone())


def update_daily(conn, stop=None, progress=None) -> dict:
    """已有行情库的每日增量：每只取最近 20 天（顺带修正被改过的几天）。名单超过 7 天没刷新就先刷新。"""
    meta = universe_meta(conn)
    if not meta.get("at") or (datetime.now() - datetime.fromisoformat(meta["at"])).days >= UNIVERSE_DAYS:
        try:
            refresh_universe(conn, stop=stop)
        except RuntimeError as e:
            log.info("hk universe refresh skipped: %s", e)                 # 刷新失败：继续用旧名单
    syms = _symbols(conn)
    missing = _symbols(conn, only_missing=True)                             # 新进名单的：补 10 年
    got = download_bars(conn, [s for s in syms if s not in set(missing)], start=(date.today() - timedelta(days=20)).isoformat(), period=None, stop=stop, progress=progress)
    if missing and not (stop and stop()):
        extra = download_bars(conn, missing, period="10y", stop=stop)
        got = {"ok": got["ok"] + extra["ok"], "empty": got["empty"] + extra["empty"], "failed": got["failed"] + extra["failed"]}
    return got


# ---- 后台任务（初始化 / 更新）-----------------------------------------------------------------------

def job_state() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _job.items()}


def _set(**kw):
    _job.update(kw)


def start_job(kind: str) -> dict:
    """kind = init（名单 + 10 年日线，可中断续跑）| update（每日增量）。已有任务在跑就不再开。"""
    if kind not in ("init", "update"):
        raise ValueError("kind 须为 init / update")
    with _lock:
        if _job["running"]:
            return {"ok": False, "message": "港股个股行情库正在更新中。", "job": job_state()}
        _job.update({"running": True, "kind": kind, "total": 0, "done": 0, "message": "准备中…", "error": None, "started": datetime.now().isoformat(timespec="seconds"),
                     "finished": None, "stop": False, "errors": []})

    def run():
        res = {}
        try:
            with settings.market_ctx("HK"), db.market_db("HK") as c:
                stop = lambda: _job["stop"]  # noqa: E731
                if kind == "init":
                    meta = universe_meta(c)
                    if not meta.get("at") or not _symbols(c):
                        _set(message="取港股名单（按成交额取前 %d 只，约 1~2 分钟）…" % UNIVERSE_TOP)
                        res["universe"] = refresh_universe(c, stop=stop, progress=lambda d, t: _set(message=f"取港股名单 {d}/{t} 页…"))
                    todo = _symbols(c, only_missing=True)
                    _set(total=len(todo), done=0, message=f"下载 10 年日线：{len(todo)} 只（可以随时中断，下次接着下）…")
                    res["bars"] = download_bars(c, todo, period="10y", stop=stop, progress=lambda d, t: _set(done=d, total=t, message=f"下载 10 年日线 {d}/{t} 只…"))
                else:
                    n = len(_symbols(c))
                    _set(total=n, done=0, message=f"更新日线：{n} 只…")
                    res["bars"] = update_daily(c, stop=stop, progress=lambda d, t: _set(done=d, total=t, message=f"更新日线 {d}/{t} 只…"))
                failed = res.get("bars", {}).get("failed", [])
                state = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "ok": not failed and not _job["stop"], "stopped": _job["stop"],
                         "failed": len(failed), "result": {k: (v if not isinstance(v, dict) else {a: b for a, b in v.items() if a != "failed"}) for k, v in res.items()}}
                db.set_meta(c, META_STATE, json.dumps(state, ensure_ascii=False))
                c.commit()
                _set(message="已停止（下次接着做）" if _job["stop"] else "完成" if not failed else f"完成，但有 {len(failed)} 只下载失败（再点一次会重试这些）", errors=failed[:20])
        except Exception as e:  # noqa: BLE001 - 后台任务不能把进程带崩
            log.exception("hk stocks job failed")
            _set(error=f"{type(e).__name__}: {str(e)[:200]}", message="失败")
        finally:
            _set(running=False, finished=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=run, name=f"hk-stocks-{kind}", daemon=True).start()
    return {"ok": True, "message": "已开始。", "job": job_state()}


def stop_job() -> dict:
    if _job["running"]:
        _set(stop=True, message="正在停止…")
    return job_state()


def status(conn=None) -> dict:
    own = conn is None
    if own:
        conn = db.market_conn("HK")
    try:
        meta = universe_meta(conn)
        n_pool = conn.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1 AND symbol LIKE 'hk.%'").fetchone()[0]
        n_bars = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bar WHERE symbol LIKE 'hk.%'").fetchone()[0]
        last = conn.execute("SELECT MAX(date) FROM daily_bar WHERE symbol LIKE 'hk.%'").fetchone()[0]
        try:
            state = json.loads(db.get_meta(conn, META_STATE) or "{}")
        except ValueError:
            state = {}
        return {"pool": n_pool, "with_bars": n_bars, "bars_asof": last, "universe_at": meta.get("at"), "universe_top": meta.get("top"), "last_run": state,
                "ready": n_bars > 0, "job": job_state(), "lots_cached": len(_lots(conn))}
    finally:
        if own:
            conn.close()


# ---- 每手股数（按需查、缓存）--------------------------------------------------------------------------

def _lots(conn) -> dict:
    try:
        return json.loads(db.get_meta(conn, META_LOTS) or "{}")
    except ValueError:
        return {}


def _fetch_lot(code5: str) -> int | None:
    d = _get_json("/api/qt/stock/get", {"secid": "116." + code5, "fields": "f57,f602"}, timeout=40)
    v = d.get("f602")
    return int(v) if isinstance(v, (int, float, str)) and str(v).isdigit() and int(v) > 0 else None


def lot_size(conn, symbol: str) -> dict:
    """每手股数：先看缓存，没有就联网查一次（慢，约几秒）并存下来。查不到返回 lot=None（不猜）。"""
    cache = _lots(conn)
    if symbol in cache:
        return {"symbol": symbol, "lot": cache[symbol], "cached": True}
    try:
        lot = _fetch_lot(code_of(symbol))
    except Exception as e:  # noqa: BLE001
        return {"symbol": symbol, "lot": None, "error": str(e)[:120]}
    if lot:
        cache[symbol] = lot
        db.set_meta(conn, META_LOTS, json.dumps(cache))
        conn.commit()
    return {"symbol": symbol, "lot": lot, "cached": False}


# ---- 搜索与 K 线 ---------------------------------------------------------------------------------

def hot(conn, n: int = 30) -> list[dict]:
    """没输入搜索词时显示：名单里成交额最高的几只（名单取回来时的快照）。"""
    return [{"symbol": i["symbol"], "name": i["name"], "price": i.get("price"), "pct": i.get("pct"), "amount": i.get("amount")} for i in universe_meta(conn).get("items", [])[:n]]


def search(conn, q: str, limit: int = 20) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return hot(conn, limit)
    snap = {i["symbol"]: i for i in universe_meta(conn).get("items", [])}
    out = []
    for r in pinyin_search.search(conn, "HK", q, limit):
        s = snap.get(r["symbol"]) or {}
        out.append({"symbol": r["symbol"], "name": r["name"], "price": s.get("price"), "pct": s.get("pct"), "amount": s.get("amount")})
    return out


def _r(v, d=2):
    return None if v is None or v != v else round(float(v), d)


def kline(conn, symbol: str, days: int = 700) -> dict:
    """K 线（前复权：用复权价按比例缩放开高低收，最新一天与真实收盘价一致，均线也基于复权价）+ 5/10/20/50/200 日均线 + 摘要。"""
    df = pd.read_sql_query("SELECT date, open, high, low, close, volume, amount, adj_close FROM daily_bar WHERE symbol=? AND trade_status=1 ORDER BY date", conn, params=(symbol,))
    if df.empty:
        return {"status": "no_data", "message": "这只股票还没有日线（名单里没有，或行情库还没初始化）。", "symbol": symbol}
    row = conn.execute("SELECT name FROM securities WHERE symbol=?", (symbol,)).fetchone()
    ratio = (df["adj_close"] / df["close"]).fillna(1.0)
    f = ratio / ratio.iloc[-1]                                      # 最新一天 = 1：价格是前复权、最新价不变
    for col in ("open", "high", "low", "close"):
        df[col + "_a"] = df[col] * f
    ca = df["close_a"]
    last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2]) if len(df) > 1 else float(df["close"].iloc[-1])
    ma = {f"ma{n}": ca.rolling(n).mean() for n in (5, 10, 20, 50, 200)}
    tail = df.tail(days)
    idx = tail.index
    bars = [{"time": r.date, "open": _r(r.open_a, 3), "high": _r(r.high_a, 3), "low": _r(r.low_a, 3), "close": _r(r.close_a, 3), "volume": float(r.volume)} for r in tail.itertuples()]
    ma_out = {k: [{"time": df["date"].iloc[i], "value": _r(s.iloc[i], 3)} for i in idx if s.iloc[i] == s.iloc[i]] for k, s in ma.items()}
    hi = float(df["high_a"].tail(250).max()) if "high_a" in df else None
    amt20 = float(df["amount"].tail(20).mean())
    return {"status": "ok", "symbol": symbol, "name": row["name"] if row else symbol, "asof": df["date"].iloc[-1], "since": df["date"].iloc[0], "bars": bars, "ma": ma_out,
            "last": {"close": _r(last, 3), "chg_1d": _r(last / prev - 1, 4), "amount": _r(float(df["amount"].iloc[-1]), 0), "amount_20d": _r(amt20, 0),
                     "hi_52w": _r(hi, 3), "dd_52w": _r(float(ca.iloc[-1]) / hi - 1, 4) if hi else None, "ret_20d": _r(float(ca.iloc[-1] / ca.iloc[-21] - 1), 4) if len(ca) > 21 else None},
            "adjusted": "前复权（用复权价按比例缩放，最新一天与真实收盘价一致）"}
