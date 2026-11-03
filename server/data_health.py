"""数据健康面板：一眼分清「是数据的问题，还是策略的问题」。
把已有的信息（数据完整性闸门、任务日志、上游限速 / 熔断、备份）和新算的（行情新鲜度、当日覆盖与缺失名单、各数据源的成功率）
汇成一份报告：顶部一个结论（正常 / 需要留意 / 有问题）+ 逐条「发现了什么、怎么办」。只读，不修改任何数据。"""
from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime

from . import backup, db, market_calendar as mc, markets, quality, settings, source_stats, throttle

# 数据源：内部名 → (显示名, 用途, 属于哪个市场)
SOURCES = {
    "baostock": ("BaoStock", "A 股日线 / 交易日历 / 财务", "CN"),
    "tencent": ("腾讯（批量报价）", "A 股每日收盘批量报价（盘后更新的主路径）", "CN"),
    "tencent_hist": ("腾讯（历史）", "A 股日线备用", "CN"),
    "eastmoney_hist": ("东方财富（历史）", "A 股日线备用", "CN"),
    "eastmoney": ("东方财富（网页）", "全市场快照备用 / 资金数据", "CN"),
    "cninfo": ("巨潮资讯", "A 股公告 / 财报日期", "CN"),
    "yfinance": ("Yahoo Finance", "美股日线 / 指数 / 新闻", "US"),
    "sec": ("SEC EDGAR", "美股公告 / 内部人交易", "US"),
}
LEVEL_RANK = {"ok": 0, "info": 1, "warn": 2, "bad": 3}
JOB_NAME = {"init": "补全 / 初始化", "daily": "盘后任务链", "catch_up": "自动更新（补跑）", "backup": "备份", "gate": "数据完整性闸门", "ready": "上游数据就绪"}


def rss_mb() -> float | None:
    """本进程当前占用的内存（MB）。优先 psutil；没有就在 Windows 用系统接口、在 Linux 读 /proc。取不到返回 None。"""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            ok = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return round(pmc.WorkingSetSize / 1048576, 1) if ok else None
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _freshness(c, market: str, asof: str | None) -> dict:
    last_closed = mc.last_closed_trading_day(c, market) if mc.trading_days(c) else None
    behind = None
    if asof and last_closed:
        behind = len([d for d in mc.trading_days(c, asof, last_closed) if asof < d <= last_closed])
    bench = []
    for sym in markets.gate_benchmarks(market):
        r = c.execute("SELECT MAX(date) FROM index_bar WHERE symbol=?", (sym,)).fetchone()
        last = r[0] if r else None
        bench.append({"symbol": sym, "last": last, "lag": bool(asof and last and last < asof)})
    return {"data_asof": asof, "last_closed": last_closed, "behind_days": behind, "calendar_source": db.get_meta(c, "calendar_source"),
            "industry_map_asof": db.get_meta(c, "industry_map_asof"), "benchmarks": bench,
            "benchmarks_lagging": [b["symbol"] for b in bench if b["lag"]]}


def _coverage(c, market: str, asof: str | None, limit: int = 40) -> dict:
    """当日覆盖：「应该有当日 K 线」的股票里，哪些没拿到；按落后几个交易日分类。口径与完整性闸门一致。"""
    if not asof:
        return {"expected": 0, "have": 0, "missing": 0, "rate": None, "recent": [], "long_stale": 0, "suspended": 0}
    exp = c.execute(
        "SELECT s.symbol, s.name FROM securities s WHERE s.in_l1=1 AND (s.status!='delisted' OR (s.delist_date IS NOT NULL AND s.delist_date>=?)) "
        "AND EXISTS (SELECT 1 FROM daily_bar b WHERE b.symbol=s.symbol AND b.date<=?)", (asof, asof)).fetchall()
    have = {r[0] for r in c.execute("SELECT symbol FROM daily_bar WHERE date=?", (asof,)).fetchall()}
    susp = c.execute("SELECT COUNT(*) FROM daily_bar WHERE date=? AND trade_status=0", (asof,)).fetchone()[0]
    missing = [(r[0], r[1]) for r in exp if r[0] not in have]
    days = mc.trading_days(c, None, asof)
    pos = {d: i for i, d in enumerate(days)}
    last_i = len(days) - 1
    rows = []
    for sym, name in missing:
        r = c.execute("SELECT MAX(date) FROM daily_bar WHERE symbol=? AND date<=?", (sym, asof)).fetchone()
        last = r[0] if r else None
        gap = (last_i - pos[last]) if last in pos else None
        rows.append({"symbol": sym, "name": name or sym, "last": last, "gap": gap})
    recent = sorted([x for x in rows if x["gap"] is not None and x["gap"] <= 20], key=lambda x: (x["gap"], x["symbol"]))
    long_stale = len(rows) - len(recent)
    n_exp = len(exp)
    return {"expected": n_exp, "have": n_exp - len(missing), "missing": len(missing), "rate": ((n_exp - len(missing)) / n_exp) if n_exp else None,
            "recent": recent[:limit], "recent_total": len(recent), "long_stale": long_stale, "suspended": int(susp)}


def _sources(market: str) -> list[dict]:
    by = {t["name"]: t for t in throttle.all_status()}
    out = []
    for name, (label, use, mk) in SOURCES.items():
        t = by.get(name) or {}
        s7 = source_stats.summary(name, 7)
        out.append({"name": name, "label": label, "use": use, "market": mk, "mine": mk == market,
                    "breaker_open": bool(t.get("breaker_open")), "breaker_remaining_s": t.get("breaker_remaining_s", 0),
                    "consecutive_failures": t.get("consecutive_failures", 0), "calls_since_start": t.get("calls", 0),
                    "ok7": s7["ok"], "err7": s7["err"], "rate7": s7["rate"], "last_ok": s7["last_ok"], "last_err": s7["last_err"],
                    "errors": s7["errors"][:3], "today": s7["today"]})
    out.sort(key=lambda x: (not x["mine"], x["market"], x["name"]))
    return out


STATUS_TEXT = {"ok": ("成功", "ok"), "error": ("失败", "bad"), "waiting": ("等待上游数据", "info"), "PASS": ("闸门通过", "ok"),
               "INCOMPLETE": ("闸门未通过", "bad"), "observed": ("记录", "info")}


def _one(c, sql: str):
    r = c.execute(sql).fetchone()
    return dict(r) if r else None


WINDOW = 300          # 最近任务日志只往回看这么多行；连续「等待」超过它时次数写成「N+」（下限），不假装精确


def _collapse(rows: list[dict], window_full: bool = False) -> list[dict]:
    """连续重复的「等待上游数据」（软件每约 10 分钟探测一次，一天能有几十条）并成一行：写明次数和最近一次。"""
    out: list[dict] = []
    for r in rows:
        if out and r["status"] == "waiting" and out[-1]["status"] == "waiting" and out[-1]["job"] == r["job"]:
            out[-1]["n"] += 1
            out[-1]["first"] = r["ts"]
            continue
        out.append({**r, "n": 1, "first": r["ts"]})
    for r in out:
        r["text"], r["tone"] = STATUS_TEXT.get(r["status"], (r["status"], "info"))
        r["n_min"] = False
    if window_full and out and out[-1]["status"] == "waiting":
        out[-1]["n_min"] = True                                  # 最后一组顶到了窗口边界：真实次数可能更多
    return out


def _jobs(c, market: str, running: str | None) -> dict:
    cols = "SELECT ts, job, status, detail FROM job_log"
    # 「上次成功 / 失败 / 正在等」单独查：任务日志里「等待上游数据」一天能有几十条，只看最近几条会把真正的成功记录淹没
    last_ok = _one(c, cols + " WHERE status='ok' AND job IN ('daily','catch_up','init') ORDER BY id DESC LIMIT 1")
    last_err = _one(c, cols + " WHERE status='error' ORDER BY id DESC LIMIT 1")
    waiting = _one(c, cols + " WHERE job='daily' AND status='waiting' ORDER BY id DESC LIMIT 1")
    raw = db.rows(c.execute(cols + f" ORDER BY id DESC LIMIT {WINDOW}"))
    recent = _collapse(raw, window_full=len(raw) >= WINDOW)[:12]
    run = c.execute("SELECT scan_date, data_gate_status, finished_at FROM scan_runs WHERE market=? ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1",
                    (market,)).fetchone()
    return {"running": running, "last_ok": last_ok, "last_error": last_err, "waiting": waiting, "recent": recent, "names": JOB_NAME,
            "last_scan": dict(run) if run else None}


def _ago_hours(ts: str | None) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600 if ts else None
    except ValueError:
        return None


def build(market: str, *, jobs_state: dict | None = None, scheduler_on: bool | None = None) -> dict:
    """market：CN / US。jobs_state：JobManager.state（传进来是为了不在这里 import jobs，避免循环依赖）。"""
    jobs_state = jobs_state or {}
    issues: list[dict] = []

    def add(level: str, title: str, hint: str = "", where: str = ""):
        issues.append({"level": level, "title": title, "hint": hint, "where": where})

    with settings.market_ctx(market):
        with db.market_db(market) as c:
            has_data = bool(c.execute("SELECT 1 FROM daily_bar LIMIT 1").fetchone())
            asof = db.get_meta(c, "data_asof")
            if not has_data or not asof:
                return {"market": market, "status": "bad", "has_data": False, "headline": "还没有行情数据",
                        "issues": [{"level": "bad", "title": "还没有行情数据", "hint": "到「设置 → 数据与更新状态」点「初始化数据」。", "where": "settings"}],
                        "generated_at": datetime.now().isoformat(timespec="seconds"), "process": _process()}
            fresh = _freshness(c, market, asof)
            cov = _coverage(c, market, asof)
            gate = quality.check_gate(c, market, asof)
            jobs = _jobs(c, market, jobs_state.get("running"))
            idx = None
            if market == "US":
                try:
                    from . import us_index
                    idx = us_index.status()
                except Exception:  # noqa: BLE001
                    idx = None
    sources = _sources(market)
    bk = backup.backup_status()
    try:
        free_gb = round(shutil.disk_usage(settings.data_dir()).free / 1073741824, 1)
    except OSError:
        free_gb = None

    # ---------- 逐条发现 ----------
    if gate["status"] != "PASS":
        add("bad", "数据完整性闸门没通过：今天不会生成观察清单", "；".join(gate["reasons"]) or "见下面的闸门明细", "gate")
    behind = fresh["behind_days"]
    if behind is not None and behind >= 2:
        add("bad", f"行情数据落后 {behind} 个交易日（截至 {fresh['data_asof']}）", "到「设置」点「补跑缺失交易日」；软件要开着才会自动更新。", "fresh")
    elif behind == 1 and jobs["waiting"] and (_ago_hours(jobs["waiting"]["ts"]) or 99) < 3:
        add("info", f"上游还没提供 {fresh['last_closed']} 的数据，软件在等", (jobs["waiting"]["detail"] or "") + "。每隔约 10 分钟探测一次，一有就自动更新，不用手动处理。", "jobs")
    elif behind == 1:
        add("info" if jobs_state.get("running") or not jobs["last_error"] else "warn",
            f"还没有更新到最近的交易日（{fresh['last_closed']}）",
            "收盘后软件开着会自动更新（A 股约 15:35 起、1~2 分钟）；正在更新时稍等一下。" if jobs_state.get("running") else "收盘后软件开着会自动更新（A 股约 15:35 起）；如果迟迟不更新，看下面「更新任务」里的报错。", "fresh")
    if fresh["calendar_source"] == "fallback":
        add("warn", "交易日历取自兜底（只按周一到周五），节假日会被当成交易日", "检查网络后重新初始化日历，否则完整性检查可能误报缺失。", "fresh")
    if fresh["benchmarks_lagging"]:
        add("warn", "基准指数没跟上：" + "、".join(fresh["benchmarks_lagging"]), "指数数据落后会让「市场环境」判断失真；补跑一次盘后任务链。", "fresh")
    if cov["rate"] is not None and cov["missing"] and cov["rate"] < 0.995:
        add("warn", f"当日有 {cov['missing']} 只股票没拿到行情（覆盖 {cov['rate']:.1%}）", "名单见下面「当日覆盖」；这些股票这一天不会进入扫描。", "coverage")
    bad_n = len(gate.get("bad_symbols") or [])
    if bad_n:
        add("info", f"{bad_n} 只股票当日价格 / 涨跌幅异常，已被排除出扫描", "多为刚除权、刚上市或上游的初步数据，下一轮更新会重新拉取。", "gate")
    for s in sources:
        if s["breaker_open"]:
            add("warn", f"数据源「{s['label']}」熔断中（约 {s['breaker_remaining_s']} 秒后恢复）", "连续失败后软件自动暂停请求，避免被封；通常不用处理，会自动换备用源。", "sources")
        elif s["mine"] and s["rate7"] is not None and s["ok7"] + s["err7"] >= 20 and s["rate7"] < 0.7:
            add("warn", f"数据源「{s['label']}」最近 7 天成功率只有 {s['rate7']:.0%}", "经常失败：检查网络；软件会自动改用备用源。", "sources")
    le = jobs["last_error"]
    if le and (_ago_hours(le["ts"]) or 999) < 48 and not (jobs["last_ok"] and jobs["last_ok"]["ts"] > le["ts"]):
        add("warn", "最近一次更新任务失败了", f"{le['ts'].replace('T', ' ')} {JOB_NAME.get(le['job'], le['job'])}：{(le['detail'] or '')[:120]}", "jobs")
    if bk["stale"]:
        add("warn", "备份已经超过 1 天没有更新" if bk["last"] else "还没有任何备份", "持仓和交易记录只有备份能找回；到「设置 → 备份与恢复」手动备份一次。", "backup")
    if free_gb is not None and free_gb < 0.5:
        add("bad", f"磁盘只剩 {free_gb} GB", "空间不够时数据更新会失败；清理磁盘或到「设置 → 存储空间」清理。", "process")
    elif free_gb is not None and free_gb < 2:
        add("warn", f"磁盘只剩 {free_gb} GB", "建议留出 2 GB 以上。", "process")
    if idx and idx.get("stale"):
        add("warn", "美股选股名单已经超过 5 周没更新", "可能一直联网失败，名单可能过时。", "sources")

    issues.sort(key=lambda i: -LEVEL_RANK[i["level"]])
    status = max((i["level"] for i in issues), key=lambda l: LEVEL_RANK[l], default="ok")
    status = "ok" if status == "info" else status
    headline = {
        "ok": "数据是新的、完整的：数据层没有发现问题。如果策略结果不如预期，原因更可能在策略本身，而不是数据。",
        "warn": "数据基本可用，但有几处需要留意（见下）。结果出问题时，先看这些，再怀疑策略。",
        "bad": "数据有问题：在修好之前，今天的选股和各项策略结果不可信，不要据此操作。",
    }[status]
    return {"market": market, "status": status, "headline": headline, "has_data": True, "issues": issues,
            "freshness": fresh, "coverage": cov, "gate": {k: gate[k] for k in ("status", "day", "checks", "reasons")} | {"bad_symbols": (gate.get("bad_symbols") or [])[:30]},
            "sources": sources, "jobs": jobs, "scheduler_on": scheduler_on, "backup": {k: bk[k] for k in ("last", "age_days", "stale", "count", "dir")},
            "disk_free_gb": free_gb, "process": _process(), "us_index": idx,
            "generated_at": datetime.now().isoformat(timespec="seconds")}


def _process() -> dict:
    from . import APP_NAME, __version__
    started = None
    try:
        from . import main as _m          # 延迟导入：避免循环依赖
        started = getattr(_m, "STARTED_AT", None)
        build = getattr(_m, "BUILD_ID", None)
        disk_build = _m._build_id() if hasattr(_m, "_build_id") else None
    except Exception:  # noqa: BLE001
        build = disk_build = None
    up = _ago_hours(started)
    return {"pid": os.getpid(), "started": started, "uptime_hours": round(up, 2) if up is not None else None, "rss_mb": rss_mb(),
            "version": __version__, "build": build, "needs_restart": bool(build and disk_build and build != disk_build),
            "python": sys.version.split()[0], "name": APP_NAME, "now": time.strftime("%Y-%m-%d %H:%M:%S")}
