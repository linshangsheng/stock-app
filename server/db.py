"""SQLite 读写层：ashare.db / us.db（行情库，表结构相同）与 portfolio.db（个人库）。WAL 模式（3.26）。
表结构依据 3.26.4。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import settings

MARKET_DB = {"CN": "ashare.db", "US": "us.db"}

MARKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS securities(
  symbol TEXT PRIMARY KEY, name TEXT, board TEXT, list_date TEXT, delist_date TEXT,
  status TEXT DEFAULT 'active', in_l1 INTEGER DEFAULT 1, l1_asof TEXT, sec_type TEXT DEFAULT 'stock'
);
CREATE TABLE IF NOT EXISTS daily_bar(
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, turnover REAL,
  adj_factor REAL DEFAULT 1.0, adj_close REAL,
  trade_status INTEGER DEFAULT 1, is_st INTEGER DEFAULT 0,
  source TEXT, is_temp INTEGER DEFAULT 0,
  PRIMARY KEY(symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_bar_date ON daily_bar(date);
CREATE TABLE IF NOT EXISTS corp_actions(
  symbol TEXT NOT NULL, ex_date TEXT NOT NULL, type TEXT NOT NULL, ratio_or_amount REAL,
  PRIMARY KEY(symbol, ex_date, type)
);
CREATE TABLE IF NOT EXISTS index_bar(
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  PRIMARY KEY(symbol, date)
);
CREATE TABLE IF NOT EXISTS industry_map(
  symbol TEXT PRIMARY KEY, industry TEXT, sector TEXT, asof TEXT
);
CREATE TABLE IF NOT EXISTS market_calendar(
  date TEXT PRIMARY KEY, is_open INTEGER NOT NULL, is_half_day INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fetch_state(
  symbol TEXT NOT NULL, task TEXT NOT NULL, last_ok_date TEXT, status TEXT, fail_count INTEGER DEFAULT 0,
  updated_at TEXT, PRIMARY KEY(symbol, task)
);
CREATE TABLE IF NOT EXISTS events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, market TEXT, event_time TEXT, publish_time TEXT, ingested_at TEXT,
  event_type TEXT, source TEXT, title TEXT, content TEXT, sentiment REAL, url TEXT,
  UNIQUE(symbol, event_type, event_time, source)
);
CREATE INDEX IF NOT EXISTS idx_events_sym ON events(symbol, event_time);
CREATE TABLE IF NOT EXISTS event_stream(
  uid TEXT PRIMARY KEY, symbol TEXT NOT NULL, market TEXT, event_time TEXT, publish_time TEXT, ingested_at TEXT,
  event_type TEXT, source TEXT, level INTEGER, title TEXT, summary TEXT, url TEXT, sentiment REAL
);
CREATE INDEX IF NOT EXISTS idx_evs_sym ON event_stream(symbol, event_time);
CREATE INDEX IF NOT EXISTS idx_evs_time ON event_stream(event_time);
CREATE TABLE IF NOT EXISTS scan_runs(
  run_id TEXT PRIMARY KEY, market TEXT, scan_date TEXT, config_hash TEXT, data_asof TEXT,
  data_gate_status TEXT, gate_detail TEXT, regime TEXT, regime_detail TEXT,
  started_at TEXT, finished_at TEXT, official INTEGER DEFAULT 1, universe_size INTEGER, n_candidates INTEGER,
  config TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS scan_results(
  run_id TEXT NOT NULL, symbol TEXT NOT NULL, setup TEXT, score REAL, reasons TEXT,
  trigger_price REAL, stop_price REAL, risk_amount REAL, shares INTEGER, extra TEXT,
  PRIMARY KEY(run_id, symbol)
);
CREATE TABLE IF NOT EXISTS scan_outcomes(
  run_id TEXT NOT NULL, symbol TEXT NOT NULL,
  ret_1d REAL, ret_3d REAL, ret_5d REAL, ret_10d REAL, ret_20d REAL, mae REAL, mfe REAL, filled INTEGER,
  PRIMARY KEY(run_id, symbol)
);
CREATE TABLE IF NOT EXISTS backtest_runs(
  run_id TEXT PRIMARY KEY, kind TEXT, strategy_id TEXT, market TEXT, config_hash TEXT, data_asof TEXT,
  metrics TEXT, trial_count INTEGER, code_version TEXT, created_at TEXT, config TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS job_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, status TEXT, detail TEXT
);
"""

PORTFOLIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist(
  market TEXT NOT NULL, symbol TEXT NOT NULL, added_at TEXT, note TEXT, PRIMARY KEY(market, symbol)
);
CREATE TABLE IF NOT EXISTS account(
  market TEXT PRIMARY KEY, asof TEXT, equity REAL, cash REAL, risk_per_trade REAL
);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL, symbol TEXT NOT NULL, open_date TEXT, qty INTEGER, avg_cost REAL,
  initial_stop REAL, current_stop REAL, setup TEXT, status TEXT DEFAULT 'open', note TEXT,
  close_date TEXT, close_price REAL,
  init_qty INTEGER, realized_pnl REAL DEFAULT 0, fees REAL DEFAULT 0, exit_reason TEXT,
  signal_run_id TEXT, regime TEXT, planned_trigger REAL, entry_value REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades(
  trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, date TEXT NOT NULL,
  price REAL NOT NULL, qty INTEGER NOT NULL, fee REAL DEFAULT 0,
  signal_run_id TEXT, setup TEXT, regime TEXT, initial_stop REAL, planned_trigger REAL,
  exit_reason TEXT, note TEXT, position_id INTEGER
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL, symbol TEXT NOT NULL, rule TEXT NOT NULL, active INTEGER DEFAULT 1
);
"""

_local = threading.local()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def db_path(name: str) -> Path:
    return settings.data_dir() / name


def market_conn(market: str = "CN") -> sqlite3.Connection:
    market = market.upper()
    if market not in MARKET_DB:
        raise ValueError(f"unknown market {market}")
    conn = _connect(db_path(MARKET_DB[market]))
    conn.executescript(MARKET_SCHEMA)
    return conn


def portfolio_conn() -> sqlite3.Connection:
    conn = _connect(db_path("portfolio.db"))
    conn.executescript(PORTFOLIO_SCHEMA)
    return conn


@contextmanager
def market_db(market: str = "CN"):
    conn = market_conn(market)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def portfolio_db():
    conn = portfolio_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- 小工具 -------------------------------------------------------------

def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, None if value is None else str(value)))


def log_job(conn, job: str, status: str, detail: str = "") -> None:
    from datetime import datetime

    conn.execute("INSERT INTO job_log(ts,job,status,detail) VALUES(?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), job, status, detail[:2000]))


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
