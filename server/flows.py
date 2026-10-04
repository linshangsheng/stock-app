"""资金与情绪数据（3.20，M6）。**原始成交数据优先于第三方加工后的「主力资金流入 / 流出」**——这里只取交易所 / 券商披露的原始数据：
  * A 股：融资融券余额（融资余额 / 融资买入额 / 融券余量）、龙虎榜上榜记录（原因、买卖额）、北向资金持股（季度披露）；
  * 美股：做空数据（Short Interest / 占流通盘比例 / 回补天数）、期权 Put/Call 成交量与持仓量比、机构持股比例。
来源：东财数据中心网页接口（M0 实测：本机可直连，但响应慢，约 20 秒/次，故按需加载 + 缓存 12 小时；AkShare 封装层在本机不可用）。
这些数据只用于展示 / 参考，不进入回测特征（没有可靠的历史点时序列，避免未来函数，3.23）。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import db, throttle

_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Referer": "https://data.eastmoney.com/"}


def _dc(report: str, flt: str, sort: str, n: int) -> list[dict]:
    import requests

    th = throttle.get("eastmoney")

    def once():
        r = requests.get(_DC, params={"reportName": report, "columns": "ALL", "pageNumber": 1, "pageSize": n, "source": "WEB", "client": "WEB",
                                      "filter": flt, "sortColumns": sort, "sortTypes": "-1"}, headers=_H, timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("success") is False and "繁忙" in str(j.get("message")):
            raise throttle.RateLimited("东财数据中心繁忙")           # 限流 / 繁忙：退避重试，不当作「无数据」
        return (j.get("result") or {}).get("data") or []

    return th.call(once, retries=2)


def _d(v) -> str | None:
    return str(v)[:10] if v else None


def cn_flows(symbol: str) -> dict:
    code = symbol.split(".")[-1]
    since = (date.today() - timedelta(days=120)).isoformat()
    with ThreadPoolExecutor(max_workers=3) as ex:             # 三个接口各约 20 秒：并行取，总耗时 ≈ 一次
        f_m = ex.submit(_dc, "RPTA_WEB_RZRQ_GGMX", f'(SCODE="{code}")', "DATE", 20)
        f_l = ex.submit(_dc, "RPT_DAILYBILLBOARD_DETAILS", f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{since}\')', "TRADE_DATE", 20)
        f_n = ex.submit(_dc, "RPT_MUTUAL_HOLDSTOCKNORTH_STA", f'(SECURITY_CODE="{code}")', "TRADE_DATE", 8)
        res = {}
        for k, f in (("margin", f_m), ("billboard", f_l), ("north", f_n)):
            try:
                res[k] = f.result()
            except throttle.CircuitOpen:
                raise
            except Exception:  # noqa: BLE001 - 单个接口失败不影响其他
                res[k] = None
    margin = [{"date": _d(r["DATE"]), "rz_balance": r.get("RZYE"), "rz_buy": r.get("RZMRE"), "rz_repay": r.get("RZCHE"), "rz_net_buy": r.get("RZJME"),
               "rq_volume": r.get("RQYL"), "rzrq_balance": r.get("RZRQYE"), "rz_pct_float": r.get("RZYEZB")} for r in (res["margin"] or [])]
    board = [{"date": _d(r["TRADE_DATE"]), "reason": r.get("EXPLAIN") or r.get("EXPLANATION"), "net_buy": r.get("BILLBOARD_NET_AMT"),
              "buy": r.get("BILLBOARD_BUY_AMT"), "sell": r.get("BILLBOARD_SELL_AMT"), "deal_amt": r.get("BILLBOARD_DEAL_AMT"),
              "change_pct": r.get("CHANGE_RATE"), "close": r.get("CLOSE_PRICE")} for r in (res["billboard"] or [])]
    north = [{"date": _d(r["TRADE_DATE"]), "hold_shares": r.get("HOLD_SHARES"), "hold_value": r.get("HOLD_MARKET_CAP"), "ratio_a": r.get("A_SHARES_RATIO"),
              "chg_value_5d": r.get("HOLD_MARKETCAP_CHG5")} for r in (res["north"] or [])]
    return {"symbol": symbol, "market": "CN", "currency": "CNY", "margin": margin, "billboard": board, "north": north,
            "failed": [k for k, v in res.items() if v is None],
            "notes": ["融资融券 / 龙虎榜为交易所披露的原始数据（等级 2）。",
                      "北向资金：自 2024-08 起沪深股通改为季度披露，个股持股为最近一次季度披露，且全市场日度净流入已不再披露。",
                      "仅供参考，不进入回测特征。"]}


def us_flows(us, symbol: str) -> dict:
    from .datasource_us import to_yahoo

    t = us.yf.Ticker(to_yahoo(symbol))
    info = us.th.call(lambda: t.info) or {}
    short = {"shares_short": info.get("sharesShort"), "short_pct_float": info.get("shortPercentOfFloat"), "short_ratio_days": info.get("shortRatio"),
             "date_short_interest": _d(datetime.fromtimestamp(info["dateShortInterest"]).date()) if info.get("dateShortInterest") else None,
             "held_pct_institutions": info.get("heldPercentInstitutions"), "held_pct_insiders": info.get("heldPercentInsiders")}
    opt = None
    try:
        exps = us.th.call(lambda: t.options, retries=1) or []
        pv = cv = po = co = 0.0
        used = []
        for e in exps[:2]:                                      # 最近两个到期日
            ch = us.th.call(lambda e=e: t.option_chain(e), retries=1)
            pv += float(ch.puts["volume"].fillna(0).sum()); cv += float(ch.calls["volume"].fillna(0).sum())
            po += float(ch.puts["openInterest"].fillna(0).sum()); co += float(ch.calls["openInterest"].fillna(0).sum())
            used.append(e)
        if used:
            opt = {"expiries": used, "put_volume": pv, "call_volume": cv, "put_oi": po, "call_oi": co,
                   "pc_volume_ratio": pv / cv if cv else None, "pc_oi_ratio": po / co if co else None}
    except throttle.CircuitOpen:
        raise
    except Exception:  # noqa: BLE001 - 无期权的股票
        opt = None
    return {"symbol": symbol, "market": "US", "currency": "USD", "short": short, "options": opt,
            "notes": ["Short Interest 每半月披露一次（有滞后）；Put/Call 为最近两个到期日的成交量 / 持仓量比（盘后快照）。", "仅供参考，不进入回测特征。"]}


def demo_flows(symbol: str, market: str) -> dict:
    import zlib
    h = zlib.crc32(symbol.encode())
    today = date.today()
    if market == "US":
        return {"symbol": symbol, "market": "US", "currency": "USD", "short": {"shares_short": 1e6 + h % 5e6, "short_pct_float": 0.01 + (h % 20) / 200,
                "short_ratio_days": 1 + (h % 40) / 10, "date_short_interest": today.isoformat(), "held_pct_institutions": 0.6, "held_pct_insiders": 0.02},
                "options": {"expiries": [today.isoformat()], "put_volume": 1000, "call_volume": 1800, "put_oi": 9000, "call_oi": 12000,
                            "pc_volume_ratio": 0.55, "pc_oi_ratio": 0.75}, "notes": ["合成演示数据。"]}
    return {"symbol": symbol, "market": "CN", "currency": "CNY",
            "margin": [{"date": (today - timedelta(days=i)).isoformat(), "rz_balance": 1e9 + i * 1e6, "rz_buy": 1e8, "rz_repay": 9e7, "rz_net_buy": 1e7,
                        "rq_volume": 10000, "rzrq_balance": 1.01e9, "rz_pct_float": 3.0} for i in range(5)],
            "billboard": [{"date": (today - timedelta(days=9)).isoformat(), "reason": "演示：日涨幅偏离值达 7%", "net_buy": 5e7, "buy": 2e8, "sell": 1.5e8,
                           "deal_amt": 8e8, "change_pct": 9.9, "close": 10.0}],
            "north": [{"date": "2026-06-30", "hold_shares": 5e7, "hold_value": 6e10, "ratio_a": 4.3, "chg_value_5d": None}], "failed": [],
            "notes": ["合成演示数据。"]}


def get_flows(conn, market: str, symbol: str, src, force: bool = False) -> dict:
    """缓存 12 小时（东财数据中心响应慢，约 20 秒/次）。"""
    from . import markets

    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"flow:{symbol}",)).fetchone()
    if row and not force:
        d = json.loads(row[0])
        try:
            if datetime.now() - datetime.fromisoformat(d["fetched_at"]) < timedelta(hours=12):
                return d
        except (KeyError, ValueError):
            pass
    d = demo_flows(symbol, market) if markets.is_demo(market) else (cn_flows(symbol) if market == "CN" else us_flows(src, symbol))
    d["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    if not d.get("failed"):                                   # 有接口失败时不缓存，下次重试
        db.set_meta(conn, f"flow:{symbol}", json.dumps(d, ensure_ascii=False, default=str))
    return d
