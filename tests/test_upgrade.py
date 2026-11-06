"""软件升级（server/upgrade.py）：全部用临时的 git 仓库（一个「远程」+ 一个「本机」），绝不碰真实仓库、真实网络，也不会真的重启进程。"""
import subprocess
import time
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

from server import upgrade  # noqa: E402

GIT = upgrade.find_git()
pytestmark = pytest.mark.skipif(GIT is None, reason="没有安装 git")


def g(cwd, *args):
    r = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin（裸仓库）+ dev（推送用）+ local（被升级的那份，data/ 里有个人数据）。"""
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    dev = tmp_path / "dev"
    subprocess.run([GIT, "clone", str(origin), str(dev)], check=True, capture_output=True)
    (dev / "server").mkdir()
    (dev / "server" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (dev / "a.txt").write_text("v1\n", encoding="utf-8")
    (dev / ".gitignore").write_text("data/\n", encoding="utf-8")
    g(dev, "add", "."); g(dev, "commit", "-m", "第一版"); g(dev, "push", "-u", "origin", "HEAD:main")
    local = tmp_path / "local"
    subprocess.run([GIT, "clone", str(origin), str(local)], check=True, capture_output=True)
    (local / "data").mkdir(); (local / "data" / "mine.db").write_text("我的数据", encoding="utf-8")
    return origin, dev, local


def push(dev, name="a.txt", text="v2\n", msg="第二版：改了 a"):
    (dev / name).write_text(text, encoding="utf-8")
    g(dev, "add", "."); g(dev, "commit", "-m", msg); g(dev, "push", "origin", "HEAD:main")


def wait_done(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = upgrade.status()
        if not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("升级线程没有结束")


def test_check_reports_up_to_date_then_new_commits(repos):
    _, dev, local = repos
    r = upgrade.check(local)
    assert r["ok"] and r["up_to_date"] and r["behind"] == 0 and r["current"]["subject"] == "第一版"
    push(dev); push(dev, "b.txt", "x\n", "第三版：加了 b")
    r = upgrade.check(local)
    assert r["ok"] and r["behind"] == 2 and r["can_upgrade"] and [c["subject"] for c in r["commits"]] == ["第三版：加了 b", "第二版：改了 a"]
    assert r["changed_files"] == 2 and r["requirements_changed"] is False


def test_check_explains_problems(tmp_path, repos):
    r = upgrade.check(tmp_path)                                                  # 不是 git 仓库
    assert not r["ok"] and ".git" in r["error"]
    _, _, local = repos
    g(local, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    r = upgrade.check(local)
    assert not r["ok"] and r["error"]


def test_upgrade_fast_forwards_keeps_data_and_restarts(repos):
    _, dev, local = repos
    push(dev)
    calls = []
    assert upgrade.start(local, restart_fn=lambda: calls.append(1))["ok"]
    s = wait_done()
    assert s["error"] is None and s["done"] and s["restarting"] and calls == [1]
    assert (local / "a.txt").read_text(encoding="utf-8") == "v2\n"               # 新代码到位
    assert (local / "data" / "mine.db").read_text(encoding="utf-8") == "我的数据"       # 个人数据没动
    assert s["percent"] >= 92 and [x["state"] for x in s["steps"]][:2] == ["done", "done"] and s["steps"][2]["state"] == "skip"   # 没改依赖：跳过装依赖
    assert s["old"]["subject"] == "第一版" and s["new"]["subject"] == "第二版：改了 a"
    assert upgrade.check(local)["up_to_date"]
    assert g(local, "status", "--porcelain", "--untracked-files=no") == ""       # 没有产生合并提交 / 脏文件


def test_already_latest_does_not_restart(repos):
    _, _, local = repos
    calls = []
    upgrade.start(local, restart_fn=lambda: calls.append(1))
    s = wait_done()
    assert s["up_to_date"] and s["done"] and not s["restarting"] and s["percent"] == 100 and calls == []


def test_refuses_when_local_edit_clashes_with_update(repos):
    _, dev, local = repos
    push(dev)
    (local / "a.txt").write_text("我自己改的\n", encoding="utf-8")                  # 本机改了、更新也要改同一个文件
    r = upgrade.check(local)
    assert r["ok"] and not r["can_upgrade"] and r["clash"] == ["a.txt"] and "冲突" in r["reason"]
    calls = []
    upgrade.start(local, restart_fn=lambda: calls.append(1))
    s = wait_done()
    assert s["error"] and "a.txt" in s["error"] and calls == []
    assert (local / "a.txt").read_text(encoding="utf-8") == "我自己改的\n"            # 一个字都没覆盖


def test_unrelated_local_edit_does_not_block(repos):
    _, dev, local = repos
    (local / "mine.txt").write_text("x", encoding="utf-8"); g(local, "add", "mine.txt"); g(local, "reset", "-q")
    (local / "a.txt").write_text("v1\n本机多写了一行\n", encoding="utf-8")
    push(dev, "b.txt", "新文件\n", "只加 b")                                      # 更新不碰 a.txt
    r = upgrade.check(local)
    assert r["can_upgrade"] and r["clash"] == []
    upgrade.start(local, restart_fn=lambda: None)
    s = wait_done()
    assert s["error"] is None and (local / "b.txt").exists() and "本机多写" in (local / "a.txt").read_text(encoding="utf-8")


def test_refuses_diverged_history(repos):
    _, dev, local = repos
    (local / "c.txt").write_text("本机提交\n", encoding="utf-8"); g(local, "add", "."); g(local, "commit", "-m", "本机独有的提交")
    push(dev)
    r = upgrade.check(local)
    assert r["ahead"] == 1 and r["behind"] == 1 and not r["can_upgrade"] and "分叉" in r["reason"]
    upgrade.start(local, restart_fn=lambda: None)
    s = wait_done()
    assert s["error"] and "分叉" in s["error"]
    assert g(local, "log", "-1", "--format=%s") == "本机独有的提交"                    # 本机提交还在


def test_dependency_install_failure_leaves_code_untouched(repos, monkeypatch):
    _, dev, local = repos
    push(dev, "server/requirements.txt", "fastapi\nsome-new-lib==1.0\n", "加了新依赖")
    assert upgrade.check(local)["requirements_changed"] is True
    seen = []

    real_run = upgrade.run

    def fake_run(cmd, root, timeout=60, on_line=None):
        if "pip" in cmd:
            seen.append(cmd)
            return 1, "ERROR: No matching distribution found for some-new-lib"
        return real_run(cmd, root, timeout, on_line)

    monkeypatch.setattr(upgrade, "run", fake_run)
    calls = []
    upgrade.start(local, restart_fn=lambda: calls.append(1))
    s = wait_done()
    assert seen and s["error"] and "依赖库安装失败" in s["error"] and "代码还没有改动" in s["error"] and calls == []
    assert g(local, "log", "-1", "--format=%s") == "第一版"                          # 代码没动


def test_dependency_install_runs_when_requirements_changed(repos, monkeypatch):
    _, dev, local = repos
    push(dev, "server/requirements.txt", "fastapi\nsome-new-lib==1.0\n", "加了新依赖")
    real_run, pip_calls = upgrade.run, []

    def fake_run(cmd, root, timeout=60, on_line=None):
        if "pip" in cmd:
            pip_calls.append(cmd)
            for line in ("Collecting some-new-lib==1.0", "Successfully installed some-new-lib-1.0"):
                on_line and on_line(line)
            return 0, "ok"
        return real_run(cmd, root, timeout, on_line)

    monkeypatch.setattr(upgrade, "run", fake_run)
    upgrade.start(local, restart_fn=lambda: None)
    s = wait_done()
    assert s["error"] is None and len(pip_calls) == 1 and "-r" in pip_calls[0] and s["steps"][2]["state"] == "done"
    assert "some-new-lib" in (local / "server" / "requirements.txt").read_text(encoding="utf-8")


def test_progress_only_goes_forward_and_cannot_start_twice(repos):
    _, dev, local = repos
    push(dev)
    seen = []
    orig = upgrade._set

    def spy(*a, **kw):
        orig(*a, **kw)
        seen.append(upgrade.status()["percent"])

    upgrade._set = spy
    try:
        assert upgrade.start(local, restart_fn=lambda: time.sleep(0.3))["ok"]
        assert upgrade.start(local, restart_fn=lambda: None).get("already") is True                # 正在跑时不会再开一个
        wait_done()
    finally:
        upgrade._set = orig
    assert seen == sorted(seen) and seen[-1] >= 90


# ---------------------------------------------------------------- 接口
@pytest.fixture
def client(demo_env):
    from server import main
    return TestClient(main.app)


def test_api_check_and_status(client, repos, monkeypatch):
    _, dev, local = repos
    monkeypatch.setattr(upgrade, "ROOT", local)
    push(dev)
    r = client.get("/api/upgrade/check").json()
    assert r["ok"] and r["behind"] == 1 and r["can_upgrade"]
    st = client.get("/api/upgrade/status").json()
    assert {"running", "percent", "steps", "message", "error", "pid"} <= set(st)


def test_api_start_asks_first_when_a_task_is_running(client, repos, monkeypatch):
    from server import main
    _, dev, local = repos
    monkeypatch.setattr(upgrade, "ROOT", local)
    push(dev)
    monkeypatch.setattr(main.manager, "state", {"running": "daily"})
    r = client.post("/api/upgrade/start", json={})
    assert r.status_code == 409 and r.json()["blocked"] and (local / "a.txt").read_text(encoding="utf-8") == "v1\n"      # 有任务在跑：不升级
    monkeypatch.setattr(upgrade, "restart", type("R", (), {"schedule": staticmethod(lambda: {}), "blockers": staticmethod(lambda *a: [])}))
    r = client.post("/api/upgrade/start", json={"force": True})
    assert r.status_code == 200 and r.json()["ok"]
    wait_done()
    assert (local / "a.txt").read_text(encoding="utf-8") == "v2\n"
