"""数据完整性闸门（3.25.1）：每次数据更新后、扫描之前必须通过；不通过则不生成观察清单（宁可不出结果）。
只管「当日可信度」，不修改历史数据。"""
from __future__ import annotations

import pandas as pd

from . import db, market_calendar as mc, markets, settings


def check_gate(conn, market: str, day: str) -> dict:
    g = settings.cfg()["gate"]
    checks: list[dict] = []
    bad_symbols: set[str] = set()

    def add(name, ok, value, threshold, msg, hard=True):
        checks.append({"name": name, "ok": bool(ok), "value": value, "threshold": threshold, "msg": msg, "hard": hard})

    # 1. 当日覆盖率：L1 中「应有当日 K 线」的股票里已取得当日 K 线的比例
    exp = pd.read_sql_query(
        "SELECT s.symbol FROM securities s WHERE s.in_l1=1 "
        "AND (s.status!='delisted' OR (s.delist_date IS NOT NULL AND s.delist_date>=?)) "
        "AND EXISTS (SELECT 1 FROM daily_bar b WHERE b.symbol=s.symbol AND b.date<=?)", conn, params=(day, day))
    have = pd.read_sql_query("SELECT symbol FROM daily_bar WHERE date=?", conn, params=(day,))
    n_exp = len(exp)
    n_have = len(set(exp["symbol"]) & set(have["symbol"]))
    cov = (n_have / n_exp) if n_exp else 0.0
    add("coverage", cov >= g["min_coverage"], round(cov, 4), g["min_coverage"],
        f"当日 K 线覆盖 {n_have}/{n_exp}" + ("" if cov >= g["min_coverage"] else "，低于下限；失败名单进入降速补拉队列"))

    # 2. 基准指数
    need = markets.gate_benchmarks(market)               # 美股另含 VIX 与 SPY / QQQ / IWM（3.25.1）
    got = {r[0] for r in conn.execute("SELECT symbol FROM index_bar WHERE date=?", (day,)).fetchall()}
    miss = [s for s in need if s not in got]
    add("benchmark", not miss, len(need) - len(miss), len(need), "基准指数齐全" if not miss else f"缺少基准指数：{', '.join(miss)}")

    # 3. 价格合理性
    bars = pd.read_sql_query(
        "SELECT symbol,open,high,low,close,volume,amount,adj_factor,trade_status FROM daily_bar WHERE date=?",
        conn, params=(day,))
    live = bars[bars["trade_status"] > 0]
    bad = live[(live["high"] < live[["open", "close"]].max(axis=1) - 1e-6) |
               (live["low"] > live[["open", "close"]].min(axis=1) + 1e-6) |
               (live[["open", "high", "low", "close", "volume"]].lt(0).any(axis=1)) |
               (live[["open", "high", "low", "close"]].isna().any(axis=1))]
    bad_symbols |= set(bad["symbol"])
    # 涨跌幅超出涨跌停且当日无除权记录
    prev_day = conn.execute("SELECT MAX(date) FROM daily_bar WHERE date<?", (day,)).fetchone()[0]
    if prev_day:
        prev = pd.read_sql_query("SELECT symbol, close AS pc, adj_factor AS pf FROM daily_bar WHERE date=?",
                                 conn, params=(prev_day,))
        m = live.merge(prev, on="symbol")
        sec = pd.read_sql_query("SELECT symbol, board FROM securities", conn).set_index("symbol")["board"]
        if markets.has_price_limits(market):
            lim = m["symbol"].map(lambda s: 0.2 if sec.get(s) in ("chinext", "star") else 0.1)
        else:
            lim = pd.Series(0.4, index=m.index)                                   # 美股无涨跌停：用 ±50% 作为异常线（1.25×0.4+0.01）
        pct = (m["close"] / m["pc"] - 1).abs()
        exdiv = (m["adj_factor"] - m["pf"]).abs() > 1e-9                      # 当日有除权 / 因子变动
        over = m[(pct > lim * 1.25 + 0.01) & ~exdiv]
        bad_symbols |= set(over["symbol"])
    ratio = (len(bad_symbols) / len(live)) if len(live) else 0.0
    add("price_sanity", ratio <= g["max_anomaly_ratio"], round(ratio, 4), g["max_anomaly_ratio"],
        f"异常行 {len(bad_symbols)} 条（占 {ratio:.2%}），已排除出当日扫描" if bad_symbols else "价格与成交量合理")

    # 4. 日期连续性：近 60 个交易日相对交易日历无缺失
    cal = mc.trading_days(conn, None, day)[-60:]
    have_days = {r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_bar WHERE date>=?", (cal[0] if cal else day,)).fetchall()}
    missing = [d for d in cal if d not in have_days]
    add("continuity", not missing, len(cal) - len(missing), len(cal),
        "无缺失交易日" if not missing else f"缺失交易日 {missing[:5]}{'…' if len(missing) > 5 else ''}，需补跑")

    # 5. 快照对账（仅告警）
    rd = db.get_meta(conn, "recon_diff_rate")
    if rd is not None:
        add("snapshot_recon", float(rd) <= 0.05, float(rd), 0.05, f"临时数据与历史接口对账差异率 {float(rd):.2%}", hard=False)

    # 6. 单位 / 口径漂移（仅告警）
    amt = pd.read_sql_query("SELECT date, amount FROM daily_bar WHERE date>=? AND trade_status>0 AND amount>0",
                            conn, params=((cal[-21] if len(cal) >= 21 else (cal[0] if cal else day)),))
    if not amt.empty:
        med = amt.groupby("date")["amount"].median()
        if day in med.index and len(med) > 5:
            ref = med.drop(day).tail(20).median()
            r = float(med[day] / ref) if ref else 1.0
            lo, hi = g["volume_drift_range"]
            add("unit_drift", lo <= r <= hi, round(r, 3), f"{lo}~{hi}",
                "成交额量级正常" if lo <= r <= hi else "成交额中位数与前 20 日偏离过大，检查单位（手 / 股）映射", hard=False)

    ok = all(c["ok"] for c in checks if c["hard"])
    return {"status": "PASS" if ok else "INCOMPLETE", "day": day, "checks": checks,
            "bad_symbols": sorted(bad_symbols),
            "reasons": [c["msg"] for c in checks if not c["ok"]]}
