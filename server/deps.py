"""启动前检查第三方依赖（只用标准库）：缺了就用中文说明缺什么、怎么装，而不是抛出一长串英文 ModuleNotFoundError。
新电脑最常见的「打不开」就是没运行过「安装.bat」——双击启动时后端在后台，报错只写进 data\\server.log，用户什么都看不到。"""
from __future__ import annotations

import importlib.util
import sys

# (导入名, pip 包名)：与 server/requirements.txt 保持一致
REQUIRED = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pandas", "pandas"), ("numpy", "numpy"), ("yaml", "PyYAML"),
            ("baostock", "baostock"), ("yfinance", "yfinance"), ("exchange_calendars", "exchange_calendars"), ("requests", "requests")]


def missing() -> list[str]:
    return [pkg for mod, pkg in REQUIRED if importlib.util.find_spec(mod) is None]


def require() -> None:
    miss = missing()
    if not miss:
        return
    print("\n".join([
        "",
        "=" * 60,
        f"  缺少运行所需的组件：{', '.join(miss)}",
        "  （新电脑第一次使用，需要先安装一次依赖，只需要做一次）",
        "",
        "  办法一（推荐）：双击软件目录里的「安装.bat」，装完再启动。",
        "  办法二：在软件目录打开命令行，依次运行：",
        "      python -m pip install --upgrade pip",
        "      python -m pip install -r server/requirements.txt",
        "  （下载慢可以在第二行末尾加上  -i https://pypi.tuna.tsinghua.edu.cn/simple ）",
        "",
        f"  当前使用的 Python：{sys.executable}（{sys.version.split()[0]}）",
        *(["  注意：这个 Python 太新（3.15 及以上），numpy / pandas / PyYAML 还没有为它发布安装包，装不上；",
           "  请卸载后改装 Python 3.12.10（或 3.13、3.14），安装时勾选「Add python.exe to PATH」。"] if sys.version_info >= (3, 15) else []),
        *(["  注意：这个 Python 太旧，需要 3.12、3.13 或 3.14（推荐 3.12.10）。"] if sys.version_info < (3, 12) else []),
        "  如果明明装过还提示缺少：多半是电脑里有不止一个 Python，装到了另一个里——",
        "  用上面「办法二」的命令（python -m pip）装，就会装进正在使用的这个 Python。",
        "=" * 60,
        "",
    ]), flush=True)
    sys.exit(3)


if __name__ == "__main__":                         # python -m server.deps：只做检查，退出码 0 = 齐全、3 = 有缺
    require()
    print("依赖齐全")
