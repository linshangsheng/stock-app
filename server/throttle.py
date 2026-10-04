"""统一限速 / 指数退避 / 熔断（3.26.3，反封 IP）。
原则：宁可慢，不可被封。不依赖 ak.set_rate_limit / ak.set_checkpoint；yfinance 不自定义 Session，
限速退避全部在这一层实现。"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from . import settings


class CircuitOpen(RuntimeError):
    """数据源熔断中：调用方应降级到备用上游或返回缓存。"""


class RateLimited(RuntimeError):
    """上游限流（429 / 空数据 / 解码错误等一律按限流处理，触发退避，而不是当作「该股无数据」）。"""


@dataclass
class Throttle:
    name: str
    min_delay: float = 1.0
    max_delay: float = 2.0
    backoff_base: float = 10.0
    backoff_max: float = 80.0
    breaker_failures: int = 3
    breaker_pause: float = 600.0
    sleep: callable = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    clock: callable = time.monotonic
    _fails: int = 0
    _open_until: float = 0.0
    _last: float | None = None
    _calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    daily_budget: int | None = None   # 稀缺资源（如 Alpha Vantage 免费档，约每天 25 次）
    _budget_day: str = ""
    _budget_used: int = 0

    @classmethod
    def from_config(cls, name: str, **kw) -> "Throttle":
        c = settings.cfg().get("throttle", {}).get(name, {})
        return cls(name=name, **{**c, **kw})

    # -- 状态 --
    @property
    def is_open(self) -> bool:
        return self.clock() < self._open_until

    def status(self) -> dict:
        remain = max(0.0, self._open_until - self.clock())
        return {"name": self.name, "breaker_open": remain > 0, "breaker_remaining_s": round(remain),
                "consecutive_failures": self._fails, "calls": self._calls}

    def _consume_budget(self) -> None:
        if self.daily_budget is None:
            return
        today = time.strftime("%Y-%m-%d")
        if today != self._budget_day:
            self._budget_day, self._budget_used = today, 0
        if self._budget_used >= self.daily_budget:
            raise CircuitOpen(f"{self.name}: 今日额度已用完 ({self.daily_budget})")
        self._budget_used += 1

    # -- 核心 --
    def wait(self) -> None:
        """请求间随机延迟，避免固定频率被识别。"""
        with self._lock:
            delay = self.rng.uniform(self.min_delay, self.max_delay)
            elapsed = self.clock() - (self._last if self._last is not None else 0.0)
            if self._last is not None and elapsed < delay:
                self.sleep(delay - elapsed)
            self._last = self.clock()

    def record_success(self) -> None:
        self._fails = 0

    def record_failure(self) -> None:
        self._fails += 1
        if self._fails >= self.breaker_failures:
            self._open_until = self.clock() + self.breaker_pause
            self._fails = 0

    def call(self, fn, *args, retries: int = 3, **kwargs):
        """带限速 / 退避 / 熔断的调用。命中限流做指数退避（10s → 20s → 40s…），而非立即重试。"""
        if self.is_open:
            raise CircuitOpen(f"{self.name}: 熔断中，约 {round(self._open_until - self.clock())}s 后恢复")
        self._consume_budget()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self.wait()
            self._calls += 1
            try:
                res = fn(*args, **kwargs)
                self.record_success()
                return res
            except CircuitOpen:
                raise
            except Exception as e:  # noqa: BLE001 - 上游异常种类繁多，统一按失败处理
                last_exc = e
                self.record_failure()
                if self.is_open:
                    raise CircuitOpen(f"{self.name}: 连续失败触发熔断：{e}") from e
                if attempt < retries:
                    self.sleep(min(self.backoff_base * (2 ** attempt), self.backoff_max))
        raise last_exc  # type: ignore[misc]


_registry: dict[str, Throttle] = {}


def get(name: str) -> Throttle:
    if name not in _registry:
        _registry[name] = Throttle.from_config(name)
    return _registry[name]


def all_status() -> list[dict]:
    return [get(n).status() for n in ("baostock", "akshare", "yfinance", "cninfo", "sec", "eastmoney")]
