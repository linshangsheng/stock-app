"""服务入口（FastAPI）：静态托管前端 + 数据接口（2.6）。默认仅监听 127.0.0.1；开放局域网必须设置访问口令（1.6）。
所有接口带 market 参数（CN / US），返回带 data_asof；扫描 / 回测类接口另带 run_id。
启动：python -m server.main    （或双击「启动股票.vbs」）"""
from __future__ import annotations

import functools
import hmac
import json
import logging
import os
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import backup, backtest, datasource_events as ev_mod, db, ingest, markets, features as feats, market_calendar as mc, portfolio, quality, scanner, settings, \
    selection, setups, throttle, universe
from .datasource_cn import get_source
from .jobs import manager
from .panel import load_panel

ROOT = settings.ROOT
app = FastAPI(title="股票 App 本地后端", version="0.7.0")


# ---- 工具 ---------------------------------------------------------------------

def _market(m: str) -> str:
    m = (m or "CN").upper()
    if m not in ("CN", "US"):
        raise HTTPException(400, "market 须为 CN / US")
    return m


def _need_cn(m: str) -> str:
    """校验 market 并把它设为本次请求的配置上下文（A股 / 美股参数各自独立，2.1）。名称沿用历史，现已支持 CN / US。"""
    m = _market(m)
    settings.set_market(m)
    return m


def _asof(market: str = "CN") -> str | None:
    try:
        with db.market_db(market) as c:
            return db.get_meta(c, "data_asof") or c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
    except Exception:
        return None


def _clean(o: Any):
    """JSON 友好：NaN / Inf -> None，numpy 标量 -> Python。"""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    return o


class CleanJSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(_clean(content), ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")


class CleanRoute(APIRoute):
    """绕过 FastAPI 的 jsonable_encoder（它不认识 numpy 标量，也不处理 NaN）：端点返回值统一经 _clean -> CleanJSON。"""

    def __init__(self, path, endpoint, **kw):
        @functools.wraps(endpoint)
        def wrapped(*a, **k):
            res = endpoint(*a, **k)
            return res if isinstance(res, Response) else CleanJSON(res)

        super().__init__(path, wrapped, **kw)


app.router.route_class = CleanRoute


def _wrap(market: str, **kw):
    return {"market": market, "data_asof": _asof(market), **kw}


# ---- 访问口令 -----------------------------------------------------------------

@app.middleware("http")
async def auth(request: Request, call_next):
    settings.set_market((request.query_params.get("market") or "CN").upper() if (request.query_params.get("market") or "CN").upper() in ("CN", "US") else "CN")
    token = settings.cfg()["server"].get("token") or ""
    if token and request.url.path.startswith("/api/") and request.url.path != "/api/ping":
        got = request.headers.get("x-token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(got, token):
            return JSONResponse({"detail": "需要访问口令"}, status_code=401)
    resp = await call_next(request)
    p = request.url.path
    if p in ("/sw.js", "/", "/index.html", "/manifest.json") or p.startswith(("/js/", "/css/")):
        resp.headers["Cache-Control"] = "no-cache"          # 每次向本机后端校验（ETag），更新代码后不会用到过期的前端文件
    return resp


def _build_id() -> str:
    """前端（js / css / index.html / sw.js）与后端代码、配置的最新修改时间：页面据此发现「软件已更新」并提示刷新（不必 Ctrl+F5）。"""
    files = [*ROOT.glob("js/*.js"), *ROOT.glob("css/*.css"), ROOT / "index.html", ROOT / "sw.js", *ROOT.glob("server/*.py"), ROOT / "server" / "config.yaml"]
    return str(int(max((f.stat().st_mtime for f in files if f.exists()), default=0)))


BUILD_ID = _build_id()


@app.get("/api/ping")
def ping():
    return {"ok": True, "version": app.version, "build": BUILD_ID, "disk_build": _build_id(), "auth_required": bool(settings.cfg()["server"].get("token")),
            "datasource": settings.cfg()["datasource"], "demo": {"CN": markets.is_demo("CN"), "US": markets.is_demo("US")}}


# ---- 搜索 / 行情 --------------------------------------------------------------

@app.get("/api/search")
def search(q: str = Query(""), market: str = "CN", limit: int = 20):
    market = _need_cn(market)
    q = q.strip()
    if not q:
        return _wrap(market, items=[])
    with db.market_db(market) as c:
        like = f"%{q}%"
        rows = db.rows(c.execute(
            "SELECT symbol,name,board,status FROM securities WHERE sec_type='stock' AND (symbol LIKE ? OR name LIKE ?) "
            "ORDER BY in_l1 DESC, symbol LIMIT ?", (like, like, limit)))
        ind = {r["symbol"]: r["industry"] for r in db.rows(c.execute("SELECT symbol,industry FROM industry_map"))}
    for r in rows:
        r["industry"] = ind.get(r["symbol"])
        r["code"] = r["symbol"].split(".")[-1]
    return _wrap(market, items=rows)


def _quotes(c, symbols: list[str]) -> dict:
    out = {}
    for s in symbols:
        rr = c.execute("SELECT date,open,high,low,close,volume,amount,trade_status,is_temp FROM daily_bar WHERE symbol=? "
                       "ORDER BY date DESC LIMIT 2", (s,)).fetchall()
        if not rr:
            continue
        cur = dict(rr[0])
        prev = rr[1]["close"] if len(rr) > 1 else None
        cur["prev_close"] = prev
        cur["change"] = (cur["close"] - prev) if prev else None
        cur["change_pct"] = (cur["close"] / prev - 1) if prev else None
        out[s] = cur
    return out


@app.get("/api/quote")
def quote(symbols: str, market: str = "CN"):
    """盘后日线快照（最新一根日线与涨跌）。盘中实时刷新为可选项（第一阶段盘后批处理为主，见 1.1）。"""
    market = _need_cn(market)
    syms = [s for s in symbols.split(",") if s]
    with db.market_db(market) as c:
        return _wrap(market, quotes=_quotes(c, syms))


def _single_features(conn, symbol: str, start: str | None = None):
    market = settings.current_market()
    panel = load_panel(conn, start, None, symbols=[symbol], only_l1=False, float32=False, market=market)
    if symbol not in panel.symbols:
        return None, None
    bench = scanner.load_bench(conn, settings.cfg()["regime"]["benchmark"], "9999-12-31")
    return panel, feats.compute_features(panel, None, bench, None)


@app.get("/api/kline")
def kline(symbol: str, market: str = "CN", period: str = "D", adj: str = "qfq", limit: int = 750,
          overlays: str = "ma,macd,rsi"):
    """K 线 + 叠加物：均线、MACD / RSI、本次观察清单的触发价与止损位、持仓成本与止损线、信号与买卖点、财报日。"""
    market = _need_cn(market)
    with db.market_db(market) as c:
        panel, feat = _single_features(c, symbol)
        if panel is None:
            raise HTTPException(404, f"{symbol} 无行情数据")
        sec = c.execute("SELECT * FROM securities WHERE symbol=?", (symbol,)).fetchone()
        ind = c.execute("SELECT industry FROM industry_map WHERE symbol=?", (symbol,)).fetchone()
        src = panel.adj if adj == "qfq" else panel.raw
        d = pd.DataFrame({k: src[k][symbol] for k in ("open", "high", "low", "close")})
        d["volume"] = panel.volume[symbol]
        d["amount"] = panel.amount[symbol]
        d = d.dropna(subset=["close"])
        F = {k: feat.f[k][symbol].reindex(d.index) for k in ("ma5", "ma10", "ma20", "ma50", "ma200", "macd_dif", "macd_dea",
                                                           "macd_hist", "rsi14", "atr14", "vol_ratio", "atr_pct", "rps_20", "ret_20")
             if k in feat.f}
        if period.upper() == "W":                                  # 周线：按自然周聚合，指标在周线收盘价上重新计算
            wk = pd.PeriodIndex(pd.to_datetime(d.index), freq="W").astype(str)
            wd = d.groupby(wk).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "amount": "sum"})
            wd.index = d.index.to_series().groupby(wk).last().reindex(wd.index).values
            F = {f"ma{n}": wd["close"].rolling(n, min_periods=n).mean() for n in (5, 10, 20, 50, 200)}
            e12, e26 = wd["close"].ewm(span=12, adjust=False).mean(), wd["close"].ewm(span=26, adjust=False).mean()
            F["macd_dif"] = e12 - e26
            F["macd_dea"] = F["macd_dif"].ewm(span=9, adjust=False).mean()
            F["macd_hist"] = (F["macd_dif"] - F["macd_dea"]) * 2
            F["rsi14"] = feats.rsi(wd[["close"]], 14)["close"]
            d = wd
        d = d.tail(limit)

        def series(s):
            s = s.reindex(d.index)
            return [{"time": t, "value": None if v != v else round(float(v), 4)} for t, v in zip(d.index, s.values) if v == v]

        out = {"symbol": symbol, "name": sec["name"] if sec else symbol, "industry": ind["industry"] if ind else None,
               "board": sec["board"] if sec else None, "period": period.upper(), "adj": adj,
               "bars": [{"time": t, "open": round(r.open, 3), "high": round(r.high, 3), "low": round(r.low, 3),
                         "close": round(r.close, 3), "volume": None if r.volume != r.volume else float(r.volume),
                         "amount": None if r.amount != r.amount else float(r.amount)} for t, r in zip(d.index, d.itertuples())]}
        ov = set(overlays.split(","))
        if "ma" in ov:
            out["ma"] = {k: series(F[k]) for k in ("ma5", "ma10", "ma20", "ma50", "ma200") if k in F}
        if "macd" in ov and "macd_dif" in F:
            out["macd"] = {"dif": series(F["macd_dif"]), "dea": series(F["macd_dea"]), "hist": series(F["macd_hist"])}
        if "rsi" in ov and "rsi14" in F:
            out["rsi"] = series(F["rsi14"])
        # 关键指标卡（来自第三章标准化字段）
        last = d.index[-1]
        lastd = panel.dates[-1]
        def lf(k):
            v = feat.f[k][symbol].iloc[-1] if k in feat.f else None
            return None if v is None or v != v else round(float(v), 4)
        out["indicators"] = {k: lf(k) for k in ("rsi14", "macd_dif", "macd_dea", "macd_hist", "vol_ratio", "atr_pct", "ret_5",
                                                 "ret_10", "ret_20", "ret_60", "close_ma20", "close_ma50", "close_ma200",
                                                 "dist_52w_high", "close_pos", "rs_20", "hv20", "trend_r2")}
        out["asof_bar"] = lastd
        # 叠加物
        lv = {}
        run = c.execute("SELECT run_id, scan_date FROM scan_runs WHERE market=? AND official=1 ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1", (market,)).fetchone()
        if run:
            r = c.execute("SELECT * FROM scan_results WHERE run_id=? AND symbol=?", (run["run_id"], symbol)).fetchone()
            if r:
                lv["scan"] = {"run_id": run["run_id"], "scan_date": run["scan_date"], "setup": r["setup"], "score": r["score"],
                              "trigger_price": r["trigger_price"], "stop_price": r["stop_price"], "shares": r["shares"],
                              "risk_amount": r["risk_amount"], "reasons": json.loads(r["reasons"] or "[]"),
                              **{k: v for k, v in json.loads(r["extra"] or "{}").items() if k in ("entry_ref", "close", "board", "adv20", "name")}}
                _attach_plan(lv["scan"], market)                # 点开股票时展开「明天怎么操作」
        sig = db.rows(c.execute("SELECT r.scan_date, s.setup FROM scan_results s JOIN scan_runs r ON r.run_id=s.run_id "
                                "WHERE s.symbol=? AND r.official=1 ORDER BY r.scan_date", (symbol,)))
        out["signals"] = sig
        out["earnings"] = [r[0] for r in c.execute("SELECT event_time FROM events WHERE symbol=? AND event_type='EARNINGS' ORDER BY event_time", (symbol,))]
        out["levels"] = lv
    with db.portfolio_db() as p:
        pos = p.execute("SELECT * FROM positions WHERE market=? AND symbol=? AND status='open'", (market, symbol)).fetchone()
        if pos:
            out["levels"]["position"] = {"avg_cost": pos["avg_cost"], "current_stop": pos["current_stop"], "qty": pos["qty"],
                                         "open_date": pos["open_date"]}
        out["trades"] = db.rows(p.execute("SELECT date, side, price, qty, exit_reason FROM trades WHERE market=? AND symbol=? ORDER BY date", (market, symbol)))
    return _wrap(market, **out)


@app.get("/api/indicators")
def indicators(symbol: str, names: str = "ma20,rsi14,macd_dif", market: str = "CN", limit: int = 500):
    market = _need_cn(market)
    with db.market_db(market) as c:
        panel, feat = _single_features(c, symbol)
    if panel is None:
        raise HTTPException(404, f"{symbol} 无行情数据")
    res = {}
    for n in names.split(","):
        if n in feat.f:
            s = feat.f[n][symbol].dropna().tail(limit)
            res[n] = [{"time": t, "value": round(float(v), 4)} for t, v in zip(s.index, s.values)]
    return _wrap(market, symbol=symbol, series=res, available=sorted(feat.f))


# ---- 条件筛选（漏斗之外的手动补充）----------------------------------------------

log = logging.getLogger("stock-app")
_ctx_cache: dict = {}


_ctx_lock = threading.Lock()


def _scan_ctx(market: str):
    with db.market_db(market) as c:
        day = db.get_meta(c, "data_asof") or c.execute("SELECT MAX(date) FROM daily_bar").fetchone()[0]
        if not day:
            return None, None
        key = (market, day)
        with _ctx_lock:                       # 单次构建：全市场面板约 2 GB / 40 秒，并发请求只能排队等同一份，不能各建一份（否则内存被吃光、界面卡死）
            if _ctx_cache.get("key") != key:
                _ctx_cache.clear()
                _ctx_cache["ctx"] = scanner.build_context(c, day, market)
                _ctx_cache["key"] = key
            return _ctx_cache["ctx"], day


@app.post("/api/analysis")
def analysis(body: dict = Body(default={}), market: str = "CN"):
    """综合分析 / 条件筛选：按第三章指标自定义组合筛选（RPS、成交量、波动率、趋势等），作为漏斗之外的手动补充。"""
    market = _need_cn(market)
    ctx, day = _scan_ctx(market)
    if not ctx:
        raise HTTPException(409, "尚无行情数据")
    F, l2 = ctx["feat"].f, ctx["l2"]
    last = ctx["panel"].dates[-1]
    row = lambda k: F[k].loc[last]  # noqa: E731
    m = l2.loc[last].copy()
    cond = body.get("conditions", {})
    def need(name, fn):
        nonlocal m
        if name in cond and cond[name] not in (None, "", False):
            m &= fn(cond[name]).fillna(False)
    need("min_rps20", lambda v: row("rps_20") >= float(v))
    need("min_rps60", lambda v: row("rps_60") >= float(v))
    need("min_vol_ratio", lambda v: row("vol_ratio") >= float(v))
    need("max_atr_pct", lambda v: row("atr_pct") <= float(v) / 100)
    need("min_ret20", lambda v: row("ret_20") >= float(v) / 100)
    need("max_dist_52w_high", lambda v: row("dist_52w_high") <= float(v) / 100)
    need("above_ma50", lambda v: row("close_ma50") > 1)
    need("above_ma200", lambda v: row("close_ma200") > 1)
    need("ma20_gt_ma50", lambda v: row("ma20_ma50") > 1)
    need("breakout20", lambda v: F["close"].loc[last] > F["hh20"].loc[last])
    need("min_amount_wan", lambda v: row("amt_ma20") >= float(v) * 1e4)
    sort = body.get("sort", "lowrisk")
    if sort == "lowrisk":                                   # 低换手 + 低波动（与候选排序同一口径）
        key = setups.score_frame(ctx["feat"], method="lowrisk").loc[last]
    else:
        key = row(sort if sort in F else "rps_20")
    syms = key[m].sort_values(ascending=False).head(int(body.get("limit", 200))).index
    items = []
    for s in syms:
        items.append({"symbol": s, "name": ctx["names"].get(s, s), "industry": ctx["industry"].get(s) if len(ctx["industry"]) else None,
                      "close": float(ctx["panel"].raw["close"].loc[last, s]), "ret_20": row("ret_20")[s], "rps_20": row("rps_20")[s],
                      "rps_60": row("rps_60")[s], "vol_ratio": row("vol_ratio")[s], "atr_pct": row("atr_pct")[s],
                      "dist_52w_high": row("dist_52w_high")[s], "rsi14": row("rsi14")[s]})
    return _wrap(market, date=last, total=int(m.sum()), universe=int(l2.loc[last].sum()), items=items)


@app.get("/api/analysis")
def analysis_get(market: str = "CN"):
    return analysis({}, market)


# ---- 选股 ---------------------------------------------------------------------

def _run_row(c, run_id: str) -> dict | None:
    r = c.execute("SELECT * FROM scan_runs WHERE run_id=?", (run_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    gate = json.loads(r.get("gate_detail") or "{}")
    reg = json.loads(r.get("regime_detail") or "{}")
    try:
        entry_mode = json.loads(r.get("config") or "{}").get("execution", {}).get("entry_mode")
    except ValueError:
        entry_mode = None
    return {"entry_mode": entry_mode or settings.cfg()["execution"]["entry_mode"], "run_id": r["run_id"], "market": r["market"], "scan_date": r["scan_date"], "data_asof": r["data_asof"],
            "config_hash": r["config_hash"], "official": bool(r["official"]), "started_at": r["started_at"], "finished_at": r["finished_at"],
            "gate": {"status": r["data_gate_status"], "checks": gate.get("checks", []), "reasons": gate.get("reasons", [])},
            "regime": reg or {"state": r["regime"]}, "summary": json.loads(r.get("summary") or "{}"),
            "valid_until": mc.next_trading_day(c, r["scan_date"]), "is_stale": scanner._is_stale(c, r["market"], r["scan_date"])}


def _attach_plan(cd: dict, market: str, acct: dict | None = None) -> dict:
    """明天的操作计算（按「当前」账户资金；录入资金晚于扫描时也能用）：买多少股、止损 / 止盈价、各种开盘价下怎么做。"""
    acct = acct if acct is not None else portfolio.get_account(market)
    ref = cd.get("entry_ref") or cd.get("close")
    cd["op"] = None
    if acct.get("equity"):
        cd["op"] = selection.operation_plan(ref, cd.get("stop_price"), cd.get("board"), acct["equity"],
                                            acct.get("risk_per_trade") or settings.cfg()["portfolio"]["risk_per_trade"],
                                            cd.get("adv20"), settings.cfg()["execution"].get("max_adv_pct"))
        if cd["op"] and cd.get("shares") is None:
            cd["shares"], cd["risk_amount"] = cd["op"]["planned_shares"], cd["op"]["planned_loss"]
    tp_pct = settings.cfg()["exits"].get("take_profit_pct") or 0
    cd["take_profit_pct"], cd["take_profit"] = tp_pct, (round(ref * (1 + tp_pct), 2) if tp_pct and ref else None)
    cd["open_plan"] = selection.open_plan(ref, cd.get("stop_price"), cd.get("board"), cd["op"], market)
    cd["account"] = {k: acct.get(k) for k in ("equity", "risk_per_trade")}
    return cd


@app.get("/api/scan")
def scan_get(market: str = "CN", date: str | None = None, run_id: str | None = None, preview: bool = False):
    """读取已存档的观察清单（不重算）。闸门 INCOMPLETE 时不展示清单，仅给原因；preview=1 时即时计算「仅供参考」版本。"""
    market = _need_cn(market)
    with db.market_db(market) as c:
        if run_id:
            rid = run_id
        else:
            q = "SELECT run_id FROM scan_runs WHERE market=?" + (" AND scan_date=?" if date else "") + \
                " ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1"
            row = c.execute(q, (market, date) if date else (market,)).fetchone()
            rid = row["run_id"] if row else None
        if not rid:
            return _wrap(market, run=None, candidates=[], message="尚无扫描记录。请先初始化数据，并运行盘后任务或点击「立即扫描」。")
        run = _run_row(c, rid)
        cands = []
        if run["official"]:
            for r in c.execute("SELECT * FROM scan_results WHERE run_id=? ORDER BY score DESC", (rid,)):
                ex = json.loads(r["extra"] or "{}")
                cands.append({"symbol": r["symbol"], "setup": r["setup"], "score": r["score"], "reasons": json.loads(r["reasons"] or "[]"),
                              "trigger_price": r["trigger_price"], "stop_price": r["stop_price"], "risk_amount": r["risk_amount"],
                              "shares": r["shares"], **ex})
            outc = {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM scan_outcomes WHERE run_id=?", (rid,))}
            for cd in cands:
                cd["outcome"] = outc.get(cd["symbol"])
        held = {r["symbol"] for r in portfolio.list_positions(market)}
        wl = {r["symbol"] for r in _watch_rows(market)}
        acct = portfolio.get_account(market)
        for cd in cands:
            cd["held"], cd["watched"] = cd["symbol"] in held, cd["symbol"] in wl
            _attach_plan(cd, market, acct)
    if not run["official"] and preview:
        res = scanner.run_scan(market, scan_date=run["scan_date"], persist=False)
        return _wrap(market, run={**run, "preview_only": True}, candidates=res.get("candidates", []), preview=True)
    return _wrap(market, run=run, candidates=cands, account=portfolio.get_account(market),
                 message=None if run["official"] else "数据完整性闸门未通过：不生成观察清单。可选择「仅供参考」查看。")


@app.post("/api/scan/run")
def scan_run(market: str = "CN", date: str | None = None):
    market = _need_cn(market)
    res = scanner.run_scan(market, scan_date=date)
    if res.get("error"):
        raise HTTPException(409, res["message"])
    if res.get("official"):
        portfolio.health(market, res["scan_date"])
    return _wrap(market, run={k: v for k, v in res.items() if k != "candidates"}, candidates=res["candidates"])


@app.get("/api/scan/history")
def scan_history(market: str = "CN", limit: int = 60):
    """历史扫描回看：按日期查看当时存档的清单（scan_results）及其后续收益（T+1/3/5/10/20），评估选股器命中率。"""
    market = _need_cn(market)
    with db.market_db(market) as c:
        runs = db.rows(c.execute("SELECT run_id, scan_date, data_gate_status, regime, n_candidates, official FROM scan_runs "
                                 "WHERE market=? ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT ?", (market, limit)))
        for r in runs:
            o = c.execute("SELECT COUNT(*) n, AVG(ret_1d) a1, AVG(ret_3d) a3, AVG(ret_5d) a5, AVG(ret_10d) a10, AVG(ret_20d) a20, "
                          "AVG(ret_5d>0) w5, AVG(ret_20d>0) w20 FROM scan_outcomes WHERE run_id=? AND filled=1", (r["run_id"],)).fetchone()
            r["outcome"] = dict(o)
        agg = c.execute("SELECT COUNT(*) n, AVG(o.ret_1d) a1, AVG(o.ret_3d) a3, AVG(o.ret_5d) a5, AVG(o.ret_10d) a10, AVG(o.ret_20d) a20, "
                        "AVG(o.ret_5d>0) w5, AVG(o.ret_20d>0) w20, AVG(o.mae) mae, AVG(o.mfe) mfe FROM scan_outcomes o "
                        "JOIN scan_runs r ON r.run_id=o.run_id WHERE r.market=? AND o.filled=1", (market,)).fetchone()
    return _wrap(market, runs=runs, overall=dict(agg), note="scan_outcomes 是真实发生的前向收益，是回测之外最无偏的证据（5.4）。")


@app.post("/api/scan/outcomes")
def scan_outcomes(market: str = "CN"):
    market = _need_cn(market)
    return _wrap(market, **scanner.backfill_outcomes(market))


# ---- 股票池 / 数据任务 --------------------------------------------------------

@app.get("/api/universe")
def universe_get(market: str = "CN", level: str = "", limit: int = 300):
    market = _need_cn(market)
    with db.market_db(market) as c:
        l1 = json.loads(db.get_meta(c, "l1_stats", "{}") or "{}")
        n_sec = c.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
        n_l1 = c.execute("SELECT COUNT(*) FROM securities WHERE in_l1=1").fetchone()[0]
        n_del = c.execute("SELECT COUNT(*) FROM securities WHERE status='delisted'").fetchone()[0]
        n_bars = c.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
        out = {"securities": n_sec, "l1": n_l1, "delisted_kept": n_del, "bars": n_bars, "l1_stats": l1, "l2": None,
               "l2_exclusions": None, "thresholds": markets.universe_cfg(market)}
        asof = db.get_meta(c, "data_asof")
        if n_bars and asof:
            ctx, _ = _scan_ctx(market)
            l2 = ctx["l2"]
            last = ctx["panel"].dates[-1]
            out["l2"] = int(l2.loc[last].sum())
            out["l2_exclusions"] = universe.l2_exclusion_stats(ctx["cond"])
            if level.upper() in ("L1", "L2"):
                names = ctx["names"]
                if level.upper() == "L2":
                    syms = list(l2.columns[l2.loc[last].to_numpy()])[:limit]
                else:
                    syms = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE in_l1=1 LIMIT ?", (limit,))]
                out["items"] = [{"symbol": s, "name": names.get(s, s)} for s in syms]
    return _wrap(market, **out)


@app.get("/api/jobs")
def jobs_get(market: str = "CN"):
    market = _need_cn(market)
    with db.market_db(market) as c:
        last_run = c.execute("SELECT run_id, scan_date, data_gate_status, gate_detail, regime, finished_at FROM scan_runs "
                             "WHERE market=? ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1", (market,)).fetchone()
        logs = db.rows(c.execute("SELECT ts, job, status, detail FROM job_log ORDER BY id DESC LIMIT 15"))
        gate = None
        asof = db.get_meta(c, "data_asof")
        if asof:
            g = quality.check_gate(c, market, asof)
            gate = {k: g[k] for k in ("status", "day", "checks", "reasons")}
        info = {"data_asof": asof, "calendar_source": db.get_meta(c, "calendar_source"),
                "last_closed_trading_day": mc.last_closed_trading_day(c, market) if mc.trading_days(c) else None,
                "industry_map_asof": db.get_meta(c, "industry_map_asof"),
                "has_data": bool(c.execute("SELECT 1 FROM daily_bar LIMIT 1").fetchone()),
                "init_sample": (lambda d: {"n": d["n"], "count": len(d["symbols"]), "at": d.get("at")} if d else None)(json.loads(db.get_meta(c, "init_sample") or "null")),
                "init_sample_default": int(settings.cfg().get("init", {}).get("sample_size", 0) or 0),
                "data_ready_observed": ingest.ready_stats(c, market), "industry_history": ingest.industry_history_stats(c)}
    return _wrap(market, job=manager.snapshot(), gate=gate, last_run=dict(last_run) if last_run else None,
                 logs=logs, backup=backup.backup_status(), throttle=throttle.all_status(), **info)


@app.post("/api/jobs/{task}")
def jobs_start(task: str, market: str = "CN", body: dict = Body(default={})):
    market = _need_cn(market)
    if task == "stop":
        manager.stop()
        return {"ok": True}
    if task not in ("init", "daily", "catch_up", "backup"):
        raise HTTPException(404, "未知任务")
    kw = {}
    if task == "init":
        kw = {"limit": body.get("limit"), "sample": body.get("sample"), "resample": bool(body.get("resample"))}
        if body.get("remember") and body.get("sample") is not None:          # 记住这次的选择（下次默认沿用）
            settings.save_user_config({"init": {"sample_size": int(body["sample"] or 0)}})
    return manager.start(task, market=market, **kw)


# ---- 自选 / 持仓 / 账户 / 交易 ----------------------------------------------------

def _watch_rows(market: str) -> list[dict]:
    with db.portfolio_db() as p:
        return db.rows(p.execute("SELECT * FROM watchlist WHERE market=? ORDER BY added_at DESC", (market,)))


@app.get("/api/watchlist")
def watchlist_get(market: str = "CN"):
    market = _need_cn(market)
    rows = _watch_rows(market)
    with db.market_db(market) as c:
        q = _quotes(c, [r["symbol"] for r in rows])
        names = {r["symbol"]: r["name"] for r in db.rows(c.execute("SELECT symbol,name FROM securities"))}
        ind = {r["symbol"]: r["industry"] for r in db.rows(c.execute("SELECT symbol,industry FROM industry_map"))}
    for r in rows:
        r.update(name=names.get(r["symbol"], r["symbol"]), industry=ind.get(r["symbol"]), quote=q.get(r["symbol"]))
    return _wrap(market, items=rows)


@app.put("/api/watchlist")
def watchlist_put(body: dict = Body(...), market: str = "CN"):
    """body: {add:[{symbol,note}], remove:[symbol], note:{symbol:note}} 或 {items:[...]}（整体替换）。"""
    market = _need_cn(market)
    now = date.today().isoformat()
    with db.portfolio_db() as p:
        if "items" in body:
            p.execute("DELETE FROM watchlist WHERE market=?", (market,))
            for it in body["items"]:
                p.execute("INSERT OR REPLACE INTO watchlist(market,symbol,added_at,note) VALUES(?,?,?,?)",
                          (market, it["symbol"], it.get("added_at") or now, it.get("note")))
        for it in body.get("add", []):
            it = {"symbol": it} if isinstance(it, str) else it
            p.execute("INSERT OR IGNORE INTO watchlist(market,symbol,added_at,note) VALUES(?,?,?,?)", (market, it["symbol"], now, it.get("note")))
        for s in body.get("remove", []):
            p.execute("DELETE FROM watchlist WHERE market=? AND symbol=?", (market, s))
        for s, n in (body.get("note") or {}).items():
            p.execute("UPDATE watchlist SET note=? WHERE market=? AND symbol=?", (n, market, s))
    return watchlist_get(market)


@app.get("/api/account")
def account_get(market: str = "CN"):
    return _wrap(_need_cn(market), account=portfolio.get_account(market))


@app.put("/api/account")
def account_put(body: dict = Body(...), market: str = "CN"):
    market = _need_cn(market)
    return _wrap(market, account=portfolio.set_account(market, body.get("equity"), body.get("cash"), body.get("risk_per_trade")))


@app.get("/api/positions")
def positions_get(market: str = "CN", status: str = "open"):
    market = _need_cn(market)
    return _wrap(market, items=portfolio.list_positions(market, None if status == "all" else status), account=portfolio.get_account(market))


@app.post("/api/positions")
def positions_post(body: dict = Body(...), market: str = "CN"):
    market = _need_cn(market)
    try:
        return _wrap(market, item=portfolio.upsert_position(market, body))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"持仓参数错误：{e}")


@app.put("/api/positions/{pid}")
def positions_put(pid: int, body: dict = Body(...), market: str = "CN"):
    market = _need_cn(market)
    return _wrap(market, item=portfolio.upsert_position(market, {**body, "id": pid}))


@app.delete("/api/positions/{pid}")
def positions_delete(pid: int, market: str = "CN"):
    portfolio.delete_position(_need_cn(market), pid)
    return {"ok": True}


@app.get("/api/portfolio/health")
def portfolio_health(market: str = "CN", date: str | None = None):
    market = _need_cn(market)
    res = portfolio.health(market, date, save=date is None)
    if res.get("error"):
        raise HTTPException(409, "尚无行情数据")
    return res


@app.get("/api/trades")
def trades_get(market: str = "CN", start: str | None = None, end: str | None = None):
    market = _need_cn(market)
    return _wrap(market, items=portfolio.list_trades(market, start, end), exit_reasons=list(portfolio.EXIT_REASONS))


@app.post("/api/trades")
def trades_post(body: dict = Body(...), market: str = "CN"):
    """录入一笔交易；body 可为单笔，或 {rows:[...]}（CSV 批量导入，前端 parse.js 解析后提交）。"""
    market = _need_cn(market)
    rows = body.get("rows") or [body]
    done, errors = [], []
    for i, r in enumerate(rows):
        try:
            done.append(portfolio.record_trade(market, r))
        except (KeyError, ValueError) as e:
            errors.append({"row": i + 1, "error": str(e)})
    if errors and not done:
        raise HTTPException(400, errors[0]["error"] if len(rows) == 1 else errors)
    return _wrap(market, items=done, errors=errors)


@app.delete("/api/trades/{trade_id}")
def trades_delete(trade_id: int, market: str = "CN"):
    portfolio.delete_trade(_need_cn(market), trade_id)
    return {"ok": True}


@app.get("/api/journal/stats")
def journal_stats(market: str = "CN", group_by: str = "setup,regime"):
    market = _need_cn(market)
    return _wrap(market, **portfolio.journal_stats(market, group_by))


# ---- 预警（4.6）：价格 / 涨跌幅预警。形态为页面内提醒，**仅在页面打开时生效**（邮件 / 手机推送为后续项）----

ALERT_KINDS = {"price_above": "收盘价 ≥", "price_below": "收盘价 ≤", "pct_above": "涨幅 ≥ (%)", "pct_below": "跌幅 ≤ (%)"}


def _alert_hit(rule: str, q: dict | None) -> bool:
    if not q:
        return False
    kind, _, val = rule.partition(":")
    try:
        v = float(val)
    except ValueError:
        return False
    if kind == "price_above":
        return q["close"] >= v
    if kind == "price_below":
        return q["close"] <= v
    pct = (q.get("change_pct") or 0) * 100
    return pct >= v if kind == "pct_above" else pct <= v if kind == "pct_below" else False


@app.get("/api/alerts")
def alerts_get(market: str = "CN", symbol: str | None = None):
    market = _need_cn(market)
    with db.portfolio_db() as p:
        q = "SELECT * FROM alerts WHERE market=?" + (" AND symbol=?" if symbol else "")
        rows = db.rows(p.execute(q, (market, symbol) if symbol else (market,)))
    with db.market_db(market) as c:
        quotes = _quotes(c, sorted({r["symbol"] for r in rows}))
        names = {r["symbol"]: r["name"] for r in db.rows(c.execute("SELECT symbol,name FROM securities"))}
    for r in rows:
        r["name"] = names.get(r["symbol"], r["symbol"])
        r["quote"] = quotes.get(r["symbol"])
        r["triggered"] = bool(r["active"]) and _alert_hit(r["rule"], r["quote"])
    return _wrap(market, items=rows, kinds=ALERT_KINDS,
                 note="预警仅在页面打开时生效；基于盘后日线数据，不是盘中实时价。")


@app.post("/api/alerts")
def alerts_post(body: dict = Body(...), market: str = "CN"):
    market = _need_cn(market)
    kind, val = body.get("kind"), body.get("value")
    if kind not in ALERT_KINDS or val in (None, ""):
        raise HTTPException(400, "预警类型 / 数值无效")
    with db.portfolio_db() as p:
        cur = p.execute("INSERT INTO alerts(market,symbol,rule,active) VALUES(?,?,?,1)", (market, body["symbol"], f"{kind}:{float(val)}"))
    return {"ok": True, "id": cur.lastrowid}


@app.delete("/api/alerts/{aid}")
def alerts_delete(aid: int, market: str = "CN"):
    with db.portfolio_db() as p:
        p.execute("DELETE FROM alerts WHERE id=? AND market=?", (aid, _need_cn(market)))
    return {"ok": True}


@app.get("/api/export")
def export(market: str = "CN"):
    return portfolio.export_all(_need_cn(market))


# ---- 事件 / 资讯（M6）-------------------------------------------------------------

def _scope_symbols(conn, market: str, scope: str | None) -> list[str] | None:
    """scope: held 持仓 | watch 自选 | candidates 最新观察清单 | None 全部。"""
    if not scope or scope == "all":
        return None
    syms: list[str] = []
    if scope == "held":
        syms = [r["symbol"] for r in portfolio.list_positions(market)]
    elif scope == "watch":
        syms = [r["symbol"] for r in _watch_rows(market)]
    elif scope == "candidates":
        run = conn.execute("SELECT run_id FROM scan_runs WHERE market=? AND official=1 ORDER BY scan_date DESC, finished_at DESC, rowid DESC LIMIT 1", (market,)).fetchone()
        if run:
            syms = [r[0] for r in conn.execute("SELECT symbol FROM scan_results WHERE run_id=?", (run[0],))]
    return syms


def _forward_returns(conn, symbol: str, day: str) -> dict:
    """事件之后 1 / 3 / 5 个交易日的收盘收益（基准 = 事件当日或之后第一个交易日的收盘；3.18：研究消息发生后的价格表现）。"""
    rows = conn.execute("SELECT date, close, adj_factor FROM daily_bar WHERE symbol=? AND date>=? ORDER BY date LIMIT 6", (symbol, day)).fetchall()
    if len(rows) < 2:
        return {}
    base = rows[0]["close"] * rows[0]["adj_factor"]
    out = {}
    for k in (1, 3, 5):
        if len(rows) > k:
            out[f"ret_{k}d"] = rows[k]["close"] * rows[k]["adj_factor"] / base - 1
    return out


@app.get("/api/events")
def events(market: str = "CN", symbol: str | None = None, type: str | None = None, scope: str | None = None,
           days: int = 90, limit: int = 200, min_level: int | None = None, with_returns: bool = True):
    """统一事件流（3.21）：公告 / 新闻 / Insider / 财报日历。可信度等级 1 官方 > 2 结构化 > 3 媒体（3.22）；
    每条带 event_time / publish_time / ingested_at（3.23）。事件之后的 1/3/5 日收益用于评估消息是否真有交易价值（3.18）。"""
    market = _need_cn(market)
    since = (date.today() - timedelta(days=days)).isoformat()
    with db.market_db(market) as c:
        syms = [symbol] if symbol else _scope_symbols(c, market, scope)
        q = "SELECT e.*, s.name FROM event_stream e LEFT JOIN securities s ON s.symbol=e.symbol WHERE e.event_time>=?"
        a: list = [since]
        if syms is not None:
            if not syms:
                return _wrap(market, items=[], types=ev_mod.TYPE_LABEL)
            q += " AND e.symbol IN (%s)" % ",".join("?" * len(syms))
            a += syms
        if type:
            q += " AND e.event_type=?"
            a.append(type)
        if min_level:
            q += " AND e.level<=?"
            a.append(min_level)
        rows = db.rows(c.execute(q + " ORDER BY e.event_time DESC, e.publish_time DESC LIMIT ?", a + [limit]))
        # 财报日历（events 表）并入同一事件流
        if not type or type == "EARNINGS":
            qq = "SELECT e.*, s.name FROM events e LEFT JOIN securities s ON s.symbol=e.symbol WHERE e.event_type='EARNINGS' AND e.event_time>=?"
            aa: list = [since]
            if syms:
                qq += " AND e.symbol IN (%s)" % ",".join("?" * len(syms))
                aa += syms
            for r in db.rows(c.execute(qq + " ORDER BY e.event_time DESC LIMIT ?", aa + [limit])):
                rows.append({"uid": f"cal-{r['event_id']}", "symbol": r["symbol"], "name": r["name"], "market": market, "event_time": r["event_time"],
                             "publish_time": r["publish_time"], "ingested_at": r["ingested_at"], "event_type": "EARNINGS", "source": r["source"],
                             "level": 2, "title": r["title"] or "财报披露", "summary": "", "url": None, "sentiment": None, "calendar": True})
        rows.sort(key=lambda r: (r["event_time"] or "", r.get("publish_time") or ""), reverse=True)
        rows = rows[:limit]
        if with_returns:
            for r in rows[:120]:
                if r["event_time"] <= date.today().isoformat():
                    r.update(_forward_returns(c, r["symbol"], r["event_time"]))
    return _wrap(market, items=rows, types=ev_mod.TYPE_LABEL,
                 note="公告 / SEC 为官方原始信息（等级 1）；Yahoo 新闻为媒体（等级 3）。事件只对候选与持仓按需拉取并缓存 6 小时。")


@app.get("/api/events/stats")
def events_stats(market: str = "CN", scope: str | None = None, days: int = 365, min_n: int = 30):
    """事件研究（3.18）：按事件类型 / 情绪方向统计事件之后 1 / 3 / 5 个交易日的平均收益与上涨占比。
    重点不是「新闻 = 利好」，而是历史上类似事件发生后价格真实怎么走；样本不足（< min_n）明确标注，不下结论。"""
    market = _need_cn(market)
    since = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()
    with db.market_db(market) as c:
        syms = _scope_symbols(c, market, scope)
        q = "SELECT symbol, event_time, event_type, level, sentiment FROM event_stream WHERE event_time>=? AND event_time<=?"
        a: list = [since, today]
        if syms is not None:
            if not syms:
                return _wrap(market, groups=[], n_events=0, min_n=min_n)
            q += " AND symbol IN (%s)" % ",".join("?" * len(syms))
            a += syms
        rows = db.rows(c.execute(q + " LIMIT 5000", a))
        buckets: dict = {}
        for r in rows:
            fr = _forward_returns(c, r["symbol"], r["event_time"])
            if not fr:
                continue
            tone = "利好倾向" if (r["sentiment"] or 0) > 0 else "利空倾向" if (r["sentiment"] or 0) < 0 else "中性 / 未判定"
            for key in ((r["event_type"], "全部"), (r["event_type"], tone)):
                b = buckets.setdefault(key, {"event_type": key[0], "tone": key[1], "n": 0, "r1": [], "r3": [], "r5": []})
                b["n"] += 1
                for k, lst in (("ret_1d", "r1"), ("ret_3d", "r3"), ("ret_5d", "r5")):
                    if k in fr:
                        b[lst].append(fr[k])
    out = []
    for b in buckets.values():
        row = {"event_type": b["event_type"], "tone": b["tone"], "n": b["n"], "enough": b["n"] >= min_n}
        for k, lst in (("1d", "r1"), ("3d", "r3"), ("5d", "r5")):
            v = b[lst]
            row[f"mean_{k}"] = float(np.mean(v)) if v else None
            row[f"up_{k}"] = float(np.mean([x > 0 for x in v])) if v else None
        out.append(row)
    out.sort(key=lambda r: (r["event_type"], r["tone"] != "全部", r["tone"]))
    return _wrap(market, groups=out, n_events=len(rows), min_n=min_n,
                 note="基准 = 事件当日收盘；A 股 / 美股只统计已入库（候选 + 持仓按需拉取）的事件，样本通常很少，请勿据此下结论。")


@app.post("/api/events/refresh")
def events_refresh(body: dict = Body(default={}), market: str = "CN"):
    """按需拉取事件：symbols 指定，或 scope = held / watch / candidates。force=true 忽略缓存。"""
    market = _need_cn(market)
    cap = int(settings.cfg().get("events", {}).get("max_symbols_per_run", 40))
    with db.market_db(market) as c:
        syms = body.get("symbols") or _scope_symbols(c, market, body.get("scope") or "held") or []
        syms = syms[:cap]
        if not syms:
            return _wrap(market, requested=0, ok=0, failed=0, new_events=0, cached=0)
        src = get_source(market)
        try:
            res = ingest.ensure_events(c, ev_mod.get_provider(market, src), syms, market, force=bool(body.get("force")))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"事件源不可用：{e}")
        finally:
            if hasattr(src, "close"):
                src.close()
    return _wrap(market, **res)


# ---- 行情总览（M6 行情页）-----------------------------------------------------------

INDEX_NAMES = {"sh.000016": "上证50", "sh.000300": "沪深300", "sh.000001": "上证指数", "sz.399001": "深证成指", "sz.399006": "创业板指", "sh.000905": "中证500",
               "sh.000852": "中证1000", "SPY": "SPY (S&P 500)", "QQQ": "QQQ (Nasdaq 100)", "IWM": "IWM (小盘)", "DIA": "DIA (道指)",
               "^GSPC": "S&P 500", "^IXIC": "Nasdaq 综合", "^VIX": "VIX 恐慌指数",
               "XLK": "XLK 科技", "XLF": "XLF 金融", "XLV": "XLV 医疗", "XLY": "XLY 可选消费", "XLP": "XLP 必需消费", "XLE": "XLE 能源", "XLI": "XLI 工业",
               "XLU": "XLU 公用事业", "XLB": "XLB 材料", "XLRE": "XLRE 房地产", "XLC": "XLC 通信"}


@app.get("/api/market/overview")
def market_overview(market: str = "CN", movers: int = 12):
    """全市场行情总览（盘后）：指数、市场宽度与环境状态、行业 / 板块涨跌（库内个股合成，3.14）、涨跌幅 / 放量 / 强势排行（交易池 L2 内）。"""
    market = _need_cn(market)
    ctx, day = _scan_ctx(market)
    if not ctx:
        raise HTTPException(409, "尚无行情数据")
    F, l2, panel = ctx["feat"].f, ctx["l2"], ctx["panel"]
    last = panel.dates[-1]
    out: dict = {"date": last}
    with db.market_db(market) as c:
        idx = []
        for sym in markets.gate_benchmarks(market) + markets.aux_indices(market):
            r = c.execute("SELECT date, close FROM index_bar WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 260", (sym, last)).fetchall()
            if len(r) < 2:
                continue
            cl = [x["close"] for x in r]
            ma50 = sum(cl[:50]) / 50 if len(cl) >= 50 else None
            ma200 = sum(cl[:200]) / 200 if len(cl) >= 200 else None
            idx.append({"symbol": sym, "name": INDEX_NAMES.get(sym, sym), "date": r[0]["date"], "close": cl[0], "chg_1d": cl[0] / cl[1] - 1,
                        "ret_5d": cl[0] / cl[5] - 1 if len(cl) > 5 else None, "ret_20d": cl[0] / cl[20] - 1 if len(cl) > 20 else None,
                        "above_ma50": None if ma50 is None else cl[0] > ma50, "above_ma200": None if ma200 is None else cl[0] > ma200})
    out["indices"] = [i for i in idx if i["symbol"] in markets.gate_benchmarks(market)]
    out["sector_etfs"] = [i for i in idx if i["symbol"] in settings.cfg()["gate"].get("sector_etfs_us", [])] if market == "US" else []
    mem = l2.loc[last]
    r1 = F["ret_1"].loc[last][mem]
    m = ctx["feat"].m
    g = lambda k: None if k not in m or m[k].loc[last] != m[k].loc[last] else float(m[k].loc[last])  # noqa: E731
    out["breadth"] = {"pool": int(mem.sum()), "up": int((r1 > 0).sum()), "down": int((r1 < 0).sum()), "flat": int((r1 == 0).sum()), "adv_ratio": g("adv_ratio"),
                      "above_ma20": g("breadth_ma20"), "above_ma50": g("breadth_ma50"), "above_ma200": g("breadth_ma200"), "new_highs": g("new_high_cnt"),
                      "vix": g("vix"), "regime": str(ctx["feat"].regime.loc[last]) if ctx["feat"].regime is not None else None}
    ind = ctx["industry"].reindex(panel.symbols)
    df = pd.DataFrame({"industry": ind, "r1": F["ret_1"].loc[last], "r5": F["ret_5"].loc[last], "r20": F["ret_20"].loc[last], "m": mem}).dropna(subset=["industry"])
    df = df[df["m"]]
    grp = df.groupby("industry").agg(n=("r1", "size"), ret_1d=("r1", "mean"), ret_5d=("r5", "mean"), ret_20d=("r20", "mean"), up=("r1", lambda x: float((x > 0).mean())))
    out["industries"] = [{"industry": k, **{c: (None if v != v else v) for c, v in row.items()}} for k, row in grp.sort_values("ret_20d", ascending=False).iterrows() if row["n"] >= 3]

    def top(series, ascending=False, k=movers, extra=()):
        s = series[mem].dropna().sort_values(ascending=ascending).head(k)
        rows = []
        for sym, v in s.items():
            rows.append({"symbol": sym, "name": ctx["names"].get(sym, sym), "value": float(v), "close": float(panel.raw["close"].loc[last, sym]),
                         "ret_1d": float(F["ret_1"].loc[last, sym]), "vol_ratio": None if F["vol_ratio"].loc[last, sym] != F["vol_ratio"].loc[last, sym] else float(F["vol_ratio"].loc[last, sym]),
                         "industry": ind.get(sym) if ind.get(sym) == ind.get(sym) else None})
        return rows

    row = lambda k: F[k].loc[last]  # noqa: E731
    out["movers"] = {"gainers": top(row("ret_1")), "losers": top(row("ret_1"), True), "volume": top(row("vol_ratio")), "strong": top(row("rps_20"))}
    return _wrap(market, **out)


@app.get("/api/storage")
def storage_get():
    """数据目录各部分大小与用途（设置页「存储空间」）。"""
    return backup.storage_report()


@app.post("/api/storage/clean")
def storage_clean(body: dict = Body(...)):
    """用户在设置页确认后执行：删除演示数据 / 旧备份，或压缩全量备份。核心数据不在可清理范围内。"""
    try:
        return backup.storage_clean(str(body.get("key", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/market/view")
def market_view_get(market: str = "CN"):
    """市场温度（全A站上 20 日线比例 + 历史证据）+ 宽基指数 / ETF 的规则买入位、止损位、规则仓位与回测。"""
    from . import market_view
    market = _need_cn(market)
    d = dict(market_view.build(market))
    acct = portfolio.get_account(market)
    from .factor_portfolio import allocation
    al = allocation()
    etf_pct = al["etf"]
    if d.get("status") == "ok" and acct.get("equity") and etf_pct and d.get("indices"):
        per_index = acct["equity"] * etf_pct / len(d["indices"])
        rule = d["rule"]
        d["allocation"] = {"equity": acct["equity"], "etf_pct": etf_pct, "etf_amount": round(acct["equity"] * etf_pct, 2),
                           "factor_pct": al["factor"], "factor_amount": round(acct["equity"] * al["factor"], 2),
                           "factor_n": settings.cfg()["factor_portfolio"]["n"], "swing_pct": al["swing"],
                           "stock_amount": round(acct["equity"] * al["swing"], 2), "per_index": round(per_index, 2),
                           "trend_amount": round(per_index * rule["trend"]["weight"], 2), "washout_amount": round(per_index * rule["washout"]["weight"], 2),
                           "stock_max_positions": settings.cfg()["portfolio"]["max_positions"],
                           "stock_risk_per_trade": acct.get("risk_per_trade") or settings.cfg()["portfolio"]["risk_per_trade"]}
        d["indices"] = [{**i, "hold_amount": round(per_index * i["position"], 2)} for i in d["indices"]]
    return _wrap(market, **d)


@app.get("/api/factor/plan")
def factor_plan(market: str = "CN", summary: bool = True):
    """低风险组合：今天的目标名单、与「组合」持仓比较后的卖 / 买、调仓日历、资金分配；summary=1 时附回测证据（后台缓存）。"""
    from . import factor_portfolio as fpm
    market = _need_cn(market)
    if market != "CN":
        return _wrap(market, status="unsupported", message="低风险组合只在 A 股做过验证（美股没有做同样的检验），暂不提供。")
    ctx, day = _scan_ctx(market)
    if not ctx:
        return _wrap(market, status="no_data", message="尚无行情数据：请先初始化数据。")
    acct = portfolio.get_account(market)
    with db.market_db(market) as c:
        plan = fpm.live_plan(c, ctx, acct, portfolio.list_positions(market), market)
    if summary:
        plan["backtest"] = fpm.get_summary(market, day, plan["allocation"]["sleeve"])
    return _wrap(market, **plan)


# ---- 基本面摘要（3.15）----------------------------------------------------------------

@app.get("/api/financials")
def financials(symbol: str, market: str = "CN", force: bool = False):
    """公司基本面摘要，7 天缓存。A 股带公告日（点时可用）；美股无披露时间，仅展示。"""
    from . import fundamentals
    market = _need_cn(market)
    src = get_source(market)
    try:
        with db.market_db(market) as c:
            return _wrap(market, **fundamentals.get_financials(c, market, symbol, src, force))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基本面数据源不可用：{type(e).__name__}: {str(e)[:120]}")
    finally:
        if hasattr(src, "close"):
            src.close()


# ---- 资金与情绪数据（3.20）---------------------------------------------------------------

@app.get("/api/flows")
def flows(symbol: str, market: str = "CN", force: bool = False):
    """A 股：融资融券 / 龙虎榜 / 北向持股；美股：做空数据 / 期权 Put-Call / 机构持股。原始披露数据，12 小时缓存，只用于展示。"""
    from . import flows as fl
    market = _need_cn(market)
    src = get_source(market) if market == "US" else None
    try:
        with db.market_db(market) as c:
            return {**_wrap(market), **fl.get_flows(c, market, symbol, src, force)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资金数据源不可用：{type(e).__name__}: {str(e)[:120]}")


# ---- 盘中低频刷新（可选；美股）---------------------------------------------------------

_live_cache: dict = {}


@app.get("/api/quote/live")
def quote_live(symbols: str, market: str = "CN"):
    """盘中低频报价（可选，≥60 秒缓存，仅页面打开时由前端轮询）。A 股：东财 push2；美股：yfinance。"""
    market = _need_cn(market)
    syms = [x for x in symbols.split(",") if x][:40]
    import time as _t
    now = _t.time()
    key = lambda x: (market, x)  # noqa: E731
    need = [x for x in syms if key(x) not in _live_cache or now - _live_cache[key(x)][0] > 60]
    if need and not markets.is_demo(market):
        try:
            if market == "CN":
                from .datasource_cn import live_quotes
                got = live_quotes(need)
            else:
                from .datasource_us import to_yahoo
                import yfinance as yf
                th = throttle.get("yfinance")
                got = {}
                for sy in need:
                    fi = th.call(lambda sy=sy: yf.Ticker(to_yahoo(sy)).fast_info, retries=1)
                    got[sy] = {"price": float(fi["last_price"]), "prev_close": float(fi["previous_close"])}
            for k, v in got.items():
                _live_cache[key(k)] = (now, v)
        except Exception as e:  # noqa: BLE001
            return {"supported": True, "error": str(e)[:200], "quotes": {k: _live_cache[key(k)][1] for k in syms if key(k) in _live_cache}}
    elif need:                                                # 演示数据源：用最近收盘价模拟「盘中价」（微小随机波动）
        import random
        with db.market_db(market) as c:
            for x in need:
                q = _quotes(c, [x]).get(x)
                if q:
                    _live_cache[key(x)] = (now, {"price": q["close"] * (1 + random.uniform(-0.004, 0.004)), "prev_close": q["close"]})
    out = {}
    for k in syms:
        if key(k) in _live_cache:
            v = _live_cache[key(k)][1]
            out[k] = {**v, "change": v["price"] - v["prev_close"], "change_pct": v["price"] / v["prev_close"] - 1, "as_of": _live_cache[key(k)][0]}
    return {"supported": True, "quotes": out, "min_interval_s": 60, "demo": markets.is_demo(market)}


# ---- 回测 ---------------------------------------------------------------------

_bt_jobs: dict[str, dict] = {}


@app.post("/api/backtest")
def backtest_post(body: dict = Body(...), market: str = "CN"):
    """kind: strategy | single_factor | event_study | ablation | param_grid。后台线程运行，GET /api/backtest/job/{id} 轮询。"""
    market = _need_cn(market)
    kind = body.get("kind", "strategy")
    jid = uuid.uuid4().hex[:10]
    _bt_jobs[jid] = {"status": "running", "kind": kind}

    def work():
        try:
            res = backtest.run_job(market, kind, body.get("params", {}))
            _bt_jobs[jid] = {"status": "done", "kind": kind, "run_id": res.get("run_id"), "result": res}
        except Exception as e:  # noqa: BLE001
            import traceback
            _bt_jobs[jid] = {"status": "error", "kind": kind, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1200:]}

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": jid, "status": "running"}


@app.get("/api/backtest/job/{jid}")
def backtest_job(jid: str):
    j = _bt_jobs.get(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@app.get("/api/backtest/runs")
def backtest_runs(market: str = "CN", limit: int = 30):
    market = _need_cn(market)
    with db.market_db(market) as c:
        rows = db.rows(c.execute("SELECT run_id, kind, strategy_id, config_hash, data_asof, metrics, trial_count, code_version, created_at "
                                 "FROM backtest_runs WHERE market=? ORDER BY created_at DESC LIMIT ?", (market, limit)))
    for r in rows:
        r["metrics"] = json.loads(r["metrics"] or "{}")
    return _wrap(market, items=rows)


@app.get("/api/backtest/run/{run_id}")
def backtest_run_get(run_id: str, market: str = "CN"):
    market = _need_cn(market)
    with db.market_db(market) as c:
        r = c.execute("SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "回测记录不存在")
    return {**json.loads(r["result"]), "run_id": r["run_id"], "trial_count": r["trial_count"], "config_hash": r["config_hash"],
            "code_version": r["code_version"], "created_at": r["created_at"]}


# ---- 设置 / 备份 --------------------------------------------------------------

@app.get("/api/settings")
def settings_get():
    c = settings.cfg()
    return {k: c.get(k) for k in ("universe", "gate", "features", "regime", "funnel", "setups", "execution", "exits", "portfolio",
                                  "costs", "limit_rules", "backup", "jobs", "history_years", "datasource", "throttle", "validation", "allocation", "factor_portfolio")} | \
           {"server": {"host": c["server"]["host"], "port": c["server"]["port"], "token_set": bool(c["server"].get("token"))},
            "editable": list(settings.USER_EDITABLE)}


@app.put("/api/settings")
def settings_put(body: dict = Body(...)):
    try:
        settings.save_user_config(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return settings_get()


@app.get("/api/l1_bias")
def l1_bias_report(market: str = "CN"):
    from . import l1_bias
    market = _need_cn(market)
    return _wrap(market, report=l1_bias.last_report(market),
                 hint="运行：python -m server.cli l1-bias --sample 100（需要全量数据；实验在数据库副本上进行）")


@app.get("/api/backups")
def backups_get():
    return backup.backup_status()


# ---- 静态文件 -----------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


for _name in ("css", "js", "vendor", "icons"):
    if (ROOT / _name).exists():
        app.mount(f"/{_name}", StaticFiles(directory=ROOT / _name), name=_name)


@app.get("/sw.js")
def sw():
    return FileResponse(ROOT / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse(ROOT / "manifest.json", media_type="application/manifest+json")


@app.get("/icon.svg")
def icon():
    return FileResponse(ROOT / "icon.svg", media_type="image/svg+xml")


@app.on_event("startup")
def _startup():
    if settings.cfg()["jobs"].get("scan_on_startup", True):
        manager.schedule()
    if settings.cfg()["jobs"].get("prewarm", True):
        threading.Thread(target=_prewarm, name="prewarm", daemon=True).start()


def _prewarm():
    """启动后在后台预热：市场温度 / 指数择时与全市场面板（首次约 40 秒）。这样打开页面时不必等待，也不会被并发请求重复构建。"""
    from . import market_view
    warmed = False
    for m in ("CN", "US"):
        try:
            with db.market_db(m) as c:
                if not c.execute("SELECT 1 FROM daily_bar LIMIT 1").fetchone():
                    continue
                market_view.ensure_breadth(c, m)
            market_view.build(m)
            if not warmed:                     # 面板缓存只有一份（约 2 GB）：只预热首页默认市场
                with settings.market_ctx(m):
                    _scan_ctx(m)
                warmed = True
            r = scanner.rescan_if_config_changed(m)       # 参数变了（如升级后默认形态改变）：当天清单按新参数重扫
            if r:
                log.info("rescanned %s %s with current config: %s candidates", m, r.get("scan_date"), len(r.get("candidates", [])))
            if m == "CN":                                  # 低风险组合的回测证据：数据日 / 参数变了才在后台重算（约 1~2 分钟）
                from . import factor_portfolio as fpm
                fpm.refresh_summary(m, background=True)
        except Exception:  # noqa: BLE001 - 预热失败不影响服务，按需时再算
            log.exception("prewarm %s failed", m)


def main():
    import uvicorn

    s = settings.cfg()["server"]
    host = s["host"]
    if host not in ("127.0.0.1", "localhost", "::1") and not s.get("token"):
        raise SystemExit("开放局域网访问（host 非 127.0.0.1）时必须设置 server.token 访问口令（系统内含持仓数据，1.6）。")
    kw = {}
    cert, key = s.get("ssl_certfile"), s.get("ssl_keyfile")
    if cert and key:                       # 本地 HTTPS：手机经局域网访问时才能安装 PWA / 注册 Service Worker（1.6）
        from pathlib import Path
        for f in (cert, key):
            if not Path(f).exists():
                raise SystemExit(f"找不到证书文件：{f}（可用 python tools/make_cert.py 生成自签名证书，见 docs/使用手册.md）")
        kw = {"ssl_certfile": cert, "ssl_keyfile": key}
        print(f"HTTPS 已启用：https://{host if host not in ('0.0.0.0', '::') else '<本机 IP>'}:{int(os.environ.get('PORT') or s['port'])}/")
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT") or s["port"]), log_level="info",
                access_log=bool(s.get("access_log", False)), **kw)      # 不逐条记录页面请求（轮询会让日志无限变大）


if __name__ == "__main__":
    main()
