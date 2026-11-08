"""后端自重启（server/restart.py + /api/restart）。测试里绝不真的退出进程：spawn / exit 都换成假的。"""
import os
import sys
import time
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from server import restart


def test_command_keeps_original_arguments(monkeypatch):
    monkeypatch.setattr(sys, "orig_argv", ["python", "-m", "server.cli", "serve", "--demo"], raising=False)
    assert restart.command() == [sys.executable, "-m", "server.cli", "serve", "--demo"]
    monkeypatch.setattr(sys, "orig_argv", ["python"], raising=False)                 # 取不到参数：回退到默认启动命令
    assert restart.command() == [sys.executable, "-m", "server.main"]


def test_blockers_list_running_work():
    assert restart.blockers({"running": None}, {}) == []
    b = restart.blockers({"running": "init"}, {"a": {"status": "done"}, "b": {"status": "running", "kind": "factor"}})
    assert len(b) == 2 and "初始化" in b[0] and "回测" in b[1]
    assert restart.blockers(None, None) == []


def test_schedule_spawns_replacement_then_exits(fresh_env):
    calls = {}
    done = []
    restart.schedule(delay=0, spawn=lambda cmd, **kw: calls.update(cmd=cmd, kw=kw), exit_fn=lambda code: done.append(code))
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done == [0]                                                           # 先拉起新进程，再退出自己
    assert calls["cmd"][0] == sys.executable
    assert calls["kw"]["env"][restart.ENV_FLAG] == str(os.getpid())              # 告诉新进程「我是接班的」
    assert calls["kw"]["cwd"] == str(restart.ROOT)
    if os.name == "nt":
        import subprocess
        assert calls["kw"]["creationflags"] & subprocess.DETACHED_PROCESS         # 没有控制台窗口，不随旧进程结束
    else:
        assert calls["kw"]["start_new_session"] is True


def test_schedule_flushes_source_stats_before_exit(fresh_env):
    from server import source_stats
    source_stats._path_loaded = None
    source_stats.record("baostock", True)
    done = []
    restart.schedule(delay=0, spawn=lambda *a, **k: None, exit_fn=lambda c: done.append(c))
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert (fresh_env / "source_stats.json").exists()                            # 重启前统计已落盘


def test_wait_previous_instance():
    state = {"n": 0}

    def is_ours(p):
        state["n"] += 1
        return state["n"] <= 4                                                    # 前几次旧进程还在应答，然后退出
    sleeps = []
    assert restart.wait_previous_instance([8000, 18000], is_ours, sleep=sleeps.append) is True and sleeps
    t = {"now": 0.0}
    assert restart.wait_previous_instance([8000], lambda p: True, timeout=5, sleep=lambda s: t.update(now=t["now"] + 2), clock=lambda: t["now"]) is False   # 一直不退：超时放弃


@pytest.fixture
def client(demo_env):
    from fastapi.testclient import TestClient
    from server import main
    return TestClient(main.app)


def test_ping_reports_pid_and_start_time(client):
    p = client.get("/api/ping").json()
    assert p["pid"] == os.getpid() and p["started"]


def test_restart_endpoint(client, monkeypatch):
    from server import main
    called = []
    monkeypatch.setattr(restart, "schedule", lambda *a, **k: called.append(1) or {"pid": os.getpid(), "command": []})
    r = client.post("/api/restart").json()
    assert r["ok"] is True and r["pid"] == os.getpid() and called == [1]
    # 有任务在跑：默认拒绝，并说明会中断什么
    monkeypatch.setitem(main.manager.state, "running", "daily")
    resp = client.post("/api/restart")
    assert resp.status_code == 409 and resp.json()["blocked"] and "盘后任务链" in resp.json()["blocked"][0] and called == [1]
    # 带 force 才强行重启
    assert client.post("/api/restart", json={"force": True}).json()["ok"] is True and called == [1, 1]


def test_restart_refuses_remote_callers_when_login_is_off(demo_env):
    from fastapi.testclient import TestClient
    from server import main
    remote = TestClient(main.app, client=("8.8.8.8", 50000))
    assert remote.post("/api/restart").status_code == 403


def test_restart_needs_login_when_auth_is_on(client, monkeypatch):
    monkeypatch.setattr(restart, "schedule", lambda *a, **k: {"pid": 1, "command": []})
    monkeypatch.delenv("STOCK_AUTH")
    assert client.post("/api/restart").status_code == 401                         # 中间件：没登录一律拒绝（只有管理员能重启：见 test_accounts.py）


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill(pid):
    import subprocess
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _ping(port):
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


@pytest.mark.skipif(os.environ.get("STOCK_SKIP_SUBPROCESS") == "1", reason="STOCK_SKIP_SUBPROCESS=1")
def test_real_restart_new_process_takes_over_same_port(demo_env):
    """真的启动一个演示后端、真的让它重启：新进程在同一个端口接班，旧进程退出。（只用本机回环端口，不弹任何窗口）"""
    import subprocess
    import urllib.request
    port = _free_port()
    env = {**os.environ, "STOCK_DATA_DIR": str(demo_env), "PORT": str(port)}
    env.pop(restart.ENV_FLAG, None)
    proc = subprocess.Popen([sys.executable, "-m", "server.cli", "serve", "--demo"], cwd=str(restart.ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    new_pid = None
    try:
        first = None
        for _ in range(120):
            first = _ping(port)
            if first:
                break
            time.sleep(0.5)
        assert first and first["pid"] == proc.pid, "演示后端没有启动起来"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/restart", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(req, timeout=5).status == 200
        again = None
        for _ in range(120):                                                        # 最多等 60 秒：新进程接班
            p = _ping(port)
            if p and p["pid"] != first["pid"]:
                again = p
                break
            time.sleep(0.5)
        assert again, "重启后新进程没有在原端口接班"
        new_pid = again["pid"]
        assert again["started"] >= first["started"]
        proc.wait(timeout=10)                                                       # 旧进程已经退出
    finally:
        for pid in (new_pid, proc.pid):
            if pid:
                _kill(pid)
