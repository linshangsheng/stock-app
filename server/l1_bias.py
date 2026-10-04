"""L1 快照偏差对比实验（3.26.1-6）。

L1 入库粗筛基于「入库当天」的价格 / 成交额：会把「过去正常、如今已跌成低价或低成交额」的股票筛掉——这类股票恰恰是趋势策略过去可能买错的样本，
回测会因此偏乐观。本实验：抽样一批被 L1 剔除（in_l1=0）的股票，补拉其 10 年历史，对**同一策略**比较「含 / 不含这些股票」的回测差异：
  * 差异显著 -> 说明 L1 过严，应放宽（代价是数据量上升，A 股全市场约 1.3~1.4 GB 量级，SQLite 可承受）；
  * 差异不显著 -> L1 粗筛对回测影响有限，可保留。
实验在**数据库副本**上进行，不改动正式库。"""
from __future__ import annotations

import json
import random
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import backtest, db, ingest, settings
from .datasource_cn import get_source


def _copy_db(market: str, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    src = db.db_path(db.MARKET_DB[market])
    con = sqlite3.connect(str(src))
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    dst = dst_dir / src.name
    for ext in ("", "-wal", "-shm"):
        (dst_dir / (src.name + ext)).unlink(missing_ok=True)
    shutil.copy2(src, dst)
    return dst


def run(market: str = "CN", sample: int = 100, seed: int = 1, start: str | None = None,
        progress=None, keep_copy: bool = False) -> dict:
    with settings.market_ctx(market):
        return _run(market, sample, seed, start, progress, keep_copy)


def _run(market, sample, seed, start, progress, keep_copy) -> dict:
    tmp = db.db_path("_l1_bias_tmp")
    with db.market_db(market) as c:
        excluded = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=0 AND status!='delisted' AND sec_type='stock'")]
    if not excluded:
        return {"error": "no_excluded", "message": "没有被 L1 剔除的股票（L1 未做粗筛，或尚未初始化），无需做偏差实验。"}
    picked = random.Random(seed).sample(excluded, min(sample, len(excluded)))
    pcopy = _copy_db(market, tmp)
    src = get_source(market)
    try:
        conn = db._connect(pcopy)
        conn.executescript(db.MARKET_SCHEMA)
        conn.executemany("UPDATE securities SET in_l1=1 WHERE symbol=?", [(s,) for s in picked])
        conn.execute("DELETE FROM fetch_state WHERE task='daily' AND symbol IN (%s)" % ",".join("?" * len(picked)), picked)
        conn.commit()
        fill = ingest.init_history(conn, src, market, progress=progress)
        got = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM daily_bar WHERE symbol IN (%s)" % ",".join("?" * len(picked)), picked)}
        base_ctx = _ctx(market, start, None)
        ext_ctx = _ctx(market, start, conn)
        base = base_ctx.run()
        ext = ext_ctx.run()
        # 抽样股票在窗口期内曾满足 L2 的数量（说明它们确实曾是「可交易」的样本）
        in_l2 = [s for s in got if s in ext_ctx.syms and bool(ext_ctx.l2[s].any())]
        tr_extra = [t for t in ext["trades"] if t["symbol"] in got]
        tr_base_like = [t for t in ext["trades"] if t["symbol"] not in got]

        def stats(ts):
            r = [t["r"] for t in ts if t.get("r") is not None]
            return {"n": len(ts), "expectancy_r": round(sum(r) / len(r), 3) if r else None,
                    "win_rate": round(sum(1 for t in ts if t["pnl"] > 0) / len(ts), 3) if ts else None}
        mb, me = base["metrics"], ext["metrics"]
        d_exp = (me.get("expectancy_r") or 0) - (mb.get("expectancy_r") or 0)
        d_dd = (me.get("max_drawdown") or 0) - (mb.get("max_drawdown") or 0)
        extra_s = stats(tr_extra)
        material = (abs(d_exp) > 0.10) or (extra_s["n"] >= 20 and extra_s["expectancy_r"] is not None
                                           and abs(extra_s["expectancy_r"] - (stats(tr_base_like)["expectancy_r"] or 0)) > 0.25)
        verdict = ("差异显著：被 L1 剔除的股票在回测里表现与其余股票不同，L1 粗筛使回测偏离——建议放宽 L1（取消价格 / 成交额粗筛，数据量上升，仍在 SQLite 能力范围内）。"
                   if material else "差异不显著：L1 粗筛对该策略回测影响有限，可保留；样本越大结论越可靠。")
        report = {"market": market, "run_at": datetime.now().isoformat(timespec="seconds"), "excluded_total": len(excluded), "sampled": len(picked),
                  "history_fetched": len(got), "sampled_ever_in_l2": len(in_l2), "fetch": fill,
                  "base": {k: mb.get(k) for k in ("n", "expectancy_r", "win_rate", "total_return", "cagr", "max_drawdown")},
                  "with_extra": {k: me.get(k) for k in ("n", "expectancy_r", "win_rate", "total_return", "cagr", "max_drawdown")},
                  "delta_expectancy_r": round(d_exp, 3), "delta_max_drawdown": round(d_dd, 4),
                  "trades_in_sampled_stocks": extra_s, "trades_in_other_stocks": stats(tr_base_like), "material": bool(material), "verdict": verdict,
                  "note": "抽样量小时结论只作参考；偏差来自「入库当天的快照条件」，退市股已一律入库、不在此列。"}
        conn.close()
    finally:
        if hasattr(src, "close"):
            src.close()
        if not keep_copy:
            shutil.rmtree(tmp, ignore_errors=True)
    with db.market_db(market) as c:
        db.set_meta(c, "l1_bias_report", json.dumps(report, ensure_ascii=False))
    return report


def _ctx(market: str, start: str | None, conn):
    if conn is not None:
        return backtest.BtContext(conn, market, start, None)
    with db.market_db(market) as c:
        return backtest.BtContext(c, market, start, None)


def last_report(market: str = "CN") -> dict | None:
    with db.market_db(market) as c:
        v = db.get_meta(c, "l1_bias_report")
    return json.loads(v) if v else None
