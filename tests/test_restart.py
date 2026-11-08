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


def test_schedule_when_supervised_only_exits_and_lets_the_supervisor_restart(fresh_env, monkeypatch):
    """由 systemd 等守护程序启动时（STOCKAPP_SUPERVISED=1）：重启 = 自己退出（非 0，Restart=on-failure 也会重启），不再自己拉接班进程——
    否则 Restart=on-failure 下退出码 0 不会被重启（网站直接下线），Restart=always 下接班进程又会被一起杀掉。"""
    monkeypatch.setenv(restart.SUPERVISED_ENV, "1")
    spawned, done = [], []
    info = restart.schedule(delay=0, spawn=lambda *a, **k: spawned.append(1), exit_fn=lambda code: done.append(code))
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done == [restart.SUPERVISED_EXIT_CODE] and restart.SUPERVISED_EXIT_CODE != 0 and spawned == [] and info["supervised"] is True
    monkeypatch.delenv(restart.SUPERVISED_ENV)
    assert restart.supervised() is False
    monkeypatch.setenv(restart.SUPERVISED_ENV, "no")
    assert restart.supervised() is False


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


def test_deploy_unit_matches_the_supervised_restart_contract():
    """deploy/stock-app.service 必须和「守护程序重启」约定一致：带 STOCKAPP_SUPERVISED=1、Restart=always、用 python -m server.main 启动；
    Caddyfile 反代到本机 8000（软件只监听 127.0.0.1）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "deploy"
    unit = (root / "stock-app.service").read_text(encoding="utf-8")
    assert f"Environment={restart.SUPERVISED_ENV}=1" in unit and "Restart=always" in unit and "-m server.main" in unit
    assert "reverse_proxy 127.0.0.1:8000" in (root / "Caddyfile").read_text(encoding="utf-8")
    assert "127.0.0.1" in __import__("server.settings", fromlist=["x"]).load_config()["server"]["host"]


# ---------------------------------------------------------------- 端口选择（部署到 Linux 后发现的问题）
def test_port_probe_sets_reuseaddr_only_where_it_is_safe(monkeypatch):
    """Linux 上旧进程刚退出时端口上有几十秒 TIME_WAIT：探测套接字不设 SO_REUSEADDR 会误判「被占用」，软件悄悄改用 18000，
    而反向代理只认 8000，网站打不开（升级后页面卡在 99%）。Windows 上不能设（它允许和正在监听的程序重复绑定，探测不出「被占用」）。"""
    import socket
    from server import main as m
    assert m._reuse_addr_for_probe() == (os.name != "nt")                                  # 真实函数：Linux / macOS 要设，Windows 不设
    made = []

    class FakeSock:
        def __init__(self, *a):
            self.opts = []
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            self.opts.append(a)

        def bind(self, addr):
            pass
    monkeypatch.setattr(m, "_is_ours", lambda p: False)
    monkeypatch.setattr(socket, "socket", FakeSock)
    monkeypatch.setattr(m, "_reuse_addr_for_probe", lambda: True)
    assert m.pick_port("127.0.0.1", 8000, []) == 8000
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in made[-1].opts
    monkeypatch.setattr(m, "_reuse_addr_for_probe", lambda: False)
    assert m.pick_port("127.0.0.1", 8000, []) == 8000
    assert made[-1].opts == []


def test_under_a_supervisor_a_busy_port_is_an_error_not_a_silent_switch(monkeypatch):
    """被 systemd 管着（STOCKAPP_SUPERVISED=1）时，8000 暂时用不了就报错退出让守护程序再拉起，不能悄悄换成 18000（Caddy 只认 8000）；
    平时（桌面）照旧会换到备用端口。"""
    import socket
    from server import main as m
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    fp = free.getsockname()[1]
    free.close()
    bp = busy.getsockname()[1]
    try:
        monkeypatch.setattr(m, "_is_ours", lambda p: False)
        monkeypatch.delenv(restart.SUPERVISED_ENV, raising=False)
        assert m.pick_port("127.0.0.1", bp, [fp]) == fp                                  # 桌面：换备用端口
        monkeypatch.setenv(restart.SUPERVISED_ENV, "1")
        with pytest.raises(SystemExit):
            m.pick_port("127.0.0.1", bp, [fp])                                           # 守护模式：不换，报错
        assert m.pick_port("127.0.0.1", fp, [bp]) == fp                                  # 首选端口空着就正常用它
    finally:
        busy.close()
