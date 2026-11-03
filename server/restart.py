"""后端自己重启（页面上点「立即重启」）：代码 / 配置更新后，不用再去任务管理器结束 Python。
做法：旧进程先拉起一个新的、脱离控制台的同命令进程（环境变量 STOCKAPP_RESTART_FROM 标记「我是接班的」），再自己退出；
新进程在 main() 里先等旧进程退出、端口空出来，再正常启动。

注意：不能用 os.kill(pid, 0) 判断旧进程还在不在——在 Windows 上，信号 0 会被当成「强制结束进程」。这里改为看旧端口上还有没有本软件在应答。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import settings, source_stats

ROOT = Path(__file__).resolve().parent.parent
ENV_FLAG = "STOCKAPP_RESTART_FROM"


def command() -> list[str]:
    """重启用的命令行 = 当前进程的启动命令（sys.orig_argv 保留了 -m server.main / -m server.cli serve --demo 这类原样参数）。"""
    orig = getattr(sys, "orig_argv", None)
    if orig and len(orig) > 1:
        return [sys.executable, *orig[1:]]
    return [sys.executable, "-m", "server.main"]


def blockers(jobs_state: dict | None, bt_jobs: dict | None) -> list[str]:
    """重启会中断的事：正在运行的数据任务（初始化 / 盘后任务链 / 备份）和回测。"""
    out: list[str] = []
    names = {"init": "初始化数据", "daily": "盘后任务链", "catch_up": "自动更新（补跑）", "backup": "备份"}
    running = (jobs_state or {}).get("running")
    if running:
        out.append(f"{names.get(running, running)}正在运行")
    for jid, j in (bt_jobs or {}).items():
        if j.get("status") == "running":
            out.append(f"回测正在运行（{j.get('kind', '')}）")
    return out


def _spawn_kwargs() -> dict:
    kw: dict = {"cwd": str(ROOT), "env": {**os.environ, ENV_FLAG: str(os.getpid())}, "stdin": subprocess.DEVNULL}
    try:
        log = open(settings.data_dir() / "server.log", "ab")           # 和桌面快捷方式启动时一样，输出追加到 data\server.log
        kw["stdout"] = kw["stderr"] = log
    except OSError:
        kw["stdout"] = kw["stderr"] = subprocess.DEVNULL
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP      # 没有控制台窗口，也不随旧进程一起结束
    else:
        kw["start_new_session"] = True
    return kw


def schedule(delay: float = 0.8, spawn=subprocess.Popen, exit_fn=os._exit) -> dict:
    """安排重启：delay 秒后拉起新进程、再退出自己（让当前这个 HTTP 请求先返回）。spawn / exit_fn 可替换，便于测试。"""
    cmd, kw = command(), _spawn_kwargs()

    def go():
        time.sleep(delay)
        try:
            source_stats.flush()                                          # 数据源统计落盘，重启后不丢
        except Exception:  # noqa: BLE001
            pass
        spawn(cmd, **kw)
        time.sleep(0.3)
        exit_fn(0)

    threading.Thread(target=go, daemon=True, name="restart").start()
    return {"command": cmd, "pid": os.getpid()}


def wait_previous_instance(ports: list[int], is_ours, timeout: float = 40.0, sleep=time.sleep, clock=time.monotonic) -> bool:
    """接班进程用：等旧进程退出（这些端口上不再有本软件应答）。超时返回 False（照常往下走，由 pick_port 报错）。"""
    t0 = clock()
    while any(is_ours(p) for p in ports):
        if clock() - t0 > timeout:
            return False
        sleep(0.4)
    return True
