"""大构建（全市场面板 / 特征 / 回测）结束后，把「已经释放、但还被进程占着」的内存还给系统。
pandas / numpy 的大数组释放后，内存常常还留在进程的堆里：任务管理器里的「内存」不降，云服务器上也会按这个数字算（被系统因内存不足杀掉进程时看的就是它）。
Linux：glibc 的 malloc_trim 真的会把空闲的堆还回去。Windows：EmptyWorkingSet 把这些页从「工作集」里收走（任务管理器里的数字会降；
被收走的页下次用到会重新读回，几乎没有代价）。尽力而为，任何一步失败都忽略，不影响业务。"""
from __future__ import annotations

import gc
import os


def trim() -> bool:
    if os.environ.get("STOCKAPP_NO_TRIM") == "1":                 # 开关：设为 1 就不做（排查性能 / 对比用）
        return False
    gc.collect()
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]                # 不声明参数类型，64 位句柄会溢出
            return bool(psapi.EmptyWorkingSet(k32.GetCurrentProcess()))
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        return bool(libc.malloc_trim(0))
    except Exception:  # noqa: BLE001 - 非 glibc（如 musl / macOS）或权限问题：不做就是了
        return False
