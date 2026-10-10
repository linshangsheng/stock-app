"""事件 / 公告 / 新闻 / Insider 取数（M6，3.16~3.22）。统一成 Event 模型：
  {event_type, event_time, publish_time, source, level, title, summary, url, sentiment}
可信度等级（3.22）：1 官方原始信息（巨潮资讯 / SEC EDGAR）；2 结构化金融数据（BaoStock / yfinance）；3 媒体新闻（Yahoo Finance）。
只对「漏斗候选 + 持仓」按需拉取并缓存 6 小时（3.17）——逐股接口不对全池拉取。

M6 实测（本机）：巨潮资讯 hisAnnouncement 与 SEC EDGAR submissions 均可直连、无需密钥（SEC 要求带联系方式的 User-Agent，且 ≤10 次/秒）。
Alpha Vantage / Finnhub 免费档额度很小（约 25 次/天），不适合逐股拉取；本实现改用额度充足的 Yahoo + SEC，不依赖密钥。"""
from __future__ import annotations

import re
import zlib
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import EMAIL as _EMAIL, settings, throttle

EVENT_TYPES = ["EARNINGS", "DIVIDEND", "BUYBACK", "INSIDER", "M_AND_A", "CONTRACT", "REGULATORY", "ANALYST", "NEWS", "OTHER"]
TYPE_LABEL = {"EARNINGS": "财报 / 业绩", "DIVIDEND": "分红", "BUYBACK": "回购", "INSIDER": "增减持 / Insider", "M_AND_A": "并购重组",
              "CONTRACT": "重大合同", "REGULATORY": "监管", "ANALYST": "分析师", "NEWS": "新闻", "OTHER": "其他"}

# 巨潮公告标题 -> 事件类型（顺序即优先级）
_CN_RULES = [
    ("REGULATORY", r"处罚|警示函|立案|监管函|问询函|关注函|违规|诉讼|仲裁|退市风险|风险警示|ST"),
    ("BUYBACK", r"回购"),
    ("INSIDER", r"增持|减持|股份变动|权益变动|持股变动|股东.*计划"),
    ("M_AND_A", r"并购|重组|收购|资产购买|资产出售|股权转让|要约"),
    ("CONTRACT", r"合同|中标|订单|框架协议"),
    ("DIVIDEND", r"分红|权益分派|利润分配|派息|送股|转增"),
    ("EARNINGS", r"业绩预告|业绩快报|年度报告|半年度报告|季度报告|年报|半年报|一季报|三季报"),
]
_POS = re.compile(r"预增|略增|续盈|扭亏|增持|回购|中标|分红")
_NEG = re.compile(r"预减|略减|首亏|续亏|预亏|减持|处罚|立案|警示|诉讼|退市|风险")


def classify_cn(title: str) -> str:
    for t, pat in _CN_RULES:
        if re.search(pat, title):
            return t
    return "OTHER"


def cn_sentiment(title: str) -> float | None:
    p, n = bool(_POS.search(title)), bool(_NEG.search(title))
    return 0.5 if p and not n else -0.5 if n and not p else None


def _uid(*parts) -> str:
    return "%08x" % (zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF) + "%08x" % (zlib.adler32("|".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------------
# A 股：巨潮资讯（官方公告）+ BaoStock（业绩预告 / 快报 / 分红）
# ---------------------------------------------------------------------------------

class CnEvents:
    def __init__(self, baostock_src=None):
        self.bs = baostock_src
        self.th_cninfo = throttle.get("cninfo")
        self._org: dict[str, str] = {}

    def _http(self, method: str, url: str, **kw):
        import requests

        r = requests.request(method, url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25, **kw)
        r.raise_for_status()
        return r.json()

    def _org_id(self, code: str) -> str | None:
        if code in self._org:
            return self._org[code]
        res = self.th_cninfo.call(self._http, "POST", "http://www.cninfo.com.cn/new/information/topSearch/query",
                                  data={"keyWord": code, "maxNum": 3})
        for r in res or []:
            if r.get("code") == code:
                self._org[code] = r["orgId"]
                return r["orgId"]
        return None

    def announcements(self, symbol: str, days: int = 120) -> list[dict]:
        ex, _, code = symbol.partition(".")
        org = self._org_id(code)
        if not org:
            return []
        end = date.today()
        res = self.th_cninfo.call(
            self._http, "POST", "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={"stock": f"{code},{org}", "tabName": "fulltext", "pageSize": 30, "pageNum": 1, "column": "sse" if ex == "sh" else "szse",
                  "category": "", "plate": "", "seDate": f"{(end - timedelta(days=days)).isoformat()}~{end.isoformat()}",
                  "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"})
        out = []
        for a in (res or {}).get("announcements") or []:
            title = re.sub(r"</?em>", "", a.get("announcementTitle") or "")
            ts = datetime.fromtimestamp(a["announcementTime"] / 1000, tz=timezone(timedelta(hours=8)))
            d = ts.date().isoformat()
            out.append({"event_type": classify_cn(title), "event_time": d, "publish_time": d, "source": "cninfo", "level": 1, "title": title,
                        "summary": "", "url": "http://static.cninfo.com.cn/" + (a.get("adjunctUrl") or ""), "sentiment": cn_sentiment(title)})
        return out

    def structured(self, symbol: str) -> list[dict]:
        """BaoStock：业绩预告 / 业绩快报 / 分红（带公告日，点时可用）。"""
        if self.bs is None:
            return []
        out = []
        start = (date.today() - timedelta(days=400)).isoformat()
        end = date.today().isoformat()
        try:
            f = self.bs._query("query_forecast_report", code=symbol, start_date=start, end_date=end)
            for r in f.itertuples():
                title = f"业绩预告（{r.profitForcastExpStatDate}）：{r.profitForcastType}"
                out.append({"event_type": "EARNINGS", "event_time": r.profitForcastExpPubDate, "publish_time": r.profitForcastExpPubDate,
                            "source": "baostock-forecast", "level": 2, "title": title, "summary": r.profitForcastAbstract or "", "url": None,
                            "sentiment": cn_sentiment(title)})
        except Exception:  # noqa: BLE001
            pass
        try:
            e = self.bs._query("query_performance_express_report", code=symbol, start_date=start, end_date=end)
            for r in e.itertuples():
                out.append({"event_type": "EARNINGS", "event_time": r.performanceExpPubDate, "publish_time": r.performanceExpPubDate,
                            "source": "baostock-express", "level": 2, "title": f"业绩快报（{r.performanceExpStatDate}）", "summary": "", "url": None, "sentiment": None})
        except Exception:  # noqa: BLE001
            pass
        try:
            for year in (date.today().year - 1, date.today().year):
                d = self.bs._query("query_dividend_data", code=symbol, year=str(year), yearType="report")
                for r in d.itertuples():
                    pub = getattr(r, "dividPreNoticeDate", "") or getattr(r, "dividAgmPumDate", "") or getattr(r, "dividPlanAnnounceDate", "")
                    if not pub:
                        continue
                    amt = getattr(r, "dividCashPsBeforeTax", "") or ""
                    out.append({"event_type": "DIVIDEND", "event_time": getattr(r, "dividOperateDate", "") or pub, "publish_time": pub,
                                "source": "baostock-dividend", "level": 2, "title": f"分红方案（{year}）：每股派现税前 {amt or '—'} 元",
                                "summary": "", "url": None, "sentiment": 0.3})
        except Exception:  # noqa: BLE001
            pass
        return [o for o in out if o["event_time"]]

    def fetch(self, symbol: str) -> list[dict]:
        out = []
        for fn in (self.announcements, self.structured):
            try:
                out += fn(symbol)
            except throttle.CircuitOpen:
                raise
            except Exception:  # noqa: BLE001 - 单个来源失败不影响其他来源
                continue
        return out


# ---------------------------------------------------------------------------------
# 美股：SEC EDGAR（官方披露）+ yfinance（新闻 / Insider）
# ---------------------------------------------------------------------------------

SEC_UA = f"stock-app personal research (contact: {_EMAIL})"     # SEC 要求带联系方式；邮箱只在 server/__init__.py 写一次
_FORM_TYPE = {"8-K": "OTHER", "10-K": "EARNINGS", "10-Q": "EARNINGS", "4": "INSIDER", "3": "INSIDER", "5": "INSIDER", "144": "INSIDER",
              "SC 13D": "INSIDER", "SC 13G": "INSIDER", "SC 13D/A": "INSIDER", "SC 13G/A": "INSIDER", "S-1": "OTHER", "424B5": "OTHER"}
_8K_ITEMS = {"1.01": ("CONTRACT", "签订重大协议"), "1.02": ("CONTRACT", "终止重大协议"), "2.01": ("M_AND_A", "完成收购 / 处置资产"),
             "2.02": ("EARNINGS", "经营业绩 / 财务状况"), "2.05": ("OTHER", "退出 / 重组成本"), "3.01": ("REGULATORY", "退市 / 上市标准通知"),
             "4.02": ("REGULATORY", "不应再依赖已发布的财务报表"), "5.02": ("OTHER", "董事 / 高管变动"), "7.01": ("OTHER", "Reg FD 披露"),
             "8.01": ("OTHER", "其他事项"), "9.01": ("OTHER", "财务报表与附件")}


class UsEvents:
    def __init__(self, us_src=None):
        self.us = us_src
        self.th_sec = throttle.get("sec")
        self._cik: dict[str, int] | None = None

    def _get(self, url: str):
        import requests

        r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=30)
        r.raise_for_status()
        return r.json()

    def cik(self, symbol: str) -> int | None:
        if self._cik is None:
            data = self.th_sec.call(self._get, "https://www.sec.gov/files/company_tickers.json")
            self._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        return self._cik.get(symbol.replace("-", ".").upper()) or self._cik.get(symbol.upper())

    def filings(self, symbol: str, days: int = 120) -> list[dict]:
        cik = self.cik(symbol)
        if not cik:
            return []
        j = self.th_sec.call(self._get, f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        rec = j["filings"]["recent"]
        since = (date.today() - timedelta(days=days)).isoformat()
        out = []
        for i, form in enumerate(rec["form"]):
            fd = rec["filingDate"][i]
            if fd < since:
                continue
            if form not in _FORM_TYPE:
                continue
            etype, label = _FORM_TYPE[form], rec["primaryDocDescription"][i] or form
            items = (rec.get("items") or [""] * len(rec["form"]))[i] or ""
            if form == "8-K" and items:
                codes = [c.strip() for c in items.split(",") if c.strip()]
                for c in codes:
                    if c in _8K_ITEMS and _8K_ITEMS[c][0] != "OTHER":
                        etype = _8K_ITEMS[c][0]
                        break
                label = "8-K：" + "；".join(f"{c} {_8K_ITEMS.get(c, ('', '其他'))[1]}" for c in codes[:3])
            acc = rec["accessionNumber"][i].replace("-", "")
            doc = rec["primaryDocument"][i]
            pub = rec.get("acceptanceDateTime", [""] * len(rec["form"]))[i] or fd
            out.append({"event_type": etype, "event_time": rec["reportDate"][i] or fd, "publish_time": pub, "source": "sec", "level": 1,
                        "title": f"{form} {label}".strip(), "summary": "", "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}",
                        "sentiment": None})
        return out

    def yahoo(self, symbol: str) -> list[dict]:
        if self.us is None:
            return []
        out = []
        for n in self.us.news(symbol):
            d = (n["publish_time"] or "")[:10]
            if d:
                out.append({"event_type": "NEWS", "event_time": d, "publish_time": n["publish_time"], "source": n["source"] or "Yahoo Finance",
                            "level": 3, "title": n["title"], "summary": n["summary"], "url": n["url"], "sentiment": None})
        for t in self.us.insider_transactions(symbol)[:15]:
            if not t["date"]:
                continue
            txt = (t["text"] or "").strip()
            low = txt.lower()
            sent = 0.5 if "purchase" in low or "buy" in low else -0.2 if "sale" in low or "sell" in low else None
            sh = f"{int(t['shares']):,} 股" if t.get("shares") == t.get("shares") and t.get("shares") else ""
            out.append({"event_type": "INSIDER", "event_time": t["date"], "publish_time": t["date"], "source": "yfinance", "level": 2,
                        "title": f"Insider：{t['insider']}（{t['position'] or '—'}） {txt or '交易'} {sh}".strip(), "summary": "",
                        "url": None, "sentiment": sent})
        return out

    def fetch(self, symbol: str) -> list[dict]:
        out = []
        for fn in (self.filings, self.yahoo):
            try:
                out += fn(symbol)
            except throttle.CircuitOpen:
                raise
            except Exception:  # noqa: BLE001
                continue
        return out


class DemoEvents:
    """演示事件源（合成，仅用于离线演示与测试）。"""

    def __init__(self, market: str, end: date | None = None):
        self.market, self.end = market, end or date.today()

    def fetch(self, symbol: str) -> list[dict]:
        rng = zlib.crc32(symbol.encode())
        out = []
        for i, (etype, title, level, sent) in enumerate([("NEWS", "演示：公司发布新产品", 3, 0.3), ("EARNINGS", "演示：业绩预告 预增", 2, 0.5),
                                                         ("REGULATORY", "演示：收到监管问询函", 1, -0.5), ("INSIDER", "演示：高管减持计划", 2, -0.2)]):
            d = (self.end - timedelta(days=3 + (rng % 5) + i * 9)).isoformat()
            out.append({"event_type": etype, "event_time": d, "publish_time": d, "source": "demo", "level": level, "title": f"{symbol} {title}",
                        "summary": "合成演示事件", "url": None, "sentiment": sent})
        return out


def get_provider(market: str, src):
    from . import markets

    if markets.is_demo(market):
        return DemoEvents(market, getattr(src, "end", None))
    return CnEvents(src) if market == "CN" else UsEvents(src)


def make_row(symbol: str, market: str, e: dict) -> tuple:
    uid = _uid(symbol, e["source"], e["event_type"], e["event_time"], e["title"])
    return (uid, symbol, market, e["event_time"], e.get("publish_time"), datetime.now().isoformat(timespec="seconds"), e["event_type"], e["source"],
            e.get("level"), e["title"], e.get("summary") or "", e.get("url"), e.get("sentiment"))
