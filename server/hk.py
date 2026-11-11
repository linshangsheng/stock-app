"""港股（独立的第三个市场，第 2 步）：恒生指数 / 国企指数 / 恒生科技 的指数日线、均线位置，和第 1 步的指数估值放在一起刷新。

数据：
  * 指数日线：东方财富的指数 K 线接口（push2his，secid 100.HSI / 100.HSCEI / 124.HSTECH，日线、不复权，2015 年起）。
    雅虎对恒生科技（HSTECH.HK）只给 1 条数据，所以三个指数都用东财，保持一致。恒生科技 2020-07 之前是指数公司回溯的数据，
    部分日子开高低收相同且成交量为 0（没有真实成交），入库时丢掉「最高 = 最低且成交量为 0」的日子。
  * 备用：腾讯的港股指数日线（web.ifzq.gtimg.cn，hkHSI / hkHSCEI / hkHSTECH）。东财会按 IP 限流——部署到云服务器、大量初始化数据之后，
    这台服务器的所有东财 push2 / push2his 请求被直接断开（RemoteDisconnected），港股指数一行都取不到。每个指数都先试东财，失败就自动换腾讯（东财被限流时是立刻被断开，每个只多花一两秒；偶然一次网络抖动只影响那一个指数）。
    腾讯一次最多给最近 2000 个交易日（恒指约回到 2018 年、恒生科技是全部），足够算 250 日均线和画图；它给的是成交额、没有成交量，成交量记 0。
  * 估值：蛋卷基金（server/valuation.py，market=HK）。
存储：hk.db 的 index_bar（symbol = HK.HSI / HK.HSCEI / HK.HSTECH）与 meta。
这里只做「指数」：港股个股的行情库、选股、持仓、回测还没做（要单独校准每手股数 / T+0 / 无涨跌停 / 印花税）。
指数均线位置只是描述现状；A 股 / 美股指数的均线结论没有拿到港股上验证过，这里不给买卖信号。"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import db, settings, valuation

log = logging.getLogger(__name__)
HKT = ZoneInfo("Asia/Hong_Kong")
HOSTS = ("push2his.eastmoney.com", "82.push2his.eastmoney.com")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Referer": "https://quote.eastmoney.com/"}
SECIDS = {"HK.HSI": "100.HSI", "HK.HSCEI": "100.HSCEI", "HK.HSTECH": "124.HSTECH"}
TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
TX_CODES = {"HK.HSI": "hkHSI", "HK.HSCEI": "hkHSCEI", "HK.HSTECH": "hkHSTECH"}
TX_MAX = 2000                          # 腾讯一次最多给最近 2000 个交易日
MAS = (20, 60, 120, 200, 250)
MA_NAMES = {20: "月线", 60: "季线", 120: "半年线", 200: "200 日线", 250: "年线"}
META = "hk_refreshed"
CLOSE_HHMM = (16, 45)                  # 港股 16:00 收盘（含收市竞价到 16:10），16:45 之后自动更新
_lock = threading.Lock()
_bg: dict = {"running": False, "last": None}


def _get(url_params: dict, timeout: float = 25) -> dict:
    import requests
    last = None
    for host in HOSTS:
        try:
            r = requests.get(f"https://{host}/api/qt/stock/kline/get", params=url_params, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r.json().get("data") or {}
        except Exception as e:  # noqa: BLE001 - 第三方网页：换一个节点再试
            last = e
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:100]}")


def fetch_bars(secid: str, beg: str) -> pd.DataFrame:
    """东财日线 -> DataFrame(date, open, high, low, close, volume, amount)。beg = 'YYYYMMDD'。"""
    d = _get({"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0", "beg": beg, "end": "20500101"})
    rows = []
    for k in d.get("klines") or []:
        p = k.split(",")                                       # 日期, 开, 收, 高, 低, 成交量, 成交额
        if len(p) < 7:
            continue
        rows.append((p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5]), float(p[6])))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    if df.empty:
        return df
    flat = (df["high"] == df["low"]) & (df["volume"] == 0)       # 回溯数据：没有真实成交的日子
    return df[~flat & (df["close"] > 0)].drop_duplicates("date", keep="last").reset_index(drop=True)


def fetch_bars_tencent(code: str, beg: str) -> pd.DataFrame:
    """腾讯日线（备用源）-> 和 fetch_bars 一样的 DataFrame。腾讯每行 = 日期, 开, 收, 高, 低, 成交额, …（没有成交量，记 0）。beg = 'YYYYMMDD'。
    按「最近 N 个交易日」取：N = 距 beg 的天数（日历天 >= 交易日，够用），最多 2000。没有数据当作失败抛出（备用源取不到就该让人知道）。"""
    import requests
    need = min(TX_MAX, max(30, (date.today() - datetime.strptime(beg, "%Y%m%d").date()).days + 5))
    r = requests.get(TX_URL, params={"param": f"{code},day,,,{need},qfq"}, headers={"User-Agent": UA["User-Agent"]}, timeout=25)
    r.raise_for_status()
    data = r.json().get("data")
    x = data.get(code) if isinstance(data, dict) else None
    ks = (x.get("day") or x.get("qfqday") or []) if isinstance(x, dict) else []
    rows = []
    for k in ks:
        if len(k) < 6:
            continue
        rows.append((k[0], float(k[1]), float(k[3]), float(k[4]), float(k[2]), 0.0, float(k[5])))
    if not rows:
        raise RuntimeError(f"腾讯没有返回 {code} 的日线")
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    flat = (df["high"] == df["low"]) & (df["amount"] == 0)               # 没有真实成交的回溯日
    return df[~flat & (df["close"] > 0)].drop_duplicates("date", keep="last").reset_index(drop=True)


def store(conn, symbol: str, df: pd.DataFrame) -> int:
    conn.executemany("INSERT OR REPLACE INTO index_bar(symbol,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
                     [(symbol, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount) for r in df.itertuples()])
    return len(df)


def refresh_indices(conn) -> dict:
    """三个指数的日线：库里没有就取 2015 年起的全量，有就从最后一天往前 15 天补起（顺带修正被改过的几天）。失败不抛出。
    每个指数先用东财；东财出错（被限流 / 连不上）就换腾讯（空数据不算出错，不换）。返回里的 sources 说明每个指数这次用的哪个源。"""
    out, errors, sources = {}, [], {}
    for sym, secid in SECIDS.items():
        last = conn.execute("SELECT MAX(date) FROM index_bar WHERE symbol=?", (sym,)).fetchone()[0]
        beg = (date.fromisoformat(last) - timedelta(days=15)).strftime("%Y%m%d") if last else "20150101"
        df, em_err = None, None
        try:
            df, sources[sym] = fetch_bars(secid, beg), "东方财富"
        except Exception as e:  # noqa: BLE001
            em_err = f"{type(e).__name__}: {str(e)[:90]}"
            log.warning("hk index %s: eastmoney failed (%s), trying tencent", sym, em_err)
        if df is None:
            try:
                df, sources[sym] = fetch_bars_tencent(TX_CODES[sym], beg), "腾讯（备用）"
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym}：东方财富 {em_err}；腾讯 {type(e).__name__}: {str(e)[:90]}")
                continue
        out[sym] = store(conn, sym, df)
    conn.commit()
    return {"rows": out, "errors": errors, "sources": sources}


def today_hkt() -> str:
    return datetime.now(HKT).date().isoformat()


def refresh_all(conn, source: str = "manual") -> dict:
    """指数日线 + 估值，一起刷新；写下刷新记录（自动更新靠它判断「今天做过没有」）。"""
    with _lock:
        idx = refresh_indices(conn)
        val = valuation.refresh(conn, "HK")
        errors = [*idx["errors"], *val["errors"]]
        stocks = None
        if source == "auto":                                           # 自动更新：行情库已经初始化的话，个股日线也一起增量更新（手动的「立即更新」只更新指数和估值，个股在自己的按钮里）
            from . import hk_stocks
            if hk_stocks.library_ready(conn):
                try:
                    stocks = hk_stocks.update_daily(conn)
                    if stocks["failed"]:
                        errors.append(f"个股日线有 {len(stocks['failed'])} 只下载失败")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"个股日线：{type(e).__name__}: {str(e)[:100]}")
        ok = not errors and val["ok"]
        info = {"at": datetime.now().isoformat(timespec="seconds"), "day": today_hkt(), "ok": ok, "source": source,
                "errors": errors, "rows": idx["rows"], "index_sources": idx["sources"], "stocks": ({k: v for k, v in stocks.items() if k != "failed"} if stocks else None)}
        db.set_meta(conn, META, json.dumps(info, ensure_ascii=False))
        conn.commit()
        if stocks is not None:                                         # 个股日线更新完，接着出今天的观察清单（放在提交之后：扫描自己另开连接写库）
            from . import hk_scan
            try:
                scan = hk_scan.run()
                scan_err = scan["message"] if scan.get("error") else None
            except Exception as e:  # noqa: BLE001 - 扫描出错不能让更新记录丢掉
                log.exception("hk scan failed")
                scan_err = f"{type(e).__name__}: {str(e)[:100]}"
            if scan_err:
                info["errors"].append(f"港股选股：{scan_err}")
                info["ok"] = False                                     # 没出清单：算今天还没做完，3 小时后自动重试
                db.set_meta(conn, META, json.dumps(info, ensure_ascii=False))
                conn.commit()
        return info


def refresh_async(source: str = "page") -> bool:
    if _bg["running"]:
        return False
    _bg["running"] = True

    def run():
        try:
            with settings.market_ctx("HK"), db.market_db("HK") as c:
                _bg["last"] = refresh_all(c, source)
        except Exception as e:  # noqa: BLE001
            _bg["last"] = {"ok": False, "errors": [str(e)[:150]]}
        finally:
            _bg["running"] = False
    threading.Thread(target=run, name="hk-refresh", daemon=True).start()
    return True


def last_refresh(conn) -> dict:
    try:
        return json.loads(db.get_meta(conn, META) or "{}")
    except ValueError:
        return {}


def auto_due(conn, now: datetime | None = None) -> bool:
    """自动更新要不要现在做：工作日、港股收盘后（16:45 起）、今天还没成功更新过；失败的话 3 小时后再试。
    港股的公共假期没有内置日历：假期那天取回来没有新的一天，也算做过，不会反复取。"""
    n = (now or datetime.now(HKT)).astimezone(HKT)
    if n.weekday() >= 5 or (n.hour, n.minute) < CLOSE_HHMM:
        return False
    info = last_refresh(conn)
    if info.get("day") == n.date().isoformat() and info.get("ok"):
        return False
    if info.get("at") and info.get("day") == n.date().isoformat():
        return (datetime.now() - datetime.fromisoformat(info["at"])).total_seconds() > 3 * 3600
    return True


# ---- 分析：价格与均线位置 --------------------------------------------------------------------------

def load(conn, sym: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT date, open, high, low, close FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(sym,))


def _r(v, d=2):
    return None if v is None or v != v else round(float(v), d)


def trend_label(above: list[bool]) -> tuple[str, str]:
    """(短标签, 说明)：按「站上几条均线（20 / 60 / 120 / 200 日）」描述现状，不是买卖信号。"""
    n = sum(above)
    return {4: ("强势", "站上 20 / 60 / 120 / 200 日全部均线"), 3: ("偏强", "站上 4 条均线里的 3 条"), 2: ("震荡", "站上 4 条均线里的 2 条"),
            1: ("偏弱", "只站上 4 条均线里的 1 条"), 0: ("弱势", "跌破 20 / 60 / 120 / 200 日全部均线")}[n]


def analyze(sym: str, name: str, df: pd.DataFrame) -> dict | None:
    if len(df) < 30:
        return None
    c = df["close"].astype(float)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    mas = {}
    for n in MAS:
        if len(c) >= n:
            m = float(c.tail(n).mean())
            mas[n] = {"n": n, "name": MA_NAMES[n], "value": _r(m), "above": last >= m, "dist": _r(last / m - 1, 4)}
    four = [mas[n]["above"] for n in (20, 60, 120, 200) if n in mas]
    tag, tag_text = trend_label(four) if len(four) == 4 else ("数据不足", "历史不足 200 日，均线不全")
    hi = float(df["high"].tail(250).max())
    ret = lambda k: _r(last / float(c.iloc[-1 - k]) - 1, 4) if len(c) > k else None  # noqa: E731
    ma_s = {n: c.rolling(n).mean() for n in (20, 60, 200)}
    tail = df.tail(300).reset_index(drop=True)
    off = len(df) - len(tail)
    hist = [{"date": r.date, "close": _r(r.close), **{f"ma{n}": _r(ma_s[n].iloc[off + i]) for n in (20, 60, 200)}} for i, r in enumerate(tail.itertuples())]
    return {"symbol": sym, "name": name, "asof": df["date"].iloc[-1], "close": _r(last), "chg_1d": _r(last / prev - 1, 4), "ret_20d": ret(20), "ret_60d": ret(60), "ret_250d": ret(250),
            "dd_52w": _r(last / hi - 1, 4), "hi_52w": _r(hi), "mas": list(mas.values()), "trend": tag, "trend_text": tag_text, "since": df["date"].iloc[0], "history": hist}


def build() -> dict:
    """/api/hk/overview：三个指数的价格 / 均线位置（+ 估值摘要）。第一次没有数据时在后台取，页面会再问。"""
    with settings.market_ctx("HK"), db.market_db("HK") as c:
        series = {sym: load(c, sym) for sym in SECIDS}
        info = last_refresh(c)
        val = valuation.view_items(c, "HK")
    names = {s: v["name"] for s, v in valuation.HK_SPEC.items()}
    items = [x for x in (analyze(s, names[s], df) for s, df in series.items()) if x]
    if not items:
        started = refresh_async("page")
        return {"status": "loading", "message": "第一次使用：正在联网取港股指数的日线（几秒钟），稍后刷新。" + ("" if started else "（取数进行中）"), "bg": _bg.get("last")}
    asof = max(x["asof"] for x in items)
    stale = (not info.get("at")) or (datetime.now() - datetime.fromisoformat(info["at"])).total_seconds() > 6 * 3600
    if stale and today_hkt() > asof:                         # 最新一根日线不是今天、而且超过 6 小时没刷新：后台补一下
        refresh_async("page")
    for x in items:
        v = val["items"].get(x["symbol"])
        x["etf"] = valuation.HK_SPEC[x["symbol"]]["etf"]
        x["valuation"] = {k: v[k] for k in ("pe", "gap", "pct", "in_zone", "batches_now", "batches_total", "window_text")} if v else None
    return {"status": "ok", "asof": asof, "indices": items, "refreshed_at": info.get("at"), "stale": stale, "errors": info.get("errors") or [],
            "source": "东方财富（指数日线，不复权）", "note": NOTE}


def status() -> dict:
    """设置 → 数据更新（港股）：每个指数最后一天、条数，估值的更新时间，最近一次刷新的结果。"""
    with settings.market_ctx("HK"), db.market_db("HK") as c:
        info = last_refresh(c)
        idx = []
        for sym in SECIDS:
            r = c.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM index_bar WHERE symbol=?", (sym,)).fetchone()
            idx.append({"symbol": sym, "name": valuation.HK_SPEC[sym]["name"], "rows": r[0], "from": r[1], "last": r[2]})
        vmeta = valuation._load(c, "HK")
    return {"indices": idx, "last": info, "valuation_at": vmeta.get("at"), "auto_update": settings.auto_update_enabled("HK"), "running": _bg["running"],
            "has_data": any(i["rows"] for i in idx), "today": today_hkt()}


NOTE = ("港股指数的均线位置只是描述现状（站上 / 跌破哪几条均线），不是买卖信号：A 股、美股指数的均线结论没有拿到港股上验证过。"
        "恒生科技 2020-07 才发布，之前是指数公司回溯的数据，没有真实成交的日子已丢掉。数据来自第三方网站，可能延迟或中断。")


if __name__ == "__main__":                                 # python -m server.hk  ：手动刷新
    with settings.market_ctx("HK"), db.market_db("HK") as c:
        print(refresh_all(c, "cli"))
