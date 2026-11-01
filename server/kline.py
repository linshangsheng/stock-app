"""「K线」页：指数日 K 线 + 均线 + 成交额；A 股指数再带筹码分布（估算，见 chips.py）。
A 股：上证50、沪深300、创业板指（有筹码分布）；美股：标普500（^GSPC）、纳斯达克100（^NDX，库里没有就退回 QQQ）（不做筹码分布：美股日线没有换手率）。
结果按「指数 + 最新交易日」缓存在内存里（算一次约 1 秒）。展示用，不进入选股与回测。"""
from __future__ import annotations

import threading

import pandas as pd

from . import chips, combo, db, market_calendar as mc, settings

INDICES = {
    "CN": [{"key": "sh.000016", "name": "上证50", "scope": "sh", "n": 50},
           {"key": "sh.000300", "name": "沪深300", "scope": "all", "n": 300},
           {"key": "sz.399006", "name": "创业板指", "scope": "chinext", "n": 100}],
    "US": [{"key": "^GSPC", "name": "标普500"},
           {"key": "^NDX", "name": "纳斯达克100", "fallback": "QQQ"}],
}
WINDOW = 1250                       # 返回最近多少个交易日的 K 线（约 5 年）
CHIP_DAYS = 500                     # 其中最近多少天带筹码分布（约 2 年）
MA_N = (5, 20, 60, 200, 250)
_cache: dict = {}
_lock = threading.Lock()


def _r(x, d=2):
    return None if x is None or x != x else round(float(x), d)


def _bars(conn, sym: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT date, open, high, low, close, volume, amount FROM index_bar WHERE symbol=? ORDER BY date", conn, params=(sym,)).set_index("date")


def indices_available(market: str) -> list[dict]:
    with settings.market_ctx(market), db.market_db(market) as c:
        out = []
        for s in INDICES[market]:
            n = c.execute("SELECT COUNT(*) FROM index_bar WHERE symbol IN (?,?)", (s["key"], s.get("fallback") or s["key"])).fetchone()[0]
            out.append({"key": s["key"], "name": s["name"], "ok": n >= 60, "chips": market == "CN"})
        return out


def build(market: str, key: str | None = None) -> dict:
    specs = INDICES[market]
    listing = indices_available(market)
    spec = next((s for s in specs if s["key"] == key), None)
    if spec is None:                                          # 没指定（或指定的不存在）：用第一个有日线数据的指数
        spec = next((s for s, i in zip(specs, listing) if i["ok"]), specs[0])
    with settings.market_ctx(market), db.market_db(market) as c:
        sym, name = spec["key"], spec["name"]
        if market == "US":
            try:
                combo._ensure_us_index(c, sym)                  # ^NDX 库里没有就先取一次历史
            except Exception:  # noqa: BLE001 - 取不到就用 ETF
                pass
            if c.execute("SELECT COUNT(*) FROM index_bar WHERE symbol=?", (sym,)).fetchone()[0] < 250 and spec.get("fallback"):
                sym, name = spec["fallback"], f"{name}（{spec['fallback']}）"
        bars = _bars(c, sym)
        closed = mc.last_closed_trading_day(c, market)
        if closed:
            bars = bars[bars.index <= closed]
        if len(bars) < 60:
            return {"status": "no_data", "indices": listing, "index": {"key": spec["key"], "name": name},
                    "message": f"{name}日线不足：请先初始化数据（设置页）。"}
        ck = (market, spec["key"], bars.index[-1], len(bars))
        with _lock:
            hit = _cache.get(ck)
        if hit:
            return {**hit, "indices": listing}
        close = bars["close"]
        ma = {str(n): close.rolling(n, min_periods=n).mean() for n in MA_N}
        w = bars.iloc[-WINDOW:]
        out = {"status": "ok", "index": {"key": spec["key"], "name": name, "symbol": sym},
               "dates": list(w.index),
               "candles": [[_r(r.open), _r(r.high), _r(r.low), _r(r.close)] for r in w.itertuples()],
               "volume": [_r(x, 0) for x in w["volume"]], "amount": [_r(x, 0) for x in w["amount"]],
               "ma": {k: [_r(x) for x in v.iloc[-WINDOW:]] for k, v in ma.items()}, "chips": None}
        if market == "CN" and spec.get("scope"):
            syms = chips.members(c, spec["scope"], spec["n"])
            if len(syms) >= 10:
                tr = chips.index_turnover(c, syms, bars.index[0])
                cb = chips.compute(bars[["open", "high", "low", "close"]], tr, keep=CHIP_DAYS)
                cb["members"] = len(syms)
                cb["note"] = (f"筹码分布是估算：用每天的价格区间和换手率推算，不是真实持仓；指数换手率由「流通市值最大的 {len(syms)} 只股票」汇总得到"
                              f"（{name}官方成分股名单取不到，只能近似），所以和同花顺等软件的具体数字会有出入，形状大体相近。")
                out["chips"] = cb
        with _lock:
            if len(_cache) > 12:
                _cache.clear()
            _cache[ck] = out
    return {**out, "indices": listing}
