"""A 股取数（2.2 / 3.3.1）：BaoStock（历史日线 / 复权因子 / 证券列表 / 日历 / 行业）+ AkShare（全市场快照 / 业绩预约披露）。
本模块是字段映射层：把上游返回统一成标准结构，并统一单位（成交量=股、成交额=元）。上游字段变化只在这里适配。

M0 实测结论（2026-10，本机）：
  * BaoStock 日线 volume 单位为「股」，amount 为「元」；adjustflag=3 取不复权价；
  * query_adjust_factor 返回按除权日分段的 backAdjustFactor（累计后复权因子），首个除权日之前视为 1.0；
  * 停牌日仍返回一行（tradeStatus=0，部分字段为空串）；
  * baostock 0.9.4 的 ResultSet.get_data() 与 pandas>=2 不兼容（DataFrame.append 已删除），改为逐行组装 DataFrame。
"""
from __future__ import annotations

import io
import contextlib
from datetime import date, timedelta

import pandas as pd

from . import settings, throttle

BAR_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover",
            "adj_factor", "trade_status", "is_st"]


def board_of(symbol: str) -> str | None:
    """沪深 A 股板块；非 A 股普通股（指数 / 基金 / B 股 / 北交所）返回 None。"""
    ex, _, code = symbol.partition(".")
    if ex == "sh":
        if code.startswith("688"):
            return "star"
        if code.startswith("60"):
            return "main"
    elif ex == "sz":
        if code.startswith(("300", "301")):
            return "chinext"
        if code.startswith(("000", "001", "002", "003")):
            return "main"
    return None


def _num(s):
    return pd.to_numeric(s, errors="coerce")


class BaoStockSource:
    name = "baostock"

    def __init__(self):
        self._bs = None
        self._logged_in = False
        self.th = throttle.get("baostock")

    # ---- 会话管理（2.5-6：login/logout，超时自动重登，并发度 1）----
    def _login(self):
        import baostock as bs

        self._bs = bs
        with contextlib.redirect_stdout(io.StringIO()):
            lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        self._logged_in = True

    def close(self):
        if self._bs and self._logged_in:
            with contextlib.redirect_stdout(io.StringIO()):
                self._bs.logout()
        self._logged_in = False

    def _query(self, fn_name: str, *args, **kwargs) -> pd.DataFrame:
        def once():
            if not self._logged_in:
                self._login()
            rs = getattr(self._bs, fn_name)(*args, **kwargs)
            if rs.error_code != "0":
                self._logged_in = False          # 会话可能超时：下次调用前重新登录
                raise RuntimeError(f"baostock {fn_name}: {rs.error_code} {rs.error_msg}")
            # 不用 rs.get_data()：baostock 0.9.4 内部调用了 pandas 2+ 已删除的 DataFrame.append（M0 实测）
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            return pd.DataFrame(rows, columns=rs.fields)

        return self.th.call(once)

    # ---- 日历 ----
    def trade_calendar(self, start: str, end: str) -> list[tuple[str, int, int]]:
        df = self._query("query_trade_dates", start_date=start, end_date=end)
        return [(r.calendar_date, int(r.is_trading_day), 0) for r in df.itertuples()]

    # ---- 证券名单 ----
    def list_securities_union(self, days: list[str]) -> pd.DataFrame:
        """多个历史交易日的证券列表求并集，补回已退市证券（3.26.1-5）。"""
        frames = []
        for d in days:
            df = self._query("query_all_stock", day=d)
            if len(df):
                frames.append(df)
        if not frames:
            return pd.DataFrame(columns=["symbol", "name"])
        allx = pd.concat(frames).drop_duplicates("code", keep="last")
        allx["board"] = allx["code"].map(board_of)
        allx = allx[allx["board"].notna()]
        return allx.rename(columns={"code": "symbol", "code_name": "name"})[["symbol", "name", "board"]].reset_index(drop=True)

    def security_basic(self, symbol: str) -> dict:
        df = self._query("query_stock_basic", code=symbol)
        if df.empty:
            return {}
        r = df.iloc[0]
        status = "active" if str(r.get("status")) == "1" else "delisted"
        return {"name": r.get("code_name"), "list_date": r.get("ipoDate") or None,
                "delist_date": r.get("outDate") or None, "status": status,
                "sec_type": {"1": "stock", "2": "index"}.get(str(r.get("type")), "other")}

    # ---- 日线 + 复权因子 ----
    def adj_factor_events(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        df = self._query("query_adjust_factor", code=symbol, start_date=start, end_date=end)
        if df.empty:
            return pd.DataFrame(columns=["date", "factor"])
        out = pd.DataFrame({"date": df["dividOperateDate"], "factor": _num(df["backAdjustFactor"])})
        return out.dropna().drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)

    def daily_bars(self, symbol: str, start: str, end: str, prev_close: float | None = None,
                   prev_factor: float | None = None) -> pd.DataFrame:
        """日线（不复权）+ 复权因子。
        全量模式（prev_factor 为空）：2 次请求（日线 + 复权因子事件）。
        增量模式（给出库内最后一日的 close 与 adj_factor）：1 次请求；只有当 BaoStock 返回的 preclose（已按除权调整的前收）
        与前一日收盘价对不上（= 发生了除权除息）时，才补取复权因子事件——把每日全市场增量的请求数从 2 倍降到 1 倍。"""
        fields = "date,open,high,low,close,preclose,volume,amount,turn,tradeStatus,isST"
        df = self._query("query_history_k_data_plus", symbol, fields, start_date=start, end_date=end,
                         frequency="d", adjustflag="3")
        if df.empty:
            return pd.DataFrame(columns=BAR_COLS)
        out = pd.DataFrame({
            "date": df["date"],
            "open": _num(df["open"]), "high": _num(df["high"]), "low": _num(df["low"]), "close": _num(df["close"]),
            "volume": _num(df["volume"]), "amount": _num(df["amount"]), "turnover": _num(df["turn"]),
            "trade_status": _num(df["tradeStatus"]).fillna(1).astype(int),
            "is_st": _num(df["isST"]).fillna(0).astype(int),
        })
        need_events = prev_factor is None
        if not need_events:
            prev = out["close"].shift(1)
            if prev_close is not None and len(prev):
                prev.iloc[0] = prev_close
            pc = _num(df["preclose"])
            ex = ((pc / prev - 1).abs() > 0.001) & prev.notna() & pc.notna()
            need_events = bool(ex.any())
        ev = None
        if need_events:
            ev = self.adj_factor_events(symbol, "1990-01-01", end)
            out["adj_factor"] = attach_adj_factor(out["date"], ev)
        else:
            out["adj_factor"] = float(prev_factor)
        res = out[BAR_COLS].copy()
        res.attrs["adj_events"] = ev                    # 调用方（初始化）直接复用，不必为同一批数据再请求一次复权因子
        return res

    def index_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        df = self._query("query_history_k_data_plus", symbol, "date,open,high,low,close,volume,amount",
                         start_date=start, end_date=end, frequency="d", adjustflag="3")
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        for c in df.columns[1:]:
            df[c] = _num(df[c])
        return df

    def report_pub_date(self, symbol: str, year: int, quarter: int) -> str | None:
        """某季度定期报告的**实际披露日**（BaoStock 季频财务的 pubDate；未披露返回 None）。
        财报日历来源：与行情同一稳定上游，免登录；AkShare 的业绩预约接口在本机不可用（M0）。"""
        df = self._query("query_profit_data", code=symbol, year=year, quarter=quarter)
        if df.empty:
            return None
        v = str(df["pubDate"].iloc[0]).strip()
        return v or None

    def industry_map(self) -> pd.DataFrame:
        df = self._query("query_stock_industry")
        if df.empty:
            return pd.DataFrame(columns=["symbol", "industry"])
        out = df.rename(columns={"code": "symbol"})[["symbol", "industry"]]
        out["industry"] = out["industry"].replace("", pd.NA)
        return out.dropna()


def attach_adj_factor(dates: pd.Series, events: pd.DataFrame) -> pd.Series:
    """按除权日分段为每个交易日取 backAdjustFactor；首个除权日之前为 1.0（3.6.1）。"""
    if events.empty:
        return pd.Series(1.0, index=dates.index)
    ev = events.sort_values("date")
    idx = pd.Index(ev["date"]).searchsorted(dates.values, side="right") - 1
    fac = ev["factor"].to_numpy()
    res = pd.Series([fac[i] if i >= 0 else 1.0 for i in idx], index=dates.index, dtype=float)
    return res


def _with_timeout(fn, seconds: float, *args, **kwargs):
    """网页接口可能长时间无响应（M0 实测：本机上 AkShare 的东财 / 新浪接口被断开或每页 ~24 秒）：超时即视为失败，不阻塞任务链。"""
    import concurrent.futures as cf

    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=seconds)
    except cf.TimeoutError as e:
        raise TimeoutError(f"{getattr(fn, '__name__', 'call')} 超过 {seconds:.0f}s 无响应") from e
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


class EastmoneySource:
    """全市场快照（一次分页取全部，约 3 分钟）：东财 push2 行情接口，直接用 requests 调用。
    M0 实测：AkShare 封装层在本机被断开，但 `82.push2.eastmoney.com` 节点可直连；单页上限 100 行，约 56 页。
    用途：① L1 入库粗筛（价格 / 流通市值 / 成交额）；② 盘后增量追加当日日线（is_temp=1，次日由 BaoStock 对账覆盖）。
    字段映射层：成交量由「手」换算为「股」，成交额为元；停牌股价格为空。上游为网页接口、易变，失败时调用方降级到 BaoStock。"""
    name = "eastmoney"
    HOSTS = ("82.push2.eastmoney.com", "push2.eastmoney.com")
    FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"          # 沪深主板 / 创业板 / 科创板 A 股（不含北交所）
    FIELDS = "f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18,f21"

    def __init__(self):
        self.th = throttle.get("eastmoney")

    def _page(self, host: str, pn: int) -> dict:
        import requests

        def once():
            r = requests.get(f"https://{host}/api/qt/clist/get", params={
                "pn": pn, "pz": 100, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2, "fid": "f12",
                "fs": self.FS, "fields": self.FIELDS},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                         "Referer": "https://quote.eastmoney.com/"}, timeout=30)
            r.raise_for_status()
            j = r.json()                                   # 被限流 / 节点不可用时返回空体：JSONDecodeError -> 按失败退避
            if not j.get("data"):
                raise throttle.RateLimited("东财 push2 返回空数据")
            return j["data"]

        return self.th.call(once, retries=2)

    def _ulist(self, host: str, symbols: list[str]) -> list[dict]:
        import requests

        secids = ",".join(_secid(x) for x in symbols)

        def once():
            r = requests.get(f"https://{host}/api/qt/ulist.np/get", params={"fltt": 2, "invt": 2, "fields": self.FIELDS, "secids": secids,
                             "ut": "bd1d9ddb04089700cf9c27f6f7426281"}, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                                      "Referer": "https://quote.eastmoney.com/"})
            r.raise_for_status()
            d = (r.json().get("data") or {}).get("diff") or []
            if not d:
                raise throttle.RateLimited("东财 ulist 返回空数据")
            return d.values() if isinstance(d, dict) else d

        return list(self.th.call(once, retries=2))

    def snapshot(self, codes: list[str] | None = None) -> pd.DataFrame:
        """优先用 clist 分页取全市场（约 56 页）；整体失败且给出了代码清单（codes）时，改用 ulist 按代码分批取（每批 80 只）。
        M0 实测：clist 的大列表翻页较容易触发 IP 级限流（单页可通，连续翻页会被断开）；ulist 小批量更稳，但没有在限流期之外联调过。"""
        try:
            return self._snapshot_clist()
        except throttle.CircuitOpen:
            raise
        except Exception as e:  # noqa: BLE001
            if not codes:
                raise
            rows = []
            for host in self.HOSTS:
                try:
                    rows = []
                    for i in range(0, len(codes), 80):
                        rows += self._ulist(host, codes[i:i + 80])
                    break
                except throttle.CircuitOpen:
                    raise
                except Exception:  # noqa: BLE001
                    rows = []
            if not rows:
                raise RuntimeError(f"东财快照不可用（clist 与 ulist 均失败）：{e}")
            return parse_clist(rows)

    def _snapshot_clist(self) -> pd.DataFrame:
        last_err = None
        for host in self.HOSTS:
            try:
                rows, total, pn = [], None, 1
                while True:
                    d = self._page(host, pn)
                    total = d.get("total", total)
                    diff = d.get("diff") or []
                    rows += diff.values() if isinstance(diff, dict) else diff
                    if not diff or (total is not None and len(rows) >= total):
                        break
                    pn += 1
                if rows:
                    return parse_clist(rows)
            except throttle.CircuitOpen:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"东财快照不可用：{last_err}")


class TencentSource:
    """腾讯行情批量报价（qt.gtimg.cn）：一次请求 80 只，全部 A 股约 60 个请求、1~2 分钟。盘后增量的主路径。
    2026-10-15 实测（收盘后取，与库里 BaoStock 当日日线 4889 只逐字段对比）：开 / 高 / 低 / 收、成交额、ST 全部一致；
    成交量一致（科创板单位是「股」、其余是「手」——按「成交额 ÷ 量」落在当日价格区间来判断单位，不靠板块规则）；
    「昨收」是交易所按除权调整后的前收：与库里前一日收盘对不上的 20 只，正好是当天 BaoStock 复权因子变化的 20 只。
    停牌股 status = S、量为 0、价格 = 昨收。字段位置（~ 分隔）：1 名称、3 现价、4 昨收、5 开、6 量、30 时间 YYYYMMDDhhmmss、
    33 高、34 低、35 价/量/额(元)、38 换手率(%)、40 状态、44 流通市值(亿)。上游是网页接口、可能变化：解析时做一致性检查，
    不通过的行交给 BaoStock；整批明显不对时调用方整体降级。"""
    name = "tencent"
    URL = "https://qt.gtimg.cn/q="
    BATCH = 80

    def __init__(self):
        self.th = throttle.get("tencent")

    @staticmethod
    def code(symbol: str) -> str:
        return symbol.replace(".", "")

    def _get(self, symbols: list[str]) -> str:
        import requests

        def once():
            r = requests.get(self.URL + ",".join(self.code(s) for s in symbols), timeout=20,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"})
            r.raise_for_status()
            txt = r.content.decode("gbk", errors="replace")
            if "~" not in txt:
                raise throttle.RateLimited("腾讯行情返回空数据")
            return txt

        return self.th.call(once, retries=2)

    def _raw(self, symbols: list[str], stop=None, progress=None) -> list[list[str]]:
        out: list[list[str]] = []
        for i in range(0, len(symbols), self.BATCH):
            if stop and stop():
                break
            for line in self._get(symbols[i:i + self.BATCH]).split(";"):
                key, _, body = line.strip().partition("=")
                f = body.strip('"').split("~")
                if len(f) > 45 and key.startswith("v_"):
                    out.append([key[2:4] + "." + key[4:]] + f)
            if progress:
                progress("quotes", min(i + self.BATCH, len(symbols)), len(symbols))
        return out

    def quotes(self, symbols: list[str], stop=None, progress=None) -> pd.DataFrame:
        """个股报价 -> symbol, name, open, high, low, close, prev_close, volume(股), amount(元), turnover(%), float_mktcap(元),
        pct(%), quote_date(YYYY-MM-DD), quote_time(hhmmss), suspended, ok（一致性检查通过）。"""
        return parse_tencent(self._raw(symbols, stop, progress))

    def index_quotes(self, symbols: list[str]) -> pd.DataFrame:
        """指数报价 -> symbol, date, open, high, low, close, volume(股), amount(元)。价格只有 2 位小数，次日 BaoStock 覆盖。"""
        rows = []
        for f in self._raw(symbols):
            num = lambda i: pd.to_numeric(f[i], errors="coerce")  # noqa: E731
            pva = f[36].split("/")
            ts = f[31]
            rows.append({"symbol": f[0], "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else None, "time": ts[8:14],
                         "open": num(6), "high": num(34), "low": num(35), "close": num(4),
                         "volume": num(7) * 100, "amount": pd.to_numeric(pva[2], errors="coerce") if len(pva) > 2 else None})
        return pd.DataFrame(rows, columns=["symbol", "date", "time", "open", "high", "low", "close", "volume", "amount"])

    def snapshot(self, codes: list[str]) -> pd.DataFrame:
        """与 EastmoneySource.snapshot 同结构（L1 粗筛用）：只保留一致性检查通过的行。"""
        q = self.quotes(codes)
        return q[q["ok"]].reset_index(drop=True)


def parse_tencent(raw: list[list[str]]) -> pd.DataFrame:
    """腾讯报价行（第 0 列是 symbol，之后是 ~ 分隔的原始字段）-> 标准结构。见 TencentSource 的字段说明。"""
    cols = ["symbol", "name", "open", "high", "low", "close", "prev_close", "volume", "amount", "turnover", "float_mktcap", "pct",
            "quote_date", "quote_time", "suspended", "ok"]
    if not raw:
        return pd.DataFrame(columns=cols)
    f = pd.DataFrame(raw)
    num = lambda i: pd.to_numeric(f[i], errors="coerce")  # noqa: E731 - 原始字段下标 + 1（第 0 列是 symbol）
    ts = f[31].astype(str)
    pva = f[36].astype(str).str.split("/")
    amount = pd.to_numeric(pva.str[2], errors="coerce")
    o, h, lo, c, pc, vol = num(6), num(34), num(35), num(4), num(5), num(7)
    status = f[41].astype(str).str.strip() if 41 in f.columns else pd.Series("", index=f.index)
    suspended = (status == "S") | ((vol.fillna(0) == 0) & (o.fillna(0) == 0))
    # 量的单位：成交额 ÷ (量 × 100) 落在当日价格区间 = 单位是「手」；成交额 ÷ 量 落在区间 = 单位是「股」（科创板）
    in_rng = lambda v: (v >= lo * 0.98) & (v <= h * 1.02)  # noqa: E731
    vw_hand, vw_share = amount / (vol * 100), amount / vol
    volume = pd.Series(float("nan"), index=f.index)
    volume[in_rng(vw_hand)] = vol * 100
    volume[~in_rng(vw_hand) & in_rng(vw_share)] = vol
    volume[suspended] = 0.0
    sane = (c > 0) & (pc > 0) & ((o > 0) & (h >= lo) & (h >= o) & (h >= c) & (lo <= o) & (lo <= c) & volume.notna() & (amount > 0))
    out = pd.DataFrame({
        "symbol": f[0], "name": f[2], "open": o, "high": h, "low": lo, "close": c, "prev_close": pc,
        "volume": volume, "amount": amount.where(~suspended), "turnover": num(39), "float_mktcap": num(45) * 1e8, "pct": num(33),
        "quote_date": ts.str[:4] + "-" + ts.str[4:6] + "-" + ts.str[6:8], "quote_time": ts.str[8:14],
        "suspended": suspended, "ok": sane | (suspended & (c > 0)),
    })
    out = out[out["symbol"].map(board_of).notna()]
    return out.reset_index(drop=True)[cols]


def _secid(symbol: str) -> str:
    ex, _, code_ = symbol.partition(".")
    return ("1." if ex == "sh" else "0.") + code_


def live_quotes(symbols: list[str]) -> dict[str, dict]:
    """A 股盘中低频报价（≥60 秒调用一次，调用方缓存）：东财 push2 ulist，几只约 1.5 秒。{symbol: {price, prev_close}}。"""
    import requests

    th = throttle.get("eastmoney")
    secids = ",".join(_secid(s) for s in symbols)

    def once():
        last = None
        for host in EastmoneySource.HOSTS:
            try:
                r = requests.get(f"https://{host}/api/qt/ulist.np/get", params={"fltt": 2, "invt": 2, "fields": "f2,f3,f4,f12,f14,f18", "secids": secids,
                                 "ut": "bd1d9ddb04089700cf9c27f6f7426281"}, timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                                          "Referer": "https://quote.eastmoney.com/"})
                d = (r.json().get("data") or {}).get("diff") or []
                if d:
                    return d
            except Exception as e:  # noqa: BLE001
                last = e
        raise throttle.RateLimited(f"东财实时报价不可用：{last}")

    out = {}
    for r in th.call(once, retries=1):
        code_ = str(r["f12"])
        sym = ("sh." if code_.startswith(("6", "9")) else "sz.") + code_
        px, pc = pd.to_numeric(r.get("f2"), errors="coerce"), pd.to_numeric(r.get("f18"), errors="coerce")
        if px == px and pc == pc and pc > 0:
            out[sym] = {"price": float(px), "prev_close": float(pc)}
    return out


def parse_clist(rows: list[dict]) -> pd.DataFrame:
    """push2 clist 行 -> 标准快照：symbol, name, open, high, low, close, volume(股), amount(元), turnover(%), float_mktcap(元), pct(%), prev_close。"""
    df = pd.DataFrame(rows)
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731 - 停牌 / 无数据为 "-"
    code_ = df["f12"].astype(str)
    out = pd.DataFrame({
        "symbol": code_.map(lambda c: ("sh." if c.startswith(("6", "9")) else "sz.") + c), "name": df["f14"],
        "open": num("f17"), "high": num("f15"), "low": num("f16"), "close": num("f2"), "prev_close": num("f18"),
        "volume": num("f5") * 100, "amount": num("f6"), "turnover": num("f8"), "float_mktcap": num("f21"), "pct": num("f3"),
    })
    out = out[out["symbol"].map(board_of).notna()]         # 只保留沪深主板 / 创业板 / 科创板 A 股
    return out.reset_index(drop=True)


def get_source(market: str = "CN"):
    """按市场与 config.datasource 选择数据源：CN = baostock | demo；US = yfinance | demo。"""
    if market == "US":
        from .datasource_us import get_us_source

        return get_us_source()
    kind = settings.cfg()["datasource"].get("cn", "baostock")
    if kind == "demo":
        from .datasource_demo import DemoSource

        return DemoSource()
    return BaoStockSource()
