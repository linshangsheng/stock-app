"""A 股备用数据源（BaoStock 连不上时自动切换）：腾讯日 K（web.ifzq.gtimg.cn newfqkline）、东方财富日 K（push2his，akshare 的
stock_zh_a_hist 用的就是这个接口——这里直接调用，不引入 akshare 这个依赖很多、改版频繁的库）、上交所交易日历（exchange_calendars XSHG）、
用腾讯行情按代码段扫描出的当前股票名单。

为什么需要：BaoStock 是免费的单一服务器，经常在北京时间下午到晚上（它处理当日数据的时段）连不上——本机任务日志 7 天里记了 101 次
「连续失败触发熔断」，底层报错是「网络接收错误」。以前初始化只认 BaoStock，熔断就整个失败（用户看到「CircuitOpen: baostock: 熔断中」）。

各源的字段都统一成 BAR_COLS（不复权价 + 后复权因子）：
  * 复权因子按交易所的除权公式自己算（与 BaoStock 的后复权因子同口径）：除权日的因子 = 前一天的因子 × 前收盘 ÷ 除权参考价。
    腾讯：除权日那一行带分红送转信息（每 10 股派现 fh_sh、「10送3股, 10转12股」文字），参考价 = (前收 − 每股派现) ÷ (1 + 每股送转)，四舍五入到分；
    东方财富：每行带涨跌额，交易所前收 = 收盘 − 涨跌额，与库里前一天收盘对不上（> 0.1%）就是除权日。
    （不能用「后复权价 ÷ 不复权价」：腾讯的后复权是把分红加回价格的「加法」口径，两者之比每天都在变——2026-10-23 实测。）
    增量更新（给了 prev_factor）时按「库里上一天的因子 × 本源的相对变化」接续，同一只股票前后换了数据源也不会出现假跳空；
  * 成交量单位按「成交额 ÷ 成交量 落在当日价格区间」判断（科创板常以股计、其余以手计），不靠板块规则；
  * 备用源没有历史 ST 标记与停牌行：is_st 用当前名称判断（近似），停牌日没有行（面板按「无数据」处理）。"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import date, timedelta

import pandas as pd

from . import throttle
from .datasource_cn import BAR_COLS, board_of

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _infer_volume(vol: pd.Series, amount: pd.Series, low: pd.Series, high: pd.Series) -> pd.Series:
    """成交量统一成「股」：成交额 ÷ (量 × 100) 落在 [最低, 最高] 附近 = 单位是手；成交额 ÷ 量 落在区间 = 单位是股。"""
    ok = lambda v: (v >= low * 0.97) & (v <= high * 1.03)  # noqa: E731
    out = vol * 100
    shares = ~ok(amount / (vol * 100)) & ok(amount / vol)
    out[shares] = vol[shares]
    return out


def _r2(x: float) -> float:
    """交易所的四舍五入到分（不是银行家舍入）。"""
    return math.floor(x * 100 + 0.5 + 1e-9) / 100


def _parse_fh(info: dict) -> tuple[float, float, float, float] | None:
    """腾讯除权信息 → (每股派现, 每股送转, 每股配股, 配股价)。例：fh_sh=1、FHcontent=「10送3股, 10转12股, 10派1元」。"""
    if not isinstance(info, dict) or not info:
        return None
    txt = str(info.get("FHcontent") or "")
    num = lambda pat: sum(float(x) for x in re.findall(pat, txt)) / 10  # noqa: E731
    cash = pd.to_numeric(info.get("fh_sh"), errors="coerce")
    cash = float(cash) / 10 if cash == cash and cash is not None else num(r"派([\d.]+)元")
    stock = num(r"送([\d.]+)股") + num(r"转(?:增)?([\d.]+)股")
    rights = num(r"配([\d.]+)股")
    m = re.search(r"配股价([\d.]+)元", txt)
    return cash, stock, rights, (float(m.group(1)) if m else 0.0)


def _factor_from_events(raw: pd.DataFrame, events: dict) -> pd.Series:
    """按除权日逐段累乘：因子 *= 前收盘 ÷ 除权参考价（参考价 = (前收 − 派现 + 配股价 × 配股) ÷ (1 + 送转 + 配股)，四舍五入到分）。"""
    cur, out, closes = 1.0, [], raw["close"].tolist()
    for i, d in enumerate(raw["date"]):
        e = events.get(d)
        if e and i > 0 and closes[i - 1] > 0:
            cash, stock, rights, rp = e
            if rights and not rp:
                rights = 0.0                                       # 没给配股价就忽略配股（近年很少）
            ref = _r2((closes[i - 1] - cash + rp * rights) / (1 + stock + rights))
            if ref > 0:
                cur *= closes[i - 1] / ref
        out.append(round(cur, 6))
    return pd.Series(out, index=raw.index)


def _factor_from_preclose(raw: pd.DataFrame) -> pd.Series:
    """东方财富：交易所前收（收盘 − 涨跌额）与前一天收盘对不上 = 除权日，因子 *= 前一天收盘 ÷ 交易所前收。"""
    cur, out = 1.0, []
    closes, pre = raw["close"].tolist(), raw["preclose"].tolist()
    for i in range(len(raw)):
        if i > 0 and pre[i] and pre[i] > 0 and closes[i - 1] > 0 and abs(closes[i - 1] / pre[i] - 1) > 0.001:
            cur *= closes[i - 1] / pre[i]
        out.append(round(cur, 6))
    return pd.Series(out, index=raw.index)


def _finish(raw: pd.DataFrame, factor: pd.Series, start: str, end: str, prev_factor: float | None, is_st: int) -> pd.DataFrame:
    """拼成 BAR_COLS；给了 prev_factor（增量）时按「库里上一天的因子」接续本源的相对变化。"""
    raw = raw.copy()
    raw["adj_factor"] = factor.values
    if prev_factor is not None:
        before = raw[raw["date"] < start]
        base = float(before["adj_factor"].iloc[-1]) if len(before) else float(raw["adj_factor"].iloc[0])
        raw["adj_factor"] = raw["adj_factor"] * (float(prev_factor) / base) if base else float(prev_factor)
    raw = raw[(raw["date"] >= start) & (raw["date"] <= end)].reset_index(drop=True)
    raw["trade_status"] = 1
    raw["is_st"] = is_st
    out = raw[BAR_COLS].copy()
    ev = out.loc[out["adj_factor"].ne(out["adj_factor"].shift()), ["date", "adj_factor"]].rename(columns={"adj_factor": "factor"})
    out.attrs["adj_events"] = ev.reset_index(drop=True)
    return out


class TencentHistory:
    """腾讯日 K（不复权）：每次最多 640 根，按结束日期往前翻页，10 年约 4~5 次请求。字段：日期、开、收、高、低、量、{除权日的分红送转信息}、换手率%、成交额(万元)。"""
    name = "tencent"
    URL = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"

    def __init__(self):
        self.th = throttle.get("tencent_hist")

    @staticmethod
    def code(symbol: str) -> str:
        return symbol.replace(".", "")

    def _page(self, symbol: str, end: str, fq: str) -> list:
        import requests

        def once():
            r = requests.get(self.URL, params={"param": f"{self.code(symbol)},day,,{end},640,{fq}"}, headers=UA, timeout=20)
            r.raise_for_status()
            j = r.json()
            d = (j.get("data") or {})
            if not isinstance(d, dict):
                raise throttle.RateLimited(f"腾讯日K返回异常：{j.get('msg')}")
            node = d.get(self.code(symbol)) or {}
            return node.get(f"{fq}day") or node.get("day") or []

        return self.th.call(once, retries=2)

    def _klines(self, symbol: str, start: str, end: str, fq: str) -> pd.DataFrame:
        rows, cur = [], end
        for _ in range(12):                                       # 12 × 640 ≈ 30 年，足够
            page = self._page(symbol, cur, fq)
            page = [p for p in page if p and p[0] <= cur]
            if not page:
                break
            rows = page + rows
            first = page[0][0]
            if first <= start or len(page) < 600:
                break
            cur = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])
        f = pd.DataFrame([r[:9] if len(r) >= 9 else r[:6] + [None, None, None] for r in rows])
        out = pd.DataFrame({"date": f[0], "open": _num(f[1]), "close": _num(f[2]), "high": _num(f[3]), "low": _num(f[4]), "volume": _num(f[5]),
                            "turnover": _num(f[7]), "amount": _num(f[8]) * 1e4, "fh": f[6].map(_parse_fh)})
        return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)

    def daily_bars(self, symbol: str, start: str, end: str, prev_close=None, prev_factor=None, is_st: int = 0) -> pd.DataFrame:
        s0 = (date.fromisoformat(start) - timedelta(days=20)).isoformat() if prev_factor is not None else start
        raw = self._klines(symbol, s0, end, "")
        raw = raw[(raw["date"] >= s0) & (raw["date"] <= end)].reset_index(drop=True)
        if raw.empty:
            return pd.DataFrame(columns=BAR_COLS)
        raw["volume"] = _infer_volume(raw["volume"], raw["amount"], raw["low"], raw["high"])
        ev = {d: e for d, e in zip(raw["date"], raw["fh"]) if e}
        return _finish(raw, _factor_from_events(raw, ev), start, end, prev_factor, is_st)

    def index_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        raw = self._klines(symbol, start, end, "")
        raw = raw[(raw["date"] >= start) & (raw["date"] <= end)].reset_index(drop=True)
        raw["volume"] = raw["volume"] * 100
        return raw[["date", "open", "high", "low", "close", "volume", "amount"]]

    def scan_current_securities(self, progress=None) -> pd.DataFrame:
        """BaoStock 名单取不到时：用腾讯行情把沪深 A 股所有可能的代码段扫一遍（约 1.4 万个代码、约 3 分钟），
        状态不是「D（已退市）」、价格 > 0 且行情在近 30 天内的才算在市（已退市的代码腾讯也会返回，仍显示最后价格）。不含已退市股票（幸存者偏差，界面会标注）。"""
        import requests
        th = throttle.get("tencent")
        ranges = [("sh", 600000, 606000), ("sh", 688000, 690000), ("sz", 1, 4000), ("sz", 300000, 302000)]
        codes = [f"{ex}{n:06d}" for ex, a, b in ranges for n in range(a, b)]
        recent = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        rows = []
        for i in range(0, len(codes), 80):
            def once(batch=codes[i:i + 80]):
                r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch), headers=UA, timeout=20)
                r.raise_for_status()
                return r.content.decode("gbk", errors="replace")
            for line in th.call(once, retries=2).split(";"):
                key, _, body = line.strip().partition("=")
                f = body.strip('"').split("~")
                if len(f) < 31 or not key.startswith("v_"):
                    continue
                sym = key[2:4] + "." + key[4:]
                px = pd.to_numeric(f[3], errors="coerce")
                status = f[40].strip() if len(f) > 40 else ""            # 第 40 个字段：D = 已退市（腾讯仍显示最后价格），S = 停牌
                if board_of(sym) and px == px and px > 0 and status != "D" and (f[30][:8] >= recent or status == "S"):
                    rows.append((sym, f[1], board_of(sym)))
            if progress:
                progress("securities_scan", min(i + 80, len(codes)), len(codes))
        return pd.DataFrame(rows, columns=["symbol", "name", "board"])


class EastmoneyHistory:
    """东方财富日 K（push2his，不复权）：一次请求取全部区间。字段 f51 日期、f52 开、f53 收、f54 高、f55 低、f56 量（手）、
    f57 成交额（元）、f61 换手率%。从国外访问会被限流（实测：第一次成功、之后断开），国内一般可用；限速 1~2 秒 / 次。"""
    name = "eastmoney_hist"
    URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self):
        self.th = throttle.get("eastmoney_hist")

    @staticmethod
    def secid(symbol: str) -> str:
        ex, _, c = symbol.partition(".")
        return ("1." if ex == "sh" else "0.") + c

    def _klines(self, symbol: str, start: str, end: str, fqt: int) -> pd.DataFrame:
        import requests

        def once():
            r = requests.get(self.URL, params={"secid": self.secid(symbol), "fields1": "f1,f2,f3,f4,f5,f6",
                                               "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": 101, "fqt": fqt,
                                               "beg": start.replace("-", ""), "end": end.replace("-", ""), "ut": "7eea3edcaed734bea9cbfc24409ed989"},
                             headers={**UA, "Referer": "https://quote.eastmoney.com/"}, timeout=20)
            r.raise_for_status()
            j = r.json()
            if not j.get("data"):
                raise throttle.RateLimited("东方财富日K返回空数据")
            return j["data"].get("klines") or []

        k = self.th.call(once, retries=2)
        if not k:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])
        f = pd.DataFrame([x.split(",") for x in k])
        out = pd.DataFrame({"date": f[0], "open": _num(f[1]), "close": _num(f[2]), "high": _num(f[3]), "low": _num(f[4]),
                            "volume": _num(f[5]), "amount": _num(f[6]), "turnover": _num(f[10])})
        out["preclose"] = (out["close"] - _num(f[9])).round(2)                 # f60 = 涨跌额 → 交易所前收（已按除权调整）
        return out

    def daily_bars(self, symbol: str, start: str, end: str, prev_close=None, prev_factor=None, is_st: int = 0) -> pd.DataFrame:
        s0 = (date.fromisoformat(start) - timedelta(days=20)).isoformat() if prev_factor is not None else start
        raw = self._klines(symbol, s0, end, 0)
        if raw.empty:
            return pd.DataFrame(columns=BAR_COLS)
        raw["volume"] = _infer_volume(raw["volume"], raw["amount"], raw["low"], raw["high"])
        return _finish(raw, _factor_from_preclose(raw), start, end, prev_factor, is_st)

    def index_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        raw = self._klines(symbol, start, end, 0)
        raw["volume"] = raw["volume"] * 100
        return raw[["date", "open", "high", "low", "close", "volume", "amount"]]


def xshg_calendar(start: str, end: str) -> list[tuple[str, int, int]]:
    """上交所交易日历（exchange_calendars 自带，离线可用；2016~2026 与 BaoStock 的日历逐日一致——2026-10-23 核对）。超出其范围的日期不返回。"""
    import exchange_calendars as xc
    cal = xc.get_calendar("XSHG")
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    lo, hi = max(s, cal.first_session), min(e, cal.last_session)
    have = {d.date().isoformat() for d in cal.sessions_in_range(lo, hi)}
    out, d = [], lo.date()
    while d <= hi.date():
        iso = d.isoformat()
        out.append((iso, 1 if iso in have else 0, 0))
        d += timedelta(days=1)
    return out


class CnMultiSource:
    """A 股数据源组合：每个功能按 BaoStock → 东方财富 → 腾讯 的顺序尝试，哪个能用就用哪个（probe() 可事先测一遍）。
    某个源熔断（连续失败）后，本次任务里不再找它，直接用下一个——不会每只股票都先等它超时。全部失败才报错。
    基本面 / 财报日历 / 公告用的 BaoStock 专有查询（_query）没有替代源，照旧走 BaoStock。"""

    def __init__(self, bs=None):
        from .datasource_cn import BaoStockSource
        self.bs = bs or BaoStockSource()
        self.sources = {"baostock": self.bs, "tencent": TencentHistory(), "eastmoney_hist": EastmoneyHistory()}   # 腾讯已逐字段验证；东方财富从国外被限流、未能实测，排最后
        self.order = list(self.sources)
        self.down: dict[str, str] = {}                          # 本次任务里已判定不可用的源 → 原因
        self.last_source = "baostock"
        self._sec_cache: pd.DataFrame | None = None
        self.th = getattr(self.bs, "th", None)

    @property
    def name(self) -> str:                                     # 写入 daily_bar.source：实际提供这批数据的源
        return self.last_source

    def _query(self, *a, **k):
        return self.bs._query(*a, **k)

    def close(self):
        self.bs.close()

    def _try(self, method: str, *a, **k):
        errs = []
        for nm in self.order:
            s = self.sources[nm]
            if nm in self.down or not hasattr(s, method):
                continue
            try:
                r = getattr(s, method)(*a, **k)
                self.last_source = nm
                return r
            except throttle.CircuitOpen as e:
                self.down[nm] = str(e)[:120]
                errs.append(f"{nm}: {str(e)[:80]}")
            except Exception as e:  # noqa: BLE001 - 这一只 / 这一次失败：换下一个源试，但不判定整个源不可用
                errs.append(f"{nm}: {type(e).__name__}: {str(e)[:80]}")
        raise throttle.CircuitOpen("A 股数据源都不可用（" + "；".join(errs or [f"{k}: {v}" for k, v in self.down.items()]) + "）")

    # ---- 行情 ----
    def daily_bars(self, symbol, start, end, prev_close=None, prev_factor=None):
        return self._try("daily_bars", symbol, start, end, prev_close=prev_close, prev_factor=prev_factor)

    def index_bars(self, symbol, start, end):
        return self._try("index_bars", symbol, start, end)

    def adj_factor_events(self, symbol, start, end):
        try:
            return self.bs.adj_factor_events(symbol, start, end) if "baostock" not in self.down else self._alt_events(symbol, start, end)
        except throttle.CircuitOpen as e:
            self.down["baostock"] = str(e)[:120]
            return self._alt_events(symbol, start, end)

    def _alt_events(self, symbol, start, end):
        b = self._try("daily_bars", symbol, max(start, "2005-01-01"), end)
        return b.attrs.get("adj_events", pd.DataFrame(columns=["date", "factor"]))

    # ---- 日历 / 名单 / 行业 / 证券信息 ----
    def trade_calendar(self, start, end):
        if "baostock" not in self.down:
            try:
                rows = self.bs.trade_calendar(start, end)
                if rows:
                    self.last_source = "baostock"
                    return rows
            except Exception as e:  # noqa: BLE001
                if isinstance(e, throttle.CircuitOpen):
                    self.down["baostock"] = str(e)[:120]
        self.last_source = "xshg"
        return xshg_calendar(start, end)

    def list_securities_union(self, days):
        if "baostock" not in self.down:
            try:
                r = self.bs.list_securities_union(days)
                self.last_source = "baostock"
                return r
            except Exception as e:  # noqa: BLE001
                if isinstance(e, throttle.CircuitOpen):
                    self.down["baostock"] = str(e)[:120]
        if self._sec_cache is None:
            self._sec_cache = self.sources["tencent"].scan_current_securities()
        self.last_source = "tencent_scan"
        return self._sec_cache.copy()

    def security_basic(self, symbol):
        if "baostock" in self.down:
            return {}
        try:
            return self.bs.security_basic(symbol)
        except throttle.CircuitOpen as e:
            self.down["baostock"] = str(e)[:120]
            return {}
        except Exception:  # noqa: BLE001
            return {}

    def industry_map(self):
        if "baostock" in self.down:
            return pd.DataFrame(columns=["symbol", "industry"])
        try:
            return self.bs.industry_map()
        except throttle.CircuitOpen as e:
            self.down["baostock"] = str(e)[:120]
            return pd.DataFrame(columns=["symbol", "industry"])

    def report_pub_date(self, symbol, year, quarter):
        return self.bs.report_pub_date(symbol, year, quarter)

    # ---- 体检：每个源各试一次（只取几天数据），用于「初始化前选最优」和设置页的「检测数据源」 ----
    def probe(self, symbol: str = "sh.600000") -> list[dict]:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=15)).isoformat()
        out = []
        for nm in self.order:
            s = self.sources[nm]
            t = time.time()
            th = getattr(s, "th", None)
            saved = (th.backoff_base, th.backoff_max, th.breaker_failures) if th is not None else None
            if th is not None:                                   # 体检只看「通不通」：失败一次就算不可用、不退避重试（否则连不上的源要等 1~2 分钟）
                th.backoff_base, th.backoff_max, th.breaker_failures = 0.5, 1.0, 1
            try:
                b = s.daily_bars(symbol, start, end) if nm != "baostock" else s.daily_bars(symbol, start, end, prev_close=None, prev_factor=1.0)
                ok = b is not None and len(b) > 0
                out.append({"source": nm, "ok": ok, "seconds": round(time.time() - t, 1), "rows": len(b) if b is not None else 0,
                            "error": None if ok else "没有返回数据"})
                if not ok:
                    self.down.setdefault(nm, "体检没有返回数据")
            except Exception as e:  # noqa: BLE001
                self.down[nm] = f"{type(e).__name__}: {str(e)[:100]}"
                out.append({"source": nm, "ok": False, "seconds": round(time.time() - t, 1), "rows": 0, "error": self.down[nm]})
            finally:
                if saved is not None:
                    th.backoff_base, th.backoff_max, th.breaker_failures = saved
        return out


SOURCE_NAMES = {"baostock": "BaoStock", "eastmoney_hist": "东方财富", "tencent": "腾讯", "xshg": "上交所日历（离线）", "tencent_scan": "腾讯行情扫描"}
