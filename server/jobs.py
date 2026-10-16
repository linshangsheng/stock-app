"""盘后任务调度与「启动补跑」（3.26.5）。
  * 任务条件 = 「该市场已收盘」且「上游已提供当日数据」，以探测方式运行，不以固定时刻硬跑
  * 任务链：数据更新 -> 完整性闸门 -> 选股扫描 -> 持仓体检 -> 回填 scan_outcomes -> 每日备份；任何一步失败，后续步骤不执行并告警
  * 启动补跑是主路径：应用不一定每天开着，启动时与运行期间定期按交易日历检测缺失的交易日并补拉 / 补跑
  * 全部按市场时区与交易日历判断，不使用本机时区"""
from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import backup, db, ingest, market_calendar as mc, markets, portfolio, quality, scanner, settings, throttle, universe
from .datasource_cn import TencentSource, get_source


def fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s // 60} 分 {s % 60:02d} 秒" if s >= 60 else f"{s} 秒"


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timer: threading.Timer | None = None
        self.state: dict = {"running": None, "progress": None, "last": {}, "started_at": None, "message": ""}

    # ---- 状态 ----
    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.state, default=str))

    def _set(self, **kw):
        self.state.update(kw)

    def _progress(self, name: str, i: int, n: int):
        self.state["progress"] = {"task": name, "done": i, "total": n}

    @contextmanager
    def _step(self, res: dict, label: str):
        """记录每一步的耗时（res["timing"]，显示在设置页和任务日志里），方便看清楚时间花在哪里。"""
        t = time.monotonic()
        self._set(message=label, progress=None)
        try:
            yield
        finally:
            res.setdefault("timing", []).append([label, round(time.monotonic() - t, 1)])

    # ---- 对外入口 ----
    def start(self, task: str, **kw) -> dict:
        if self.state["running"]:
            return {"ok": False, "message": f"任务进行中：{self.state['running']}"}
        t = threading.Thread(target=self._run, args=(task,), kwargs=kw, daemon=True)
        t.start()
        return {"ok": True, "message": f"已启动：{task}"}

    def stop(self):
        self._stop.set()

    def _run(self, task: str, **kw):
        with self._lock:
            self._stop.clear()
            self._set(running=task, started_at=datetime.now().isoformat(timespec="seconds"), progress=None, message="")
            t0 = time.monotonic()
            market = kw.get("market", "CN")
            try:
                fn = {"init": self.init, "daily": self.daily_chain, "catch_up": self.catch_up,
                      "backup": lambda **_: backup.run_backup()}[task]
                res = fn(**kw)
                if task == "init" and not self._stop.is_set():
                    with db.market_db(market) as c:
                        complete = db.get_meta(c, "init_complete") == "1"
                    if complete:                                   # 补全后接着把数据更新到最近一个交易日并出清单：一次点击就完整
                        self._set(message="补全完成，接着更新到最近交易日")
                        res["catch_up"] = self.catch_up(market=market)
                secs = round(time.monotonic() - t0)
                timing = _collect_timing(res)
                self.state["last"][f"{task}:{market}"] = {"ok": True, "at": datetime.now().isoformat(timespec="seconds"), "market": market,
                                            "secs": secs, "timing": timing, "result": res}
                if len(timing) > 1:                                # 只等待上游数据（只有「日历与探测」一步）时不记，免得每 10 分钟一条
                    with db.market_db(market) as c:
                        db.log_job(c, task, "ok", f"{market} 用时 {fmt_secs(secs)}：" + "，".join(f"{k} {fmt_secs(v)}" for k, v in timing))
                        c.commit()
            except Exception as e:  # noqa: BLE001
                self.state["last"][f"{task}:{market}"] = {"ok": False, "at": datetime.now().isoformat(timespec="seconds"), "market": market,
                                            "secs": round(time.monotonic() - t0),
                                            "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
                with db.market_db(market) as c:
                    db.log_job(c, task, "error", f"{type(e).__name__}: {e}")
            finally:
                self._set(running=None, progress=None)

    # ---- 任务 ----
    def init(self, market: str = "CN", limit: int | None = None, sample: int | None = None, resample: bool = False, **_) -> dict:
        """首次初始化：日历 -> 证券名单 -> L1 -> 10 年日线（+ 复权因子 / 拆股）（可中断续跑）-> 指数 / 行业。
        初始化范围：默认全部（config.init.sample_size = 0）；sample = N 时只随机拉 N 只（试用 / 省流量）。"""
        with settings.market_ctx(market):
            n = settings.cfg().get("init", {}).get("sample_size", 0) if sample is None else sample
            return self._init(market, limit, int(n or 0), resample)

    def _init(self, market: str, limit: int | None, sample: int = 0, resample: bool = False) -> dict:
        src = get_source(market)
        out: dict = {}
        demo = markets.is_demo(market)
        try:
            with db.market_db(market) as c:
                with self._step(out, "交易日历"):
                    out["calendar"] = ingest.ensure_calendar(c, src, market)
                with self._step(out, "证券名单"):
                    self._set(message="证券名单" + ("（多个历史交易日并集，含已退市）" if market == "CN" else "（Nasdaq Trader 清单：普通股）"))
                    out["securities_new"] = ingest.refresh_securities(c, src, market, progress=self._progress, fast=bool(sample))
                    if market == "CN":
                        out["industry"] = ingest.refresh_industry(c, src)
                snap = None
                if market == "CN" and not demo and not sample:          # 随机抽样模式：不需要全市场快照 / 预筛（省流量）
                    with self._step(out, "全市场快照"):
                        self._set(message="全市场快照（腾讯批量报价，约 1~2 分钟；不行再试东财）：用于 L1 粗筛")
                        try:
                            codes = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE status!='delisted'")]
                            snap = ingest.cn_snapshot(codes)
                        except Exception as e:  # noqa: BLE001 - 降级：L1 走近 45 天预筛
                            out["snapshot_error"] = str(e)[:200]
                elif market == "US" and not demo and not sample:        # 美股：Yahoo 筛选器一次性取全市场快照（~15 个请求，代替 ~1 小时的逐批预筛）
                    with self._step(out, "全市场快照"):
                        self._set(message="全市场快照（Yahoo 筛选器，约 1 分钟）：用于 L1 粗筛")
                        try:
                            l1cfg = markets.universe_cfg(market)["l1"]
                            snap = src.screen_snapshot(l1cfg["min_price"])
                            if len(snap) < 500:
                                raise RuntimeError(f"快照只有 {len(snap)} 只，疑似不完整")
                        except Exception as e:  # noqa: BLE001 - 降级：逐批预筛
                            snap = None
                            out["snapshot_error"] = str(e)[:200]
                out["l1"] = universe.apply_l1(c, snap, market=market, exclude_missing=(market == "US"))
                out["l1"]["snapshot_used"] = snap is not None
                only = None
                if sample:
                    picked = ingest.choose_sample(c, market, sample, resample=resample)
                    only = set(picked)
                    out["sample"] = {"requested": sample, "picked": len(picked), "note": "随机抽样模式：只下载这批股票的历史（不做全市场快照 / 预筛 / 已退市名单并集）"}
                else:
                    ingest.clear_sample(c)
                if snap is None and not demo and not sample:
                    with self._step(out, "L1 预筛"):
                        self._set(message="L1 预筛（每只拉最近 45 天，筛掉低价 / 低成交额）")
                        out["prefilter"] = ingest.prefilter_l1(c, src, market, progress=self._progress, stop=self._stop.is_set)
                with self._step(out, "拉取历史日线"):
                    self._set(message="拉取历史日线（限速，可中断续跑；已拉过的不会重复下载）")
                    out["history"] = ingest.init_history(c, src, market, limit=limit, progress=self._progress, stop=self._stop.is_set, only=only)
                remaining = c.execute("SELECT COUNT(*) FROM securities s LEFT JOIN fetch_state f ON f.symbol=s.symbol AND f.task='daily' "
                                      "WHERE s.in_l1=1 AND s.status!='delisted' AND (f.status IS NULL OR f.status!='ok')").fetchone()[0]
                db.set_meta(c, "init_complete", "1" if remaining == 0 else "0")       # 完整初始化后，每周 L1 刷新才自动回补新进入者
                with self._step(out, "指数日线"):
                    out["index_rows"] = ingest.refresh_indices(c, src, market, quotes_src=TencentSource() if market == "CN" and not demo else None)
                last = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
                db.set_meta(c, "data_asof", last)                     # 先标记数据截止日：行业补全很慢（美股逐股），不应阻塞扫描
                c.commit()
                if market == "US":
                    with self._step(out, "行业 / Sector"):
                        self._set(message="行业 / Sector（未补全前行业强弱按缺失处理）")
                        asof = db.get_meta(c, "industry_map_asof")
                        fresh = bool(asof) and (datetime.now().date() - datetime.fromisoformat(asof).date()).days < 7
                        if not demo and not sample and not fresh:      # 行业映射一周内刷新过就不再整表重下（约 150 个请求、10 分钟）
                            try:
                                out["industry_bulk"] = ingest.refresh_industry_us_bulk(c, src, progress=self._progress)
                            except Exception as e:  # noqa: BLE001 - 降级：逐股
                                out["industry_bulk_error"] = str(e)[:200]
                        elif fresh:
                            out["industry_bulk"] = f"跳过：{asof} 刚刷新过（每周刷新一次）"
                        out["industry"] = ingest.refresh_industry_us(c, src, stop=self._stop.is_set, progress=self._progress, only=only)
                db.log_job(c, "init", "ok", json.dumps({k: v for k, v in out.items() if k != "timing"}, ensure_ascii=False, default=str)[:1500])
        finally:
            if hasattr(src, "close"):
                src.close()
        return out

    def daily_chain(self, market: str = "CN", scan_day: str | None = None, **_) -> dict:
        """数据更新 -> 闸门 -> 扫描 -> 持仓体检 -> 回填 outcomes -> 备份；任何一步失败，后续步骤不执行并告警。"""
        with settings.market_ctx(market):
            return self._daily_chain(market, scan_day)

    def _daily_chain(self, market: str, scan_day: str | None) -> dict:
        src = get_source(market)
        res: dict = {}
        demo = markets.is_demo(market)
        tx = TencentSource() if market == "CN" and not demo else None            # A 股收盘批量报价（主路径）
        try:
            with db.market_db(market) as c:
                with self._step(res, "日历与探测"):
                    ingest.ensure_calendar(c, src, market)
                    day = scan_day or mc.last_closed_trading_day(c, market)
                    if not day:
                        return {"skipped": "无已收盘交易日"}
                    if not scan_day:                                               # 自动触发：先便宜地探测上游是否已提供当日数据
                        if not ingest.probe_day_available(src, market, day, quotes_src=tx):
                            db.log_job(c, "daily", "waiting", f"上游尚未提供 {day} 数据（探测基准指数）")
                            return {"status": "waiting_data", "day": day}
                        mins = ingest.record_ready_observation(c, market, day)
                        if mins is not None:
                            db.log_job(c, "ready", "observed", f"{market} 当日数据在收盘后约 {mins} 分钟可用")
                with self._step(res, "更新个股日线"):
                    self._set(message=f"更新数据 -> {day}")
                    snap_fn = quotes_fn = None
                    if tx is not None:
                        codes = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=1 AND status!='delisted'")]
                        snap_fn = lambda: ingest.EastmoneySource().snapshot(codes)   # noqa: E731 - 备用
                        if ingest.quotes_enabled(c):
                            quotes_fn = lambda syms: tx.quotes(syms, stop=self._stop.is_set, progress=self._progress)   # noqa: E731
                    res["update"] = ingest.update_incremental(c, src, market, day, progress=self._progress, stop=self._stop.is_set,
                                                              snapshot_fn=snap_fn, quotes_fn=quotes_fn)
                with self._step(res, "指数日线"):
                    res["index_rows"] = ingest.refresh_indices(c, src, market, quotes_src=tx)
                have = c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
                if have is None or have < day:
                    res["status"] = "waiting_data"
                    db.log_job(c, "daily", "waiting", f"上游尚未提供 {day} 数据（已有 {have}）")
                    return res
                db.set_meta(c, "data_asof", have)
                with self._step(res, "市场温度"):
                    try:                                                           # 市场温度：全市场宽度增量（几秒）
                        from . import market_view
                        res["breadth_rows"] = market_view.ensure_breadth(c, market)
                    except Exception as e:  # noqa: BLE001 - 不影响选股主链
                        res["breadth_error"] = str(e)[:200]
                # 每周刷新 L1 / 行业映射
                last_l1 = c.execute("SELECT MAX(l1_asof) FROM securities").fetchone()[0]
                if not last_l1 or (datetime.now().date() - datetime.fromisoformat(last_l1).date()).days >= 7:
                    with self._step(res, "每周名单 / 行业刷新"):
                        self._weekly_refresh(c, src, market, res)
                with self._step(res, "完整性闸门"):
                    gate = quality.check_gate(c, market, day)
                    bad_px = any(ch["name"] == "price_sanity" and not ch["ok"] for ch in gate["checks"])
                    if gate["status"] != "PASS" and bad_px and gate.get("bad_symbols") and hasattr(src, "daily_bars_batch"):
                        # 美股：多半是收盘后不久的初步数据，上游稍后会修正——只把异常股最近几天重拉一遍，再检查一次
                        self._set(message=f"重新拉取 {len(gate['bad_symbols'])} 只价格异常的股票（上游数据可能已修正）")
                        try:
                            res["refetch_bad"] = ingest.refetch_recent(c, src, market, gate["bad_symbols"], day)
                            gate = quality.check_gate(c, market, day)
                        except Exception as e:  # noqa: BLE001 - 重拉失败不影响主链，下一轮调度再试
                            res["refetch_bad_error"] = str(e)[:200]
                    res["gate"] = {"status": gate["status"], "reasons": gate["reasons"]}
                    db.log_job(c, "gate", gate["status"], "; ".join(gate["reasons"]))
            with self._step(res, "选股扫描"):
                scan = scanner.run_scan(market, scan_date=day)
                res["scan"] = {"run_id": scan.get("run_id"), "official": scan.get("official"), "n": len(scan.get("candidates", []))}
            if scan.get("official"):
                with self._step(res, "持仓体检"):
                    h = portfolio.health(market, day)
                    res["health"] = h.get("summary", {}).get("levels")
                    res["outcomes"] = scanner.backfill_outcomes(market)
                if market == "CN":
                    with self._step(res, "低风险组合证据"):
                        try:
                            from . import factor_portfolio as fpm
                            res["factor_summary"] = fpm.refresh_summary(market)
                        except Exception as e:  # noqa: BLE001 - 不影响主链
                            res["factor_summary_error"] = str(e)[:200]
                with self._step(res, "事件 / 公告"):
                    self._set(message="事件 / 公告 / 新闻（候选 + 持仓，缓存 6 小时）")
                    res["events"] = self._events_for_candidates(market, scan, h)
                with self._step(res, "备份"):
                    res["backup"] = backup.run_backup()
                with self._step(res, "对账 / 抽查"):
                    res["recon"] = self._reconcile(market, src)
                    if tx is not None:
                        try:
                            with db.market_db(market) as c:
                                res["quote_check"] = ingest.spot_check_quotes(c, src, stop=self._stop.is_set)
                        except Exception as e:  # noqa: BLE001 - 抽查失败不影响主链
                            res["quote_check_error"] = str(e)[:200]
            else:
                res["note"] = "数据完整性闸门未通过：不生成观察清单、不进入后续步骤（体检 / 回填 / 备份仍可手动触发）"
                with self._step(res, "备份"):
                    res["backup"] = backup.run_backup()      # 备份不依赖闸门
        finally:
            if hasattr(src, "close"):
                src.close()
        return res

    def _weekly_refresh(self, c, src, market: str, res: dict) -> None:
        """每周刷新 L1 名单 / 行业映射；新进入 L1 的股票回补历史。"""
        demo = markets.is_demo(market)
        if market == "US":                                                        # 新上市 / 新进入：名单 -> 预筛 -> 拉历史 -> 行业
            res["new_securities"] = ingest.refresh_securities(c, src, market)
            if not demo:
                snap = None
                try:                                                              # 优先：Yahoo 筛选器快照（~15 个请求）；失败才逐批预筛
                    snap = src.screen_snapshot(markets.universe_cfg(market)["l1"]["min_price"])
                    if len(snap) < 500:
                        snap = None
                except Exception:  # noqa: BLE001
                    snap = None
                if snap is not None:
                    res["l1_refresh"] = universe.apply_l1(c, snap, market=market, exclude_missing=True)
                else:
                    res["prefilter"] = ingest.prefilter_l1(c, src, market, progress=self._progress, stop=self._stop.is_set)
            res["l1_backfill"] = ingest.init_history(c, src, market, progress=self._progress, stop=self._stop.is_set)
            if not demo:
                try:
                    res["industry_bulk"] = ingest.refresh_industry_us_bulk(c, src)
                except Exception:  # noqa: BLE001
                    pass
            res["industry"] = ingest.refresh_industry_us(c, src, stop=self._stop.is_set)
            return
        snap = None
        if not demo:
            try:                                                                  # 腾讯批量报价优先，东财备用
                codes = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE status!='delisted'")]
                snap = ingest.cn_snapshot(codes)
            except Exception:  # noqa: BLE001
                snap = None
        if snap is not None:                                                      # 无快照时 L1 由 init 预筛决定，不自动翻转
            res["l1_refresh"] = universe.apply_l1(c, snap, market=market)
            if db.get_meta(c, "init_complete") == "1":                            # 新进入 L1 的股票：回补历史（可中断续跑）
                res["l1_backfill"] = ingest.init_history(c, src, market, progress=self._progress, stop=self._stop.is_set)
        ingest.refresh_industry(c, src)

    def _reconcile(self, market: str, src) -> dict | None:
        """次日用历史接口覆盖前一日的快照临时数据并对账（3.25.1）。放在任务链末尾：不阻塞选股，耗时长（每只 1 次请求）可中断续跑。"""
        try:
            with db.market_db(market) as c:
                if db.get_meta(c, "has_temp_rows") != "1":
                    return None
                self._set(message="对账：用历史接口覆盖临时快照数据（可中断，不影响清单）")
                r = ingest.reconcile_temp(c, src, progress=self._progress, stop=self._stop.is_set)
                if not c.execute("SELECT 1 FROM daily_bar WHERE is_temp=1 LIMIT 1").fetchone():
                    db.set_meta(c, "has_temp_rows", "0")
                return r
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    def _events_for_candidates(self, market: str, scan: dict, health: dict) -> dict:
        """只对「漏斗最终候选 + 当前持仓」按需拉取事件（3.17）；上游失败不影响任务链。"""
        from . import datasource_events as ev
        cap = int(settings.cfg().get("events", {}).get("max_symbols_per_run", 40))
        syms = [p["symbol"] for p in health.get("positions", [])] + [c["symbol"] for c in scan.get("candidates", []) if c.get("fit", True)]
        syms = list(dict.fromkeys(syms))[:cap]
        if not syms:
            return {"requested": 0}
        src = get_source(market)
        try:
            with db.market_db(market) as c:
                return ingest.ensure_events(c, ev.get_provider(market, src), syms, market, stop=self._stop.is_set)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        finally:
            if hasattr(src, "close"):
                src.close()

    def catch_up(self, market: str = "CN", **_) -> dict:
        """启动补跑：按交易日历对比已扫描日期，补拉缺失数据，并为跳过的交易日补出观察清单。
        补出的清单 scan_date = 缺失日 D（started_at 为实际运行时间），界面据此提示「这是 D 日收盘的清单」，
        若其有效期（下一交易日）已过，则只作记录，不再作为可执行清单（3.26.5）。"""
        res = {}
        with db.market_db(market) as c:
            if not c.execute("SELECT 1 FROM daily_bar LIMIT 1").fetchone():
                return {"skipped": "尚未初始化数据"}
            last_scan0 = c.execute("SELECT MAX(scan_date) FROM scan_runs WHERE market=? AND official=1", (market,)).fetchone()[0]
        res["daily"] = self.daily_chain(market)
        with db.market_db(market) as c:
            asof = db.get_meta(c, "data_asof")
            missing = []
            if asof and last_scan0 and last_scan0 < asof:
                missing = [d for d in mc.trading_days(c, last_scan0, asof) if last_scan0 < d < asof][-5:]     # 最近 5 个缺失日
        done = []
        for d in missing:
            if self._stop.is_set():
                break
            s = scanner.run_scan(market, scan_date=d)
            done.append({"date": d, "official": s.get("official"), "stale": s.get("is_stale")})
        res["backfilled_scans"] = done
        return res

    # ---- 调度（运行期间定期检查）----
    @staticmethod
    def _data_ready(conn, market: str, day: str) -> bool:
        """上游当日数据是否「应已可用」：历史交易日恒为真；当日需收盘后再等 data_ready_after_close_minutes（按市场时区，M0 实测校准）。"""
        n = mc.now_in_market(market)
        if day != n.date().isoformat():
            return True
        cfgm = mc.MARKETS[market]
        close_t = cfgm.get("half_close", cfgm["close"]) if (market == "US" and mc.is_half_day(conn, day)) else cfgm["close"]
        close_dt = datetime.combine(n.date(), close_t, tzinfo=n.tzinfo)
        with settings.market_ctx(market):
            wait = settings.cfg()["jobs"].get("probe_start_minutes", 20)       # 探测很便宜：早点开始，由探测结果决定是否真的可用
        return n >= close_dt + timedelta(minutes=wait)

    def tick(self):
        try:
            if self.state["running"]:
                return
            for market in ("CN", "US"):                           # 各市场按自己的时区与交易日历独立判断
                with db.market_db(market) as c:
                    if not c.execute("SELECT 1 FROM daily_bar LIMIT 1").fetchone():
                        continue
                    day = mc.last_closed_trading_day(c, market)
                    if not day:
                        continue
                    asof = db.get_meta(c, "data_asof")
                    last_scan = c.execute("SELECT MAX(scan_date) FROM scan_runs WHERE market=? AND official=1", (market,)).fetchone()[0]
                    ready = self._data_ready(c, market, day)
                if ready and (asof is None or asof < day or last_scan is None or last_scan < day):
                    self.start("catch_up", market=market)
                    return
        except Exception:  # noqa: BLE001 - 调度失败不应影响服务
            traceback.print_exc()

    def schedule(self, interval_min: int | None = None):
        interval = (interval_min or settings.cfg()["jobs"]["poll_interval_minutes"]) * 60

        def loop():
            self.tick()
            self._timer = threading.Timer(interval, loop)
            self._timer.daemon = True
            self._timer.start()

        t = threading.Timer(5, loop)
        t.daemon = True
        t.start()
        self._timer = t


def _collect_timing(res) -> list:
    """把各段任务（初始化 / 补跑里嵌套的盘后任务链）的分步耗时合并成一张表。"""
    out = []
    if isinstance(res, dict):
        out += res.get("timing") or []
        for k in ("daily", "catch_up"):
            out += _collect_timing(res.get(k))
    return out


manager = JobManager()
