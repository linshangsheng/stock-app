"""市场差异的集中出口（2.1：A股 / 美股各自独立——交易日历、复权口径、涨跌停、T+1、基准、费用、股票池阈值都不同）。
其余模块只问「这个市场怎么样」，不再到处写 if market == "CN"。"""
from __future__ import annotations

from . import settings

CN, US = "CN", "US"


def key(market: str) -> str:
    return market.lower()


def universe_cfg(market: str) -> dict:
    return settings.cfg()["universe"][key(market)]


def has_price_limits(market: str) -> bool:
    return market == CN                      # A 股涨跌停；美股无涨跌停（熔断另当别论）


def t_plus_1(market: str) -> bool:
    return market == CN                      # A 股买入当日不可卖出；美股 T+0


def gate_benchmarks(market: str) -> list[str]:
    g = settings.cfg()["gate"]
    return g["benchmark_symbols_cn"] if market == CN else g["benchmark_symbols_us"]


def aux_indices(market: str) -> list[str]:
    """辅助序列（不参与闸门）：美股行业 ETF；指数择时用到、但不在闸门基准里的宽基指数（如上证 50）。"""
    extra = settings.cfg()["gate"].get("sector_etfs_us", []) if market == US else []
    mv = settings.cfg().get("market_view", {}).get("indices_us" if market == US else "indices_cn", [])
    bench = set(gate_benchmarks(market))
    return list(dict.fromkeys([*extra, *(i["symbol"] for i in mv if i["symbol"] not in bench)]))


def currency(market: str) -> str:
    return "CNY" if market == CN else "USD"


def cost_cfg(market: str) -> dict:
    return settings.cfg()["costs"][key(market)]


def board_of(symbol: str, market: str) -> str | None:
    if market == CN:
        from .datasource_cn import board_of as cn_board
        return cn_board(symbol)
    return "us"


def source_kind(market: str) -> str:
    return settings.cfg()["datasource"].get(key(market), "")


def is_demo(market: str) -> bool:
    return source_kind(market) == "demo"
