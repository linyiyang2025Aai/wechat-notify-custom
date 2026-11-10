# -*- coding: utf-8 -*-
"""企业微信提醒：盯会话列表首行的变化 + 首行未读红点。

背景（2026-10-30 实测企微 5.0.13）：
- 企微在后台收到消息时**不弹自动提示**；托盘上那个提示窗只是鼠标悬停时
  的消息预览，不能当提醒用。
- 本地消息库是自研加密（AES-256-GCM，密钥不落盘），读不了。
- 导航栏红点上的数字只有 6~8 像素高，Windows OCR 时灵时不灵，不能靠它。

所以改用纯像素信号：新消息会把对应会话顶到列表第一行并带未读红点，
而手机端已读只会改红点、不动列表内容。这里每秒抓一次主窗，对
「会话列表文本列」做指纹：指纹变化且首行有红点 → 弹「企业微信 · 有新消息」。

限制：收到托盘（隐藏）时窗口不渲染，抓不到，跳过；企微在前台时不打扰。
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import time

try:
    from PIL import Image
except Exception:  # pragma: no cover - 打包环境一定有 PIL
    Image = None

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SCAN_INTERVAL = 1.0          # 轮询间隔（秒）
RED_MIN = 190                # 红点判断阈值
GREEN_MAX = 105
BLUE_MAX = 105
TEXT_X = (128, 235)          # 96 DPI 下会话列表的文本列（不含头像/时间/红点）
TEXT_Y0 = 60
TOP_BADGE = (96, 58, 130, 96)  # 96 DPI 下首行头像右上角的未读红点区域

user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL


# ============================================================
# 进程 / 窗口
# ============================================================
def process_name(pid):
    if pid <= 0:
        return ""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def is_wecom_process(pid):
    return process_name(pid).startswith("wxwork")


def wecom_pids():
    out = set()

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (0, -1):
        return out
    pe = PE32()
    pe.dwSize = ctypes.sizeof(PE32)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.lower().startswith("wxwork"):
            out.add(pe.th32ProcessID)
        ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    return out


def _class_title(hwnd):
    cls = ctypes.create_unicode_buffer(256)
    title = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, cls, 256)
    user32.GetWindowTextW(hwnd, title, 512)
    return cls.value, title.value


def find_wecom_main_window():
    """返回企业微信主窗口 hwnd（WeWorkWindow）；找不到返回 0。"""
    found = []

    def cb(hwnd, _lp):
        cls, _title = _class_title(hwnd)
        if cls == "WeWorkWindow":
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if is_wecom_process(pid.value):
                found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else 0


def foreground_is_wecom():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not is_wecom_process(pid.value):
        return False
    cls, _ = _class_title(hwnd)
    return cls.strip().lower() != "weworknewtipswindow"


def bring_wecom_front():
    """把企业微信主窗带到前台（横幅点击时用）。"""
    hwnd = find_wecom_main_window()
    if not hwnd:
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ============================================================
# 抓图 / 区域判定
# ============================================================
def ui_scale(hwnd):
    """窗口 DPI 折算比例（96 DPI = 1.0）；取不到时按 1.0。"""
    try:
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [wt.HWND]
        get_dpi.restype = wt.UINT
        dpi = int(get_dpi(hwnd) or 0)
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def capture_window(hwnd):
    """PrintWindow 抓主窗像素；抓不到返回 None。"""
    if Image is None or not hwnd:
        return None
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w < 200 or h < 200:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    try:
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    except Exception:
        img = None
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    return img


def frame_is_valid(img):
    """PrintWindow 抓不到时会给纯黑/纯白帧，用来排除。"""
    if img is None:
        return False
    try:
        gray = img.convert("L")
        lo, hi = gray.getextrema()
        return (hi - lo) >= 16
    except Exception:
        return False


def _scaled_box(box, scale):
    return tuple(int(round(v * scale)) for v in box)


def list_signature(img, scale=1.0):
    """会话列表文本列的指纹：新消息会改内容/顺序，手机端已读只改红点。"""
    if img is None:
        return None
    x0, x1 = _scaled_box(TEXT_X, scale)
    y0 = int(round(TEXT_Y0 * scale))
    w, h = img.size
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), max(0, h - 8)
    if x1 - x0 < 20 or y1 - y0 < 40:
        return None
    crop = img.crop((x0, y0, x1, y1)).convert("L")
    crop = crop.resize((54, 320), Image.LANCZOS)
    return hashlib.md5(crop.tobytes()).hexdigest()


def top_row_has_badge(img, scale=1.0):
    """会话列表第一行的头像右上角是否有红色未读徽标。"""
    if img is None:
        return False
    x0, y0, x1, y1 = _scaled_box(TOP_BADGE, scale)
    w, h = img.size
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return False
    px = img.load()
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            if r >= RED_MIN and g <= GREEN_MAX and b <= BLUE_MAX:
                n += 1
                if n >= 40:
                    return True
    return False


# ============================================================
# 监控线程
# ============================================================
class WeComMonitor:
    """盯企微会话列表首行变化并投递横幅消息。

    与微信监控共用同一个 queue，消息里带 source="wecom"，横幅据此换色片
    和点击行为。开关、免打扰、提醒规则都读主程序的 config。
    """

    def __init__(self, q, cfg, log_fn=None, quiet_fn=None, rules_fn=None,
                 window_fn=None, capture_fn=None, foreground_fn=None,
                 scale_fn=None):
        self.q = q
        self.cfg = cfg
        self.log = log_fn or (lambda _m: None)
        self.quiet_fn = quiet_fn
        self.rules_fn = rules_fn
        self.window_fn = window_fn or find_wecom_main_window
        self.capture_fn = capture_fn or capture_window
        self.foreground_fn = foreground_fn or foreground_is_wecom
        self.scale_fn = scale_fn or ui_scale
        self._last_sig = None
        self._last_emit = 0.0
        self._last_skip_log = 0.0

    def loop(self):
        self.log("[企微] 监控线程已启动（会话列表变化监听）")
        while True:
            try:
                if not self.cfg.get("wecom_on", True):
                    self._last_sig = None
                    time.sleep(0.8)
                    continue
                self.scan_once()
            except Exception as e:
                self.log(f"[企微] 扫描异常: {e}")
                time.sleep(1.0)
            time.sleep(SCAN_INTERVAL)

    def scan_once(self):
        hwnd = self.window_fn()
        if not hwnd:
            self._last_sig = None
            return
        img = self.capture_fn(hwnd)
        if not frame_is_valid(img):
            self._last_sig = None
            now = time.time()
            if now - self._last_skip_log > 120:
                self.log("[企微] 主窗当前不可抓取（可能已收到托盘），跳过本轮")
                self._last_skip_log = now
            return
        try:
            scale = float(self.scale_fn(hwnd) or 1.0)
        except Exception:
            scale = 1.0
        sig = list_signature(img, scale)
        if sig is None or sig == self._last_sig:
            return
        changed = self._last_sig is not None
        self._last_sig = sig
        if not changed:
            return
        if not top_row_has_badge(img, scale):
            return
        if self.foreground_fn():
            self.log("[企微] 企业微信在前台，本次不弹窗")
            return
        self.notify()

    def notify(self):
        if self._in_quiet():
            self.log("[企微] 免打扰时段，静默")
            return
        msg = {
            "sender": "企业微信",
            "content": "有新消息",
            "is_group": False,
            "timestamp": int(time.time()),
            "session_key": "wecom",
            "source": "wecom",
            "source_name": "企业微信",
        }
        if self.rules_fn is not None:
            try:
                if not self.rules_fn(msg, self.cfg):
                    return
            except Exception as e:
                self.log(f"[企微] 提醒规则判定失败: {e}")
                return
        now = time.time()
        merge_window = float(self.cfg.get("merge_window", 15.0) or 15.0)
        kind = "merge" if (self._last_emit
                           and now - self._last_emit < merge_window) else "new"
        self._last_emit = now
        self.q.put((kind, time.strftime("%H:%M:%S"), msg))
        self.log("[企微] 提醒：企业微信有新消息")

    def _in_quiet(self):
        if not self.cfg.get("quiet_enabled") or not self.quiet_fn:
            return False
        try:
            return bool(self.quiet_fn(self.cfg.get("quiet_start", "23:00"),
                                      self.cfg.get("quiet_end", "07:00")))
        except Exception:
            return False


def wecom_available():
    return bool(find_wecom_main_window())
