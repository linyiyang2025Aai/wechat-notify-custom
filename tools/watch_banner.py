# -*- coding: utf-8 -*-
"""等横幅出现，读出它真实的屏幕矩形（只读：不点击、不移动鼠标）。

用法：
    python tools/watch_banner.py <进程PID> [更多PID...]

PID 可以从任务管理器里看，或者：
    PowerShell:  Get-Process | Where-Object { $_.ProcessName -like "*微信提醒*" } | Select Id

它会枚举该进程所有可见窗口，挑出尺寸像横幅的那个（宽 200~1400、高 30~400），
打印「左上角=(x, y) 宽=w 高=h」。用来验证填进配置的坐标是否真的生效。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

if len(sys.argv) < 2:
    raise SystemExit(__doc__)

user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
pids = {int(a) for a in sys.argv[1:] if a.isdigit()}
print("目标进程:", sorted(pids))

rects = []


def cb(hwnd, lparam):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in pids and user32.IsWindowVisible(hwnd):
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if 200 < w < 1400 and 30 < h < 400:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            rects.append((r.left, r.top, w, h, buf.value))
    return True


proc = EnumProc(cb)
deadline = time.time() + 90
while time.time() < deadline and not rects:
    rects.clear()
    user32.EnumWindows(proc, 0)
    if rects:
        break
    time.sleep(0.4)

if rects:
    for x, y, w, h, title in rects:
        print("横幅窗口: 左上角=(%d, %d) 宽=%d 高=%d 标题=%r" % (x, y, w, h, title))
else:
    print("90 秒内没有等到横幅（这段时间没有新消息，或者 PID 填错了）")
