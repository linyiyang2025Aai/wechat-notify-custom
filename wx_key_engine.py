# -*- coding: utf-8 -*-
"""微信数据库密钥提取引擎（无界面）。

从 wx_key_tool.py 拆出：设置窗口内嵌提取面板和独立「微信密钥提取工具」
共用这一份流程（关微信 → 启微信 → 注入 Hook → 捕获密钥），避免两处各写
一套底层进程/DLL 操作而修一个漏一个。
"""
import ctypes
import os
import subprocess
import sys
import time


def resource_base():
    """资源根目录：PyInstaller onefile 打包后资源在 _MEIPASS 临时目录，
    源码运行时在本文件所在目录。2026-09-08 修复：原用 __file__ 目录，
    打包后指向 _MEI 临时目录但 wx_libs 未被打入，导致路径找不到。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(__file__))


BASE = resource_base()
DLL_DIR = os.path.join(BASE, "wx_libs", "dll")
RUNTIME_DIR = os.path.join(BASE, "wx_libs", "runtime")
KEY_DLL = os.path.join(DLL_DIR, "wx_key.dll")
_NO_WINDOW = 0x08000000          # 别在 windowed 版上闪黑框
_WECHAT_EXES = ("Weixin.exe", "WeChat.exe")


def preflight():
    """提取开始前自检依赖，返回 (可用?, 说明)。

    必须在「关掉微信」之前查——否则 DLL 缺失时用户白被杀一次微信、还要重新扫码。"""
    if not os.path.exists(KEY_DLL):
        return False, f"找不到提取组件 {KEY_DLL}（打包时 wx_libs 要作为数据一起带上）"
    return True, ""


def wechat_pids():
    """当前所有微信进程 PID（兼容 Weixin.exe / WeChat.exe）"""
    for name in _WECHAT_EXES:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV"],
                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW
            ).decode("gbk", errors="replace")
            pids = []
            for line in out.strip().split("\n")[1:]:
                parts = line.strip('"').split('","')
                if len(parts) >= 2 and parts[0].lower() == name.lower():
                    pids.append(int(parts[1]))
            if pids:
                return pids
        except Exception:
            pass
    return []


def find_wechat_pid():
    """微信主进程 PID。

    老实现取"最小 PID"当主进程——这个假设在本机已被证伪（微信 4.x 起 6 个
    Weixin.exe，最小 PID 是子进程，PID 会被回收复用）。这里仍返回一个"最可能"
    的候选，但注入失败时 run() 会遍历其余 PID 逐个重试，不再单点依赖该假设。"""
    pids = wechat_pids()
    return min(pids) if pids else None


def kill_wechat(timeout=8.0):
    """关闭微信，并**确认进程真的退干净**再返回。

    老实现 taskkill 之后只 sleep(1)：微信子进程没死透时，紧接着的
    find_wechat_pid 会立刻命中残留进程，把 Hook 注到错的进程上。"""
    try:
        for name in _WECHAT_EXES:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if not wechat_pids():
            return True
        time.sleep(0.3)
    return not wechat_pids()


def find_wechat_path():
    """查找微信安装路径：注册表（卸载项/App Paths）→ 常见路径。

    2026-09-08 修复：原只查 3 个固定路径，自定义安装路径（如 D 盘）找不到，
    只能退回 weixin:// 协议且启动异常，导致"等待微信启动超时"。"""
    import winreg
    # 1. 注册表卸载项
    reg_candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in reg_candidates:
        try:
            with winreg.OpenKey(hive, sub) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, name) as subkey:
                            try:
                                display = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                            except OSError:
                                continue
                            if not display or not loc:
                                continue
                            dl = display.lower()
                            if ("微信" in display) or ("weixin" in dl) or ("wechat" in dl):
                                for exe in ("Weixin.exe", "WeChat.exe", "wechat.exe"):
                                    p = os.path.join(loc, exe)
                                    if os.path.exists(p):
                                        return p
                    except OSError:
                        continue        # 单个卸载子键读不了（AccessDenied 很常见），
                        # 不能 break——break 会中断整个 hive 的枚举，后面的项全漏掉
        except OSError:
            pass
    # 2. App Paths 注册表
    for exe in ("Weixin.exe", "WeChat.exe"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                p = winreg.QueryValue(key, None)
                if p and os.path.exists(p):
                    return p
        except OSError:
            pass
    # 3. 常见路径
    for p in [
        r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\Weixin\Weixin.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Tencent\WeChat\WeChat.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Tencent\WeChat\WeChat.exe"),
    ]:
        if os.path.exists(p):
            return p
    # 4. 微信正在运行时，从进程快照取 exe 路径（自定义路径场景兜底）
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process Weixin -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path)"],
            timeout=10, creationflags=_NO_WINDOW)
        p = out.decode("utf-8", errors="replace").strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return None


def launch_wechat():
    """启动微信（注册表查找优先；失败则尝试 weixin:// 协议）"""
    p = find_wechat_path()
    if p:
        try:
            subprocess.Popen([p])
            return True
        except Exception:
            pass
    try:
        os.startfile("weixin://")
        return True
    except Exception:
        return False


class KeyExtractor:
    """密钥提取器（在后台线程里 run()；log/done 回调可能来自该线程，
    界面侧需自行切回 Tk 主线程）。"""

    def __init__(self, log_callback, done_callback):
        self.log = log_callback
        self.done = done_callback
        self._stop = False
        self.lib = None

    def stop(self):
        self._stop = True

    def _load_dll(self):
        """加载 wx_key.dll。附加目录不存在时只跳过——原来直接 add_dll_directory
        一个不存在的目录会抛 FileNotFoundError，而它排在"关掉微信"之后。"""
        for d in (RUNTIME_DIR, DLL_DIR):
            try:
                if os.path.isdir(d):
                    os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
        if DLL_DIR not in os.environ.get("PATH", "").split(";"):
            # 反复提取不该把 PATH 越拼越长
            os.environ["PATH"] = RUNTIME_DIR + ";" + DLL_DIR + ";" + os.environ.get("PATH", "")
        if not os.path.exists(KEY_DLL):
            self.log(f"[错误] 找不到 {KEY_DLL}")
            return False
        self.lib = ctypes.CDLL(KEY_DLL)
        self.lib.InitializeHook.argtypes = [ctypes.c_uint32]
        self.lib.InitializeHook.restype = ctypes.c_bool
        self.lib.PollKeyData.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.lib.PollKeyData.restype = ctypes.c_bool
        self.lib.CleanupHook.argtypes = []
        self.lib.CleanupHook.restype = ctypes.c_bool
        self.lib.GetLastErrorMsg.argtypes = []
        self.lib.GetLastErrorMsg.restype = ctypes.c_char_p
        return True

    def _cleanup(self):
        try:
            if self.lib:
                self.lib.CleanupHook()
        except Exception:
            pass

    def run(self):
        """执行提取流程。

        依赖自检排在关微信之前；不论成功、失败还是抛异常，finally 都会撤 Hook
        （原来异常分支会把注入留在微信进程里，直到微信自己退出）。"""
        try:
            ok, why = preflight()
            if not ok:
                self.log("[错误] " + why)
                self.done(False, None)
                return
            if self._stop:
                return        # 开始前就被取消：不做关微信/启动这种破坏性动作

            # 步骤 1: 关闭微信
            self.log("[1/4] 正在关闭微信...")
            kill_wechat()
            if self._stop:
                return
            self.log("[1/4] 微信已关闭")

            # 步骤 2: 启动微信
            self.log("[2/4] 正在启动微信...")
            if not launch_wechat():
                self.log("[提示] 未找到微信，请手动打开微信后再继续")
            # 等待微信启动（最长 40 秒；启动慢/自定义路径时给足时间）
            pid = None
            for _ in range(80):
                if self._stop:
                    return
                pid = find_wechat_pid()
                if pid:
                    break
                time.sleep(0.5)
            if not pid:
                self.log("[错误] 等待微信启动超时（40秒）")
                self.log("[提示] 请确认微信已安装，或手动打开微信后重试")
                self.done(False, None)
                return
            self.log(f"[2/4] 微信已启动 (PID={pid})")

            # 加载 DLL
            if not self._load_dll():
                self.done(False, None)
                return

            # 步骤 3: 注入 Hook。"最小 PID 就是主进程"并不可靠（微信 4.x 多进程、
            # PID 会被回收复用），所以第一个候选失败后逐个重试其它微信进程。
            cands = [pid] + [p for p in wechat_pids() if p != pid]
            hooked = False
            for i, target in enumerate(cands):
                if self._stop:
                    return
                self.log(f"[3/4] 正在注入 Hook (PID={target})..." if not i
                         else f"[3/4] 上一个进程不对，换 PID={target} 重试...")
                if self.lib.InitializeHook(target):
                    hooked = True
                    break
                err = self.lib.GetLastErrorMsg()
                self.log("      " + (err.decode("utf-8", errors="replace")
                                     if err else "注入失败"))
            if not hooked:
                self.log("[错误] Hook 注入失败")
                self.done(False, None)
                return
            self.log("[3/4] Hook 注入成功！")

            # 步骤 4: 等待登录捕获密钥
            self.log("[4/4] 请在微信中扫码登录（最多等待 120 秒）...")
            buf = ctypes.create_string_buffer(128)
            start = time.time()
            while time.time() - start < 120:
                if self._stop:
                    return
                buf.raw = b"\x00" * 128          # 复用同一个缓冲，避免读到上一轮的残留
                if self.lib.PollKeyData(buf, 128):
                    raw = bytes(buf)
                    key = None
                    for offset in range(0, min(len(raw) - 64, 64)):
                        candidate = raw[offset:offset + 64]
                        try:
                            s = candidate.decode("ascii")
                            if len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
                                key = s
                                break
                        except Exception:
                            continue
                    if not key:
                        key = buf.value.decode("ascii", errors="replace")[:64]
                    if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key):
                        self.log(f"[成功] 密钥提取成功！前16位: {key[:16]}...")
                        self.done(True, key)
                        return
                time.sleep(0.2)

            self.log("[错误] 密钥提取超时（120秒）")
            self.log("提示：请确保在微信启动后尽快扫码登录")
            self.done(False, None)
        except Exception as e:
            self.log(f"[错误] 提取过程异常: {e}")
            self.done(False, None)
        finally:
            self._cleanup()        # 撤 Hook：成功/超时/取消/异常都走到这一次
