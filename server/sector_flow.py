"""「资金流向」：某一日（或近 5 / 20 天累计）资金流入哪些板块、流出哪些板块。**估算，不是主力资金。**

交易所没有公开「谁买谁卖」，同花顺 / 东方财富的「主力净流入」是用逐笔成交按单子大小拆出来的；本机没有这类数据。
这里只用每天的成交额和涨跌估算，口径写死、可解释：
  · 估算净流入 = 板块里「上涨股票的成交额合计 − 下跌股票的成交额合计」（平盘不计）；窗口 > 1 天时逐日累加
  · 净流入强度 = 估算净流入 ÷ 板块成交额（−100% ~ +100%）：消除板块大小的影响，小板块也能进前列——这是默认的排序依据
  · 成交额占比变化 = 板块成交额占全市场的比例，减去「窗口之前 20 个交易日」的平均占比（百分点）：钱有没有往这个板块集中
它反映的是方向和相对强弱，不是真实买卖金额。展示用，不进入选股与回测，没有证据说它能提高胜率。
板块：A 股 = 证监会行业分类（83 个，BaoStock 只提供这一种）；美股 = 细分行业（约 140 个）或 11 个大板块。少于 5 只股票的板块不统计（只数太少，方向比例没有意义）。"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np
import pandas as pd

from . import db, market_calendar as mc, settings

MIN_STOCKS = 5
BASE_DAYS = 20                                  # 占比变化的对照：窗口之前的 20 个交易日
WINDOWS = (1, 5, 20)
_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_lock = threading.Lock()

# 证监会行业代码 → 短名（圆圈里放不下长名字；完整名字在鼠标悬停和详情里）
CN_SHORT = {
    "A01": "农业", "A02": "林业", "A03": "畜牧", "A04": "渔业", "A05": "农林服务", "B06": "煤炭", "B07": "油气开采", "B08": "黑色矿采", "B09": "有色矿采",
    "B10": "非金属矿采", "B11": "采矿服务", "C13": "农副加工", "C14": "食品", "C15": "酒饮", "C17": "纺织", "C18": "服装", "C19": "皮革鞋", "C20": "木材",
    "C21": "家具", "C22": "造纸", "C23": "印刷", "C24": "文教娱乐", "C25": "油煤加工", "C26": "化工", "C27": "医药", "C28": "化纤", "C29": "橡塑",
    "C30": "建材", "C31": "钢铁", "C32": "有色冶炼", "C33": "金属制品", "C34": "通用设备", "C35": "专用设备", "C36": "汽车", "C37": "运输设备",
    "C38": "电气设备", "C39": "电子", "C40": "仪器仪表", "C41": "其他制造", "C42": "资源回收", "C43": "设备修理", "D44": "电力热力", "D45": "燃气",
    "D46": "水务", "E47": "房建", "E48": "土木工程", "E49": "建筑安装", "E50": "建筑装饰", "F51": "批发", "F52": "零售", "G53": "铁路", "G54": "公路",
    "G55": "水运", "G56": "航空", "G58": "多式联运", "G59": "仓储", "G60": "邮政", "H61": "住宿", "H62": "餐饮", "I63": "电信传媒", "I64": "互联网",
    "I65": "软件信息", "J66": "银行", "J67": "证券", "J68": "保险", "J69": "其他金融", "K70": "房地产", "L71": "租赁", "L72": "商务服务", "M73": "研发",
    "M74": "专业技术", "M75": "科技推广", "N76": "水利", "N77": "环保", "N78": "公共设施", "O81": "修理服务", "P83": "教育", "Q84": "卫生",
    "R86": "新闻出版", "R87": "影视", "R88": "文化艺术", "R89": "体育", "S91": "综合",
}

# 美股行业 / 板块（雅虎分类）→ 中文短名：圆里放英文长名字会被截断、也和 A 股的中文名不统一；完整英文名在悬停和详情里
US_ZH = {
    "Agricultural Inputs": "农业投入品", "Building Materials": "建筑材料", "Chemicals": "化学品", "Copper": "铜", "Gold": "黄金", "Other Industrial Metals & Mining": "工业金属",
    "Other Precious Metals & Mining": "贵金属", "Specialty Chemicals": "特种化工", "Steel": "钢铁", "Advertising Agencies": "广告", "Entertainment": "娱乐",
    "Internet Content & Information": "互联网内容", "Publishing": "出版", "Telecom Services": "电信", "Apparel Manufacturing": "服装制造", "Apparel Retail": "服装零售",
    "Auto & Truck Dealerships": "汽车经销", "Auto Manufacturers": "整车", "Auto Parts": "汽车零件", "Footwear & Accessories": "鞋类配饰", "Furnishings, Fixtures & Appliances": "家居家电",
    "Gambling": "博彩", "Internet Retail": "网络零售", "Leisure": "休闲", "Lodging": "住宿", "Luxury Goods": "奢侈品", "Packaging & Containers": "包装", "Personal Services": "个人服务",
    "Recreational Vehicles": "休闲车", "Residential Construction": "住宅建筑", "Resorts & Casinos": "度假赌场", "Restaurants": "餐饮", "Specialty Retail": "专业零售",
    "Travel Services": "旅游服务", "Beverages—Brewers": "啤酒", "Beverages—Non-Alcoholic": "软饮", "Discount Stores": "折扣店", "Education & Training Services": "教育培训",
    "Farm Products": "农产品", "Food Distribution": "食品分销", "Grocery Stores": "超市", "Household & Personal Products": "日化个护", "Packaged Foods": "包装食品", "Tobacco": "烟草",
    "Oil & Gas Drilling": "油气钻探", "Oil & Gas E&P": "油气开采", "Oil & Gas Equipment & Services": "油服设备", "Oil & Gas Integrated": "综合油气", "Oil & Gas Midstream": "油气中游",
    "Oil & Gas Refining & Marketing": "炼油销售", "Uranium": "铀", "Asset Management": "资产管理", "Banks—Diversified": "综合银行", "Banks—Regional": "区域银行",
    "Capital Markets": "资本市场", "Credit Services": "信贷服务", "Financial Data & Stock Exchanges": "金融数据", "Insurance Brokers": "保险经纪", "Insurance—Diversified": "综合保险",
    "Insurance—Life": "寿险", "Insurance—Property & Casualty": "财产险", "Insurance—Reinsurance": "再保险", "Insurance—Specialty": "专业保险", "Biotechnology": "生物科技",
    "Diagnostics & Research": "诊断研究", "Drug Manufacturers—General": "大型药企", "Drug Manufacturers—Specialty & Generic": "特药仿制药", "Health Information Services": "健康信息",
    "Healthcare Plans": "医保计划", "Medical Care Facilities": "医疗机构", "Medical Devices": "医疗器械", "Medical Instruments & Supplies": "医疗耗材", "Aerospace & Defense": "航空国防",
    "Airlines": "航空公司", "Building Products & Equipment": "建筑产品", "Conglomerates": "综合企业", "Consulting Services": "咨询服务", "Electrical Equipment & Parts": "电气设备",
    "Engineering & Construction": "工程建设", "Farm & Heavy Construction Machinery": "重型机械", "Industrial Distribution": "工业分销", "Integrated Freight & Logistics": "货运物流",
    "Marine Shipping": "海运", "Metal Fabrication": "金属加工", "Railroads": "铁路", "Rental & Leasing Services": "租赁", "Security & Protection Services": "安保服务",
    "Specialty Business Services": "商务服务", "Specialty Industrial Machinery": "工业机械", "Staffing & Employment Services": "人力资源", "Tools & Accessories": "工具配件",
    "Trucking": "卡车运输", "Waste Management": "废物处理", "REIT—Diversified": "综合REIT", "REIT—Healthcare Facilities": "医疗REIT", "REIT—Hotel & Motel": "酒店REIT",
    "REIT—Industrial": "工业REIT", "REIT—Mortgage": "抵押REIT", "REIT—Office": "办公REIT", "REIT—Residential": "住宅REIT", "REIT—Retail": "零售REIT", "REIT—Specialty": "特种REIT",
    "Real Estate Services": "房地产服务", "Communication Equipment": "通信设备", "Computer Hardware": "计算机硬件", "Electronic Components": "电子元件", "Electronics & Computer Distribution": "电子分销",
    "Information Technology Services": "IT服务", "Scientific & Technical Instruments": "科学仪器", "Semiconductor Equipment & Materials": "半导体设备", "Semiconductors": "半导体",
    "Software—Application": "应用软件", "Software—Infrastructure": "基础软件", "Solar": "太阳能", "Utilities—Diversified": "综合公用", "Utilities—Independent Power Producers": "独立发电",
    "Utilities—Regulated Electric": "电力", "Utilities—Regulated Gas": "燃气", "Utilities—Regulated Water": "水务", "Utilities—Renewable": "可再生能源",
    # 11 个大板块
    "Technology": "科技", "Healthcare": "医疗保健", "Financial Services": "金融", "Consumer Cyclical": "可选消费", "Consumer Defensive": "必选消费", "Industrials": "工业",
    "Energy": "能源", "Utilities": "公用事业", "Real Estate": "房地产", "Basic Materials": "基础材料", "Communication Services": "通信服务",
}


def short_name(market: str, name: str) -> str:
    if market == "CN":
        code = name[:3]
        if code in CN_SHORT:
            return CN_SHORT[code]
        return name.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")[:4] or name
    return US_ZH.get(name) or name.split("—")[0].split(" & ")[0].strip()[:12]


def _level_col(market: str, level: str) -> str:
    return "sector" if (market == "US" and level == "sector") else "industry"


def _load(conn, market: str, level: str, day: str, window: int):
    """取窗口 + 对照期的日线，算每只股票每天的涨跌与方向成交额。返回 (日线表, 窗口内的交易日, 对照期的交易日)。"""
    tdays = mc.trading_days(conn, None, day)
    if day not in tdays:
        return None
    need = window + BASE_DAYS + 1
    seg = tdays[-need:]
    win, base = seg[-window:], seg[:-window]
    base = base[-BASE_DAYS:]
    col = _level_col(market, level)
    df = pd.read_sql_query(
        "SELECT d.symbol, d.date, d.close, d.adj_factor, d.adj_close, d.amount, d.trade_status, m." + col + " AS grp, s.name FROM daily_bar d "
        "JOIN industry_map m ON m.symbol = d.symbol JOIN securities s ON s.symbol = d.symbol WHERE d.date >= ? AND d.date <= ? AND m." + col + " IS NOT NULL",
        conn, params=(seg[0], day))
    df["px"] = np.where(df["adj_close"].notna(), df["adj_close"], df["close"] * df["adj_factor"])
    df = df.sort_values(["symbol", "date"])
    df["ret"] = df.groupby("symbol")["px"].pct_change()
    df = df[(df["trade_status"] == 1) & (df["amount"] > 0)]
    return df, win, base


def build(market: str, day: str | None = None, window: int = 1, level: str = "industry") -> dict:
    window = window if window in WINDOWS else 1
    with settings.market_ctx(market), db.market_db(market) as conn:
        last = db.get_meta(conn, "data_asof") or mc.last_closed_trading_day(conn, market)
        if not last:
            return {"status": "no_data", "message": "尚无行情数据，请先初始化数据"}
        tdays = mc.trading_days(conn, None, last)
        day = day if day and day <= last else last
        if day not in tdays:                                              # 选的不是交易日：取之前最近的交易日
            earlier = [d for d in tdays if d <= day]
            day = earlier[-1] if earlier else tdays[0]
        key = (market, day, window, level, last)
        with _lock:
            if key in _cache:
                return _cache[key]
        got = _load(conn, market, level, day, window)
        if got is None or got[0].empty:
            return {"status": "no_data", "message": f"{day} 没有足够的日线数据"}
        df, win, base = got
        w = df[df["date"].isin(win) & df["ret"].notna()].copy()
        if w.empty:
            return {"status": "no_data", "message": f"{day} 没有足够的日线数据"}
        w["net"] = w["amount"] * np.sign(w["ret"])
        cum = w.groupby("symbol")["ret"].apply(lambda r: float((1 + r).prod() - 1)).rename("cum")
        per = w.groupby("symbol").agg(grp=("grp", "first"), net=("net", "sum"), amount=("amount", "sum")).join(cum)
        g = per.groupby("grp").agg(n=("amount", "size"), amount=("amount", "sum"), net=("net", "sum"), ret=("cum", "mean"))
        g = g[g["n"] >= MIN_STOCKS]
        tot_amt = float(per["amount"].sum())
        g["strength"] = g["net"] / g["amount"]
        g["share"] = g["amount"] / tot_amt
        b = df[df["date"].isin(base)]
        if len(base) >= 5 and not b.empty:                                # 对照期：逐日占比的平均
            daily = b.groupby(["date", "grp"])["amount"].sum().unstack(fill_value=0.0)
            share_b = (daily.div(daily.sum(axis=1), axis=0)).mean()
            g["share_chg"] = g["share"] - share_b.reindex(g.index).fillna(0.0)
        else:
            g["share_chg"] = np.nan
        last_day = w[w["date"] == win[-1]]
        out = {"status": "ok", "day": day, "window": window, "level": level, "available_days": tdays[-250:],
               "window_days": win, "total": {"amount": tot_amt, "net": float(w["net"].sum()), "strength": float(w["net"].sum() / tot_amt),
                                             "n_stocks": int(per.shape[0]), "up": int((last_day["ret"] > 0).sum()), "down": int((last_day["ret"] < 0).sum())},
               "sectors": [{"key": k, "name": k, "short": short_name(market, k), "n": int(r.n), "amount": float(r.amount), "net": float(r.net),
                            "strength": float(r.strength), "share": float(r.share), "share_chg": None if r.share_chg != r.share_chg else float(r.share_chg),
                            "ret": float(r.ret)} for k, r in g.sort_values(["strength", "ret"], ascending=False).iterrows()],
               "note": ("估算净流入 = 板块里上涨股票的成交额 − 下跌股票的成交额（平盘不计；窗口多于一天时逐日累加）；净流入强度 = 估算净流入 ÷ 板块成交额。"
                        "这是用成交额和涨跌推算的，不是主力资金，和同花顺 / 东方财富的「主力净流入」数字不会一致；只反映方向和板块之间的相对强弱。")}
        with _lock:
            _cache[key] = out
            while len(_cache) > 24:
                _cache.popitem(last=False)
        return out


def stocks(market: str, day: str, window: int, key: str, level: str = "industry", top: int = 10) -> dict:
    """某个板块里，估算净流入最多 / 最少（流出最多）的股票。"""
    window = window if window in WINDOWS else 1
    with settings.market_ctx(market), db.market_db(market) as conn:
        got = _load(conn, market, level, day, window)
        if got is None:
            return {"status": "no_data", "inflow": [], "outflow": []}
        df, win, _ = got
        w = df[df["date"].isin(win) & df["ret"].notna() & (df["grp"] == key)].copy()
        if w.empty:
            return {"status": "no_data", "inflow": [], "outflow": []}
        w["net"] = w["amount"] * np.sign(w["ret"])
        cum = w.groupby("symbol")["ret"].apply(lambda r: float((1 + r).prod() - 1)).rename("cum")
        per = w.groupby("symbol").agg(name=("name", "first"), net=("net", "sum"), amount=("amount", "sum")).join(cum).reset_index()
        row = lambda r: {"symbol": r.symbol, "name": r.name, "net": float(r.net), "amount": float(r.amount), "ret": float(r.cum)}   # noqa: E731
        s = per.sort_values("net", ascending=False)
        return {"status": "ok", "key": key, "day": day, "window": window, "n": int(len(per)),
                "inflow": [row(r) for r in s.head(top).itertuples() if r.net > 0],
                "outflow": [row(r) for r in s.tail(top).iloc[::-1].itertuples() if r.net < 0]}
