# -*- coding: utf-8 -*-
"""
微信来消息提醒 v4.6.0
====================
Windows 微信 / 企业微信新消息 → 屏幕顶部横幅提醒。

功能：
  - 监控微信 message/media 的 -wal 文件变化，实时弹出横幅
  - v3.0 增强：横幅显示发件人昵称和消息具体内容（数据库解密读取）
  - v4.6.0：企业微信提醒——识别企微的通知弹窗（OCR），与微信共用横幅
  - 横幅外观自定义：位置 / 主题 / 标题 / 圆角弧度
  - 免打扰时段、提示音、微信前台不打扰、公众号消息开关
  - 图形化设置界面（微信绿主题、浅色/深色切换、圆角、左侧导航、全局拖动）
  - 开机自启（注册表 Run）、单实例保护、托盘图标、日志文件

用法：
  双击 exe 直接运行；或 python wechat_notify.py
  命令行参数：--minimized 启动时不显示设置窗口（用于开机自启）

模块结构（自上而下）：
  配置 → 日志 → Win32 辅助 → 微信检测 → 开机自启 → 单实例 →
  免打扰时段 → 横幅主题/几何 → UI 组件（圆角按钮/输入框）→
  Banner → SettingsWindow → 托盘 → Monitor → main
  企业微信的弹窗检测/OCR 在独立模块 wecom_notify.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import tkinter as tk
import base64
import hashlib
from tkinter import messagebox, filedialog, colorchooser

# v3.0: 微信数据库解密读取模块
try:
    from wechat_db import WeChatDB
    _HAS_WXDB = True
except ImportError:
    _HAS_WXDB = False

# v4.4: 密钥提取引擎——高级设置里内嵌的提取面板直接调用它，
# 不再另开「微信密钥提取工具」独立进程。缺模块时面板降级为手动输入。
try:
    from wx_key_engine import KeyExtractor, DLL_DIR, preflight
    _HAS_KEY_ENGINE = True
except ImportError:
    _HAS_KEY_ENGINE = False
    DLL_DIR = ""
    preflight = lambda: (False, "缺少 wx_key_engine.py")

# v4.6.0: 企业微信提醒——企微的库是自研加密读不了、后台也不弹提示，改走
# "原生导航栏未读红点"这条路（wecom_notify.py）。缺模块时只少这一块功能，
# 微信提醒照常。
try:
    from wecom_notify import WeComMonitor
    _HAS_WECOM = True
except Exception:
    _HAS_WECOM = False

APP_NAME = "微信提醒"
VERSION = "4.6.1"
# 对外唯一联系入口（关于我们页展示，点击可复制）
CONTACT_EMAIL = "smallfishanddream@outlook.com"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "微信提醒")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "微信提醒.log")

# ============================================================
# 配置
# ============================================================
DEFAULTS = {
    "data_root": os.path.expandvars(r"%USERPROFILE%\Documents\xwechat_files"),
    "poll_interval": 0.15,          # 检测间隔（秒）
    "merge_window": 15.0,           # 去重窗口（秒）
    "popup_duration": 10.0,         # 横幅显示时长（秒）：3 行订单/地址 6 秒读不完
    "sound": True,                  # 提示音
    "custom_sound": "",             # 自定义提示音路径（wav；空=系统默认音）
    "sound_source": "default",      # 提示音来源: default 系统 / builtin1-4 内置 / custom 自定义
    "win_geometry": "",             # 设置窗口位置记忆（"+x+y"；空=屏幕居中）
    "skip_if_wechat_active": True,  # 微信前台不打扰
    "include_biz_messages": False,  # 是否提醒公众号
    "wecom_on": True,               # v4.6.0: 同时提醒企业微信（原生未读红点）
    "banner_width": 560,
    "banner_height": 64,            # 横幅最小高度：内容多会自动长；正文只剩 1 行后
                                    # 再留 96 的下限就是纯撑高（底部一片空的元凶）
    "banner_title": "微信 · 新消息",
    "banner_hint": "左键单击打开微信　·　右键单击关闭",
    "banner_pos": "center",         # 横幅位置: center 居中 / left 居左 / right 居右 / custom 自定义左上角坐标
    "banner_theme": "dark",         # 横幅主题: dark 深色·微信绿 / pink 粉色 / light 浅色 / custom 自定义
    "banner_custom_bg": "#202126", "banner_custom_title": "#07C160",
    "banner_custom_text": "#E8EAED", "banner_custom_accent": "#07C160",
    "banner_custom_hint": "#6A7078",
    "banner_radius": 18,            # 圆角弧度（0=直角）
    "banner_font_size": 11,         # v4.1.0: 横幅内容字号（9-16）
    "banner_opacity": 1.0,          # v4.1.0: 横幅不透明度（0.3-1.0）
    "banner_vertical": "top",       # v4.1.0: 横幅垂直位置: top 顶部 / center 中部 / bottom 底部
    "banner_pos_x": 40,             # v4.6.2: 自定义位置——横幅左上角 X（屏幕像素）
    "banner_pos_y": 40,             # v4.6.2: 自定义位置——横幅左上角 Y（屏幕像素）
    "auto_start": False,            # 开机自启
    "quiet_enabled": False,         # 免打扰时段开关
    "quiet_start": "23:00",         # 免打扰开始（HH:MM）
    "quiet_end": "07:00",           # 免打扰结束（HH:MM）
    "theme": "light",               # 设置窗口主题: light 浅色 / dark 深色
    # v4.2.0: 提醒规则系统（关键词/联系人过滤 + 屏蔽列表）
    "banner_mode": "all",           # 提醒模式: all 全部提醒 / filtered 按规则提醒
    "alert_keywords": "",           # 仅提醒命中关键词（逗号分隔；空=不启用关键词白名单）
    "alert_contacts": "",           # 仅提醒这些联系人/群（逗号分隔；空=不启用联系人白名单）
    "mute_keywords": "",            # 命中关键词则静默（逗号分隔；屏蔽优先于白名单）
    "mute_contacts": "",            # 这些联系人/群不提醒（软件内屏蔽列表，覆盖私聊/群聊）
    "sound_volume": 70,             # v4.2.0: 提示音音量（0-100，动态缩放 wav 样本）
    # v4.5.0: 私聊快捷回复——横幅底部的话术按钮，点击把这句话复制进剪贴板
    # （不碰微信窗口、不自动发送；发送由你自己 Ctrl+V 完成）
    "quick_reply_on": True,
    "quick_replies": ["收到，马上处理", "在的，稍等", "好的，我看一下"],
    # v4.5.5：自动回复——点横幅跳到对方会话；点话术先跳到对方、核对通过后把这句话
    # 打进输入框（不回车、不发送）。默认关：它会抢前台几秒，而且同名/没在常用列表
    # 里的人可能跳错，所以每次都要过"那个会话的未读有没有清零"核对，核对不上一个字
    # 都不打。群聊一律只复制不驱动微信（搜群名不可靠）。
    "auto_reply": False,
    # v4.5.2：驱动总闸。关掉后跳会话/填入/自检**一步都不动微信**（连前台都不抢、
    # 连一个键都不发），话术照旧复制进剪贴板。出了事先拨这一格，不用逐个功能关。
    # 除它之外还有频率闸（见 WX_DRV_MIN_GAP / WX_DRV_PER_MIN / WX_DRV_PER_HOUR）。
    "drive_on": True,
}
QR_MAX = 3          # 横幅上最多放几个话术按钮（再多就挤成第二行，卡片会跳高）
QR_LEN = 40         # 单条话术最长字符数
config = dict(DEFAULTS)
_config_lock = threading.Lock()


def _coerce_config():
    """按 DEFAULTS 的类型校正配置值。

    界面上的数字输入框能被清空或填成文本（也兼容老版本存过的奇怪类型）；
    不校正的话 `int(config["popup_duration"] * 1000)` 这类转换会在启动/建窗
    路径上抛异常——正是社区反馈"右键托盘打不开设置界面"的那一类根因。"""
    for key, dft in DEFAULTS.items():
        if key not in config:
            config[key] = dft
            continue
        cur = config[key]
        if isinstance(dft, bool):
            config[key] = bool(cur)
        elif isinstance(dft, int) and not isinstance(dft, bool):
            try:
                config[key] = int(float(cur))
            except (TypeError, ValueError):
                config[key] = int(dft)
        elif isinstance(dft, float):
            try:
                config[key] = float(cur)
            except (TypeError, ValueError):
                config[key] = float(dft)
        elif isinstance(dft, str):
            config[key] = dft if cur is None else (cur if isinstance(cur, str) else str(cur))
        elif isinstance(dft, list):
            # 列表项（话术库）：老版本可能存成字符串或塞进奇怪类型，逐项清洗。
            # 清空是合法状态（横幅就不显示按钮），所以空列表不回退成默认值——
            # 缺键的情况上面 `key not in config` 已经补过默认值了。
            raw = cur if isinstance(cur, (list, tuple)) else ([cur] if cur else [])
            config[key] = [str(x).strip()[:QR_LEN] for x in raw if str(x).strip()][:QR_MAX]


def _migrate_auto_reply(saved):
    """v4.5.5：跳会话 / 填入两个实验开关合成「自动回复」。
    老配置里开过任意一个的，升级后保持开启；旧键不再保留。"""
    if "auto_reply" not in saved and (saved.get("quick_reply_fill") or saved.get("jump_on_click")):
        config["auto_reply"] = True
    config.pop("quick_reply_fill", None)
    config.pop("jump_on_click", None)


def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                config.update(saved)
                _migrate_auto_reply(saved)
    except Exception as e:
        log(f"[config] 读取配置失败: {e}")
        # 主文件坏掉时退回上一次的备份：宁可回到昨天的设置，也不要整份变默认值
        try:
            with open(CONFIG_FILE + ".bak", "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                config.update(saved)
                _migrate_auto_reply(saved)
                log("[config] 已从备份文件恢复配置")
        except Exception as e2:
            log(f"[config] 备份不可用: {e2}")
    _coerce_config()


def save_config():
    try:
        # 加锁：Monitor 线程读取 config 时不会遇到写一半的配置
        with _config_lock:
            os.makedirs(APP_DIR, exist_ok=True)
            # 原子写：先写 .tmp 再 replace。原来直接 open(path, "w") 会先把好的
            # 配置文件截断，写到一半崩溃就只剩半截 JSON，下次启动静默回退默认值
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                if os.path.exists(CONFIG_FILE):
                    shutil.copyfile(CONFIG_FILE, CONFIG_FILE + ".bak")
            except Exception:
                pass
            os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        log(f"[config] 保存配置失败: {e}")


# ============================================================
# 日志
# ============================================================
MAX_LOG_BYTES = 2 * 1024 * 1024  # 单份日志上限 2MB，超出后轮转保留最近两份


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        # 轮转：日志超过 2MB 时把旧文件备份为 .1（保留最近两份），防止无限增长；
        # 备份失败（权限等）时直接截断当前文件，保证日志不再继续膨胀
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
                bak = LOG_FILE + ".1"
                try:
                    if os.path.exists(bak):
                        os.remove(bak)
                    os.rename(LOG_FILE, bak)
                except Exception:
                    open(LOG_FILE, "w", encoding="utf-8").close()
        except Exception:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    try:
        print(msg, flush=True)
    except Exception:
        pass


# ============================================================
# Win32 辅助
# ============================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WEIXIN_MAIN_CLASS = "Qt51514QWindowIcon"
WEIXIN_TITLE = "微信"


def _is_weixin_main_class(cls):
    """窗口类名判定。

    微信主窗体类名是 "Qt<Qt版本号>QWindowIcon"，Qt 一升级中间的 51514 就变，
    写死等值比较会让"微信在前台就不弹窗"整块功能静默失效（弹窗照弹、水位不再
    记录）。进程归属已经由 is_weixin_process 限定，这里放宽到 Qt 类名前缀即可。"""
    return bool(cls) and (cls == WEIXIN_MAIN_CLASS or
                          (cls.startswith("Qt") and cls.endswith("QWindowIcon")))


def round_toplevel(win, radius, width=None, height=None):
    """给 Tk 顶层窗口设置圆角。

    winfo_id() 返回的是内层 TkChild 子窗口，需用 GetParent 取真正的顶层窗口。
    width/height 可选，用于窗口尚未映射时按配置尺寸直接设置。
    """
    try:
        hwnd = win.winfo_id()
        parent = user32.GetParent(hwnd)
        if parent:
            hwnd = parent
        w = width or win.winfo_width()
        h = height or win.winfo_height()
        if w <= 1 or h <= 1:
            return
        hrgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        if hrgn:
            user32.SetWindowRgn(hwnd, hrgn, True)
    except Exception:
        pass


def process_name(pid):
    if pid <= 0:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_weixin_process(pid):
    name = process_name(pid)
    return name.startswith("weixin") or name.startswith("wechat")


_ACCOUNT_PREFIXES = ("wxid_", "openim_", "v3_")


def is_account_dir(name: str) -> bool:
    """识别微信账号数据目录（wxid_ / openim_ / v3_ 开头）"""
    return name.lower().startswith(_ACCOUNT_PREFIXES)


def find_weixin_main_window():
    result = []

    def cb(hwnd, lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not is_weixin_process(pid.value):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if _is_weixin_main_class(cls.value):
            result.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    for h in result:
        t = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, t, 256)
        if t.value == WEIXIN_TITLE:
            return h
    return result[0] if result else 0


VK_CONTROL, VK_MENU, VK_W = 0x11, 0x12, 0x57


def _send_wechat_toggle_hotkey():
    """模拟微信默认的「显示/隐藏窗口」热键 Ctrl+Alt+W。

    微信用自己的快捷键隐藏窗口时，应用内部状态仍是「隐藏」；直接 ShowWindow
    只会把最后一帧缓存画面拽出来，窗口看着像假死、点哪都没反应（2026-10-30 反馈）。
    模拟同一个热键让微信自己走显示流程，窗口才真正活过来。
    """
    for vk in (VK_CONTROL, VK_MENU, VK_W):
        user32.keybd_event(vk, 0, 0, None)
        time.sleep(0.01)
    for vk in (VK_W, VK_MENU, VK_CONTROL):
        user32.keybd_event(vk, 0, 0x0002, None)
        time.sleep(0.01)


def bring_wechat_front():
    hwnd = find_weixin_main_window()
    if not hwnd:
        return False
    # v4.0.1: 增强激活——最小化则恢复；SetForegroundWindow 受前台锁限制，
    # 失败时用 ALT 键释放前台锁重试，确保点击横幅能真正把微信带到前台
    try:
        hidden = (not user32.IsWindowVisible(hwnd)) and (not user32.IsIconic(hwnd))
    except Exception:
        hidden = False
    if hidden:
        # v4.6.1: 用户用快捷键（默认 Ctrl+Alt+W）隐藏窗口，不是最小化。
        # 强行 ShowWindow 会让微信保持“隐藏”内部状态 → 画面定格、不响应输入。
        try:
            _send_wechat_toggle_hotkey()
            time.sleep(0.25)
        except Exception:
            pass
        hwnd = find_weixin_main_window() or hwnd
        if not user32.IsWindowVisible(hwnd):
            # 热键被改过/没生效：退回强制显示，至少先把窗口拉出来
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
    elif user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE：最小化时恢复
    try:
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_TOPMOST + NOSIZE + NOMOVE + SHOWWINDOW
    except Exception:
        pass
    ok = bool(user32.SetForegroundWindow(hwnd))
    # v4.0.1: 还原置顶层级，避免微信被长期置顶遮挡其它窗口
    try:
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_NOTOPMOST + NOSIZE + NOMOVE
    except Exception:
        pass
    if not ok:
        try:
            # 前台锁兜底：模拟 ALT 键释放当前前台窗口的锁，再激活
            user32.keybd_event(0x12, 0, 0, 0)   # ALT down
            user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT up
            ok = bool(user32.SetForegroundWindow(hwnd))
        except Exception:
            ok = False
    return ok


# ===================== 驱动微信界面（自动回复用，默认关闭） =====================
# 微信 4.x 的界面是自绘的：UIA 树只有 3 个节点、零子元素，既读不到会话列表也读不到
# 焦点，也没有"按 wxid 打开会话"的 URL 协议。所以只剩"点搜索框 + 输名字 + 回车"这一条，
# 而它唯一的可验证点是这个搜索下拉面板——它是**独立顶层窗口**（类名以
# QWindowToolSaveBits 结尾）。下面每一步都在拿到证据之后才走下一步：
#   面板没出现 = 没聚焦 = 一个字符都不输入；输入没让面板变化 = 立刻退格撤销且不回车。
# 这样最坏情况是"什么都没发生"，不存在把联系人名发给客户的路径。

WX_UI_CHILD_CLASS = "MMUIRenderSubWindow"      # 微信整个客户区其实是这个子窗口在画
WX_PANEL_CLASS_TAIL = "QWindowToolSaveBits"    # 搜索下拉面板的窗口类名后缀
WX_SEARCH_DX = 107          # 搜索框中心：相对子窗口左上的偏移（实测标定）
WX_SEARCH_DY = 50
WX_PANEL_WAIT = 1.3         # 每次点击后等面板出现的秒数（最多点三次，见 wx_open_search_panel）
WX_PANEL_TRIES = 3
# 聊天输入框的"安全点"：横向取窗口 62% 处（一定在会话列表右边的聊天栏里），
# 纵向取"底边往上 130 逻辑像素"——输入框那块面板是贴着底部固定高度的，
# 按窗口高度的比例算反而会在高窗口里飘到消息列表上。
# 真机实测：搜索框回车之后焦点还留在搜索框，直接打字会全打进搜索框（落点判据
# 当场把它退了回来），所以填入前必须先点这里把焦点要过来。
WX_INPUT_FX = 0.62
WX_INPUT_DY = 130
VK_ENTER, VK_ESC, VK_BACK = 0x0D, 0x1B, 0x08
VK_CONTROL, VK_A, VK_DELETE = 0x11, 0x41, 0x2E
# 他正在敲键盘/挪鼠标时不去动微信：整条链子要抢三四秒焦点，那期间他自己的键会
# 落进微信的搜索框。这里要求"开始注入之前已经安静了 WX_QUIET_MS 毫秒"，等不到就放弃
# 这一次（宁可只复制进剪贴板）。注意 GetLastInputInfo 把我们自己 SendInput 也算进去，
# 所以只能在**动手之前**查，打一半再查永远是 0。
WX_QUIET_MS = 500
WX_QUIET_WAIT = 2.0
# 填入成功后把他原来那份剪贴板还回去的上限：再大就不值得为它冒"复制没写完"的风险
QR_CLIP_KEEP_MAX = 100000
# ---------- 驱动总闸与频率闸（v4.5.2）----------
# 为什么要限频率：往微信里"动一下"是有代价的——抢前台、发合成键、点它的窗口。
# 人手工点一次，和"哪条链路卡住了在那里反复自触发"，在微信那边是完全不同的两回事。
# 所以下手前必须问闸，三道都不许超：
#   ① 两次"用户动作"之间至少隔 WX_DRV_MIN_GAP 秒（连点两下不会背靠背打两轮）
#   ② 60 秒内最多 WX_DRV_PER_MIN 次
#   ③ 60 分钟内最多 WX_DRV_PER_HOUR 次（卡死的循环最迟一小时后被切断）
# 阈值按"人手速的极限"留了余量：点一次话术（跳+填）已经合成**一条**链子、只算 1 次驱动，
# 一秒点一次才是 60 次/分，所以 20 次/分不是正常回客户消息会撞到的墙；
# 真撞到了，就说明不对劲，这时候停下来才是对的。
WX_DRV_MIN_GAP = 1.2
WX_DRV_PER_MIN = 20
WX_DRV_PER_HOUR = 200
WX_DRV_WIN_MIN = 60.0
WX_DRV_WIN_HOUR = 3600.0
# 2026-10-23 真机复测（截图 shots_search/person_0_typed.png、enter_after.png）：
# 搜一个真实联系人名，下拉第一节是「最常使用」，第一行就是那个联系人；
# 输完直接回车，微信右栏标题变成她 —— 回车这条路是通的。
# 上一轮"只剩搜索网络结果、没有联系人行"是我方 bug：这个框记住上一次的内容且
# 光标停在开头，残留一个 "4" 时逐字输入变成"文4件传输助手"，本地自然搜不到人。
# 所以现在输入前先全选删除（见 wx_open_chat），回车后再拿未读数核对。
# 已知残留风险：文件传输助手这类"功能"不在「最常使用」里，第一行是网络结果——
# 所以这条链子只用于私聊联系人，且核对不上就立刻 Esc 收手。
WX_SEARCH_USABLE = True


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _IUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    # 不要设 _pack_：x64 上 Windows 的 INPUT 是 4(类型)+4(对齐填充)+32(union)=40 字节，
    # 紧凑排布会算成 36，SendInput 因 cbSize 不符直接返回 0（返回 0 是静默失败，
    # 曾经据此得出过"微信不接收合成输入"的假结论）
    _fields_ = [("type", wt.DWORD), ("u", _IUNION)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwTime", wt.DWORD)]


def wx_idle_ms():
    """离系统上一次收到任何输入（键盘或鼠标）过了多少毫秒；取不到返回 None。"""
    try:
        li = _LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)      # 同样是 cbSize：写错这调用就静默失败
        if not user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        # GetTickCount 在 kernel32，不在 user32（写错会 AttributeError，
        # 而这条又被包在"取不到就放行"里，最容易悄悄废掉整个闸）
        return (int(kernel32.GetTickCount()) - int(li.dwTime)) & 0xFFFFFFFF
    except Exception:
        return None


def wx_wait_quiet(secs=WX_QUIET_WAIT, need=WX_QUIET_MS):
    """等他手停下来再动微信。返回 (可以动了吗, 最后测到的静默毫秒)。
    取不到数值时放行——不能因为一个早就有的调用失败就把整条功能废掉。"""
    idle = wx_idle_ms()
    end = time.time() + secs
    while idle is not None and idle < need and time.time() < end:
        time.sleep(0.15)
        idle = wx_idle_ms()
    return idle is None or idle >= need, idle


def wx_pids():
    """当前机器上所有微信/企业微信进程的 pid 集合（不查子进程名，按进程名匹配）。"""
    TH32CS_SNAPPROCESS = 0x2

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = set()
    if snap in (0, -1) or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = kernel32.Process32First(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile.decode("utf-8", "replace").lower()
            if name in ("weixin.exe", "wechat.exe"):
                out.add(int(e.th32ProcessID))
            ok = kernel32.Process32Next(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def wx_visible_tops(pids=None):
    """微信进程的可见顶层窗口 -> {hwnd: (类名, rect)}"""
    pids = pids if pids is not None else wx_pids()
    out = {}
    if not pids:
        return out
    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, l):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(h):
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            c = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, c, 256)
            out[h] = (c.value, (r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(Proc(cb), 0)
    return out


def wx_ui_child():
    """(主窗口, UI 子窗口, 子窗口 rect)。子窗口找不到时退回主窗口自身。"""
    main = find_weixin_main_window()
    if not main:
        return 0, 0, None
    found = []
    Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, l):
        c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, c, 256)
        if WX_UI_CHILD_CLASS in c.value and user32.IsWindowVisible(h):
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            found.append((h, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumChildWindows(main, Proc(cb), 0)
    if found:
        found.sort(key=lambda x: -(x[1][2] - x[1][0]) * (x[1][3] - x[1][1]))
        return main, found[0][0], found[0][1]
    r = wt.RECT()
    user32.GetWindowRect(main, ctypes.byref(r))
    return main, main, (r.left, r.top, r.right, r.bottom)


def wx_activate_verified(timeout=2.5):
    """把微信带到前台，并**确认**前台真的是它。

    带不来前台就谈不上下一步：任何按键都会打进别的窗口。之前吃过这个亏——
    脚本里 SetForegroundWindow 返回 True，0.8 秒后前台却是别的应用。"""
    main = find_weixin_main_window()
    if not main:
        return False, "微信窗口没找到"
    end = time.time() + timeout
    while time.time() < end:
        bring_wechat_front()
        for _ in range(12):
            if user32.GetForegroundWindow() == main:
                return True, "ok"
            time.sleep(0.05)
    return False, "抢不到前台（前台被别的应用占着）"


def wx_print_window(hwnd):
    """PrintWindow(PW_RENDERFULLCONTENT) 取窗口自己的画面。

    必须用它而不是截屏：截屏拿到的是"那块屏幕上最上面的东西"，别的窗口一盖上来
    就成了假证据（这轮排查里被记事本和一个全屏覆盖层坑过两次）。"""
    if not hwnd:
        return None
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 16 or h < 16:
        return None
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    img = None
    try:
        user32.PrintWindow(hwnd, mem, 2)
        gdi32.GdiFlush()

        class BI(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bi = BI()
        bi.biSize = ctypes.sizeof(BI)
        bi.biWidth = w
        bi.biHeight = -h          # 负数 = 自上下行序，省得再翻一次
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            from PIL import Image
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
    return img


def wx_diff_box(a, b, thresh=10):
    """两张图的差异比例与包围盒；完全一样返回 (0.0, None)。"""
    if a is None or b is None or a.size != b.size:
        return 1.0, [0, 0, 0, 0]
    from PIL import ImageChops
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    bb = d.getbbox()
    if not bb:
        return 0.0, None
    px = d.convert("L").point(lambda v: 255 if v > thresh else 0)
    hist = px.histogram()
    w, h = px.size
    return hist[255] / float(max(1, w * h)), [bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]]


def wx_region_diff(a, b, region, thresh=10):
    """两张图在指定矩形 (x0, y0, x1, y1) 内的差异比例（占该矩形面积）。

    整窗 diff 的包围盒会被**别的区域**的变化污染——聊天区同时进来一条新消息，
    落点判定的包围盒就跟着变大判错（2026-10-27 真机：字填进了输入框，被同屏
    新消息的包围盒污染，误判后自己擦掉了）。只关心一块区域时用它裁开再比。"""
    if a is None or b is None or a.size != b.size:
        return 1.0
    x0, y0, x1, y1 = region
    if x1 <= x0 or y1 <= y0:
        return 1.0
    ca = a.crop((int(x0), int(y0), int(x1), int(y1)))
    cb = b.crop((int(x0), int(y0), int(x1), int(y1)))
    pct, _ = wx_diff_box(ca, cb, thresh)
    return pct


class WxFocusLost(RuntimeError):
    """布防期间前台不再是微信、或微信窗口没了。
    合成按键是发给"当前前台"的，不是发给我们盯着的那个句柄——前台一换，
    下一次 Ctrl+A / Delete / 逐字输入就打进了别的应用（编辑器里等于删掉他的正文）。
    所以从布防那一刻起，**每一手输入之前都要重新确认前台**，确认不了就什么都不发。"""


_wx_guard = None            # {"main": hwnd, "pids": set()}；None = 没布防（自检脚本/老调用点）
_wx_drive = threading.Lock()    # 同一时刻只允许一条链路在驱动微信


def wx_guard_arm(main, pids):
    global _wx_guard
    _wx_guard = {"main": main, "pids": set(pids or ())}


def wx_guard_disarm():
    global _wx_guard
    _wx_guard = None


def wx_front_pid():
    """当前前台窗口属于哪个进程；取不到返回 -1。"""
    fg = user32.GetForegroundWindow()
    if not fg:
        return -1
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    return pid.value or -1


def win_alive(hwnd):
    try:
        return bool(hwnd) and bool(user32.IsWindow(hwnd))
    except Exception:
        return False


def wx_guard_ok():
    """没布防时放行；布防后要求「微信主窗口还在 + 前台属于微信的进程」。"""
    g = _wx_guard
    if not g:
        return True
    return win_alive(g["main"]) and wx_front_pid() in g["pids"]


def wx_guard_alive():
    """失败时用来分辨"微信窗口没了"还是"只是前台被抢了"。"""
    g = _wx_guard
    if not g:
        return True
    return win_alive(g["main"])


def _wx_guard_or_die():
    if not wx_guard_ok():
        raise WxFocusLost("前台已经不是微信（或微信窗口已经没了），后续输入全部取消")


def wx_drive_try():
    """抢驱动权。抢不到说明上一条链路还在打字（点了两下话术、或跳会话和填入撞车），
    两批键混在一起打是最坏的情况，后来者直接放弃。"""
    return _wx_drive.acquire(blocking=False)


def wx_drive_release():
    try:
        _wx_drive.release()
    except RuntimeError:
        pass


_wx_rate_lock = threading.Lock()
_wx_stamps = []            # 最近一小时内每次"问过闸且准备动手"的时刻（单调时钟，不受改表影响）
_wx_last_drive = None
_wx_rate_hits = {"off": 0, "gap": 0, "min": 0, "hour": 0}
_wx_rate_done = 0
_wx_outcomes = {}          # {"跳会话": {"ok": 2, "no-front": 1, ...}, ...} 每次过闸后的结局
_WX_OK_STEPS = frozenset({"typed", "entered", "filled", "drive-ok"})


def wx_drive_note(where, step):
    """记一次驱动结局。只在链子真动过微信（过了频率闸）或撞车退回时调；
    被 off/gap/min/hour 挡下的那些由 _wx_rate_hits 记，不在这里重复。
    结局按最终 step 归桶（"ok" 是收成步：跳+填里弱核对挡下的 weak-changed
    虽然 ok 标志还挂在跳那半，落点也绝不是"成功"）。"""
    with _wx_rate_lock:
        d = _wx_outcomes.setdefault(where, {})
        d[step] = d.get(step, 0) + 1


def wx_rate_check(now=None):
    """问闸：现在能不能动微信。放行就当场登记这一次，返回 (True, "", 0.0)；
    挡住返回 (False, step, 还要等几秒)。

    登记放在问闸这一刻，而不是打完字之后：链子后面就算在"没抢到前台"那步收了手，
    前面也已经点过搜索框、发过键。宁可多算一次，不能少算。
    调用点在驱动锁之后，所以 busy / user-typing 被退回的请求不会消耗配额。

    一次"用户动作"= 一条链子。点话术那种要跳要填的也已经合成一条（`wx_jump_fill`），
    所以这里不需要给"第二条腿"开后门——上一版开后门是因为一次点击真跑了两轮链子，
    合并之后那个坑本身没了。"""
    global _wx_last_drive, _wx_rate_done
    if now is None:
        now = time.monotonic()
    with _wx_rate_lock:
        _wx_stamps[:] = [t for t in _wx_stamps if t > now - WX_DRV_WIN_HOUR]
        if not (config or {}).get("drive_on", True):
            _wx_rate_hits["off"] += 1
            return False, "drive-off", 0.0
        if _wx_last_drive is not None and now - _wx_last_drive < WX_DRV_MIN_GAP:
            _wx_rate_hits["gap"] += 1
            return False, "rate-gap", WX_DRV_MIN_GAP - (now - _wx_last_drive)
        win = [t for t in _wx_stamps if t > now - WX_DRV_WIN_MIN]
        if len(win) >= WX_DRV_PER_MIN:
            _wx_rate_hits["min"] += 1
            return False, "rate-min", WX_DRV_WIN_MIN - (now - win[0])
        if len(_wx_stamps) >= WX_DRV_PER_HOUR:
            _wx_rate_hits["hour"] += 1
            return False, "rate-hour", WX_DRV_WIN_HOUR - (now - _wx_stamps[0])
        _wx_stamps.append(now)
        _wx_last_drive = now
        _wx_rate_done += 1
        return True, "", 0.0


def wx_rate_gate(r, where):
    """问闸 + 把原因写进结果字典，供横幅和自检界面显示。挡住时调用方必须一步都不动微信。"""
    ok, step, wait = wx_rate_check()
    if ok:
        return True
    r["step"] = step
    r["wait"] = round(wait, 1)
    log(f"[频率闸] {where}：这一步不动微信（{step}，还要等 {wait:.1f}s；"
        f"已驱动 {_wx_rate_done} 次、挡下 {sum(_wx_rate_hits.values())} 次）")
    return False


def wx_drive_allowed():
    return bool((config or {}).get("drive_on", True))


def wx_rate_reset():
    """清掉配额（时间戳与"上一次动过"的时刻），累计计数保留。
    他把总闸关回来这一刻要用：否则上一段卡死循环攒下的配额会把刚恢复的操作继续挡着，
    而界面上又只写"总闸已开"，人只会更糊涂。测试里也靠它把每条用例回到同一起点。"""
    global _wx_last_drive
    with _wx_rate_lock:
        _wx_stamps[:] = []
        _wx_last_drive = None


def wx_off_gate(r, where):
    """总闸挡在**入口**：关着的时候连"等他手停下来 0.5 秒"都不做，直接只走剪贴板。
    频率闸放在驱动锁之后，所以被 busy / user-typing 退回的请求不会消耗配额。"""
    if wx_drive_allowed():
        return True
    with _wx_rate_lock:
        _wx_rate_hits["off"] += 1
    r["step"] = "drive-off"
    log(f"[总闸] {where}：驱动总闸关着，一个键都没往微信发")
    return False


def wx_rate_why(step, wait=0.0):
    """把闸的 step 翻成一句能给人看的话（设置界面用，横幅另有一句短的）。"""
    if step == "drive-off":
        return ("驱动总闸是关着的：高级设置里勾上「允许软件动微信界面」才会动它"
                "（话术仍会复制进剪贴板）")
    if step == "rate-gap":
        return f"离上一次动微信还不到 {WX_DRV_MIN_GAP} 秒，再等 {wait:.1f} 秒"
    if step == "rate-min":
        return (f"这一分钟已经动了 {WX_DRV_PER_MIN} 次微信，先到这儿，"
                f"再等 {wait:.0f} 秒（正常回消息撞不到这道闸）")
    if step == "rate-hour":
        return (f"这一小时已经动了 {WX_DRV_PER_HOUR} 次微信，今天先收手，"
                f"再等 {wait:.0f} 秒——一直这么多说明有链路在反复自触发")
    return "被频率闸挡住（步骤 " + str(step) + "）"


def wx_drive_stats():
    """给界面/日志看的计数：配额用了多少、被哪一道闸挡过多少次、结局分布。"""
    now = time.monotonic()
    with _wx_rate_lock:
        return {
            "on": bool((config or {}).get("drive_on", True)),
            "done": _wx_rate_done,
            "blocked": sum(_wx_rate_hits.values()),
            "hits": dict(_wx_rate_hits),
            "used_min": sum(1 for t in _wx_stamps if t > now - WX_DRV_WIN_MIN),
            "used_hour": len(_wx_stamps),
            "gap": WX_DRV_MIN_GAP,
            "per_min": WX_DRV_PER_MIN,
            "per_hour": WX_DRV_PER_HOUR,
            "outcomes": {k: dict(v) for k, v in _wx_outcomes.items()},
        }


def wx_drive_stat_line():
    s = wx_drive_stats()
    return (f"总闸 {'开' if s['on'] else '关'} · 本次运行已驱动 {s['done']} 次 · "
            f"闸下挡回 {s['blocked']} 次 · "
            f"额度：{s['gap']}s 间隔 / {s['used_min']}/{s['per_min']} 每分钟 / "
            f"{s['used_hour']}/{s['per_hour']} 每小时")


_WX_STEP_NAMES = {
    "ok": "成功",
    "busy": "撞车退回",
    "no-front": "没抢到前台",
    "no-child": "找不到输入区",
    "no-panel": "面板没出现",
    "panel-stuck": "残留面板关不掉",
    "click-fail": "点击失败",
    "type-fail": "打字失败",
    "text-not-accepted": "输入没生效",
    "no-print": "抓不到画面",
    "not-verified": "未读没清零",
    "weak-changed": "画面大变没敢填",
    "weak-no-pre": "弱核对没抓到跳前帧",
    "weak-no-print": "弱核对没抓到跳后帧",
    "text-not-in-input": "没落进输入框",
    "wx-gone": "微信中途没了",
    "lost-front": "前台被抢",
    "user-typing": "他正在打字，没敢碰",
}


def wx_drive_stat_text():
    """设置页看的多行版：第一行配额，后面按链路列这次运行各结局的次数。
    没动过微信时就只有一行，不占地方。"""
    s = wx_drive_stats()
    lines = [wx_drive_stat_line()]
    for where in ("跳会话", "填入", "跳+填", "自检"):
        d = s["outcomes"].get(where) or {}
        if not d:
            continue
        parts = []
        for step in sorted(d, key=lambda k: (k != "ok", -d[k], k)):
            parts.append(f"{_WX_STEP_NAMES.get(step, step)} {d[step]} 次")
        lines.append(f"· {where}：{'、'.join(parts)}")
    return "\n".join(lines)


def wx_type(s):
    """逐字符 SendInput(unicode)。走键盘而不是剪贴板，是为了不覆盖用户自己的剪贴板内容。
    每个字符前都重新确认一次前台：中途 alt-tab 最多丢半个词，不会把整句打进别的应用。"""
    for ch in s:
        _wx_guard_or_die()
        for up in (False, True):
            ev = _INPUT()
            ev.type = 1
            ev.u.ki.wScan = ord(ch)
            ev.u.ki.dwFlags = 4 | (2 if up else 0)      # KEYEVENTF_UNICODE [+ KEYUP]
            if user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(ev)) != 1:
                raise RuntimeError("SendInput 键盘失败 err=%d" % ctypes.GetLastError())
            time.sleep(0.018)


def wx_key(vk):
    _wx_guard_or_die()
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 2, 0)


def wx_chord(vk1, vk2):
    """两键组合（Ctrl+A 这种）。用 keybd_event，和 wx_key 保持同一套注入方式。"""
    _wx_guard_or_die()
    for vk in (vk1, vk2):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in (vk2, vk1):
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.02)


def wx_click(x, y):
    _wx_guard_or_die()
    old = wt.POINT()

    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.10)
    for flags in (0x0002, 0x0004):      # LEFTDOWN / LEFTUP
        ev = _INPUT()
        ev.type = 0
        ev.u.mi.dwFlags = flags
        if user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(ev)) != 1:
            raise RuntimeError("SendInput 鼠标失败 err=%d" % ctypes.GetLastError())
        time.sleep(0.05)
    user32.SetCursorPos(old.x, old.y)   # 光标放回原处，别停在微信上


def wx_dismiss_panels(pids, secs=0.8):
    """把已经开着的搜索下拉关掉，返回"现在确实没有面板了"。

    必须关：面板的判定靠"点完之后多出一个新窗口"，上一次操作留下的面板还挂在
    那儿的话，新面板就不会是"新"窗口，整条链子会被误判成"点了没反应"。
    只在真看到面板时才按 Esc，不去动他正开着的聊天窗。"""
    end = time.time() + secs
    while time.time() < end:
        panels = [h for h, (cls, _) in wx_visible_tops(pids).items()
                  if cls.endswith(WX_PANEL_CLASS_TAIL) or "SaveBits" in cls]
        if not panels:
            return True
        wx_key(VK_ESC)
        time.sleep(0.25)
    return False


def wx_wait_search_panel(pids, secs=WX_PANEL_WAIT):
    """等一个可见的搜索下拉面板出现，返回 (hwnd, 矩形)；超时 (None, None)。

    **不要求它是"新"窗口**：Qt 会复用同一个句柄，上一次留下的面板（哪怕已经看不见、
    只是还挂在枚举里）会让"只认新窗口"的判据永远等不到东西——2026-10-23 真机就
    突然连着几轮报 no-panel，而手点明明能弹。所以先用 wx_dismiss_panels 确认
    一个都不剩，再在这里等"有"。"""
    end = time.time() + secs
    while time.time() < end:
        for h, (cls, rect) in wx_visible_tops(pids).items():
            if cls.endswith(WX_PANEL_CLASS_TAIL) or "SaveBits" in cls:
                return h, rect
        time.sleep(0.06)
    return None, None


def wx_quiet_gate(r, where):
    """动手之前的"他没在敲键盘"闸。两次都要查：入口那次是为了**连焦点都不抢**，
    动手前那次是因为抢焦点那一轮要一两秒，这期间他可能正好开始打字。"""
    quiet, idle = wx_wait_quiet()
    r["idle_ms"] = idle
    if not quiet:
        r["step"] = "user-typing"
        log(f"[闸] {where}：他这边还在动键盘/鼠标（才安静 {idle} ms），这次不碰微信")
    return quiet


def wx_open_chat(name, press_enter=False, verify_username=None):
    """尝试把微信切到 name 这个联系人的会话。返回逐步判定结果，任何一步失败都立刻收手。

    闸门顺序：总闸 -> 安静闸 -> 驱动权 -> 频率闸 -> 确认前台 -> 点搜索框 ->
    等面板（没出现就 Esc 收工）-> 清空输入框（面板出现=焦点在框里，Ctrl+A 全选 + Delete）-> 输名字 ->
    核对面板画面确实变了（没变就退格撤销 + Esc，绝不回车）
    -> 回车 -> 可选：拿 verify_username 的未读数核对是否真打开了那个会话。

    整条链子受四把闸保护：总闸（drive_on，关掉就一步不动）、驱动权互斥（两条链路
    不能同时往微信里打字）、频率闸（间隔/每分钟/每小时三道配额）、以及每一手输入前的
    "前台还是微信"确认（见 WxFocusLost）。"""
    if not WX_SEARCH_USABLE:
        return {"name": name, "step": "unsupported-search", "ok": False,
                "why": "搜一搜那条路还没复测通过，闸门关着"}
    r = {"name": name, "step": "start", "ok": False}
    if not name:
        r["step"] = "no-name"
        return r
    if not wx_off_gate(r, "跳会话入口"):
        return r
    if not wx_quiet_gate(r, "入口"):
        return r
    if not wx_drive_try():
        r["step"] = "busy"
        wx_drive_note("跳会话", "busy")
        log("[跳会话] 上一次的微信操作还没结束，这次一律不动它")
        return r
    try:
        if not wx_rate_gate(r, "跳会话"):
            return r
        r["_driven"] = True
        return _wx_open_chat(name, press_enter, verify_username, r)
    except WxFocusLost as e:
        r["ok"] = False
        r["step"] = "wx-gone" if not wx_guard_alive() else "lost-front"
        r["why"] = str(e)
        log(f"[跳会话] {r['why']}（此后没有再发任何一个键）")
        return r
    finally:
        if r.pop("_driven", None):
            step = r.get("step") or "?"
            wx_drive_note("跳会话", "ok" if step in _WX_OK_STEPS else step)
        wx_guard_disarm()
        wx_drive_release()


def _wx_open_chat(name, press_enter, verify_username, r):
    ok, why = wx_activate_verified()
    if not ok:
        r["step"], r["why"] = "no-front", why
        return r
    main, child, crect = wx_ui_child()
    if not crect:
        r["step"] = "no-child"
        return r
    pids = wx_pids()
    wx_guard_arm(main, pids)      # 从这里开始，每一手输入前都要重新确认前台
    return _wx_open_steps(name, press_enter, verify_username, r, main, crect, pids)


def _wx_open_steps(name, press_enter, verify_username, r, main, crect, pids,
                   weak_check=False):
    """跳会话这一段的全部证据步骤。前提：已经抢到前台并且布好防。
    之所以单独拆出来，是让"点一次话术"能在**同一条链子**里接着打字（见 wx_jump_fill），
    而不是松掉驱动锁、还掉配额、再重新抢一次前台。

    weak_check：跳+填链路专用的弱核对。未读没有信号可核（本来就没未读/库打不开）时，
    拿"跳转前 vs 回车后"的微信主窗口画面做对比：画面大变说明打开了别的聊天（跳错了
    或没跳成），不填；画面没大变说明打开的正是跳转前那个会话，按目标会话已打开处理。
    挡得住"跳错人"，但"本来开着别的聊天、其实跳对了"也会被误挡（宁可误挡不误填）。"""
    img_pre = None
    if weak_check:
        # 在碰搜索框之前先取一张主窗口的稳定帧：刚抢完前台窗口可能还在动画，
        # 不稳定的帧拿去对比会把动画误判成"打开了别的聊天"
        img_pre = wx_wait_stable(main, secs=1.2, gap=0.25)
        r["weak_pre_ok"] = img_pre is not None
    if not wx_quiet_gate(r, "动手前"):
        return r
    if not wx_dismiss_panels(pids):
        r["step"] = "panel-stuck"
        log("[跳会话] 上一个搜索面板关不掉，停手（没点、没输入）")
        return r
    try:
        panel, clicks, lost = wx_open_search_panel(crect, pids)
    except WxFocusLost:
        raise                       # 前台丢了不是"点击失败"，让上层按 lost-front 收手
    except Exception as e:
        r["step"], r["why"] = "click-fail", str(e)
        return r
    r["clicks"] = clicks
    if not panel:
        wx_key(VK_ESC)
        r["step"] = "lost-front" if lost else "no-panel"
        log("[跳会话] 微信前台被别的窗口抢走了，已收手（没输入任何文字）" if lost
            else "[跳会话] 点了 %d 次搜索框都没出面板，已收手（没输入任何文字）" % clicks)
        return r
    r["panel"] = panel
    # 先清空：这个框会记住上一次的内容，而且光标停在开头。残留一个字符时逐字输入
    # 会被它插在第一个字后面（实测把"文件传输助手"变成"文4件传输助手"），
    # 本地搜不到联系人，下拉就只剩网络结果，回车进的是搜一搜网页那一路
    # （2026-10-23 真机就是这么把微信搞到卡退的）。
    #
    # 清空靠三道防线，缺一不可：面板必须真出现（出现 = 焦点在框里，Ctrl+A 才落得
    # 进框而不是按到别处）、Ctrl+A 全选后再 Delete（全选语义保证删得干净，不存在
    # "只删一半"）、每一键发出前的前台守卫（焦点中途被抢就抛 WxFocusLost 收手）。
    # 清空之后取一帧稳定基线，输一遍名字并核对面板确实变了才许回车。
    wx_chord(VK_CONTROL, VK_A)
    wx_key(VK_DELETE)
    # 等面板自己变完再取基线：面板刚弹出时"最近搜索"列表还在异步加载、窗口还在
    # 动画，抓早了会把"面板自己变完"误判成输入没生效（2026-10-27 真机：残留
    # 0.2113 是面板在变，不是框里真有字）。
    img_empty = wx_wait_stable(panel, secs=2.0, gap=0.3) or wx_print_window(panel)
    try:
        wx_type(name)
    except WxFocusLost:
        raise
    except Exception as e:
        wx_key(VK_ESC)
        r["step"], r["why"] = "type-fail", str(e)
        return r
    time.sleep(0.7)
    pct, box = wx_diff_box(img_empty, wx_print_window(panel))
    r["panel_changed"] = round(pct, 4)
    if pct < 0.002:
        for _ in range(len(name) + 1):
            wx_key(VK_BACK)
        wx_key(VK_ESC)
        r["step"] = "text-not-accepted"
        log("[跳会话] 面板在但没吃到输入，已退格撤销并关闭（没回车）")
        return r
    r["step"] = "typed"
    if not press_enter:
        log(f"[跳会话] 已把「{name}」填进微信搜索框，等你自己回车/点结果")
        r["ok"] = True
        return r
    # 回车之前先记下这个会话的未读数：回车之后它有没有被清零，就是"打开的到底
    # 是不是它"的唯一硬证据。放在回车前取，是为了不把他中途用手机读掉的情况
    # 误判成成功（那样未读本来就是 0，核对不出任何东西）。
    before = wx_read_unread(verify_username) if verify_username else None
    wx_key(VK_ENTER)
    r["step"] = "entered"
    r["ok"] = True
    if verify_username:
        if before is None:
            r["verified"] = "no-db"
            log("[跳会话] 已回车，但库打不开，没法核对打开的是不是目标会话")
        elif before == 0:
            r["verified"] = "no-signal"
            log(f"[跳会话] 已回车（「{verify_username}」本来就没有未读，无法用清零核对）")
        else:
            cleared = wx_wait_unread_cleared(verify_username)
            r["verified"] = cleared
            if not cleared:
                wx_key(VK_ESC)
                r["step"] = "not-verified"
                r["ok"] = False
                log(f"[跳会话] 回车了但「{verify_username}」的未读没清零，"
                    f"说明打开的不是它（或根本没打开），已 Esc 收手")
    # 弱核对：未读那边给不出信号（no-db / no-signal / 压根没给会话 id）时，
    # 用"跳转前后主窗口画面变没变"兜底。未读明确没清零（verified=False）说明
    # 强信号已经说了"跳错了"，弱核对不翻案、原样保留 not-verified。
    if weak_check and r.get("verified") in ("no-signal", "no-db", None):
        if img_pre is None:
            r["verified"] = "weak-no-pre"
            log("[跳会话] 弱核对：抓不到跳转前的主窗口画面，按核对不上处理（不填）")
        else:
            img_after = wx_wait_stable(main, secs=2.0, gap=0.3)
            if img_after is None:
                r["verified"] = "weak-no-print"
                log("[跳会话] 弱核对：抓不到回车后的主窗口画面，按核对不上处理（不填）")
            else:
                # 只比右侧聊天面板：左上角是搜索框和会话列表，打开会话本来就会
                # 置顶/高亮列表项、搜索框也会回位，那里变了不代表聊错了
                # （2026-10-27 真机：留在同一聊天，列表区差异 0.0102 被误判成"画面大变"）。
                W, H = img_pre.size
                reg = (0.30 * W, 0.08 * H, 0.99 * W, 0.96 * H)
                pct = wx_region_diff(img_pre, img_after, reg)
                _, box = wx_diff_box(
                    img_pre.crop((int(reg[0]), int(reg[1]), int(reg[2]), int(reg[3]))),
                    img_after.crop((int(reg[0]), int(reg[1]), int(reg[2]), int(reg[3]))))
                r["weak_changed"] = round(pct, 4)
                r["weak_bbox"] = box
                rw, rh = reg[2] - reg[0], reg[3] - reg[1]
                # 打开另一个聊天 = 整个面板重绘，差异铺满面板；同会话里多几条新消息
                # 只是一条横带。三重判：差异比例、包围盒高度、宽高组合。
                big = (pct > 0.05) or bool(
                    box and box[3] > 0.45 * rh) or bool(
                    box and box[2] > 0.85 * rw and box[3] > 0.20 * rh)
                r["verified"] = "weak-changed" if big else True
                if big:
                    r["step"] = "weak-changed"
                    log(f"[跳会话] 弱核对：回车后聊天面板大变（{pct:.4f}，"
                        f"差异区 {box}，面板 {int(rw)}x{int(rh)}），"
                        f"打开的恐怕不是跳转前的会话，按核对不上处理")
                else:
                    log(f"[跳会话] 弱核对：回车后聊天面板没大变（{pct:.4f}，"
                        f"差异区 {box}，面板 {int(rw)}x{int(rh)}），"
                        f"按目标会话已打开处理")
    return r


def wx_read_unread(username):
    """读一次某会话的未读数；库打不开/会话不存在返回 None。"""
    db = _fresh_wxdb()
    if db is None or not username:
        return None
    try:
        return db.get_unread(username)
    except Exception:
        return None


def wx_scale(hwnd):
    """窗口所在显示器的缩放比（1.0 / 1.25 / 1.5…）。拿不到就按 1.0 算。"""
    try:
        u = ctypes.windll.user32
        dpi = u.GetDpiForWindow(int(hwnd))
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def wx_open_search_panel(crect, pids):
    """点搜索框并等下拉面板出现，最多点三次；每次重点前先确认前台还在微信。

    为什么要重点：真机实测面板出现时间不稳定（偶尔 1.5 秒才弹），更要命的是
    "窗口不是前台时那一下点击只是把窗口激活、并不会交给控件"——失败的那几次
    抓到现场：点击那一刻 `GetForegroundWindow()` 直接返回 0。所以重试前先重新抢
    一次前台，抢不到就如实报 lost-front，不再白点。
    """
    px, py = crect[0] + WX_SEARCH_DX, crect[1] + WX_SEARCH_DY
    lost = False
    clicks = 0
    for i in range(WX_PANEL_TRIES):
        if i:
            lost = not wx_activate_verified()[0]
            if lost:
                break
        wx_click(px, py)
        clicks += 1
        panel, _rect = wx_wait_search_panel(pids)
        if panel:
            return panel, clicks, False
    return None, clicks, lost


def wx_fill_input(text):
    """把一句话打进微信**当前会话**的输入框，绝不按回车。

    谁来调用都有同一个前提：必须先确认现在打开的就是目标会话（见 wx_open_chat 里
    的未读核对）。这个函数本身只知道"字有没有落进输入框"——它拿微信自己的画面
    做前后对比，变化包围盒必须落在右下角的输入区；落点不对就逐字退格撤销，
    等于没打过。这样最坏情况是"字被自己擦掉"，不会出现"半句话留在客户输入框里"。

    和跳会话共用同一把驱动锁、同一套前台确认：打字中途前台换了，剩下的字一个不发。
    「点一条话术」那条路走的不是这里，而是 `wx_jump_fill`——它把跳和填合成一条链子，
    只抢一次前台、只算一次配额。这里是"往当前会话填一句"这一个动作本身。"""
    r = {"step": "start", "ok": False, "chars": len(text or "")}
    if not text:
        r["step"] = "no-text"
        return r
    if not wx_off_gate(r, "填入入口"):
        return r
    if not wx_quiet_gate(r, "入口"):
        return r
    if not wx_drive_try():
        r["step"] = "busy"
        wx_drive_note("填入", "busy")
        log("[填入] 上一次的微信操作还没结束，这次不打字")
        return r
    try:
        if not wx_rate_gate(r, "填入"):
            return r
        r["_driven"] = True
        return _wx_fill_input(text, r)
    except WxFocusLost as e:
        r["ok"] = False
        r["step"] = "wx-gone" if not wx_guard_alive() else "lost-front"
        r["why"] = str(e)
        log(f"[填入] {r['why']}（此后没有再发任何一个键）")
        return r
    finally:
        if r.pop("_driven", None):
            step = r.get("step") or "?"
            wx_drive_note("填入", "ok" if step in _WX_OK_STEPS else step)
        wx_guard_disarm()
        wx_drive_release()


def _wx_fill_input(text, r):
    ok, why = wx_activate_verified()
    if not ok:
        r["step"], r["why"] = "no-front", why
        return r
    main, child, crect = wx_ui_child()
    if not crect:
        r["step"] = "no-child"
        return r
    pids = wx_pids()
    wx_guard_arm(main, pids)
    return _wx_fill_steps(text, r, main, crect, pids)


def _wx_fill_steps(text, r, main, crect, pids):
    """往当前会话输入框打字那一段的全部证据步骤。前提：已抢到前台并布防。
    和 `_wx_open_steps` 一样单独拆出来，是为了能被同一条链子接着用完。"""
    if not wx_quiet_gate(r, "点输入框前"):
        return r
    # 回车之后搜索面板按理自己就关了；还开着的话这一下坐标绝不能点——点上去等于
    # 在面板里再选一行，选中的是谁完全看排在前面的是谁。
    if not wx_dismiss_panels(pids):
        r["step"] = "panel-stuck"
        log("[填入] 搜索面板还开着且关不掉，不点输入框、不打字（话术仍在剪贴板里）")
        return r
    # 先把焦点要过来：搜索框回车之后焦点还在搜索框，直接打字会打进搜索框
    s = wx_scale(main)
    ix = crect[0] + int(WX_INPUT_FX * (crect[2] - crect[0]))
    iy = crect[3] - int(WX_INPUT_DY * s)
    try:
        wx_click(ix, iy)
    except WxFocusLost:
        raise                       # 这一下没点成也不能接着打字：交给上层按 lost-front 收手
    except Exception as e:
        r["step"], r["why"] = "click-fail", str(e)
        return r
    r["focus_click"] = (ix, iy)
    time.sleep(0.3)
    img0 = wx_wait_stable(main)
    if img0 is None:
        r["step"] = "no-print"          # 没有基线就没法判断落点，停手
        return r
    try:
        wx_type(text)
    except WxFocusLost:
        raise                       # 绝不能在这种时候补退格：那几下会删掉别人窗口里的内容
    except Exception as e:
        for _ in range(len(text) + 1):
            wx_key(VK_BACK)
        r["step"], r["why"] = "type-fail", str(e)
        return r
    time.sleep(0.5)
    after = wx_print_window(main)
    pct, box = wx_diff_box(img0, after)
    W, H = img0.size
    # 落点判定只看"输入框那一条"：整窗 diff 的包围盒会被聊天区同时进来的新消息
    # 污染——2026-10-27 真机，字明明填进了输入框，被同屏新消息的包围盒拉高判错，
    # 自己把字擦掉了。裁出底部输入条（横向越过会话列表、纵向在窗口下方）单独比：
    # 字落在这一条里才认；聊天区怎么变都不参与判分。
    in_pct = wx_region_diff(img0, after, (0.28 * W, 0.60 * H, 0.99 * W, 0.97 * H))
    in_box = in_pct > 0.0005
    r["changed"] = round(pct, 4)
    r["bbox"] = box
    r["in_pct"] = round(in_pct, 4)
    r["in_input"] = in_box
    if not in_box:
        for _ in range(len(text) + 1):
            wx_key(VK_BACK)
        r["step"] = "text-not-in-input"
        log(f"[填入] 字没落进输入条（整窗变化 {pct:.4f} 包围盒 {box}，"
            f"输入条变化 {in_pct:.4f}），已逐字退格撤销")
        return r
    r["step"] = "filled"
    r["ok"] = True
    log(f"[填入] {len(text)} 个字已进微信输入框（没有回车，发送由你自己按）")
    return r


def wx_jump_fill(name, text, session_key=None):
    """一次点击要做的两件事合成**一条**链子：跳到那个人 → 核对上了 → 把话术打进输入框。

    为什么要合：拆开跑是两轮完整链子——两次抢前台、两次等画面稳定、两道静默闸、
    两把驱动锁、两份配额（加起来 3~5 秒里他自己的键盘是归微信的），而且两轮之间
    驱动锁是放开的，别的点击能插进来。合起来只抢一次前台、只算一次配额，
    "核对通过"和"打字"之间再没有别人插一脚的窗口。

    判定一步都没减：总闸 → 静默 → 驱动锁 → 频率闸 → 抢前台+布防 → 关残留面板 →
    点搜索框 → 清空（面板出现=焦点在框里，Ctrl+A 全选+Delete）→ 打名字 → 面板真的变了 → 回车 → **未读清零核对**
    （必须核过才许打字：未读有信号用清零核对；没信号时用跳转前后主窗口画面对比
    兜底，画面大变按跳错收手）→ 面板已关 → 点输入框 → 落点包围盒 → 不对就逐字退格撤销。
    任何一步没过就带着那一步的 step 和 stage 回来，输入框里一个字都不留。
    绝不按回车发送。"""
    r = {"step": "start", "ok": False, "name": name, "chars": len(text or "")}
    if not name:
        r["step"] = "no-name"
        return r
    if not text:
        r["step"] = "no-text"
        return r
    if not WX_SEARCH_USABLE:
        r["step"] = "unsupported-search"
        return r
    if not wx_off_gate(r, "跳+填入口"):
        return r
    if not wx_quiet_gate(r, "入口"):
        return r
    if not wx_drive_try():
        r["step"] = "busy"
        wx_drive_note("跳+填", "busy")
        log("[跳+填] 上一次的微信操作还没结束，这次一律不动它")
        return r
    try:
        if not wx_rate_gate(r, "跳+填"):
            return r
        r["_driven"] = True
        ok, why = wx_activate_verified()
        if not ok:
            r["step"], r["why"] = "no-front", why
            return r
        main, child, crect = wx_ui_child()
        if not crect:
            r["step"] = "no-child"
            return r
        pids = wx_pids()
        # 只布防一次，一直到打完字才撤：整条链子共用同一份"前台还是微信"的确认
        wx_guard_arm(main, pids)
        r["stage"] = "jump"
        _wx_open_steps(name, True, session_key or None, r, main, crect, pids,
                       weak_check=True)
        if r.get("verified") is not True:
            step = r.get("step") or "?"
            if r.get("verified") in ("weak-no-pre", "weak-no-print"):
                step = r["verified"]      # 这两档 step 还停在 entered，落点不是"成功"
            wx_drive_note("跳+填", step)
            r["_noted"] = True
            log(f"[跳+填] 没能确认打开的就是「{name}」（{r.get('step')}），"
                f"没往输入框打字，话术仍在剪贴板里")
            return r
        r["stage"] = "fill"
        r["ok"] = False
        return _wx_fill_steps(text, r, main, crect, pids)
    except WxFocusLost as e:
        r["ok"] = False
        r["step"] = "wx-gone" if not wx_guard_alive() else "lost-front"
        r["why"] = str(e)
        log(f"[跳+填] {r['why']}（此后没有再发任何一个键）")
        return r
    finally:
        if r.pop("_driven", None):
            if not r.pop("_noted", None):
                step = r.get("step") or "?"
                wx_drive_note("跳+填", "ok" if step in _WX_OK_STEPS else step)
        wx_guard_disarm()
        wx_drive_release()


def wx_wait_unread_cleared(username, secs=3.0):
    """等某个会话的未读数变成 0。True=确实清零了；False=超时没清或读不到库。"""
    db = _fresh_wxdb()
    if db is None or not username:
        return False
    end = time.time() + secs
    while time.time() < end:
        try:
            u = db.get_unread(username)
        except Exception:
            u = None
        if u == 0:
            return True
        time.sleep(0.3)
    return False


def _fresh_wxdb():
    """按当前活跃账号 + 该账号自己的密钥，临时开一个只读的库句柄（自检核对用）。"""
    try:
        acct = _find_active_account(_resolve_data_root())
    except Exception:
        return None
    if not acct:
        return None
    wxid = os.path.basename(os.path.normpath(acct))
    if not _has_account_key(wxid):
        return None
    key = _load_master_key(wxid)
    if not key:
        return None
    try:
        db = WeChatDB(key, os.path.join(acct, "db_storage"))
        db.probe_wal()
        return db
    except Exception as e:
        log(f"[自检] 打不开消息库：{e}")
        return None


def wx_wait_stable(hwnd, secs=2.5, gap=0.3):
    """等窗口自绘画面稳定后再取基线。

    刚跳完会话时微信会重画整个聊天区（加载历史消息），这时候取基线会把"重画"
    误判成"我打的字"——真机第一次自检就是这么被自己的落点闸门挡下的。"""
    prev = wx_print_window(hwnd)
    end = time.time() + secs
    while prev is not None and time.time() < end:
        time.sleep(gap)
        cur = wx_print_window(hwnd)
        pct, _ = wx_diff_box(prev, cur)
        if pct < 0.001:
            return cur
        prev = cur
    return prev


def wx_drive_selftest(query="文件传输助手"):
    """界面驱动自检：只验"点得到 / 打得进 / 抓得到画面"，不回车、不发消息。

    为什么不发测试消息（这里原来写的是"给文件传输助手发一条"）：回车在微信搜索
    下拉里选中的是第一行。搜真实联系人名时第一行是「最常使用」里的本人（已实测，
    回车确实打开她的会话）；但「文件传输助手」属于"功能"，第一行是网络结果，
    回车只会进搜一搜。也就是说"用文件传输助手做发送自检"没有可靠写法——
    硬做只会在它没打开对的会话时，把测试内容打进当时正开着的那个聊天框。
    自检退到只验三件事：抢得到前台、点得中搜索框、字打得进去且抓屏看得见。

    自检同样受总闸和频率闸管：它是"手动驱动一次微信"，不是局外测试——真按下去
    就是真的在点它的窗口、往它里面打字。"""
    if not wx_drive_allowed():
        r = {"step": "start", "ok": False}
        wx_off_gate(r, "自检入口")
        r["why"] = wx_rate_why(r["step"])
        return r
    if not wx_drive_try():
        wx_drive_note("自检", "busy")
        return {"step": "busy", "ok": False, "why": "跳会话/填入正在进行，不跟它抢微信"}
    r = {"step": "start", "ok": False}
    try:
        if not wx_rate_gate(r, "自检"):
            r["why"] = wx_rate_why(r["step"], r.get("wait") or 0.0)
            return r
        r["_driven"] = True
        return _wx_drive_selftest(query, r)
    except WxFocusLost as e:
        r["step"] = "wx-gone" if not wx_guard_alive() else "lost-front"
        r["why"] = str(e)
        return r
    finally:
        if r.pop("_driven", None):
            step = r.get("step") or "?"
            wx_drive_note("自检", "ok" if step in _WX_OK_STEPS else step)
        wx_guard_disarm()
        wx_drive_release()


def _wx_drive_selftest(query, r):
    ok, why = wx_activate_verified()
    if not ok:
        r["step"], r["why"] = "no-front", why
        return r
    main, child, crect = wx_ui_child()
    if not crect:
        r["step"] = "no-child"
        return r
    r["captured"] = wx_wait_stable(main) is not None
    pids = wx_pids()
    wx_guard_arm(main, pids)
    if not wx_dismiss_panels(pids):
        r["step"] = "panel-stuck"
        log("[自检] 上一个搜索面板关不掉，停手（没点、没输入）")
        return r
    try:
        panel, clicks, lost = wx_open_search_panel(crect, pids)
    except WxFocusLost:
        raise
    except Exception as e:
        r["step"], r["why"] = "click-fail", str(e)
        return r
    r["clicks"] = clicks
    if not panel:
        wx_key(VK_ESC)
        r["step"] = "lost-front" if lost else "no-panel"
        log("[自检] 微信前台被别的窗口抢走了，已收手（没输入任何文字）" if lost
            else "[自检] 点了 %d 次搜索框都没出下拉：这台机器上合成点击进不了微信，已收手" % clicks)
        return r
    r["step"] = "panel-ok"
    img0 = wx_print_window(panel)
    wx_chord(VK_CONTROL, VK_A)
    wx_key(VK_DELETE)
    time.sleep(0.2)
    img1 = wx_print_window(panel)
    try:
        wx_type(query)
    except WxFocusLost:
        raise
    except Exception as e:
        wx_key(VK_ESC)
        r["step"], r["why"] = "type-fail", str(e)
        return r
    time.sleep(0.7)
    pct, box = wx_diff_box(img1, wx_print_window(panel))
    r["panel_changed"] = round(pct, 4)
    # 收尾：不管成没成，都把框清干净再 Esc，不把测试字样留在他的微信里
    wx_chord(VK_CONTROL, VK_A)
    wx_key(VK_DELETE)
    time.sleep(0.15)
    wx_key(VK_ESC)
    if pct < 0.002:
        r["step"] = "text-not-accepted"
        log("[自检] 下拉出现了但输入没生效：这条机器上跳会话不可用（已清框收手）")
        return r
    r["step"] = "drive-ok"
    r["ok"] = True
    log(f"[自检] 通过：点得中搜索框、字打得进（面板变化 {pct:.4f}），抓屏可用")
    return r
def foreground_is_wechat():
    """判断前台窗口是否属于微信。
    以进程名判断为准：微信进程名 Weixin.exe / WeChat.exe，企业微信为 WXWork.exe，
    不会误判。v3.5.1 起移除严格的类名硬校验——微信 4.x 的子窗口（如设置窗口）
    类名/标题可能不含"微信"，硬校验会导致用户在微信内操作时后台消息误弹；
    进程名已足以排除非微信进程。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return is_weixin_process(pid.value)


def foreground_fullscreen():
    """前台窗口是否覆盖整个监视器（全屏/最大化）。

    v3.6.2 新增：用户在微信内全屏看视频时，微信仍是前台进程但用户看不到
    聊天界面——此时若按"微信前台不打扰"跳过弹窗，消息会漏提醒。
    前台窗口铺满监视器（含 8px 容差）即视为全屏，弹窗照常进行。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        # 自定义 MONITORINFO（ctypes.wintypes 不一定提供，按 Win32 布局定义）
        class _MI(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong),
                        ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT),
                        ("dwFlags", ctypes.c_ulong)]
        mi = _MI()
        mi.cbSize = ctypes.sizeof(mi)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        sw = mi.rcMonitor.right - mi.rcMonitor.left
        sh = mi.rcMonitor.bottom - mi.rcMonitor.top
        return (r.right - r.left) >= sw - 8 and (r.bottom - r.top) >= sh - 8
    except Exception:
        return False


# ============================================================
# 微信消息检测（WAL 文件变化）
# ============================================================
_DB_RE = re.compile(r"^(message|media)_\d+\.db-wal$")
_BIZ_DB_RE = re.compile(r"^biz_message_\d+\.db-wal$")


def find_message_dbs(root):
    """扫描所有 wxid_* 账号下的 message 目录，返回可监控的 -wal 文件集合。

    v4.1.0: 兼容两种数据目录输入——
    ① xwechat_files 根目录（含多个 wxid_* 账号子目录）；
    ② 用户手动选中的单个账号目录（wxid_* 或含 db_storage），直接扫其 db_storage。
    此前只认根目录，用户在设置里选中账号目录层时始终监控 0 个文件（社区反馈）。"""
    dbs = {}
    if not os.path.isdir(root):
        return dbs
    accounts = []
    if is_account_dir(os.path.basename(root)) or os.path.isdir(os.path.join(root, "db_storage")):
        accounts.append(root)          # 输入本身就是账号目录
    else:
        for acc in os.listdir(root):
            if not is_account_dir(acc):
                continue
            accounts.append(os.path.join(root, acc))
    for acc_dir in accounts:
        msgdir = os.path.join(acc_dir, "db_storage", "message")
        if not os.path.isdir(msgdir):
            continue
        for fn in os.listdir(msgdir):
            if _DB_RE.match(fn) or (config["include_biz_messages"] and _BIZ_DB_RE.match(fn)):
                dbs[os.path.join(msgdir, fn)] = None
    return dbs


def snapshot(dbs):
    """记录每个文件当前 (mtime_ns, size)，用于比对变化。"""
    out = {}
    for p in dbs:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


def account_of(db_path):
    parts = db_path.replace("\\", "/").split("/")
    for seg in parts:
        if is_account_dir(seg):
            return seg
    return "微信"


# ============================================================
# 开机自启动
# ============================================================
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "微信提醒"


def auto_start_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{_pick_pythonw()}" "{os.path.abspath(__file__)}" --minimized'


# 脚本模式原先直接用 sys.executable 同目录的 pythonw.exe，而源码常由临时沙箱
# 解释器（豆包 sandbox_runtime 等）拉起；这类目录会被轮换清理，Run 键随之静默
# 失效。故改为优选本机稳定的 Python 安装，并校验其依赖可用（文件存在不等于
# 装齐了包，本机就同时存在缺依赖的解释器）。
_VOLATILE_MARKS = ("sandbox_runtime", "_meipass", "\\appdata\\local\\temp\\", "\\temp\\",
                   "\\sandbox", "\\cachedpackages\\")
# 注意不要再往里加 "doubao" 这种宽泛词：本机项目根就在 ...\Doubao\chats\ 下，
# 加进去会把正常的自启命令判成"已失效"，界面每次开机都显示自启未开启。
# 沙箱解释器本来就有 sandbox_runtime / \sandbox / \temp\ 这些更准的标记兜住。
# tkinter 缺失则无法运行；其余缺失只降级功能，参与择优不参与淘汰
_DEP_PROBE = ("import importlib.util as u;"
              "print(','.join(m for m in ('tkinter','PIL','pystray','Crypto')"
              " if u.find_spec(m)))")


def _is_volatile(path):
    p = os.path.normpath(path).lower()
    return any(m in p for m in _VOLATILE_MARKS)


def _pythonw_candidates():
    """按优先级返回本机稳定 pythonw.exe 候选（已去重、剔除临时目录）。"""
    out = [os.path.join(os.path.dirname(sys.executable), "pythonw.exe")]
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for root_name in (r"Software\Python\PythonCore",
                              r"Software\WOW6432Node\Python\PythonCore"):
                try:
                    key = winreg.OpenKey(hive, root_name)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(key)[1]):
                    try:
                        ver = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, f"{ver}\\InstallPath") as ip:
                            out.append(os.path.join(winreg.QueryValue(ip, None),
                                                     "pythonw.exe"))
                    except OSError:
                        continue
                winreg.CloseKey(key)
    except Exception as e:
        log(f"[自启] 枚举注册表解释器失败: {e}")
    local = os.environ.get("LOCALAPPDATA", "")
    import glob
    for pat in (os.path.join(local, "Programs", "Python", "Python3*", "pythonw.exe"),
                r"C:\Program Files\Python3*\pythonw.exe",
                r"C:\Program Files (x86)\Python3*\pythonw.exe"):
        out.extend(sorted(glob.glob(pat), reverse=True))

    seen, cands = set(), []
    for p in out:
        if not os.path.exists(p):
            continue
        k = os.path.normcase(os.path.abspath(p))
        if k in seen or _is_volatile(p):
            continue
        seen.add(k)
        cands.append(p)
    return cands


def _interp_deps(exe):
    """探测解释器可用的依赖模块集合，不可用返回 None。"""
    py = os.path.join(os.path.dirname(exe), "python.exe")
    if not os.path.exists(py):
        return None
    try:
        r = subprocess.run([py, "-c", _DEP_PROBE], capture_output=True, text=True,
                           timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return {m for m in r.stdout.strip().split(",") if m}


def _pick_pythonw():
    scored = []
    for i, p in enumerate(_pythonw_candidates()):
        deps = _interp_deps(p)
        if deps and "tkinter" in deps:
            scored.append((len(deps), -i, p))
    if scored:
        return max(scored)[2]
    # 没有稳定可用的解释器时退回旧行为，至少不让自启彻底失效
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def set_auto_start(enable):
    try:
        import winreg
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, auto_start_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        config["auto_start"] = bool(enable)
        save_config()
        log(f"[自启] 已{'开启' if enable else '关闭'}")
        return True
    except Exception as e:
        log(f"[自启] 操作失败: {e}")
        return False


def auto_start_enabled():
    """界面上的勾选状态：值存在**且现在还能真的启动**才算开启。

    只看"值存在"不够——这台机器出过自启键指向豆包沙箱临时 Python 的事，目录被
    轮换清理后开机什么都不起，界面却仍显示"已开启"。"""
    if not _run_value_exists():
        return False
    if auto_start_healthy():
        return True
    log("[自启] Run 键里的命令已失效（解释器或脚本不在了），界面按未开启显示")
    return False


def _run_value_exists():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, RUN_VALUE)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _command_healthy(cmd):
    """纯函数：判断一条自启命令现在还能不能真的起来（不碰注册表，便于测试）。"""
    if not cmd:
        return False
    import shlex
    try:
        parts = [p for p in shlex.split(str(cmd)) if p]
    except ValueError:
        return False
    if not parts:
        return False
    exe = parts[0]
    if not os.path.exists(exe) or _is_volatile(exe):
        return False
    # 脚本模式还要看脚本本身在不在（项目被挪走/删掉，自启同样会静默失败）
    script = next((p for p in parts[1:] if p.lower().endswith(".py")), None)
    if script and (not os.path.exists(script) or _is_volatile(script)):
        return False
    return True


def auto_start_healthy():
    """校验 Run 键里存的命令现在还能不能用（解释器与脚本都在、且不是临时目录）。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            cmd, _ = winreg.QueryValueEx(key, RUN_VALUE)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False
    return _command_healthy(cmd)


# ============================================================
# 单实例保护
# ============================================================
_MUTEX_HANDLE = None
_MUTEX_NAME = "微信提醒_WechatNotify_Mutex"


def _wechat_notify_running():
    """检测是否存在真实运行的微信提醒实例（进程名含"提醒"，或命令行含
    wechat_notify）。用于区分"真实例"与"幽灵互斥量"：
    2026-09-08 修复——强杀进程后命名互斥量可能残留（无进程仍返回 183），
    导致新版本永远无法启动；互斥量冲突时必须再验证进程是否存在。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            encoding="gbk", errors="replace", timeout=5)
        for line in out.splitlines():
            if "提醒" in line or "WechatNotify" in line:
                return True
    except Exception:
        pass
    # 兜底：python 源码运行场景（进程名 python.exe），排除自身及查询进程
    try:
        my_pid = os.getpid()
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process | Where-Object { "
             "$_.CommandLine -like '*wechat_notify*' "
             "-and $_.Name -notlike 'powershell*' -and $_.Name -notlike 'pwsh*' } "
             "| Select-Object -ExpandProperty ProcessId)"],
            encoding="utf-8", errors="replace", timeout=10)
        for line in out.splitlines():
            pid = line.strip()
            if pid.isdigit() and int(pid) != my_pid:
                return True
    except Exception:
        pass
    return False


def acquire_single_instance():
    """用命名互斥量防止多实例重复运行；已存在实例时返回 False。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32.CreateMutexW.restype = wt.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        # ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == 183:
            # 互斥量冲突：验证是否真有实例在运行；无则清掉残留互斥量继续启动
            if _wechat_notify_running():
                return False
            if _MUTEX_HANDLE:
                kernel32.CloseHandle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if kernel32.GetLastError() == 183:
                # 仍冲突且无进程：忽略，放行（避免误伤）
                log("[启动] 互斥量残留但无实例进程，继续启动")
            else:
                log("[启动] 已清理残留互斥量")
        return True
    except Exception:
        return True  # 失败则放行，避免误伤


# ============================================================
# 免打扰时段
# ============================================================
def in_quiet_time(start="23:00", end="07:00", now=None):
    """判断当前时间是否处于免打扰时段（支持跨零点）。"""
    try:
        now = now or time.localtime()

        def to_min(s):
            h, m = str(s).strip().split(":")
            return int(h) * 60 + int(m)

        sm, em = to_min(start), to_min(end)
        if sm == em:
            return False
        cm = now.tm_hour * 60 + now.tm_min
        if sm < em:
            return sm <= cm < em
        return cm >= sm or cm < em
    except Exception:
        return False


# ============================================================
# 横幅主题与几何
# ============================================================
def banner_x(bw, sw):
    """根据配置计算横幅水平坐标。"""
    pos = config.get("banner_pos", "center")
    # v4.6.2: 自定义位置——直接用用户输入的左上角 X。
    # 这里不按主屏宽度截断：副屏、超宽屏、竖屏拼接时主屏宽度之外也要能放。
    if pos == "custom":
        try:
            return max(0, int(config.get("banner_pos_x", 40)))
        except (TypeError, ValueError):
            return max(0, (sw - bw) // 2)
    if pos == "left":
        return 16
    if pos == "right":
        return max(0, sw - bw - 16)
    return max(0, (sw - bw) // 2)


def banner_y(bh, sh):
    """v4.1.0: 根据配置计算横幅垂直坐标（社区反馈：可设置显示位置顶部/中间/底部）。"""
    vpos = config.get("banner_vertical", "top")
    # v4.6.2: 选了「自定义」时，纵坐标同样取用户输入的左上角 Y
    if config.get("banner_pos", "center") == "custom":
        try:
            return max(0, int(config.get("banner_pos_y", 40)))
        except (TypeError, ValueError):
            return 16
    if vpos == "bottom":
        return max(0, sh - bh - 16)
    if vpos == "center":
        return max(0, (sh - bh) // 2)
    return 16


def _account_dir_ids():
    """活跃账号目录名 + 纯 wxid（目录名可能带 _xxxx 设备后缀，如 wxid_xxx_e98c）。

    每次现查 _find_active_account，换账号后自动跟着变；查不到返回空集合。"""
    ids = set()
    try:
        acct = _find_active_account()
        base = os.path.basename(os.path.normpath(acct)) if acct else ""
        if base:
            ids.add(base)
            m = re.match(r"^(.+)_[0-9a-fA-F]{4}$", base)
            if m:
                ids.add(m.group(1))
    except Exception:
        pass
    return ids


def _text_at_me(content):
    """正文里 @ 后面紧跟当前账号身份串（wxid/目录名）就判 @我。

    真库里 @ 的正文形状是 @群昵称\u2005（显示名），wxid 匹配只是兜底——
    主信号是监控线程按 atuserlist 算好的 at_me 字段。名字后要求非单词字符边界，
    免得 '@wxid_abc2' 误配给身份 'wxid_abc'。"""
    if not content:
        return False
    for nm in _account_dir_ids():
        if nm and re.search(r"@" + re.escape(nm) + r"(?![\w])", content):
            return True
    return False


def _compute_at_me(msg, wxdb):
    """群消息是否 @ 了当前账号；私聊/无 wxdb 身份时返回 None（横幅侧自行兜底）。

    优先信 source XML 的 atuserlist（被 @ 人的 wxid/微信号别名，机器可靠）；
    atuserlist 为空（source 缺失/没解析出来）才退回正文 @ 文本匹配。
    身份用 wxdb.self_names()：换账号后 wxdb 重建，集合跟着新账号走。"""
    if not msg or not msg.get("is_group"):
        return None
    try:
        names = wxdb.self_names() if wxdb else set()
        if not names:
            return None
        mentioned = msg.get("mentioned") or []
        if mentioned:
            return any(m in names for m in mentioned)
        content = msg.get("content", "") or ""
        return any(re.search(r"@" + re.escape(nm) + r"(?![\w])", content)
                   for nm in names)
    except Exception:
        return None


def theme_colors(theme):
    """横幅配色：dark 深色·微信绿 / pink 粉色 / light 浅色 / custom 自定义。"""
    if theme == "custom":
        bg = config.get("banner_custom_bg", "#202126")
        return {"bg": bg, "border": bg,
                "accent": config.get("banner_custom_accent", "#07C160"),
                "title": config.get("banner_custom_title", "#07C160"),
                "text": config.get("banner_custom_text", "#E8EAED"),
                "hint": config.get("banner_custom_hint", "#6A7078"),
                "time": config.get("banner_custom_hint", "#6A7078")}
    if theme == "pink":
        return {"bg": "#2A2226", "border": "#4A3A42", "accent": "#FF6FA5",
                "title": "#FF9CBE", "text": "#f2eaee", "hint": "#a89aa2", "time": "#b09aa4"}
    if theme == "light":
        return {"bg": "#F7F7F8", "border": "#E2E4E9", "accent": "#07C160",
                "title": "#1E7A4D", "text": "#2B2F36", "hint": "#9AA0A8", "time": "#A0A6AF"}
    if theme == "wx":
        # v4.2.0: 微信绿·浅色——原生微信聊天界面风格（白底 + 微信绿标题）
        return {"bg": "#FFFFFF", "border": "#E5E5E5", "accent": "#07C160",
                "title": "#07C160", "text": "#191919", "hint": "#8A8F99", "time": "#A0A6AF"}
    if theme == "ios":
        # v4.2.0: 手机通知·深色——iOS/Android 系统通知样式（深底 + 蓝白文字）
        return {"bg": "#1C1C1E", "border": "#2C2C2E", "accent": "#0A84FF",
                "title": "#FFFFFF", "text": "#E8E8ED", "hint": "#98989E", "time": "#98989E"}
    return {"bg": "#202126", "border": "#3A3D45", "accent": "#07C160",
            "title": "#07C160", "text": "#E8EAED", "hint": "#6A7078", "time": "#8A8F99"}


# ============================================================
# 横幅配色派生（主题只给 7 个基色，其余层次色由它们算出来）
# ============================================================
def _hex2rgb(c):
    c = (c or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (128, 128, 128)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (128, 128, 128)


def _mix(c1, c2, t):
    """两个 #rrggbb 按 t 线性混合：t=0 得 c1，t=1 得 c2。"""
    a, b = _hex2rgb(c1), _hex2rgb(c2)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _luminance(c):
    r, g, b = (v / 255.0 for v in _hex2rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(c1, c2):
    """两个颜色的 WCAG 对比度（1..21）。"""
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    def lum(c):
        r, g, b = _hex2rgb(c)
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
    a, b = sorted((lum(c1), lum(c2)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _legible(c, bg, fg_ref, floor):
    """颜色在背景上太淡时，自动往正文色压深/提亮到看得清为止。

    浅色主题的 hint（#9AA0A8 on #F7F7F8）实测只有 2.46 对比度，那行
    "点击查看微信 · 右键关闭"几乎读不出来——与其逐套主题手改，不如派生时兜住，
    用户自定义背景色也一并受益。"""
    out = c
    for _ in range(28):
        if _contrast(out, bg) >= floor:
            return out
        out = _mix(out, fg_ref, 0.1)
    return out


def banner_palette(cols):
    """由主题的基色派生出横幅全套用色。

    这样 6 套主题（含用户自定义 bg/accent）都能自动得到协调的分隔线、色片、
    描边颜色，不必每加一套主题就手补一遍颜色。"""
    bg, text, accent = cols["bg"], cols["text"], cols["accent"]
    dark = _luminance(bg) < 0.5
    pal = dict(cols)
    pal["divider"] = _mix(bg, text, 0.14)
    pal["content"] = _legible(_mix(text, bg, 0.26), bg, text, 4.5)   # 比主标题暗一档，拉开三级层次
    pal["hint"] = _legible(cols["hint"], bg, text, 2.8)
    pal["time"] = _legible(cols["time"], bg, text, 2.8)
    # 品牌绿放纯白底上实测只有 2.38（wx 主题），署名那行会发虚——同样自动压深
    pal["title"] = _legible(cols["title"], bg, text, 3.0)
    pal["chip_bg"] = _mix(bg, accent, 0.18 if dark else 0.12)
    pal["chip_fg"] = _legible(_mix(accent, text, 0.30 if dark else 0.55), bg, text, 3.0)
    pal["border"] = _mix(cols["border"], bg, 0.25)
    return pal


# ============================================================
# UI 组件（设置窗口用的粉红主题圆角控件）
# ============================================================
FONT = "Microsoft YaHei UI"
# v4.4 整体重做视觉：
#  ① 比例——窗口由 1120 宽扁矩形收到 980，两列内容不再被横向拉散；
#  ② 色带——标题栏/导航/底部合并为同一块面板色，去掉四条横向色带切割感；
#  ③ 品牌底——中性灰改极浅绿灰（浅色 #EEF2F0 / 深色 #141517），白卡片浮在上面，
#     整个窗口有统一色调而不是"白底贴灰纸"；
#  ④ 卡片——双层假阴影改单层柔和阴影 + 1px 同色系边框（灰底上双层阴影会发脏）。
UI_LIGHT = {
    "BG": "#FFFFFF", "CARD": "#FFFFFF", "BORDER": "#E3E8E5",
    "ACCENT": "#07C160", "ACCENT_D": "#06AD56", "DEEP": "#07A050",
    # 提示文字 SUB 对白 4.59:1（WCAG AA）；占位符 PH 3.03:1，明显弱于正文
    "TEXT": "#1F2329", "SUB": "#6E7681", "CHK": "#E6F7EE",
    "TAB": "#EBEEF2", "TAB_HOVER": "#DCF3E6",
    "BTN_QUIT": "#FFFFFF", "BTN_QUIT_FG": "#4B5563", "BTN_QUIT_EDGE": "#E3E8E5",
    "BTN_OFF": "#EFF1F4",
    "TITLE_BAR": "#FFFFFF", "TITLE_FG": "#1F2329", "TITLE_SUB": "#9AA0A6",
    "NAV_BG": "#FFFFFF", "NAV_FG": "#6B7280", "NAV_HOVER": "#F1F4F2",
    "NAV_ACTIVE_BG": "#E6F7EE", "NAV_ACTIVE_FG": "#07A050",
    "CONTENT_BG": "#EEF2F0", "PAGE_BG": "#EEF2F0",
    "CARD_BG": "#FFFFFF", "INPUT_BG": "#F5F7F6",
    "HEADER_LINE": "#EBEEEC", "SHADOW1": "#E7ECE9", "SHADOW2": "#DCE3DF",
    "PH": "#8A9099",
    # v4.4 访达式侧栏：侧栏比内容区再深一档灰绿，选中项用品牌绿胶囊
    "SIDEBAR": "#E4EAE7", "SIDEBAR_FG": "#4B5563", "SIDEBAR_HOVER": "#DAE2DE",
    "SIDEBAR_SEL": "#07C160", "SIDEBAR_SEL_FG": "#FFFFFF",
    "SIDEBAR_CAP": "#8A9099",
}

UI_DARK = {
    "BG": "#1D1F22", "CARD": "#232529", "BORDER": "#33363B",
    "ACCENT": "#07C160", "ACCENT_D": "#06AD56", "DEEP": "#3EC081",
    "TEXT": "#E7E9EC", "SUB": "#9AA2AC", "CHK": "#1E3A2C",
    "TAB": "#26282C", "TAB_HOVER": "#2F3B35",
    "BTN_QUIT": "#2C2F34", "BTN_QUIT_FG": "#C6CBD3", "BTN_QUIT_EDGE": "#3A3D42",
    "BTN_OFF": "#1D1F22",
    "TITLE_BAR": "#1D1F22", "TITLE_FG": "#EDEFF2", "TITLE_SUB": "#8A9099",
    "NAV_BG": "#1D1F22", "NAV_FG": "#9AA1AB", "NAV_HOVER": "#2A2D31",
    "NAV_ACTIVE_BG": "#1E3A2C", "NAV_ACTIVE_FG": "#3EC081",
    "CONTENT_BG": "#141517", "PAGE_BG": "#141517",
    "CARD_BG": "#232529", "INPUT_BG": "#1B1D20",
    "HEADER_LINE": "#2E3136", "SHADOW1": "#1A1B1E", "SHADOW2": "#131416",
    "PH": "#6E757F",
    "SIDEBAR": "#1B1C1F", "SIDEBAR_FG": "#B9C0C8", "SIDEBAR_HOVER": "#26282C",
    "SIDEBAR_SEL": "#07C160", "SIDEBAR_SEL_FG": "#08120C",
    "SIDEBAR_CAP": "#6E757F",
}

UI = UI_LIGHT  # 当前主题（启动时按 config["theme"] 切换）

# v4.4 访达式布局栅格：左侧 200px 侧栏 + 右侧单列分组卡片
# 内容列宽 = 860 - 200 - 2*20 = 620；卡片内容区 = 620 - 2*18 = 584
WIN_W = 860
WIN_H = 700          # 访达式：窗口尺寸固定，内容区滚动（单列卡片总高会超过屏幕）
SIDEBAR_W = 200
RAD_CARD = 12        # 卡片圆角
RAD_CTRL = 8         # 按钮/输入框圆角
ICON_BOX = 18        # 复选/单选图标边长（原 20，视觉过重）
LBL_W = 140          # 卡片内标签列宽
CTRL_X = LBL_W + 8   # 控件/带标签提示的起始列（_row 控件默认左间距 8）
HINT_W = 436         # 带标签行的提示换行宽度（卡片内容区 - 控件列）
HINT_FULL = 580      # 整宽说明文字换行宽度（卡片内容区内）
ROW_PADY = 6         # 卡片内行距
CARD_PADX = 18       # 卡片左右内边距


def _apply_theme(name):
    """v4.0.1: 切换设置窗口主题（浅色/深色）。全局替换 UI 字典，
    设置窗口重建后所有控件按新主题重绘。"""
    global UI
    UI = UI_DARK if str(name).lower() == "dark" else UI_LIGHT


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（平滑多边形）。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundedButton(tk.Canvas):
    """圆角按钮（Canvas 绘制，支持悬停变色与可选描边）。

    fill/fg：常态底色与文字色；hover：悬停底色；edge：描边色（次级按钮用，
    传 None 则无描边）。"""

    def __init__(self, master, text, command, fill, fg, hover=None,
                 radius=10, height=34, bold=False, edge=None, padx=22):
        from tkinter import font as tkfont
        self._fnt = tkfont.Font(family=FONT, size=10,
                                weight="bold" if bold else "normal")
        w = self._fnt.measure(text) + padx * 2
        self._bw, self._bh, self._radius = w, height, radius
        super().__init__(master, width=w, height=height, bg=master.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._command = command
        self._fill, self._fg, self._hover, self._edge = fill, fg, hover, edge
        self._base = (fill, fg, hover, edge)
        self._text = text
        self._bold = bold
        self._disabled = False
        self._draw(fill)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._draw(self._hover or fill))
        self.bind("<Leave>", lambda e: self._draw(fill))

    def _click(self, e):
        if not self._disabled:
            self._command()

    def set_enabled(self, on):
        """禁用态：灰底灰字、无描边、不响应点击（提取进行中/结束时切换主按钮）。"""
        P = UI
        self._disabled = not on
        self.configure(cursor="hand2" if on else "arrow")
        if on:
            self._fill, self._fg, self._hover, self._edge = self._base
        else:
            self._fill, self._fg, self._hover, self._edge = P["BTN_OFF"], P["PH"], None, None
        self._draw(self._fill)

    def _draw(self, fill):
        self.delete("all")
        if self._edge:
            round_rect(self, 1, 1, self._bw - 1, self._bh - 1, self._radius,
                       fill=self._edge, outline="")
            round_rect(self, 2, 2, self._bw - 2, self._bh - 2, max(1, self._radius - 1),
                       fill=fill, outline="")
        else:
            round_rect(self, 1, 1, self._bw - 1, self._bh - 1, self._radius,
                       fill=fill, outline="")
        self.create_text(self._bw / 2, self._bh / 2 + 1, text=self._text, fill=self._fg,
                         font=(FONT, 10, "bold" if self._bold else "normal"))


class WinButton(tk.Canvas):
    """无边框窗口右上角的最小化 / 关闭按钮：线性图标 + 悬停圆角底。

    overrideredirect 窗口没有系统标题栏，这两个按钮只能自己画。"""

    _drag_block = True   # 按下时不触发窗口拖动（见 SettingsWindow._start_move）

    def __init__(self, master, kind, command, bg, fg, hover, size=28):
        super().__init__(master, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._kind, self._cmd = kind, command
        self._fg, self._hover, self._s = fg, hover, size
        self.bind("<Enter>", lambda e: self._draw(True))
        self.bind("<Leave>", lambda e: self._draw(False))
        self.bind("<Button-1>", lambda e: self._cmd())
        self._draw(False)

    def _draw(self, hot):
        self.delete("all")
        m = self._s / 2
        if hot:
            round_rect(self, 0, 0, self._s - 1, self._s - 1, 7,
                       fill=self._hover, outline="")
        if self._kind == "min":
            self.create_line(m - 5, m + 4, m + 5, m + 4, fill=self._fg, width=2)
        else:
            self.create_line(m - 5, m - 5, m + 5, m + 5, fill=self._fg, width=2)
            self.create_line(m + 5, m - 5, m - 5, m + 5, fill=self._fg, width=2)


class RoundedEntry(tk.Canvas):
    """粉红主题圆角输入框：Canvas 圆角边框 + 内嵌无边框 Entry。

    on_commit 在失焦或回车时回调，用于保存输入。
    """

    def __init__(self, master, textvariable=None, width=6, height=30, radius=9,
                 justify="center", on_commit=None, show=None, placeholder=""):
        super().__init__(master, height=height, bg=UI["CARD"], highlightthickness=0, bd=0)
        self._var = textvariable if textvariable is not None else tk.StringVar()
        self._radius = radius
        self._on_commit = on_commit
        self._focused = False
        self._ph = placeholder
        self._ph_lbl = None
        self._entry = tk.Entry(self, textvariable=self._var, bd=0, relief="flat",
                               bg=UI["INPUT_BG"], fg=UI["TEXT"], font=(FONT, 10),
                               justify=justify, highlightthickness=0,
                               insertbackground=UI["ACCENT"], show=show,
                               disabledbackground=UI["INPUT_BG"],
                               disabledforeground=UI["PH"])
        self._win = self.create_window(0, 0, window=self._entry, anchor="nw")
        self.configure(width=int(width) * 10 + radius * 2 + 8)
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", lambda e: self._focus_entry())
        self._entry.bind("<FocusIn>", lambda e: (self._set_focus(True), self._redraw()))
        self._entry.bind("<FocusOut>", lambda e: (self._set_focus(False), self._redraw(), self._commit()))
        self._entry.bind("<Return>", lambda e: (self._commit(), self.master.focus_set()))
        # 占位符只在"空且未聚焦"时画在 Canvas 上（不写进 Entry，避免污染真实值）
        try:
            self._var.trace_add("write", lambda *a: self._redraw())
        except AttributeError:
            self._var.trace("w", lambda *a: self._redraw())
        self._redraw()

    def _set_focus(self, on):
        self._focused = on

    def _focus_entry(self):
        self._entry.focus_set()
        self._entry.icursor(len(self._var.get()))

    def _commit(self):
        if self._on_commit:
            self._on_commit()

    def get(self):
        return self._var.get()

    def set(self, value):
        self._var.set(value)

    def configure_state(self, state):
        self._entry.configure(state=state)

    def _redraw(self, e=None):
        W = self.winfo_width()
        H = self.winfo_height()
        if W <= 2 or H <= 2:
            return
        self.delete("bg")
        edge = UI["ACCENT"] if self._focused else UI["BORDER"]
        # 外层描边（聚焦绿色）+ 内层浅灰底
        round_rect(self, 1, 1, W - 1, H - 1, self._radius, fill=edge,
                   outline="", tags="bg")
        round_rect(self, 2, 2, W - 2, H - 2, max(1, self._radius - 1),
                   fill=UI["INPUT_BG"], outline="", tags="bg")
        # 内嵌控件四周都内缩，避免盖住上下边框
        pad = self._radius + 1
        self.coords(self._win, pad, 4)
        self.itemconfig(self._win, width=max(1, W - pad * 2),
                        height=max(1, H - 8))
        if self._ph:
            # 占位符必须是**子窗口**：Canvas 上后画的图形项仍会被先画的
            # Entry 窗口盖住（Tk 里窗口项永远浮在图形项之上），用 Label 才可见
            show = (not self._focused) and (not self._var.get())
            if show and self._ph_lbl is None:
                self._ph_lbl = tk.Label(self, text=self._ph, bg=UI["INPUT_BG"],
                                        fg=UI["PH"], font=(FONT, 10))
                self._ph_lbl.bind("<Button-1>", lambda e: self._focus_entry())
            if self._ph_lbl is not None:
                if show:
                    if str(self._entry.cget("justify")) == "center":
                        self._ph_lbl.place(x=W / 2, y=H / 2, anchor="center")
                    else:
                        self._ph_lbl.place(x=pad + 4, y=H / 2, anchor="w")
                else:
                    self._ph_lbl.place_forget()


class RoundedSpinbox(tk.Canvas):
    """粉红主题圆角数字输入：Canvas 圆角边框 + 内嵌无边框 Spinbox（保留上下箭头）。

    on_change 在点击箭头、输入或失焦时回调，用于保存。
    """

    def __init__(self, master, from_, to, textvariable=None, width=5, height=30,
                 radius=9, on_change=None):
        super().__init__(master, height=height, bg=UI["CARD"], highlightthickness=0, bd=0)
        self._var = textvariable if textvariable is not None else tk.StringVar()
        self._radius = radius
        self._on_change = on_change
        self._focused = False
        kw = dict(from_=from_, to=to, textvariable=self._var, bd=0, relief="flat",
                  highlightthickness=0, bg=UI["INPUT_BG"], fg=UI["TEXT"],
                  font=(FONT, 10), justify="center", wrap=False,
                  disabledbackground=UI["INPUT_BG"], disabledforeground=UI["PH"],
                  command=self._change)
        # v4.4: 箭头按钮底色与输入框一致（原来用 BORDER，浅灰框上像两个深块）；
        # arrowcolor 并非所有 Tk 构建都支持，故只调 buttonbackground
        try:
            kw["buttonbackground"] = UI["INPUT_BG"]
        except Exception:
            pass
        self._spin = tk.Spinbox(self, **kw)
        self._win = self.create_window(0, 0, window=self._spin, anchor="nw")
        self.configure(width=int(width) * 10 + 26 + radius * 2)
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", lambda e: self._focus_spin())
        self._spin.bind("<FocusIn>", lambda e: (setattr(self, "_focused", True), self._redraw()))
        self._spin.bind("<FocusOut>", lambda e: (setattr(self, "_focused", False), self._redraw(), self._change()))
        self._spin.bind("<KeyRelease>", lambda e: self._change())
        self._redraw()

    def _focus_spin(self):
        self._spin.focus_set()

    def _change(self):
        if self._on_change:
            self._on_change()

    def get(self):
        try:
            return int(self._var.get())
        except Exception:
            return 0

    def set(self, value):
        self._var.set(value)

    def configure_state(self, state):
        self._spin.configure(state=state)

    def _redraw(self, e=None):
        W = self.winfo_width()
        H = self.winfo_height()
        if W <= 2 or H <= 2:
            return
        self.delete("bg")
        edge = UI["ACCENT"] if self._focused else UI["BORDER"]
        round_rect(self, 1, 1, W - 1, H - 1, self._radius, fill=edge,
                   outline="", tags="bg")
        round_rect(self, 2, 2, W - 2, H - 2, max(1, self._radius - 1),
                   fill=UI["INPUT_BG"], outline="", tags="bg")
        # 内嵌控件四周都内缩，避免盖住上下边框
        pad = self._radius + 1
        self.coords(self._win, pad, 4)
        self.itemconfig(self._win, width=max(1, W - pad * 2),
                        height=max(1, H - 8))


class RoundedCheckbox(tk.Canvas):
    """圆角复选框：18px 图标 + 按字体度量取宽。

    variable 为 tk.BooleanVar，点击切换并调用 command。
    v4.4: 旧实现用 len(text)*15 估宽（中英文混排不是 15px/字，长文案会被裁掉、
    点击区也忽大忽小），改用 tkfont.measure；未选中内芯原来硬编码 #FFFFFF，
    深色主题下会在深灰卡片上留一块白斑，改为跟随 CARD。"""

    def __init__(self, master, text, variable, command=None, height=28):
        from tkinter import font as tkfont
        self._var = variable
        self._command = command
        self._fnt = tkfont.Font(family=FONT, size=10)
        b = ICON_BOX
        self._tx = b + 8
        w = self._tx + int(self._fnt.measure(text)) + 6
        super().__init__(master, width=w, height=height, bg=UI["CARD"],
                         highlightthickness=0, bd=0, cursor="hand2")
        # 图标：1px 边框（外框描边色 + 内芯卡片色）；勾选时整体变绿 + 白色对勾
        round_rect(self, 2, 5, 2 + b, 5 + b, 5, fill=UI["BORDER"], outline="", tags="ic")
        round_rect(self, 3, 6, 1 + b, 4 + b, 4, fill=UI["CARD"], outline="", tags="icf")
        self._mark = self.create_text(2 + b / 2, 5 + b / 2, text="", fill="#FFFFFF",
                                      font=(FONT, 10, "bold"))
        self.create_text(self._tx, height / 2, text=text, fill=UI["TEXT"], anchor="w",
                         font=(FONT, 10))
        self.bind("<Button-1>", self._toggle)
        self._sync()

    def _toggle(self, e):
        self._var.set(not self._var.get())
        self._sync()
        if self._command:
            self._command()

    def _sync(self):
        if self._var.get():
            self.itemconfig("ic", fill=UI["ACCENT"])
            self.itemconfig("icf", fill=UI["ACCENT"])
            self.itemconfig(self._mark, text="✓")
        else:
            self.itemconfig("ic", fill=UI["BORDER"])
            self.itemconfig("icf", fill=UI["CARD"])
            self.itemconfig(self._mark, text="")


# 所有 RoundedRadio 实例注册表（v4.0：单选组刷新需跨父节点）
_RADIO_GROUP = []


class RoundedRadio(tk.Canvas):
    """粉红主题圆角单选按钮：Canvas 绘制圆形图标，选中时内芯粉色+白点。

    variable 为 tk.StringVar，点击选中 value 并调用 command。
    """

    def __init__(self, master, text, value, variable, command=None, height=28):
        from tkinter import font as tkfont
        self._value = value
        self._var = variable
        self._command = command
        # 全局注册：radio 可能分布在不同的父 Frame（如提示音两行排布），
        # 单选刷新必须跨父节点，仅扫 master 的兄弟会漏掉其它行导致"多选"假象
        try:
            _RADIO_GROUP.append(self)
        except Exception:
            pass
        self._fnt = tkfont.Font(family=FONT, size=10)
        b = ICON_BOX
        self._tx = b + 8
        w = self._tx + int(self._fnt.measure(text)) + 6
        super().__init__(master, width=w, height=height, bg=UI["CARD"],
                         highlightthickness=0, bd=0, cursor="hand2")
        # 圆形图标：1px 灰环 + 卡片色内芯；选中时绿环 + 中心绿点
        self.create_oval(2, 5, 2 + b, 5 + b, fill=UI["BORDER"], outline="", tags="ic")
        self.create_oval(3, 6, 1 + b, 4 + b, fill=UI["CARD"], outline="", tags="icf")
        # 选中中心绿点（居中，直径 7）
        self._dot = self.create_oval(2 + b / 2 - 3.5, 5 + b / 2 - 3.5,
                                     2 + b / 2 + 3.5, 5 + b / 2 + 3.5,
                                     fill=UI["ACCENT"], outline="", tags="dot",
                                     state="hidden")
        self.create_text(self._tx, height / 2, text=text, fill=UI["TEXT"], anchor="w",
                         font=(FONT, 10))
        self.bind("<Button-1>", self._select)
        self._sync()

    def _select(self, e):
        if self._var.get() != self._value:
            self._var.set(self._value)
            self._refresh_group()
            if self._command:
                self._command()

    def _refresh_group(self):
        """刷新绑定同一 variable 的所有 RoundedRadio，保证单选视觉同步
        （只刷新自己会出现同组多个圆点同时呈选中态的"多选"假象）。
        v4.0：改为跨父节点全局刷新（提示音 radio 分两行排布，仅扫 master
        兄弟会漏掉另一行的实例）。"""
        try:
            for w in list(_RADIO_GROUP):
                if w._var is self._var:
                    w._sync()
        except Exception:
            pass

    def _sync(self):
        if self._var.get() == self._value:
            self.itemconfig("ic", fill=UI["ACCENT"])
            self.itemconfig("icf", fill=UI["CARD"])
            self.itemconfig("dot", state="normal")
        else:
            self.itemconfig("ic", fill=UI["BORDER"])
            self.itemconfig("icf", fill=UI["CARD"])
            self.itemconfig("dot", state="hidden")


# ============================================================
# 横幅弹窗
# ============================================================
class Banner:
    # 正文只显示一行（你定的）：横幅是"提示有消息"，不是聊天窗口，长内容交给点开
    # 微信去看；超出部分按像素截断加省略号。
    CONTENT_LINES = 1
    # 首行右侧固定预留：时间(~62)+组间距；群消息再多留一个"群"色片(~28)。
    # 写成常量是为了让"截断预算"和测试用的是同一个数（之前测试硬编码，改一次
    # 就得跟着猜一遍）。
    HEAD_RESERVE = 74
    HEAD_RESERVE_GROUP = 102

    def __init__(self, root, q):
        self.root = root
        self.q = q
        self.win = None
        self.hide_after = None
        self._anim_after = None  # 动画（淡入/淡出）定时器 ID，重建/隐藏时取消
        self._cnt = 0          # 当前合并窗口内累计消息条数
        # 上次已受理消息的身份（吸收同一条消息的二次写库）。以前用"1 秒时间闸"，
        # 两条**不同**消息间隔不到 1 秒时后一条会被整个丢掉，见 _msg_ident。
        self._last_ident = None
        self._pending = []     # v4.1.0: 合并窗口内排队待补弹的消息（横幅消失后依次显示）
        self._current_msg = None  # v4.1.0-审查: 当前横幅显示的消息（被新消息覆盖前入队防丢失）
        self._build()
        root.after(120, self._poll)

    def rebuild(self):
        """按最新配置重建横幅（位置/主题/标题）。"""
        # 取消旧的隐藏/消失/动画定时器，避免重建后旧回调误操作已销毁窗口
        for after_id in (self.hide_after, self._anim_after):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
        self.hide_after = None
        self._anim_after = None
        try:
            if self.win:
                self.win.destroy()
        except Exception:
            pass
        self.win = None
        self._build()

    def _apply_round(self):
        s = getattr(self, "_s", 1.0)
        radius = max(0, min(60, int(int(config.get("banner_radius", 18)) * s)))
        # 必须用窗口**当前**高度：长消息时横幅会自动长高（_resize_banner），
        # 若仍按 config 里的固定高度做圆角遮罩，多出来的下半截会被遮掉——
        # 表现就是长消息的提示行/最后一行内容看不见（实测 204 高的横幅被裁到 152）
        h = int(int(config["banner_height"]) * s)
        try:
            cur = self.win.winfo_height()
            if cur and cur > h:
                h = cur
        except Exception:
            pass
        self._h = h
        round_toplevel(self.win, radius,
                       getattr(self, "_w", int(int(config["banner_width"]) * s)), h)

    def _set_avatar(self, blob):
        """把微信本地缓存里的真实头像画进头像位（圆角靠 alpha 遮罩）。

        拿不到图就整块留空、连头像位一起收掉——首字母占位按你的意见不要了，
        返回 False 让调用方把这一列的宽度还给正文。"""
        cv = getattr(self, "avatar", None)
        if cv is None:
            return False
        cv.delete("all")
        self._avatar_img = None
        if not blob:
            return False
        try:
            from PIL import Image, ImageDraw, ImageTk
            import io as _io
            d = max(8, int(getattr(self, "_av", 38)))
            im = Image.open(_io.BytesIO(blob)).convert("RGB").resize((d, d), Image.LANCZOS)
            mask = Image.new("L", (d, d), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, d - 1, d - 1), radius=max(3, int(d * 0.18)), fill=255)
            im.putalpha(mask)
            self._avatar_img = ImageTk.PhotoImage(im)
            cv.create_image(0, 0, image=self._avatar_img, anchor="nw")
            return True
        except Exception as e:
            log(f"[banner] 头像渲染失败: {e}")
            return False

    def _content_w_for(self, show_avatar):
        """正文可用宽度 = 窗口宽 - 左右内边距 -（头像 + 头像与正文的间距）。

        必须和 content_lbl 的 wraplength 同源，且**每次换消息重算**：头像有无
        会让可用宽度差出几十像素，用旧值会出现提前省略号或末行被挤掉。"""
        chrome = self._pad_x * 2 + (self._av + self._av_gap if show_avatar else 0)
        return max(200, self._w - chrome)

    def _build(self):
        sw = self.root.winfo_screenwidth()
        # 分辨率/缩放适配：字号用"点"，Tk 会随它读到的 DPI 自动放大；像素几何不会，
        # 所以这里按同一个比值放大宽度、内边距、头像与截断预算，两边才不会脱节。
        # 进程不感知 DPI 时 Tk 读到的仍是 96（改由 Windows 做位图拉伸），s=1.0，
        # 不会二次放大。
        try:
            s = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            s = 1.0
        self._s = s
        # v4.1.0: 横幅字号可调（社区反馈：弹窗大小和字体大小可调）
        fs = int(config.get("banner_font_size", 11))
        w = min(int(int(config["banner_width"]) * s), max(260, sw - int(24 * s)))
        h = int(int(config["banner_height"]) * s)
        x = banner_x(w, sw)
        # v4.6.2: 初始几何也跟随自定义纵坐标（此前这里写死 16）
        y = banner_y(h, self.root.winfo_screenheight())
        colors = banner_palette(theme_colors(config.get("banner_theme", "dark")))
        self.P = colors
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        # v4.0.1-6: 工具窗口样式——任务栏不出现横幅按钮（用户反馈的"羽毛"图标）
        try:
            self.win.attributes("-toolwindow", True)
        except Exception:
            pass
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", float(config.get("banner_opacity", 1.0)))
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self._w = w
        self._pad_x = int(16 * s)
        self._av_gap = int(13 * s)
        self._av = int(max(32, min(52, fs * 3 + 5)) * s)
        self._head_reserve = int(self.HEAD_RESERVE * s)
        self._head_reserve_group = int(self.HEAD_RESERVE_GROUP * s)

        outer = tk.Frame(self.win, bg=colors["bg"], highlightbackground=colors["border"],
                         highlightthickness=1)
        outer.pack(fill="both", expand=True)
        # 左侧绿色竖条去掉了：头像已经是强调色，再加一条等于两个绿块互相抢，
        # 而且通到上下边缘时会被圆角遮罩切出缺口（用户反馈"看着奇怪"的主因之一）
        wrap = tk.Frame(outer, bg=colors["bg"])
        # 上下内边距差一档：提示行本身就占着一行高，两边等宽会看着"下面空一片"
        wrap.pack(side="left", fill="both", expand=True, padx=self._pad_x,
                  pady=(int(13 * s), int(7 * s)))
        wrap.columnconfigure(1, weight=1)

        # 头像位：画微信本地缓存里的真实头像，取不到就整列收掉（不再画首字母占位）
        self.avatar = tk.Canvas(wrap, width=self._av, height=self._av, bg=colors["bg"],
                                highlightthickness=0, bd=0)
        self.avatar.grid(row=0, column=0, rowspan=3, sticky="n", pady=(int(1 * s), 0))
        self.avatar.grid_remove()

        body = tk.Frame(wrap, bg=colors["bg"])
        body.grid(row=0, column=1, sticky="nsew", padx=(self._av_gap, 0))
        body.columnconfigure(0, weight=1)
        self._content_w = self._content_w_for(False)

        head = tk.Frame(body, bg=colors["bg"])
        head.grid(row=0, column=0, sticky="ew")
        # 发送人不再比正文大一号：大一号加粗在 560px 宽的横幅上会压过正文，
        # 层次改由颜色（text 比 content 更亮）+ 加粗来给
        self.sender_lbl = tk.Label(head, text="微信收到新消息", bg=colors["bg"],
                                   fg=colors["text"], font=(FONT, fs, "bold"),
                                   anchor="w")
        self.sender_lbl.pack(side="left")
        self.group_chip = tk.Label(head, text="群", bg=colors["chip_bg"],
                                   fg=colors["chip_fg"], font=(FONT, max(8, fs - 3)),
                                   padx=6, pady=1)
        # ✕ 按钮按你的意见去掉：横幅本来就会自动消失，右键也能关，右上角那个叉
        # 只是把整张卡片搞得像对话框
        self.time_lbl = tk.Label(head, text="", bg=colors["bg"], fg=colors["time"],
                                 font=(FONT, max(9, fs - 1)))
        self.time_lbl.pack(side="right")

        self.content_lbl = tk.Label(body, text="", bg=colors["bg"], fg=colors["content"],
                                    font=(FONT, fs), justify="left", anchor="w",
                                    wraplength=self._content_w)
        self.content_lbl.grid(row=1, column=0, sticky="w", pady=(int(4 * s), 0))
        self.content_lbl.grid_remove()

        # 操作提示改成"鼠标悬停时才出现"。这一行**常驻占位、平时文字为空**——
        # 若靠显隐整行，鼠标一进一出卡片会跳高跳低；留空则高度恒定，
        # 视觉上只是底部那点内边距。
        self._hint_text = config.get("banner_hint", "")
        self.hint_lbl = tk.Label(body, text="", bg=colors["bg"],
                                 fg=colors["hint"], font=(FONT, max(8, fs - 3)),
                                 justify="left", anchor="w", wraplength=self._content_w,
                                 pady=0)
        self.hint_lbl.grid(row=2, column=0, sticky="w", pady=(0, 0))

        # v4.5.0: 私聊快捷回复——横幅底部一排话术按钮。点一下只把这句话**复制进剪贴板**，
        # 不碰微信窗口、不替你发送（发送是你自己 Ctrl+V）。
        # 按会话显隐（群聊不显示），不参与悬停逻辑；卡片高度走 show() 里那套自适应重算。
        import tkinter.font as tkfont
        self._qr_btns = []
        self._qr_shown = []
        self._qr_fb = ""
        self._qr_fb_after = None
        self._qr_font = tkfont.Font(family=FONT, size=max(9, fs - 1), weight="bold")
        self.qr_frame = tk.Frame(body, bg=colors["bg"])
        self.qr_frame.grid(row=3, column=0, sticky="w", pady=(int(7 * s), 0))
        self.qr_frame.grid_remove()
        self._qr_gap = int(8 * s)
        self._qr_pad = int(20 * s)      # 按钮左右内边距之和（padx 10*s ×2）

        # v4.1.0: 点击打开微信 / 拖动移动窗口——Tkinter 子控件不向父窗口冒泡事件，
        # 若只绑定 win，点击内容/昵称/提示文字等区域时左键/右键全部失效
        # （社区反馈：右键点击横幅无法关闭）。这里递归绑定全部子控件。
        # 话术按钮要跳过：否则点它会走到 _release → 打开微信，按钮就白做了。
        self._bind_banner_events(skip={self.qr_frame})
        self._press_pos = None
        self._moved = False
        self._drag_data = None
        self.win.after(80, self._apply_round)
        log("[banner] 横幅初始化完成")

    def _pointer_on_banner(self):
        """鼠标现在是否停在横幅（含其子控件）上。"""
        try:
            w = self.win.winfo_containing(self.win.winfo_pointerx(),
                                          self.win.winfo_pointery())
            while w is not None:
                if w is self.win:
                    return True
                w = getattr(w, "master", None)
        except Exception:
            pass
        return False

    def _sync_hint(self, *_a):
        """悬停时才显示操作提示。

        不用"进出窗口计数"而用 winfo_containing 实测指针位置：计数在快速划过、
        或子控件之间来回移动时会漂（漏掉一次 Leave 就永远显示/永远不显示）。
        另外 _poll 里也会兜一次——Enter/Leave 在 overrideredirect 窗口上偶发不
        送达（本机实测到过），只靠事件会出现提示卡住不消失。"""
        try:
            if not self.win or not self.win.winfo_exists():
                return
            # 刚点过话术按钮时先显示"已复制"回执，鼠标移出也不立刻抹掉——
            # 否则复制成没成功根本看不出来
            want = self._qr_fb or (self._hint_text if self._pointer_on_banner() else "")
            if self.hint_lbl.cget("text") != want:     # 文案没变就别 config，省得重绘
                self.hint_lbl.config(text=want)
        except Exception:
            pass

    def _quick_replies(self):
        """当前该显示的话术按钮文案（私聊 + 开关打开 + 有话术才有）。"""
        if not bool(config.get("quick_reply_on", True)):
            return []
        return [t for t in (config.get("quick_replies") or []) if t][:QR_MAX]

    def _hide_quick_replies(self):
        for b in self._qr_btns:
            try:
                b.destroy()
            except Exception:
                pass
        self._qr_btns = []
        self._qr_shown = []
        try:
            self.qr_frame.grid_remove()
        except Exception:
            pass

    def _apply_quick_replies(self, is_group):
        """按这条消息该不该给按钮，重排话术行（按钮按当次文案现建，话术改了立刻生效）。"""
        texts = [] if is_group else self._quick_replies()
        self._hide_quick_replies()
        if not texts:
            return
        # 三个按钮挤在一行里，超宽就各自按像素截断，宁可少显示一个也不让按钮顶出
        # 卡片右边界（横幅宽度本来就夹过屏幕，撑破了反而看不全）
        budget = max(80, self._content_w)
        per = int((budget - self._qr_pad * len(texts)
                   - self._qr_gap * (len(texts) - 1)) / max(1, len(texts)))
        for t in texts:
            self._add_qr_button(t, per)
        if not self._qr_btns:
            self.qr_frame.grid_remove()
            return
        self.qr_frame.grid()

    def _add_qr_button(self, text, max_px):
        b = tk.Label(self.qr_frame, text="", bg=self.P["chip_bg"], fg=self.P["chip_fg"],
                     font=self._qr_font, padx=int(self._s * 10), pady=int(self._s * 3),
                     cursor="hand2")
        full = text
        try:
            # 逐字回退后补**一个**省略号。不能写成 `text[:-1] + "…"`：那样字符串长度
            # 不变，用"长度 > 1"当循环条件时截到全是省略号仍然超宽就永远出不去，
            # 界面会直接钉死在这里（2026-10-22 截图实测挂住过一次）。
            if self._qr_font.measure(text) > max_px:
                keep = text
                while keep and self._qr_font.measure(keep + "…") > max_px:
                    keep = keep[:-1]
                text = (keep + "…") if keep else "…"
                # 连一个省略号都放不下：这条干脆不画，别顶破卡片右边界
                if self._qr_font.measure(text) > max_px:
                    b.destroy()
                    return None
        except Exception:
            pass
        b.config(text=text)
        b.pack(side="left", padx=(0, self._qr_gap))
        self._qr_btns.append(b)
        self._qr_shown.append(full)      # 存原文：按钮上显示的是截断后的，剪贴板要给完整的
        i = len(self._qr_shown) - 1      # 按"实际画出来的第几个"绑定，某条被丢掉也不会错位
        b.bind("<Button-1>", lambda e, idx=i: self._qr_button_click(e, idx))
        b.bind("<Button-3>", lambda e: self._qr_button_rightclick(e))
        return b

    def _qr_button_click(self, e, idx):
        """话术按钮左键：阻止事件冒泡到横幅"""
        self._copy_reply(idx)
        return "break"

    def _qr_button_rightclick(self, e):
        """话术按钮右键：关闭横幅"""
        self._dismiss()
        return "break"

    def _copy_reply(self, idx):
        """点话术按钮：复制到剪贴板 + 横幅上给回执。
        开了「直接填进输入框」还会多一步：先跳到那个人，核对上了才打字。
        群聊除外：要跳得搜群名，同名群/折叠群一律搜不准，只复制不驱动微信
        （横幅点击那边同一原则）。"""
        texts = getattr(self, "_qr_shown", []) or []
        if idx >= len(texts):
            return
        text = texts[idx]
        msg = getattr(self, "_current_msg", None) or {}
        fill = bool(config.get("auto_reply")) and not msg.get("is_group")
        # 开了填入的话，剪贴板那份只是"没填成时的兜底"，字真进了输入框就该把他
        # 原来抄的东西还回去。只能还原文本：图片/文件这类读不出也写不回（clipboard_get
        # 直接抛错），那种情况就当没这回事，话术留在剪贴板里。
        prev_clip = None
        if fill:
            try:
                s = self.win.clipboard_get()
                if isinstance(s, str) and 0 < len(s) <= QR_CLIP_KEEP_MAX and s != text:
                    prev_clip = s
            except Exception:
                prev_clip = None
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.win.update_idletasks()      # 让剪贴板立刻归位，别等横幅淡出时才写入
            log(f"[快捷回复] 已复制话术：{text}")
            if fill:
                self._show_qr_fb("已复制，正在填进微信输入框……")
            elif msg.get("is_group"):
                self._show_qr_fb("已复制（群聊不自动跳转，切过去 Ctrl+V）")
            else:
                self._show_qr_fb("已复制到剪贴板，切到微信按 Ctrl+V 发送")
        except Exception as e:
            fill = False
            self._show_qr_fb("复制失败，可在设置里检查话术")
            log(f"[快捷回复] 复制失败: {e}")
        if not fill:
            return
        name = (msg.get("sender") or "").strip()
        if not name:
            log("[快捷回复] 开了填入，但这条横幅连对方是谁都没有，只留在剪贴板")
            self._fb_later("没认出对方是谁，话术在剪贴板里")
            return
        threading.Thread(target=self._fill_after_jump,
                         args=(name, msg.get("session_key") or "", text, prev_clip),
                         daemon=True).start()

    def _show_qr_fb(self, text):
        """横幅底部那行回执。**只能在 Tk 线程里调**，工作线程要走 _fb_later。"""
        self._qr_fb = text
        self._sync_hint()
        try:
            if getattr(self, "_qr_fb_after", None):
                self.root.after_cancel(self._qr_fb_after)
            self._qr_fb_after = self.root.after(2600, self._clear_qr_fb)
        except Exception:
            pass

    def _fb_later(self, text):
        """跳会话+打字要三四秒，横幅可能已经收了；递回 Tk 线程改回执，
        还挂在屏幕上就能看到，被收掉了就当没说过（日志里一定有）。"""
        try:
            self.win.after(0, lambda: self._show_qr_fb(text))
        except Exception:
            pass

    # 闸门停手的各种原因 → 横幅上那一句短话。宽度都按"不超过那句 Ctrl+V 提示"控制，
    # 别把横幅底部那一行撑出第二行（改了会顶高卡片，见 _sync_hint）。
    _QR_FAIL_FB = {
        "busy": "上一条还在操作微信，先不抢",
        "user-typing": "你在敲键盘，先不动微信",
        "drive-off": "驱动总闸关着，只复制进剪贴板",
        "rate-gap": "刚动过微信，等一秒再点",
        "rate-min": "这一分钟动微信太多，先等一等",
        "rate-hour": "今天动微信太频繁，先只复制",
        "weak-changed": "画面大变没敢填，话术在剪贴板",
        "weak-no-pre": "核对不了，话术在剪贴板",
        "weak-no-print": "核对不了，话术在剪贴板",
        "wx-gone": "微信窗口不见了，先打开微信再点",
        "lost-front": "前台被抢走，没打字（话术在剪贴板）",
        "panel-stuck": "上次的面板关不掉，话术在剪贴板",
        "no-front": "没抢到微信前台，话术在剪贴板",
        "no-child": "找不到微信窗口，话术在剪贴板",
        "click-fail": "点不动微信，话术在剪贴板",
        "no-panel": "没能点到微信搜索框，话术在剪贴板",
        "text-not-accepted": "字没打进微信搜索框，话术在剪贴板",
        "text-not-in-input": "字没落进输入框，已自动擦掉",
        "no-print": "抓不到微信的画面，话术在剪贴板",
    }

    def _fill_after_jump(self, name, session_key, text, prev_clip=None):
        """点一条话术：跳到那个人 + 核对上了 + 把这句话打进输入框，**一条链子跑完**。

        核对靠的是"跳完那个会话的未读被清零"。核对不上（包括它本来就没未读、
        根本没信号可核）就一律不打字——宁可让你 Ctrl+V，也不能把这句话打进
        当时正开着的别人家窗口里。以前这里是两个独立调用（跳一次、填一次），
        现在合成 `wx_jump_fill`：少抢一次前台、配额只算一次，中间也插不进别的点击。

        真打进去了，就把他原来抄着的那段文本还给剪贴板（prev_clip 只在开填入时才会
        被捕获；话术只是兜底，用完就该物归原主）。没打成则不还，剪贴板里留的是话术。"""
        try:
            out = wx_jump_fill(name, text, session_key or None)
            if out.get("ok") and out.get("step") == "filled":
                if prev_clip is None or not self._give_clip_back(prev_clip):
                    self._fb_later("已填进输入框，发送由你自己按")
                return
            log(f"[快捷回复] 没填进输入框（阶段 {out.get('stage')}，步骤 {out.get('step')}），"
                f"话术仍在剪贴板里")
            default = ("没填进去，话术在剪贴板里" if out.get("stage") == "fill"
                       else "没确认是本人，话术只在剪贴板里")
            self._fb_later(self._QR_FAIL_FB.get(out.get("step"), default))
        except Exception as e:
            log(f"[快捷回复] 填入异常：{e}")
            self._fb_later("填入出错了，话术在剪贴板里")

    def _give_clip_back(self, s):
        """把剪贴板还给他原来那份文本。成没成用回执说出来——悄悄换掉会更让人慌。"""
        def _do():
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(s)
                self.win.update_idletasks()
                self._show_qr_fb("已填进输入框，剪贴板也还回去了")
            except Exception as e:
                log(f"[快捷回复] 剪贴板没还回去：{e}")
                self._show_qr_fb("已填进输入框，发送由你自己按")
        try:
            self.win.after(0, _do)
            return True
        except Exception:
            return False

    def _clear_qr_fb(self):
        self._qr_fb_after = None
        self._qr_fb = ""
        self._sync_hint()

    def _bind_banner_events(self, skip=None):
        """递归绑定横幅窗口及其所有子控件的交互事件。
        skip: 需跳过（保持自身绑定）的控件集合。"""
        skip = skip or set()

        def _rec(w):
            for child in w.winfo_children():
                if child in skip:
                    continue
                _rec(child)
                child.bind("<ButtonPress-1>", self._press)
                child.bind("<B1-Motion>", self._drag)
                child.bind("<ButtonRelease-1>", self._release)
                child.bind("<Button-3>", lambda e: self._dismiss())
                child.bind("<Enter>", self._sync_hint)
                child.bind("<Leave>", self._sync_hint)

        _rec(self.win)
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<B1-Motion>", self._drag)
        self.win.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Button-3>", lambda e: self._dismiss())
        self.win.bind("<Enter>", self._sync_hint)
        self.win.bind("<Leave>", self._sync_hint)

    def _press(self, e):
        self._press_pos = (e.x_root, e.y_root)
        self._drag_data = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y())
        self._moved = False

    def _drag(self, e):
        if self._drag_data:
            dx = e.x_root - self._drag_data[0]
            dy = e.y_root - self._drag_data[1]
            if abs(dx) + abs(dy) > 6:
                self._moved = True
            self.win.geometry(f"+{self._drag_data[2] + dx}+{self._drag_data[3] + dy}")

    def _release(self, e):
        if self._press_pos and not self._moved:
            self._open_wechat()
        self._press_pos = None
        self._drag_data = None

    def _open_wechat(self):
        msg = getattr(self, "_current_msg", None) or {}
        if (msg.get("source") or "") == "wecom":
            self._dismiss()
            try:
                from wecom_notify import bring_wecom_front
                threading.Thread(target=bring_wecom_front, daemon=True).start()
            except Exception as e:
                log(f"[横幅] 打开企业微信失败: {e}")
            return
        name = (msg.get("sender") or "").strip()
        jump = bool(config.get("auto_reply")) and msg and not msg.get("is_group") and name
        self._dismiss()
        if not jump:
            threading.Thread(target=bring_wechat_front, daemon=True).start()
            return
        # 群聊不走这条路：群里"打开某个会话"要搜的是群名，同名群/折叠群一律搜不准
        threading.Thread(target=self._jump_then_report,
                         args=(name, msg.get("session_key") or ""), daemon=True).start()

    def _jump_then_report(self, name, session_key=""):
        """跳会话整条链子要 1~3 秒，必须放在工作线程里跑，不然横幅的淡出/定时器会卡住。

        session_key 只用来做事后核对（未读有没有清零），不参与"搜什么"。"""
        try:
            r = wx_open_chat(name, press_enter=True, verify_username=session_key or None)
        except Exception as e:
            log(f"[跳会话] 异常：{e}")
            return
        log(f"[跳会话] 结果 {r.get('step')} ok={r.get('ok')} 目标={r.get('name')}")
        if r.get("step") in ("drive-off", "rate-gap", "rate-min", "rate-hour"):
            log("[跳会话] 被闸拦下：" + wx_rate_why(r["step"], r.get("wait") or 0.0))

    def _dismiss(self):
        if self.hide_after:
            self.root.after_cancel(self.hide_after)
            self.hide_after = None
        # v4.1.0-审查: 取消淡出动画定时器——淡出过程中右键关闭时，
        # 若动画回调继续跑完，i<0 分支会再次 withdraw 并误弹排队消息
        if self._anim_after:
            try:
                self.root.after_cancel(self._anim_after)
            except Exception:
                pass
            self._anim_after = None
        try:
            self.win.withdraw()
        except Exception:
            pass

    def _visible(self):
        try:
            return self.win.state() == "normal"
        except Exception:
            return False

    @staticmethod
    def _msg_ident(msg_info):
        """消息身份标识：session_key + 发送者 + local_id + 时间戳。

        与 Monitor._msg_fingerprint 用同一组字段——那边已经按它去重，这里只是
        在横幅侧再挡一次"同一条消息被写两遍"（微信到达时写一次、约 10 秒后状态
        更新再写一次）。以前这道闸是"距上次合并不足 1 秒就丢弃"，会把两条**不同**
        消息里后一条整个吞掉（既不计数也不更新内容），改成按身份判定。"""
        if not msg_info:
            return None
        return "%s|%s|%s|%s" % (
            msg_info.get("session_key") or msg_info.get("sender") or "",
            msg_info.get("sender_wxid", ""), msg_info.get("local_id", ""),
            msg_info.get("timestamp", 0))

    def show(self, kind="new", ts=None, msg_info=None):
        """显示/更新横幅。kind="new" 表示全新消息（重置计数），
        kind="merge" 表示合并窗口内的新消息（计数 +1 并刷新横幅）。
        msg_info: {"sender": "xxx", "content": "xxx"} 可选，v3.0 消息内容"""
        if ts is None:
            ts = time.strftime("%H:%M:%S")
        # v4.6.0: 微信与企微不混排——来源不同就当一条新消息，计数从 1 开始；
        # 否则企微消息会把正在显示的微信横幅加进计数（反之亦然），
        # 横幅上的来源色片与条数就对不上了。
        if kind == "merge":
            incoming_src = (msg_info or {}).get("source") or "wechat"
            current_src = (getattr(self, "_current_msg", None) or {}).get("source") or "wechat"
            if incoming_src != current_src:
                kind = "new"
        if kind == "merge":
            ident = self._msg_ident(msg_info)
            if ident and ident == self._last_ident:
                return     # 同一条消息的二次写库（WAL 抖动），只算一次
            self._last_ident = ident
            self._cnt += 1
            if self._visible():
                # 横幅正在显示：更新计数文案并重置消失计时
                # v4.1.0: 当前显示的消息被新消息覆盖前先入队（用户可能还没看够），
                # 横幅消失后按时间顺序依次补弹（保留最近 2 条防刷屏）。
                # 新消息 msg_info 会完整显示，不入队，避免重复显示
                cur = getattr(self, "_current_msg", None)
                if cur and cur.get("content") and cur.get("content") != (msg_info or {}).get("content"):
                    self._pending.append(cur)
                    if len(self._pending) > 2:
                        self._pending.pop(0)
                self._current_msg = msg_info
                self._update_content(msg_info)
                self.time_lbl.config(text=ts)
                if self.hide_after:
                    self.root.after_cancel(self.hide_after)
                self.hide_after = self.root.after(
                    int(config["popup_duration"] * 1000), self._fade_out)
                return
            # 横幅已消失：重弹，把漏掉的消息补上
            log(f"[banner] 重弹横幅（累计 {self._cnt} 条） {ts}")
        else:
            self._cnt = 1
            self._last_ident = self._msg_ident(msg_info)
            self._current_msg = msg_info
        self._do_show(ts, msg_info)

    def _truncate_text(self, text, max_lines=1, wrap=480):
        """按像素宽度把文本截为最多 max_lines 行，超出部分以…结尾。
        用真实字体测量，保证每行都不溢出（Label 不会二次裁切）。
        v4.1.0: 字号跟随 banner_font_size（社区反馈：字体大小可调）。"""
        fs = int(config.get("banner_font_size", 11))
        try:
            import tkinter.font as tkfont
            f = tkfont.Font(font=(FONT, fs))
        except Exception:
            f = None
        ell = "…"
        lines = []
        while text and len(lines) < max_lines:
            if f is None:
                line = text[:32]  # 兜底：按字符数切
            else:
                # 二分找当前行能容纳的最大前缀（含省略号余量）
                lo, hi = 1, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if f.measure(text[:mid]) <= wrap:
                        lo = mid
                    else:
                        hi = mid - 1
                line = text[:lo]
            lines.append(line)
            text = text[len(line):]
        if text and lines:
            # 还有剩余：最后一行截短并加…
            last = lines[-1]
            while last and (f.measure(last + ell) > wrap if f else len(last + ell) > 31):
                last = last[:-1]
            lines[-1] = last + ell
        return "\n".join(lines) if lines else ""

    def _with_cnt(self, who):
        """合并窗口内累计多条时把条数标出来。

        原来这行恒写"给你发送了一条消息"——连来 3 条也显示"一条"，而条数只进了
        日志。现在单条只显示"谁"，多条才追加"· N 条"。"""
        try:
            c = max(1, int(getattr(self, "_cnt", 1) or 1))
        except Exception:
            c = 1
        return who if c <= 1 else f"{who} · {c} 条"

    def _update_content(self, msg_info=None):
        """更新横幅消息内容（v3.1：昵称行与内容行分离，长消息不裁剪昵称。
        v3.1.1：内容按像素精确截断为最多 2 行 +…，窗口高度随内容自适应，
        不再出现内容被窗口下缘裁切的情况。
        v4.1.0：群消息改为「群名」一行 +「发送人：内容」一行（社区反馈：
        "谁谁在什么群里说了什么"不好分辨，建议群名换行显示谁谁发了什么）；
        字号跟随 banner_font_size。"""
        fs = int(config.get("banner_font_size", 11))
        s = getattr(self, "_s", 1.0)
        is_wecom = ((msg_info or {}).get("source") or "") == "wecom"
        # 企微横幅点击打开的是企业微信，提示语跟着换
        self._hint_text = ("左键单击打开企业微信　·　右键单击关闭" if is_wecom
                           else config.get("banner_hint", ""))
        # 头像有无会改变正文可用宽度，所以先定头像、再算宽度，最后才截文字
        show_av = self._set_avatar((msg_info or {}).get("avatar"))
        try:
            if show_av:
                self.avatar.grid()
            else:
                self.avatar.grid_remove()
        except Exception:
            pass
        self._content_w = self._content_w_for(show_av)
        cw = self._content_w
        try:
            self.content_lbl.config(wraplength=cw)
            self.hint_lbl.config(wraplength=cw)
        except Exception:
            pass
        # 首行右侧要留给时间；群消息/企微再多留一个色片
        hw = max(120, cw - (self._head_reserve_group
                            if ((msg_info or {}).get("is_group") or is_wecom)
                            else self._head_reserve))
        try:
            def _chip(show, text="群"):
                if show:
                    self.group_chip.config(text=text)
                    self.group_chip.pack(side="left", padx=(int(9 * s), 0),
                                         pady=(int(2 * s), 0))
                else:
                    self.group_chip.pack_forget()

            try:
                import tkinter.font as tkfont
                f = tkfont.Font(font=(FONT, fs, "bold"))

                def _fit(text):
                    if f.measure(text) <= hw:
                        return text
                    while text and f.measure(text + "…") > hw:
                        text = text[:-1]
                    return text + "…"
            except Exception:
                _fit = lambda t: t

            if msg_info and msg_info.get("content"):
                sender = msg_info.get("sender", "")
                is_group = bool(msg_info.get("is_group"))
                content = msg_info.get("content", "").replace("\n", " ").strip()
                # 群聊只有"@我"才给话术按钮（@别人、没人@都不弹）。判定由监控线程
                # 按活跃账号现算好塞在 at_me 里（换账号后自动跟着变）；字段缺失时
                # 先用 mentioned 对账号目录身份，再退回正文 @wxid 的文本兜底。
                at_me = msg_info.get("at_me")
                if at_me is None:
                    ids = _account_dir_ids()
                    mentioned = msg_info.get("mentioned") or []
                    at_me = (any(m in ids for m in mentioned) if mentioned
                             else _text_at_me(content))
                is_at_me = is_group and bool(at_me)
                # 昵称保护：过长也截断
                if len(sender) > 18:
                    sender = sender[:18] + "…"
                if is_wecom:
                    # 企业微信：首行是联系人/群名，右侧用「企业微信」色片标来源；
                    # 不接话术按钮（那条链只对微信的联系人会话做了核对）
                    self.sender_lbl.config(text=_fit(self._with_cnt(sender or "企业微信")))
                    _chip(True, "企业微信")
                elif is_group:
                    # 群名当主角，"群"用色片标出（不再往群名后面硬拼一个"群"字），
                    # 发送人放进正文前缀——"谁在哪个群说了什么"一眼能对上
                    grp = msg_info.get("group_name", "") or "群聊"
                    self.sender_lbl.config(text=_fit(self._with_cnt(grp)))
                    _chip(True, "群")
                    if sender and not content.startswith(sender + "："):
                        content = f"{sender}：{content}"
                else:
                    self.sender_lbl.config(text=_fit(self._with_cnt(sender or "微信好友")))
                    _chip(False)
                content = self._truncate_text(content, max_lines=self.CONTENT_LINES,
                                              wrap=cw)
                self.content_lbl.config(text=content)
                self.content_lbl.grid()
                # 私聊或"@我"的群聊给话术按钮；必须在算高度之前排好，否则按钮会被卡片下缘切掉
                if is_wecom:
                    self._hide_quick_replies()
                else:
                    self._apply_quick_replies(is_group and not is_at_me)
                # 高度自适应：按实际布局需求计算（避免固定 150 裁掉内容）
                try:
                    self.win.update_idletasks()
                    # 只留 4px 余量（够 1px 描边 + 圆角遮罩）：以前留 16px，
                    # 正文只剩一行时那 16px 全变成底部空白
                    need = self.win.winfo_reqheight() + int(4 * s)
                    self._resize_banner(max(int(int(config["banner_height"]) * s), need))
                except Exception:
                    self._resize_banner(max(int(int(config["banner_height"]) * s), 150))
            else:
                self.content_lbl.grid_remove()
                self._hide_quick_replies()     # 读不到内容时没有会话上下文，不给按钮
                _chip(False)
                # 「横幅标题」设置项现在管这里：读不到内容时它就是横幅的主角
                title = ("企业微信 · 新消息" if is_wecom else
                         (config.get("banner_title") or "微信 · 新消息").strip())
                self.sender_lbl.config(
                    text=(f"{title} · {self._cnt} 条" if self._cnt > 1 else title))
                # 没有正文时同样贴着内容收（下限走 banner_height）
                try:
                    self.win.update_idletasks()
                    self._resize_banner(max(int(int(config["banner_height"]) * s),
                                            self.win.winfo_reqheight() + int(4 * s)))
                except Exception:
                    self._resize_banner(int(int(config["banner_height"]) * s))
        except Exception as e:
            log(f"[banner] 更新内容失败: {e}")

    def _resize_banner(self, h):
        """按需调整横幅高度并重绘圆角（v4.1.0: 垂直位置跟随 banner_vertical）"""
        try:
            self._h = int(h)         # 记录真实高度：_do_show 定位要用它，
            w = getattr(self, "_w", int(int(config["banner_width"]) * self._s))
            x = banner_x(w, self.root.winfo_screenwidth())   # 长横幅被顶出屏幕外
            y = banner_y(self._h, self.root.winfo_screenheight())
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.after(40, self._apply_round)
        except Exception:
            pass

    def _do_show(self, ts, msg_info=None):
        log(f"[banner] 显示横幅 {ts}")
        try:
            # 取消可能还在运行的旧动画（如旧的淡出），防止与本次淡入交错
            if self._anim_after:
                try:
                    self.root.after_cancel(self._anim_after)
                except Exception:
                    pass
                self._anim_after = None
            self.time_lbl.config(text=ts)
            self._update_content(msg_info)
            sw = self.root.winfo_screenwidth()
            w = getattr(self, "_w", int(int(config["banner_width"]) * self._s))
            x = banner_x(w, sw)
            # v4.1.0: 不透明度可调（社区反馈：弹窗背景可透明不影响使用别的）
            alpha = max(0.3, min(1.0, float(config.get("banner_opacity", 1.0))))
            y = banner_y(int(getattr(self, "_h", 0) or
                              int(int(config["banner_height"]) * self._s)),
                         self.root.winfo_screenheight())
            self.win.geometry(f"+{x}+{y}")
            self.win.attributes("-alpha", 0.0)
            self.win.deiconify()
            self.win.lift()
            self._sync_hint()     # 横幅可能正好弹在静止的鼠标下面
            if config["sound"]:
                try:
                    play_notify_sound()
                except Exception as e:
                    log(f"[提示音] 播放失败: {e}")
            if self.hide_after:
                self.root.after_cancel(self.hide_after)
            self._animate_in(x, 10, 1)
            self.hide_after = self.root.after(int(config["popup_duration"] * 1000),
                                              self._fade_out)
        except Exception as e:
            log(f"[banner] 显示失败: {e}")

    def _animate_in(self, x, steps, i):
        a = i / steps
        try:
            # v4.1.0: 淡入目标透明度 = 用户配置的不透明度
            target = max(0.3, min(1.0, float(config.get("banner_opacity", 1.0))))
            self.win.attributes("-alpha", target * a)
            if not self._moved:
                y = banner_y(int(getattr(self, "_h", 0) or config["banner_height"]),
                         self.root.winfo_screenheight())
                self.win.geometry(f"+{x}+{int(y + (1 - a) * 10)}")
        except Exception:
            pass
        if i < steps:
            self._anim_after = self.root.after(12, lambda: self._animate_in(x, steps, i + 1))
        else:
            self._anim_after = None

    def _fade_out(self):
        self._animate_out(10)

    def _animate_out(self, i):
        if i < 0:
            try:
                self.win.withdraw()
                self.win.attributes("-alpha", 1.0)
            except Exception:
                pass
            self._anim_after = None
            # v4.1.0: 合并窗口内排队待补弹的消息，横幅消失后依次显示
            # （社区反馈：群信息先展示，6 秒后自己发的测试信息不显示）
            if self._pending:
                msg_info = self._pending.pop(0)
                self._cnt = 1
                self._current_msg = msg_info  # v4.1.0-审查: 同步当前显示消息
                ts = time.strftime("%H:%M:%S")
                try:
                    if msg_info and msg_info.get("timestamp"):
                        ts = time.strftime("%H:%M:%S", time.localtime(msg_info["timestamp"]))
                except Exception:
                    pass
                self._do_show(ts, msg_info)
            return
        try:
            self.win.attributes("-alpha", max(0.05, i / 10))
        except Exception:
            pass
        self._anim_after = self.root.after(15, lambda: self._animate_out(i - 1))

    def _poll(self):
        try:
            while True:
                try:
                    t = self.q.get_nowait()
                    if isinstance(t, tuple) and len(t) >= 3:
                        self.show(t[0], t[1], t[2])
                    elif isinstance(t, tuple) and len(t) == 2:
                        self.show(t[0], t[1])
                    else:
                        self.show("new", t)
                except queue.Empty:
                    break
        except Exception as e:
            log(f"[banner] 队列轮询异常: {e}")
        # 可见时兜一次悬停状态（Enter/Leave 偶发不送达，见 _sync_hint 注释）
        try:
            if self.win and self.win.winfo_viewable():
                self._sync_hint()
        except Exception:
            pass
        self.root.after(120, self._poll)


# ============================================================
# 设置界面
# ============================================================
class SettingsWindow:
    """粉红主题、选项卡分页的设置窗口。"""

    def __init__(self, root, banner, on_quit):
        self.root = root
        self.banner = banner
        self.on_quit = on_quit
        self.win = None
        self.status_lbl = None
        self.P = UI
        # 提取器要活得比某一次窗口构建久：切主题会销毁重建窗口（_rebuild），
        # 放在 _build_advanced 里会被重置成 None，正在跑的提取就再也取消不了，
        # 还能再起第二个（两个 Hook 抢同一个进程，先结束的会把对方的拆掉）
        self._extractor = None
        self._build()

    # ---------- 构建 ----------
    def _build(self):
        """v4.0 界面重构：深色标题栏 + 左侧导航 + 分组卡片内容区 + 底部操作栏。"""
        P = self.P
        w = self.win = tk.Toplevel(self.root)
        w.resizable(False, False)
        w.configure(bg=P["BG"])
        w.overrideredirect(True)  # 无边框窗口：去掉系统标题栏与默认图标

        main = tk.Frame(w, bg=P["BG"])
        main.pack(fill="both", expand=True)

        # 全局拖动：窗口任意空白处按住可移动（交互控件除外）
        self._dragging = False
        w.bind("<Button-1>", self._start_move)
        w.bind("<B1-Motion>", self._on_move)
        w.bind("<ButtonRelease-1>", self._end_move)

        # ================= 左侧栏（仿 macOS 访达）=================
        # 品牌块 + 「设置」分组标题 + 图标导航 + 侧栏底部的运行状态
        side = tk.Frame(main, bg=P["SIDEBAR"], width=SIDEBAR_W)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=P["SIDEBAR"])
        brand.pack(fill="x", padx=14, pady=(16, 12))
        logo = tk.Canvas(brand, width=24, height=24, bg=P["SIDEBAR"], highlightthickness=0)
        logo.pack(side="left", padx=(2, 9))
        round_rect(logo, 1, 1, 23, 23, 7, fill=P["ACCENT"], outline="")
        round_rect(logo, 5, 7, 19, 15, 3, fill="#FFFFFF", outline="")
        logo.create_polygon(8, 14, 8, 18, 12, 14, fill="#FFFFFF", smooth=True)
        for dx in (8.5, 12, 15.5):
            logo.create_oval(dx - 1, 9.5, dx + 1, 11.5, fill=P["ACCENT"], outline="")
        title_wrap = tk.Frame(brand, bg=P["SIDEBAR"])
        title_wrap.pack(side="left")
        tk.Label(title_wrap, text="微信来消息提醒", bg=P["SIDEBAR"], fg=P["TITLE_FG"],
                 font=(FONT, 10, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text=f"v{VERSION}", bg=P["SIDEBAR"], fg=P["SIDEBAR_CAP"],
                 font=(FONT, 8)).pack(anchor="w")
        tk.Label(side, text="设置", bg=P["SIDEBAR"], fg=P["SIDEBAR_CAP"],
                 font=(FONT, 9, "bold"), anchor="w"
                 ).pack(fill="x", padx=(20, 0), pady=(2, 4))
        self._nav_bg = P["SIDEBAR"]
        self._nav_items = {}
        self._active_tab = None
        nav_box = tk.Frame(side, bg=P["SIDEBAR"])
        nav_box.pack(fill="x", padx=10)
        NAV_W = SIDEBAR_W - 20
        for key, label, icon in (("basic", "基础设置", "sliders"),
                                 ("beauty", "横幅美化", "bell"),
                                 ("advanced", "高级设置", "key"),
                                 ("about", "关于我们", "info")):
            cv = tk.Canvas(nav_box, width=NAV_W, height=30, bg=P["SIDEBAR"],
                           highlightthickness=0, bd=0, cursor="hand2")
            cv.pack(fill="x", pady=1)
            state = {"hover": False}

            def paint(k=key, c=cv, lab=label, ic=icon, st=state):
                c.delete("all")
                active = self._active_tab == k
                if active:
                    bgc, fgc = P["SIDEBAR_SEL"], P["SIDEBAR_SEL_FG"]
                elif st["hover"]:
                    bgc, fgc = P["SIDEBAR_HOVER"], P["SIDEBAR_FG"]
                else:
                    bgc, fgc = None, P["SIDEBAR_FG"]
                if bgc:
                    round_rect(c, 1, 1, NAV_W - 1, 29, 7, fill=bgc, outline="")
                self._nav_icon(c, ic, fgc)
                c.create_text(34, 15, text=lab, fill=fgc, anchor="w",
                              font=(FONT, 10, "bold" if active else "normal"))

            cv.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))

            def _enter(e, k=key, st=state):
                st["hover"] = True
                self._paint_nav(k)

            def _leave(e, k=key, st=state):
                st["hover"] = False
                self._paint_nav(k)
            cv.bind("<Enter>", _enter)
            cv.bind("<Leave>", _leave)
            self._nav_items[key] = {"cv": cv, "paint": paint, "hover": state}

        # 侧栏底部：运行状态指示灯（原在底部栏左侧）
        st_wrap = tk.Frame(side, bg=P["SIDEBAR"])
        st_wrap.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        self.status_dot = tk.Canvas(st_wrap, width=10, height=12, bg=P["SIDEBAR"],
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(2, 6))
        self._dot = self.status_dot.create_oval(1, 2, 9, 10, fill=P["SUB"], outline="")
        self.status_lbl = tk.Label(st_wrap, text="正在启动…", bg=P["SIDEBAR"],
                                   fg=P["SUB"], font=(FONT, 9))
        self.status_lbl.pack(side="left")

        # ================= 右侧主区 =================
        right = tk.Frame(main, bg=P["BG"])
        right.pack(side="left", fill="both", expand=True)

        # 工具栏：与侧栏同一块"外壳"色（访达式：标题栏和侧栏是一体的），
        # 只有内容区是浅底。之前标题栏用纯白，被切成一条白带，标题像浮在半空。
        toolbar = tk.Frame(right, bg=P["SIDEBAR"], height=46)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        self._page_title = tk.Label(toolbar, text="基础设置", bg=P["SIDEBAR"],
                                    fg=P["TITLE_FG"], font=(FONT, 13, "bold"))
        self._page_title.pack(side="left", padx=(22, 0))
        # 无边框窗口没有系统标题栏：最小化 / 关闭按钮自己画（关闭=收进托盘，
        # 退出走底部「退出」按钮，避免误点 × 把后台监控一起关掉）
        WinButton(toolbar, "close", self.hide, P["SIDEBAR"], P["TITLE_FG"],
                  P["SIDEBAR_HOVER"]).pack(side="right", padx=(0, 10))
        WinButton(toolbar, "min", self._minimize, P["SIDEBAR"], P["TITLE_FG"],
                  P["SIDEBAR_HOVER"]).pack(side="right", padx=(0, 2))

        # 内容区（浅绿灰底 + 单列分组卡片）
        content = tk.Frame(right, bg=P["CONTENT_BG"])
        content.pack(side="top", fill="both", expand=True)
        self._content = content
        self._sections = []  # 圆角分组卡片 (canvas, outer, sync)，供 _refit 强制同步
        self._col_last = {}  # 每列最后一张卡的容器 id -> wrap（纵向拉伸只给它）
        self.CARD_BG = P["CARD_BG"]  # 主题化：卡片背景随主题切换
        # 访达式：窗口尺寸固定、内容区可滚动（单列分组卡片总高会超过屏幕）
        self._scroll_pages = []
        self._pages = {}
        self._page_body = {}
        for key in ("basic", "beauty", "advanced", "about"):
            page, body = self._make_scroll_page(content)
            self._pages[key] = page
            self._page_body[key] = body
        self._build_basic(self._page_body["basic"])
        self._build_beauty(self._page_body["beauty"])
        self._build_advanced(self._page_body["advanced"])
        self._build_about(self._page_body["about"])
        w.bind_all("<MouseWheel>", self._on_wheel)

        # 底部操作栏（状态灯已移到侧栏，这里只留通用按钮）——和标题栏同色，
        # 让"上-左-下"三边是同一块外壳，中间才是内容面板
        tk.Frame(right, bg=P["SHADOW2"], height=1).pack(fill="x")
        footer = tk.Frame(right, bg=P["SIDEBAR"], height=58)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        RoundedButton(footer, "退出", self.on_quit, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]
                      ).pack(side="right", padx=(12, 20), pady=12)
        RoundedButton(footer, "打开日志文件", self._open_log, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]
                      ).pack(side="right", padx=(8, 0), pady=12)
        RoundedButton(footer, "测试弹窗", self._test_banner, P["ACCENT"], "#FFFFFF",
                      hover=P["ACCENT_D"], bold=True).pack(side="right", padx=(8, 0), pady=12)

        # 隐藏到托盘，不退出
        w.protocol("WM_DELETE_WINDOW", self.hide)
        # overrideredirect 窗口默认不进任务栏，最小化后就再也点不回来：
        # 补 WS_EX_APPWINDOW。title 只用于任务栏按钮文字，窗口本身没有标题栏。
        w.title(f"{APP_NAME} 设置")
        w.update_idletasks()
        self._taskbar_button()
        w.withdraw()
        # 调试用：设置环境变量 WXNOTIFY_START_TAB=beauty 可直接停在指定页（截图核对用）
        _start_tab = os.environ.get("WXNOTIFY_START_TAB", "").strip()
        self._switch_tab(_start_tab if _start_tab in ("basic", "beauty", "advanced", "about")
                         else "basic", first=True)

    def _taskbar_hwnd(self):
        cid = self.win.winfo_id()
        return user32.GetParent(cid) or cid

    def _taskbar_button(self):
        """让无边框设置窗口在任务栏上有按钮（最小化后可点回）。"""
        try:
            hwnd = self._taskbar_hwnd()
            WS_EX_APPWINDOW = 0x00040000
            cur = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if not cur & WS_EX_APPWINDOW:
                user32.SetWindowLongW(hwnd, -20, cur | WS_EX_APPWINDOW)
                # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED：让外壳重新登记按钮
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x1 | 0x2 | 0x40 | 0x20)
        except Exception as e:
            log(f"[设置] 任务栏按钮设置失败: {e}")

    def _minimize(self):
        """最小化：Tk 的 iconify 对 override-redirect 窗口会直接报错
        （can't iconify: override-redirect flag is set），改用 Win32 SW_MINIMIZE；
        恢复走任务栏按钮或托盘「打开设置」（deiconify 会把最小化一起还原）。"""
        try:
            user32.ShowWindow(self._taskbar_hwnd(), 6)  # SW_MINIMIZE
            log("[设置] 窗口已最小化")
        except Exception:
            self.hide()

    def _nav_icon(self, c, kind, color):
        """侧栏 16px 线性图标（Canvas 矢量绘制，不用字体表情符号）。"""
        if kind == "sliders":          # 基础设置：两条滑杆
            c.create_line(9, 12, 25, 12, fill=color, width=2)
            c.create_line(9, 19, 25, 19, fill=color, width=2)
            c.create_oval(14, 8, 20, 14, fill=color, outline="")
            c.create_oval(18, 15, 24, 21, fill=color, outline="")
        elif kind == "bell":           # 横幅美化：铃铛
            c.create_polygon(12, 19, 12, 14, 14, 10, 20, 10, 22, 14, 22, 19,
                             smooth=True, fill=color, outline="")
            c.create_rectangle(10, 19, 24, 21, fill=color, outline="")
            c.create_oval(15, 6, 19, 10, fill=color, outline="")
        elif kind == "key":            # 高级设置：钥匙
            c.create_oval(8, 11, 16, 19, outline=color, width=2)
            c.create_line(15, 15, 25, 15, fill=color, width=2)
            c.create_line(21, 15, 21, 19, fill=color, width=2)
        else:                          # 关于我们：ⓘ 圆圈 + i
            c.create_oval(8, 7, 24, 23, outline=color, width=2)
            c.create_text(16, 15, text="i", fill=color, font=(FONT, 10, "bold"))

    # ---------- 导航 ----------
    def _paint_nav(self, key=None):
        """重绘导航胶囊（选中=浅绿底绿字，悬停=浅灰，其余透明）。"""
        keys = (key,) if key else tuple(self._nav_items)
        for k in keys:
            self._nav_items[k]["paint"]()

    def _switch_tab(self, key, first=False):
        for k, page in self._pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self._active_tab = key
        # 访达式：工具栏标题 = 当前页名
        item = self._nav_items.get(key)
        if item is not None and self._page_title is not None:
            try:
                self._page_title.configure(
                    text={"basic": "基础设置", "beauty": "横幅美化",
                          "advanced": "高级设置", "about": "关于我们"}.get(key, ""))
            except Exception:
                pass
        self._paint_nav()
        if key == "advanced":
            self._refresh_advanced_status()
        if not first:
            self._refit()

    # ---------- 全局拖动 ----------
    def _start_move(self, e):
        if getattr(e.widget, "_drag_block", False):
            self._dragging = False
            return
        if isinstance(e.widget, (RoundedEntry, RoundedSpinbox, RoundedCheckbox,
                                 RoundedRadio)):
            self._dragging = False
            return
        cls = e.widget.winfo_class()
        if cls in ("Button", "Checkbutton", "Spinbox", "Entry", "Menu", "Radiobutton"):
            self._dragging = False
            return
        self._dragging = True
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _on_move(self, e):
        if not self._dragging:
            return
        try:
            self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        except Exception:
            pass

    # ---------- 窗口圆角与自适应 ----------
    def _apply_round(self):
        round_toplevel(self.win, 18)

    def _refit(self):
        try:
            # 强制同步所有圆角分组卡片的高度（内容变化后 Canvas 不会自动重排）
            for _cv, _inner, _sync in getattr(self, "_sections", []):
                try:
                    _sync()
                except Exception:
                    pass
            # 第二遍：整宽说明文字的换行宽度是在第一遍里按卡片实际宽度改的，
            # 行数变了但父容器请求尺寸要等几何重算，只跑一遍会把末行切掉
            try:
                self.win.update_idletasks()
            except Exception:
                pass
            for _cv, _inner, _sync in getattr(self, "_sections", []):
                try:
                    _sync()
                except Exception:
                    pass
            # v4.4 访达式：窗口尺寸固定为 WIN_W×WIN_H，内容超出交给滚动区，
            # 不再按页面内容把窗口拉到 1200+ 高（超过屏幕就没法用了）
            # 屏幕本身不够大时（1366x768 开 125% 缩放后可用高只有 ~720）按屏幕夹紧，
            # 否则窗口底部连标题栏带按钮一起被裁到屏幕外，滚动区也救不回来
            try:
                sw = int(self.win.winfo_screenwidth())
                sh = int(self.win.winfo_screenheight())
            except Exception:
                sw = sh = 0
            w = min(WIN_W, max(520, sw - 40)) if sw else WIN_W
            h = min(WIN_H, max(380, sh - 60)) if sh else WIN_H
            self.win.geometry(f"{w}x{h}")
            self.win.update_idletasks()
        except Exception:
            pass
        self._apply_round()

    # ---------- 布局辅助（v4.0 分组卡片） ----------
    CARD_BG = "#FFFFFF"  # 主题化：_build 时改为 self.P["CARD_BG"]

    def _make_scroll_page(self, parent):
        """可滚动的页面容器（访达式：窗口固定高，内容纵向滚动）。

        返回 (page, body)：page 参与 content 的 pack/切换，body 是给
        _columns/_section 放卡片的内容框。滚轮由 _on_wheel 统一处理。"""
        P = self.P
        page = tk.Frame(parent, bg=P["PAGE_BG"])
        cv = tk.Canvas(page, bg=P["PAGE_BG"], highlightthickness=0, bd=0)
        bar = tk.Scrollbar(page, orient="vertical", command=cv.yview, width=9,
                           bd=0, relief="flat", bg=P["BORDER"],
                           troughcolor=P["PAGE_BG"], activebackground=P["SUB"],
                           highlightthickness=0)
        cv.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        body = tk.Frame(cv, bg=P["PAGE_BG"], padx=20, pady=16)
        win_id = cv.create_window((0, 0), window=body, anchor="nw")

        def _sync(_e=None):
            try:
                cv.configure(scrollregion=cv.bbox("all") or (0, 0, 1, 1))
                # 内容宽度跟随视口：卡片才能撑满一列
                cv.itemconfigure(win_id, width=max(1, cv.winfo_width()))
            except Exception:
                pass

        body.bind("<Configure>", _sync)
        cv.bind("<Configure>", _sync)
        page._scroll_cv = cv
        self._scroll_pages.append(page)
        return page, body

    def _on_wheel(self, e):
        """滚轮只作用于指针下方的那个页面滚动区（bind_all 是全窗口级的，
        不加位置判断会连带影响横幅窗口的滚轮行为）。"""
        try:
            for page in self._scroll_pages:
                if not page.winfo_manager():
                    continue
                cv = page._scroll_cv
                x, y = cv.winfo_rootx(), cv.winfo_rooty()
                if x <= e.x_root < x + cv.winfo_width() and \
                        y <= e.y_root < y + cv.winfo_height():
                    cv.yview_scroll(int(-e.delta / 120), "units")
                    return
        except Exception:
            pass

    def _columns(self, page):
        """v4.4 访达式：单列分组卡片（窗口收窄后两列会把表单挤变形）。
        仍返回 (left, right) 两个名字——两者是同一个容器，卡片按调用顺序
        自上而下排成一列，四个页面的构建代码无需改动。"""
        wrap = tk.Frame(page, bg=self.P["PAGE_BG"])
        wrap.pack(fill="both", expand=True)
        return wrap, wrap

    def _section(self, page, title):
        """v4.4 分组卡片：标题移进卡片（标题 + 1px 分隔线 + 内容区），
        消除"标题浮在卡外、归属不清"的问题；卡片按列可用高度自动拉伸，
        两列不再一高一矮留大片灰底。返回内容区 Frame（调用方按旧写法塞行）。

        双层柔和阴影 + 12px 圆角 + 细边框沿用 v4.3.0。"""
        P = self.P
        wrap = tk.Frame(page, bg=P["PAGE_BG"])
        # 只有每列最后一张卡参与纵向拉伸：列间高度差由它吸收，
        # 既消除灰底空白，又不会把每张卡都撑出大片内部留白
        prev = self._col_last.get(id(page))
        if prev is not None:
            try:
                prev.pack_forget()
                prev.pack(fill="x", pady=(0, 14))
            except Exception:
                pass
        wrap.pack(fill="both", expand=True, pady=(0, 14))
        self._col_last[id(page)] = wrap
        cv = tk.Canvas(wrap, bg=P["PAGE_BG"], highlightthickness=0, height=40)
        cv.pack(fill="both", expand=True)
        outer = tk.Frame(cv, bg=self.CARD_BG)
        tk.Label(outer, text=title, bg=self.CARD_BG, fg=P["TEXT"],
                 font=(FONT, 11, "bold"), anchor="w"
                 ).pack(fill="x", padx=CARD_PADX, pady=(13, 8))
        tk.Frame(outer, bg=P["HEADER_LINE"], height=1).pack(fill="x")
        inner = tk.Frame(outer, bg=self.CARD_BG, padx=CARD_PADX, pady=6)
        inner.pack(fill="x", pady=(0, 6))
        inner._wrap_lbls = []       # 需要按卡片宽度换行的标签 (label, 额外让位像素)
        cv.create_window(3, 3, window=outer, anchor="nw")

        def _sync(_e=None):
            # 重入保护：Configure 事件在 mainloop 中排队，避免"改高→重绘→重排→再触发"死循环
            if _sync._busy:
                return
            _sync._busy = True
            try:
                W = cv.winfo_width()
                prev_h = int(cv.cget("height"))
                # 整宽说明文字的换行宽度跟着卡片实际宽度走：写死 HINT_FULL 时，窗口被
                # 屏幕夹紧（或高缩放下字体变大）就会让文字顶出右边界被裁。
                usable = W - 10 - 2 * CARD_PADX
                wrapped = False
                if usable > 80:
                    for lb, dx in getattr(inner, "_wrap_lbls", ()):
                        want = max(60, min(HINT_FULL - dx, usable - dx - 8))
                        # 再扣 8px：Label 自身 borderwidth/padx 各占 2px，且 Tk 对无空格
                        # 的中文是"越过限宽才断行"，不留余量仍会顶出 2~3px。
                        # 上限仍是 HINT_FULL：宽窗口保持原排版，只有卡片变窄才收缩
                        try:
                            if int(lb.cget("wraplength")) != want:
                                lb.configure(wraplength=want)
                                wrapped = True
                        except Exception:
                            pass
                if wrapped:
                    # 改过换行就必须先冲刷一次几何：Label 自己的请求高度是即时的，但
                    # inner/outer 这两级 frame 要等 canvas 的 idle 处理才冒泡新高度。
                    # 不冲的话下面量到的仍是旧高度，末行会被卡片底边切掉
                    try:
                        cv.update_idletasks()
                    except Exception:
                        pass
                need = outer.winfo_reqheight() + 6
                cv.configure(height=need)          # 请求高度=内容高度（决定页面总高）
                h = max(need, cv.winfo_height())   # 被列拉伸时按分配高度绘制
                cv.delete("all")
                if W > 12 and h > 8:
                    # v4.4: 单层柔和投影（向下偏 2px）+ 1px 同色系边框。
                    # 旧的双层假阴影在浅绿灰底上会糊成脏边
                    round_rect(cv, 2, 4, W - 3, h + 4, RAD_CARD,
                               fill=P["SHADOW2"], outline="")
                    round_rect(cv, 1, 1, W - 4, h + 1, RAD_CARD,
                               fill=self.CARD_BG, outline=P["BORDER"], width=1)
                    cv.create_window(3, 3, window=outer, anchor="nw", width=W - 10)
                if wrapped:
                    _sync._tries = 0        # 换行刚变，从这轮起重新计数
                if (wrapped or need != prev_h) and _sync._tries < 12:
                    # 换了 wraplength 就换了行数，但 Label 之外的 frame 链要等 pack 重排
                    # 才把新的请求高度冒泡上来：本轮量到的仍是旧高度，末行会被卡片底边
                    # 切掉。所以隔一帧再确认，直到高度稳定；12 次上限防死循环（实测 2~4
                    # 次收敛，一次约 16ms，用户看不出来）
                    _sync._tries += 1
                    cv.after(16, _sync)
            except Exception:
                pass
            finally:
                _sync._busy = False

        _sync._busy = False
        _sync._tries = 0
        cv.bind("<Configure>", _sync)
        self._sections.append((cv, outer, _sync))
        return inner

    def _dyn_wrap(self, card, lbl, dx=0):
        """登记整宽说明文字：换行宽度由所属卡片在重排时算（见 _section._sync）。
        dx 用于给同排的缩进/控件让位。"""
        try:
            card._wrap_lbls.append((lbl, dx))
        except Exception:
            pass
        return lbl

    def _row(self, card, label, widget=None, padx=(0, 0)):
        """卡片内一行：标签列固定 LBL_W 像素（不随字号/中英文宽度漂移），
        控件与提示文字都从这条基线开始对齐。"""
        P = self.P
        r = tk.Frame(card, bg=self.CARD_BG)
        r.pack(fill="x", pady=ROW_PADY)
        cell = tk.Frame(r, bg=self.CARD_BG, width=LBL_W, height=24)
        cell.pack(side="left")
        cell.pack_propagate(False)
        lb = tk.Label(cell, text=label, bg=self.CARD_BG, fg=P["TEXT"],
                      font=(FONT, 10), anchor="w")
        lb.pack(fill="both", expand=True)
        if widget is not None:
            widget.pack(side="left", padx=padx)
        return r, lb

    def _hint(self, card, text):
        P = self.P
        # 与 _row 的控件列对齐（标签列 150 + 控件左间距 8）
        lbl = tk.Label(card, text=text, bg=self.CARD_BG, fg=P["SUB"],
                       font=(FONT, 9), anchor="w", justify="left",
                       wraplength=HINT_W)
        # v4.5.0: 换行宽度交给卡片按实际宽度重算。写死 HINT_W=436 时，默认窗口下
        # "卡片内宽 - 控件列"只有 417px，每行行尾那一个字被卡片右边界切掉——
        # 实测「…窗口。最多 3 条」的 3 就是在这里丢的（截图核对时发现的）
        self._dyn_wrap(card, lbl, CTRL_X)
        lbl.pack(fill="x", padx=(CTRL_X, 0), pady=(0, ROW_PADY - 2))
        return lbl

    # ---------- 基础设置页 ----------
    def _build_basic(self, page):
        P = self.P
        CARD_BG = self.CARD_BG
        # v4.2.0: 两列横向布局——左列：常规/提醒行为；右列：提示音/提醒规则
        col_l, col_r = self._columns(page)

        # 常规
        card = self._section(col_l, "常规")
        self.var_autostart = tk.BooleanVar(value=config["auto_start"])
        RoundedCheckbox(card, "开机自动启动", self.var_autostart,
                        command=self._on_autostart).pack(anchor="w", pady=3)
        self.var_sound = tk.BooleanVar(value=config["sound"])
        RoundedCheckbox(card, "弹窗时播放提示音", self.var_sound,
                        command=self._save).pack(anchor="w", pady=3)
        self.var_skip = tk.BooleanVar(value=config["skip_if_wechat_active"])
        RoundedCheckbox(card, "微信在前台时不弹窗", self.var_skip,
                        command=self._save).pack(anchor="w", pady=3)
        self.var_biz = tk.BooleanVar(value=config["include_biz_messages"])
        RoundedCheckbox(card, "同时提醒公众号消息", self.var_biz,
                        command=self._save).pack(anchor="w", pady=3)
        self.var_wecom = tk.BooleanVar(value=config["wecom_on"])
        RoundedCheckbox(card, "同时提醒企业微信", self.var_wecom,
                        command=self._save).pack(anchor="w", pady=3)
        # v4.0.1: 界面主题（浅色/深色），切换即重建窗口
        r, _ = self._row(card, "界面主题")
        self.var_theme = tk.StringVar(value=config.get("theme", "light"))
        RoundedRadio(r, "浅色", "light", self.var_theme,
                     command=self._on_theme_change).pack(side="left", padx=(8, 0))
        RoundedRadio(r, "深色", "dark", self.var_theme,
                     command=self._on_theme_change).pack(side="left", padx=(8, 0))

        # 提示音（v4.0：内置音库 + 用户自定义 + 试听）
        card = self._section(col_r, "提示音")
        self.var_ssrc = tk.StringVar(value=config.get("sound_source", "default"))
        _opts = (("default", "系统默认音"), ("builtin1", "内置·叮咚"),
                 ("builtin2", "内置·清脆"), ("builtin3", "内置·柔和"),
                 ("builtin4", "内置·气泡"), ("custom", "自定义音频"))
        for i in range(0, len(_opts), 3):
            _orow = tk.Frame(card, bg=CARD_BG)
            _orow.pack(anchor="w", pady=3)
            for _val, _lab in _opts[i:i + 3]:
                RoundedRadio(_orow, _lab, _val, self.var_ssrc,
                             command=self._on_sound_src).pack(side="left", padx=(0, 14))
        # 自定义文件行（仅选「自定义」时显示）
        self.var_csound = tk.StringVar(value=config.get("custom_sound", ""))
        self.csound_card = tk.Frame(card, bg=CARD_BG)
        r, _lb = self._row(self.csound_card, "自定义文件")
        self.csound_lbl = tk.Label(r, text="", bg=CARD_BG, fg=self.P["DEEP"],
                                   font=(FONT, 9), anchor="w")
        self.csound_lbl.pack(side="left", padx=(8, 0))
        RoundedButton(r, "选择", self._pick_sound, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(4, 0))
        RoundedButton(r, "试听", self._preview_custom_sound, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(4, 0))
        RoundedButton(r, "清除", self._clear_sound, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(4, 0))
        self.csound_path_lbl = tk.Label(self.csound_card, text="", bg=CARD_BG,
                                        fg=self.P["SUB"], font=(FONT, 9), anchor="w",
                                        justify="left", wraplength=HINT_W)
        self.csound_path_lbl.pack(fill="x", padx=(CTRL_X, 0), pady=(2, 0))

        def _update_csound(*_a):
            raw = self.var_csound.get()
            if raw:
                self.csound_lbl.config(text=os.path.basename(raw) or raw, fg=self.P["DEEP"])
                self.csound_path_lbl.config(text=f"完整路径：{raw}")
            else:
                self.csound_lbl.config(text="（未选择，将回退到系统默认音）", fg=self.P["SUB"])
                self.csound_path_lbl.config(text="")
        self.var_csound.trace_add("write", _update_csound)
        _update_csound()
        # v4.2.0: 提示音音量（0-100，动态缩放 wav 样本）
        r, _ = self._row(card, "音量")
        self.var_vol = tk.StringVar(value=str(int(config.get("sound_volume", 70))))
        RoundedSpinbox(r, 0, 100, textvariable=self.var_vol, width=4,
                       on_change=self._save).pack(side="left", padx=(8, 0))
        tk.Label(r, text="0-100（0 静音）", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(6, 0))
        RoundedButton(r, "试听", self._preview_sound, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]).pack(side="left",
                                                                          padx=(10, 0))
        self._hint(card, "音量对三种来源都生效（系统默认音会先解析成它实际用的 wav 再按音量播放）；"
                         "切换来源时自动试听，改完音量可点「试听」再听一遍。")
        if config.get("sound_source", "default") == "custom":
            self.csound_card.pack(fill="x", pady=(2, 0))
        else:
            self.csound_card.pack_forget()

        # 提醒行为
        card = self._section(col_l, "提醒行为")
        r, _lb = self._row(card, "横幅显示时长(秒)")
        self.var_dur = tk.StringVar(value=str(int(config["popup_duration"])))
        RoundedSpinbox(r, 2, 20, textvariable=self.var_dur, width=5,
                       on_change=self._save).pack(side="left", padx=(8, 0))
        self.var_quiet = tk.BooleanVar(value=config["quiet_enabled"])
        RoundedCheckbox(card, "开启免打扰时段（不弹窗不响铃）", self.var_quiet,
                        command=self._toggle_quiet).pack(anchor="w", pady=3)
        self.qtime = tk.Frame(card, bg=CARD_BG)
        r2, _lb2 = self._row(self.qtime, "免打扰时段")
        tk.Label(r2, text="从", bg=CARD_BG, fg=self.P["TEXT"],
                 font=(FONT, 10)).pack(side="left", padx=(8, 0))
        self.var_qstart = tk.StringVar(value=config["quiet_start"])
        self.ent_qstart = RoundedEntry(r2, textvariable=self.var_qstart, width=5,
                                       on_commit=self._save)
        self.ent_qstart.pack(side="left", padx=(6, 6))
        tk.Label(r2, text="到", bg=CARD_BG, fg=self.P["TEXT"],
                 font=(FONT, 10)).pack(side="left")
        self.var_qend = tk.StringVar(value=config["quiet_end"])
        self.ent_qend = RoundedEntry(r2, textvariable=self.var_qend, width=5,
                                     on_commit=self._save)
        self.ent_qend.pack(side="left", padx=(6, 6))
        self.qtime.pack(fill="x")
        self._hint(card, "免打扰时段内不弹窗、不响铃；可跨零点，如 23:00-07:00")
        self._toggle_quiet(save=False)

        # 提醒规则（v4.2.0：关键词/联系人过滤 + 软件内屏蔽列表）
        card = self._section(col_r, "提醒规则")
        r, _ = self._row(card, "提醒模式")
        self.var_bmode = tk.StringVar(value=config.get("banner_mode", "all"))
        RoundedRadio(r, "全部提醒", "all", self.var_bmode,
                     command=self._save).pack(side="left", padx=(8, 0))
        RoundedRadio(r, "按规则提醒", "filtered", self.var_bmode,
                     command=self._save).pack(side="left", padx=(8, 0))
        r, _ = self._row(card, "仅提醒关键词")
        self.var_alert_kw = tk.StringVar(value=config.get("alert_keywords", ""))
        RoundedEntry(r, textvariable=self.var_alert_kw, width=30, justify="left",
                     placeholder="如：发货,回款（英文逗号分隔）",
                     on_commit=self._save).pack(side="left", padx=(8, 0))
        r, _ = self._row(card, "仅提醒联系人/群")
        self.var_alert_ct = tk.StringVar(value=config.get("alert_contacts", ""))
        RoundedEntry(r, textvariable=self.var_alert_ct, width=30, justify="left",
                     placeholder="如：张三,客户群A",
                     on_commit=self._save).pack(side="left", padx=(8, 0))
        r, _ = self._row(card, "屏蔽关键词")
        self.var_mute_kw = tk.StringVar(value=config.get("mute_keywords", ""))
        RoundedEntry(r, textvariable=self.var_mute_kw, width=30, justify="left",
                     placeholder="命中这些词的消息不提醒",
                     on_commit=self._save).pack(side="left", padx=(8, 0))
        r, _ = self._row(card, "屏蔽联系人/群")
        self.var_mute_ct = tk.StringVar(value=config.get("mute_contacts", ""))
        RoundedEntry(r, textvariable=self.var_mute_ct, width=30, justify="left",
                     placeholder="这些联系人/群的消息不提醒",
                     on_commit=self._save).pack(side="left", padx=(8, 0))
        self._hint(card, "规则用英文逗号分隔多个值。关键词匹配消息内容，联系人/群匹配昵称。"
                         "「全部提醒」模式下屏蔽列表仍生效；「按规则提醒」时，仅命中"
                         "「仅提醒」关键词或联系人/群的消息才会弹窗。屏蔽优先于仅提醒。")

        # 快捷回复（v4.5.0：话术库——私聊横幅下方成排话术，点一条只复制不发送）
        card = self._section(col_r, "快捷回复")
        self.var_qr_on = tk.BooleanVar(value=bool(config.get("quick_reply_on", True)))
        RoundedCheckbox(card, "横幅下方显示话术按钮（群聊仅 @我的消息）", self.var_qr_on,
                        command=self._save_quick_replies).pack(anchor="w", pady=3)
        _qrs = list(config.get("quick_replies") or [])
        _qrs += [""] * (QR_MAX - len(_qrs))
        self.var_qr = []
        for _i in range(QR_MAX):
            r, _ = self._row(card, f"话术 {_i + 1}")
            _var = tk.StringVar(value=_qrs[_i])
            RoundedEntry(r, textvariable=_var, width=30, justify="left",
                         placeholder="留空则这条不显示",
                         on_commit=self._save_quick_replies).pack(side="left", padx=(8, 0))
            self.var_qr.append(_var)
        self._hint(card, f"私聊消息弹横幅时，横幅下方会出现这几条话术；群聊里只有 @你 的消息才出现"
                         f"（@别人、没人 @ 都不弹，换账号后按新账号判定）。点一条就把它复制进剪贴板，"
                         f"切到微信按 Ctrl+V 即可发出——软件不会替你发消息。"
                         f"最多 {QR_MAX} 条、每条最长 {QR_LEN} 字。")
        # v4.5.5：跳会话 / 填话术两个实验开关合并成一个「自动回复」
        self.var_auto_reply = tk.BooleanVar(value=bool(config.get("auto_reply", False)))
        RoundedCheckbox(card, "自动回复", self.var_auto_reply,
                        command=self._save_auto_reply).pack(anchor="w", pady=(6, 3))
        self._hint(card, "开启后只对私聊生效：点横幅会跳到该联系人的会话；点一条话术会"
                         "先跳到那个人、核对确实打开了他的会话，再把这句话逐字打进输入框——"
                         "到输入框为止，绝不替你按回车发送。群聊一律只复制、不驱动微信"
                         "（搜群名容易跳错）。核对不上（比如你已经在手机上读过、没有未读"
                         "可以核对）就一个字都不打，话术留在剪贴板；字没落进输入框会逐字"
                         "退格擦掉；你正在敲键盘/挪鼠标时它不动微信；真填进去了，剪贴板会"
                         "还成你原来抄的那段文本（图片/文件还不了）。同一时刻只允许一条"
                         "链路动微信；配额每分钟 "
                         f"{WX_DRV_PER_MIN} 次、每小时 {WX_DRV_PER_HOUR} 次。高级设置里的"
                         "驱动总闸一关，连抢前台都不做，话术照旧进剪贴板。")

    # ---------- 提示音（v4.0） ----------
    def _play_current_sound(self, src=None):
        try:
            play_notify_sound(src)
        except Exception as e:
            log(f"[提示音] 试听失败: {e}")

    def _preview_sound(self):
        """按当前来源 + 当前音量试听"""
        self._play_current_sound()

    def _preview_custom_sound(self):
        self._play_current_sound("custom")

    def _on_sound_src(self):
        src = self.var_ssrc.get()
        config["sound_source"] = src
        if src == "custom":
            self.csound_card.pack(fill="x", pady=(2, 0))
        else:
            self.csound_card.pack_forget()
        self._save()
        self._play_current_sound(src)

    # ---------- 横幅美化页 ----------
    def _build_beauty(self, page):
        P = self.P
        CARD_BG = self.CARD_BG
        # v4.2.0: 两列横向布局——左列：显示/样式；右列：配色
        col_l, col_r = self._columns(page)

        # 显示
        card = self._section(col_l, "显示")
        r, _ = self._row(card, "水平位置")
        self.var_pos = tk.StringVar(value=config.get("banner_pos", "center"))
        for val, lab in (("center", "居中"), ("left", "居左"), ("right", "居右")):
            RoundedRadio(r, lab, val, self.var_pos,
                         command=self._apply_banner).pack(side="left", padx=(8, 0))
        # v4.1.0: 垂直位置（社区反馈：可设置横幅显示位置顶部/中间/底部）
        r, _ = self._row(card, "垂直位置")
        # v4.6.2: 自定义坐标模式下不显示垂直预设的选中态（纵坐标此时由 Y 决定）
        self.var_bvertical = tk.StringVar(
            value="" if config.get("banner_pos") == "custom"
            else config.get("banner_vertical", "top"))
        for val, lab in (("top", "顶部"), ("center", "中部"), ("bottom", "底部")):
            RoundedRadio(r, lab, val, self.var_bvertical,
                         command=self._on_vertical).pack(side="left", padx=(8, 0))
        # v4.6.2: 自定义位置——直接填横幅左上角的屏幕像素坐标
        r, _ = self._row(card, "自定义坐标")
        self.var_px = tk.StringVar(value=str(int(config.get("banner_pos_x", 40))))
        RoundedSpinbox(r, 0, 9999, textvariable=self.var_px, width=5,
                       on_change=self._on_pos_xy).pack(side="left", padx=(8, 0))
        tk.Label(r, text=" X", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(2, 10))
        self.var_py = tk.StringVar(value=str(int(config.get("banner_pos_y", 40))))
        RoundedSpinbox(r, 0, 9999, textvariable=self.var_py, width=5,
                       on_change=self._on_pos_xy).pack(side="left", padx=(2, 0))
        tk.Label(r, text=" Y", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(2, 0))
        self._hint(card, "X / Y = 横幅左上角的屏幕像素坐标（副屏可直接填副屏坐标，"
                         "如副屏从 2560 起就填 2600）；填了坐标即以坐标为准，"
                         "点上面「居中/居左/居右」可切回预设位置")

        # 配色
        card = self._section(col_r, "配色")
        r, _ = self._row(card, "配色主题")
        # v4.1.0: 修复命名冲突——原 var_theme 同时被"界面主题"（基础设置页）
        # 与"横幅配色主题"（本页）使用，后者覆盖前者导致界面主题切换后
        # 读取到横幅配色值、永远切不回浅色（社区反馈）。本页改用 var_btheme。
        self.var_btheme = tk.StringVar(value=config.get("banner_theme", "dark"))
        # v4.2.0: 6 个主题分两行（每行 3 个）——pack 不换行，单行放不下会被裁切
        _bthemes = (("dark", "深色·绿"), ("pink", "粉色"), ("light", "浅色"),
                    ("wx", "微信绿"), ("ios", "手机通知"), ("custom", "自定义"))
        _bcol = tk.Frame(r, bg=self.CARD_BG)
        _bcol.pack(side="left", padx=(8, 0))
        for i in range(0, len(_bthemes), 3):
            _bline = tk.Frame(_bcol, bg=self.CARD_BG)
            _bline.pack(anchor="w")
            for val, lab in _bthemes[i:i + 3]:
                RoundedRadio(_bline, lab, val, self.var_btheme,
                             command=self._apply_banner).pack(side="left", padx=(0, 12))
        r, _ = self._row(card, "自定义颜色")
        RoundedButton(r, "背景色", lambda: self._pick_custom_color("bg"),
                      P["BTN_QUIT"], P["BTN_QUIT_FG"], hover=P["NAV_HOVER"],
                      edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(8, 0))
        RoundedButton(r, "标题色", lambda: self._pick_custom_color("title"),
                      P["BTN_QUIT"], P["BTN_QUIT_FG"], hover=P["NAV_HOVER"],
                      edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(4, 0))
        RoundedButton(r, "文字色", lambda: self._pick_custom_color("text"),
                      P["BTN_QUIT"], P["BTN_QUIT_FG"], hover=P["NAV_HOVER"],
                      edge=P["BTN_QUIT_EDGE"]).pack(side="left", padx=(4, 0))
        self._hint(card, "选「自定义」主题后生效，三种颜色可自由组合")

        # 样式
        card = self._section(col_l, "样式")
        r, _ = self._row(card, "圆角弧度")
        self.var_radius = tk.StringVar(value=str(int(config.get("banner_radius", 18))))
        RoundedSpinbox(r, 0, 40, textvariable=self.var_radius, width=4,
                       on_change=self._apply_banner).pack(side="left", padx=(8, 0))
        tk.Label(r, text="0 = 直角", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(6, 0))
        r, _ = self._row(card, "横幅标题")
        self.var_btitle = tk.StringVar(value=config.get("banner_title", "微信 · 新消息"))
        RoundedEntry(r, textvariable=self.var_btitle, width=16, justify="left",
                     placeholder="读不到消息内容时显示的标题",
                     on_commit=self._apply_banner).pack(side="left", padx=(8, 0))
        # v4.1.0: 内容字号 / 不透明度（社区反馈：弹窗大小和字体大小可调、背景可透明）
        r, _ = self._row(card, "内容字号")
        self.var_bfont = tk.StringVar(value=str(int(config.get("banner_font_size", 11))))
        RoundedSpinbox(r, 9, 16, textvariable=self.var_bfont, width=4,
                       on_change=self._apply_banner).pack(side="left", padx=(8, 0))
        tk.Label(r, text="9-16", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(6, 0))
        r, _ = self._row(card, "不透明度")
        self.var_bopacity = tk.StringVar(value=str(int(float(config.get("banner_opacity", 1.0)) * 100)))
        RoundedSpinbox(r, 30, 100, textvariable=self.var_bopacity, width=4,
                       on_change=self._apply_banner).pack(side="left", padx=(8, 0))
        tk.Label(r, text="30%-100%", bg=CARD_BG, fg=self.P["SUB"],
                 font=(FONT, 9)).pack(side="left", padx=(6, 0))

    # ---------- 高级设置页（密钥管理） ----------
    def _build_advanced(self, page):
        P = self.P
        CARD_BG = self.CARD_BG
        ACCENT = P["ACCENT"]
        # v4.2.0: 两列横向布局——左列：增强说明/数据目录；右列：账号密钥/工具
        col_l, col_r = self._columns(page)

        card = self._section(col_l, "消息内容增强")
        self._dyn_wrap(card, tk.Label(
            card, text="配置数据库密钥后，横幅可显示发件人昵称和消息具体内容。",
            bg=CARD_BG, fg=self.P["SUB"], font=(FONT, 9), justify="left",
            wraplength=HINT_FULL)).pack(anchor="w", pady=2)

        # 数据目录（卡片标题已说明是什么，行内不再重复"数据目录"标签；
        # 路径独占一行——与按钮同排时会被按钮挤到截断而不是换行）
        card = self._section(col_l, "数据目录")
        self.adv_dataroot_lbl = self._dyn_wrap(card, tk.Label(
            card, text=config["data_root"], bg=CARD_BG,
            fg=self.P["DEEP"], font=(FONT, 9), anchor="w",
            justify="left", wraplength=HINT_FULL))
        self.adv_dataroot_lbl.pack(fill="x", pady=(2, 6))
        r = tk.Frame(card, bg=CARD_BG)
        r.pack(fill="x")
        RoundedButton(r, "选择", self._pick_dataroot, ACCENT, "#FFFFFF",
                      hover=P["ACCENT_D"]).pack(side="right")
        # v4.0.1-3: 自动扫描——一键从注册表/文档/常见位置/各盘符自动定位，方便不懂技术的朋友
        RoundedButton(r, "自动扫描", self._auto_scan_dataroot, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"]).pack(side="right", padx=(0, 6))
        self._dyn_wrap(card, tk.Label(
            card, text="选哪里：微信文档目录 = xwechat_files 所在的那一层文件夹\n"
                       "（例如 D:\\WeChatFiles 或 F:\\Documents，其下能看到 wxid_xxx 账号文件夹）。\n"
                       "微信装在 D 盘、文档重定向到 F 盘等自动检测不到账号时，手动指定这里。",
            bg=CARD_BG, fg=self.P["SUB"], font=(FONT, 9), justify="left",
            wraplength=HINT_FULL)).pack(anchor="w", pady=(4, 2))

        # 账号与密钥
        card = self._section(col_r, "账号与密钥")
        account = _find_active_account(config["data_root"])
        account_short = os.path.basename(account) if account else ""
        r, _ = self._row(card, "当前账号")
        self.adv_account_lbl = tk.Label(r, text=account_short or "未检测到", bg=CARD_BG,
                                        fg=self.P["DEEP"], font=(FONT, 10, "bold"))
        self.adv_account_lbl.pack(side="left", padx=(8, 0))
        key_status = "未配置"
        key_color = self.P["SUB"]
        if account:
            key_path = os.path.join(APP_DIR, f"wx_key_{os.path.basename(account)}.txt")
            if os.path.exists(key_path):
                key_status = "已配置 ✓"
                key_color = P["DEEP"]
            elif os.path.exists(os.path.join(APP_DIR, "wx_key.txt")):
                key_status = "已配置（通用）✓"
                key_color = P["DEEP"]
        r, _ = self._row(card, "密钥状态")
        self.adv_key_status_lbl = tk.Label(r, text=key_status, bg=CARD_BG,
                                           fg=key_color, font=(FONT, 10, "bold"))
        self.adv_key_status_lbl.pack(side="left", padx=(8, 0))
        r, _ = self._row(card, "手动输入密钥")
        self.var_adv_key = tk.StringVar()
        self.adv_key_entry = RoundedEntry(r, textvariable=self.var_adv_key,
                                          width=8, justify="left", show="•",
                                          placeholder="粘贴 64 位密钥或 wx_key 内容")
        self.adv_key_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        RoundedButton(r, "保存密钥", self._save_manual_key, ACCENT, "#FFFFFF",
                      hover=P["ACCENT_D"]).pack(side="left")

        # 密钥提取（v4.4：内嵌进设置窗口，不再另开「微信密钥提取工具」独立进程）
        card = self._section(col_r, "密钥提取")
        self._ext_steps = []
        row = tk.Frame(card, bg=CARD_BG)
        row.pack(fill="x", pady=(4, 10))
        for i, name in enumerate(("关闭微信", "启动微信", "注入 Hook", "捕获密钥")):
            cell = tk.Frame(row, bg=CARD_BG)
            cell.pack(side="left", expand=True, fill="x")
            cv = tk.Canvas(cell, width=46, height=20, bg=CARD_BG, highlightthickness=0)
            cv.pack(anchor="center")
            lab = tk.Label(cell, text=name, bg=CARD_BG, fg=P["SUB"], font=(FONT, 8))
            lab.pack(anchor="center")
            self._ext_steps.append((cv, lab))
            self._set_step(i, "pending")
        self.ext_log = tk.Text(card, height=7, width=1, font=("Consolas", 9), wrap="word",
                               bg=P["INPUT_BG"], fg=P["TEXT"], relief="flat", bd=0,
                               highlightthickness=1, highlightbackground=P["BORDER"],
                               insertbackground=P["ACCENT"], padx=8, pady=6,
                               state="disabled")
        self.ext_log.pack(fill="x")

        def _log_wheel(e):
            # 滚轮经过日志框时交给页面滚动区（返回 "break" 挡掉 Text 自己的滚动，
            # 否则日志和页面会各滚一次）
            self._on_wheel(e)
            return "break"

        self.ext_log.bind("<MouseWheel>", _log_wheel)
        r = tk.Frame(card, bg=CARD_BG)
        r.pack(fill="x", pady=(10, 0))
        self.ext_start = RoundedButton(r, "开始提取", self._start_key_extract,
                                       ACCENT, "#FFFFFF", hover=P["ACCENT_D"],
                                       bold=True, height=30)
        self.ext_start.pack(side="left")
        self.ext_cancel = RoundedButton(r, "取消", self._cancel_key_extract,
                                        P["BTN_QUIT"], P["BTN_QUIT_FG"],
                                        hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"],
                                        height=30)
        self.ext_cancel.pack(side="left", padx=(8, 0))
        RoundedButton(r, "清除密钥", self._clear_key, P["BTN_QUIT"], P["BTN_QUIT_FG"],
                      hover=P["NAV_HOVER"], edge=P["BTN_QUIT_EDGE"],
                      height=30).pack(side="left", padx=(8, 0))
        if _HAS_KEY_ENGINE:
            self.ext_cancel.set_enabled(bool(self._extractor))
            self.ext_start.set_enabled(not self._extractor)
            self._ext_append("提取进行中……（切主题/关窗口不会中断，但完成前请勿重复开始）"
                             if self._extractor else
                             "准备就绪：点击「开始提取」，然后在微信登录框扫码。")
        else:
            self.ext_start.set_enabled(False)
            self._ext_append("提取引擎缺失（wx_key_engine.py / wx_key.dll），"
                             "请改用上方「手动输入密钥」。")
        self._dyn_wrap(card, tk.Label(
            card, text="提取会关闭并重启微信，需要在弹出的二维码上重新扫码登录；\n"
                       "密钥与微信账号绑定，换账号要重新提取。",
            bg=CARD_BG, fg=self.P["SUB"], font=(FONT, 9), justify="left",
            wraplength=HINT_FULL)).pack(anchor="w", pady=(6, 2))

        # v4.5.5：界面驱动自检——先测"能不能驱动微信界面"，再决定要不要开自动回复
        card = self._section(col_r, "界面驱动自检")
        # v4.5.2：驱动总闸 + 频率闸计数
        self.var_drive_on = tk.BooleanVar(value=bool(config.get("drive_on", True)))
        RoundedCheckbox(card, "允许软件动微信界面（总闸：出问题先关这一格）",
                        self.var_drive_on,
                        command=self._save_drive_on).pack(anchor="w", pady=(0, 4))
        r = tk.Frame(card, bg=CARD_BG)
        r.pack(fill="x", pady=4)
        self.drive_run = RoundedButton(r, "测一次能不能驱动微信界面", self._selftest_drive,
                                       ACCENT, "#FFFFFF", hover=P["ACCENT_D"])
        self.drive_run.pack(side="left")
        self.drive_lbl = self._dyn_wrap(card, tk.Label(
            card, text="还没跑过。", bg=CARD_BG, fg=self.P["SUB"], font=(FONT, 9),
            justify="left", wraplength=HINT_FULL))
        self.drive_lbl.pack(anchor="w", pady=(4, 2))
        self.drive_stat = self._dyn_wrap(card, tk.Label(
            card, text=wx_drive_stat_text(), bg=CARD_BG, fg=self.P["SUB"], font=(FONT, 9),
            justify="left", wraplength=HINT_FULL))
        self.drive_stat.pack(anchor="w", pady=(2, 0))
        self._hint(card, "自检只做三件事：把微信带到前台、点左上角搜索框、往框里打几个字，"
                         "然后用微信自己的画面核对每一步是否真的发生了；跑完会把搜索框清空再"
                         "关掉，不会按回车、不会发消息、不会打开任何聊天。开着的那几秒请不要"
                         "动鼠标键盘。测出来不通，上面那个「自动回复」在这台机器上就不会生效。\n"
                         "限流（v4.5.2）：跳会话、跳+填、自检这几条路共用一套配额——两次"
                         f"之间至少隔 {WX_DRV_MIN_GAP} 秒，每分钟最多 {WX_DRV_PER_MIN} 次，"
                         f"每小时最多 {WX_DRV_PER_HOUR} 次；配额用满就只复制进剪贴板、"
                         "一个字都不发。点一次话术算 1 次驱动：跳和填已经合成一条链子，"
                         "只抢一次前台、只等一次画面稳定，微信被占住的时间也比两轮短。"
                         "上面那格总闸是「一步都不动微信」，关掉后连抢前台都没有，"
                         "上面的计数会跟着变。")

    def _selftest_drive(self):
        """整条链子要几秒，放工作线程跑，结果用 after 编组回主线程显示。"""
        if getattr(self, "_drive_busy", False):
            return
        self._drive_busy = True
        try:
            self.drive_run.set_enabled(False)
        except Exception:
            pass
        self.drive_lbl.config(text="正在自检：抢前台 → 点搜索框 → 打字 → 核对面板画面……"
                                   "（期间请不要动鼠标键盘）")

        def _work():
            try:
                r = wx_drive_selftest()
            except Exception as e:
                r = {"step": "exception", "why": str(e), "ok": False}
            log(f"[自检] 结果 {r}")
            try:
                self.win.after(0, lambda: self._show_drive_result(r))
            except Exception:
                pass
        threading.Thread(target=_work, daemon=True).start()

    def _show_drive_result(self, r):
        self._drive_busy = False
        try:
            self.drive_run.set_enabled(True)
        except Exception:
            pass
        try:
            self.drive_stat.config(text=wx_drive_stat_text())
        except Exception:
            pass
        if r.get("ok"):
            txt = ("通过：点得中微信的搜索框、字也打得进去，抓屏能看见结果 —— "
                   "这台机器上「点横幅跳到会话」具备生效条件（会不会跳错仍要看未读核对）")
            color = self.P["DEEP"]
        else:
            step = r.get("step")
            if step in ("drive-off", "rate-gap", "rate-min", "rate-hour"):
                txt = "没动手：" + wx_rate_why(step, r.get("wait") or 0.0)
            else:
                txt = {
                    "no-front": "没抢到微信前台，已停手（什么都没输入）",
                    "lost-front": "中途微信的前台被别的窗口抢走了，已停手（此后一个键都没发）",
                    "wx-gone": "微信窗口中途不见了（崩了/被关了），已停手（此后一个键都没发）",
                    "busy": "上一条操作还在驱动微信，先不跟它抢——等一两秒再测",
                    "no-child": "找不到微信的自绘子窗口，已停手（什么都没输入）",
                    "panel-stuck": "微信上一次搜索的下拉面板关不掉，已停手（没点、没输入）——"
                                   "手动点一下微信再 Esc，或重开微信",
                    "click-fail": "合成点击失败：" + str(r.get("why", "")),
                    "no-panel": "点了搜索框但下拉没出现：这台机器上合成点击进不了微信，"
                                "已按 Esc 收手（什么都没输入）",
                    "type-fail": "键盘注入失败：" + str(r.get("why", "")),
                    "text-not-accepted": "下拉出现了但字没打进去：已清框并关闭，不会回车",
                    "exception": "自检异常：" + str(r.get("why", "")),
                }.get(step, "未通过（步骤 " + str(step) + "）")
            color = "#e5484d"
        try:
            self.drive_lbl.config(text=txt, fg=color)
        except Exception:
            pass

    def _refresh_advanced_status(self):
        """刷新高级设置页面的密钥状态"""
        if not hasattr(self, 'adv_key_status_lbl'):
            return
        account = _find_active_account(config["data_root"])
        if account:
            # v4.0.1: 显示账号 wxid（目录名），避免长路径截断
            self.adv_account_lbl.config(text=os.path.basename(account))
            key_path = os.path.join(APP_DIR, f"wx_key_{os.path.basename(account)}.txt")
            if os.path.exists(key_path):
                self.adv_key_status_lbl.config(text="已配置 ✓", fg=self.P["DEEP"])
            elif os.path.exists(os.path.join(APP_DIR, "wx_key.txt")):
                self.adv_key_status_lbl.config(text="已配置（通用）✓", fg=self.P["DEEP"])
            else:
                self.adv_key_status_lbl.config(text="未配置", fg=self.P["SUB"])
        else:
            self.adv_account_lbl.config(text="未检测到")
            self.adv_key_status_lbl.config(text="未配置", fg=self.P["SUB"])

    def _save_manual_key(self):
        """保存手动输入的密钥。
        v4.1.0: 兼容两种输入——64 位明文 hex，或直接从 wx_key*.txt 复制的
        加密内容（社区反馈：从密钥文件复制粘贴提示"格式错误"——文件里存的是
        加密内容而非明文，需先解密校验再保存）。
        v4.4.0: 密钥文件已改成便携格式 WXKEY3，这里不能再只认 WXKEY1，
        否则"从自己程序生成的文件里复制"这条最常见的路径反而解不开。"""
        key = self.var_adv_key.get().strip()
        if key.upper().startswith("WXKEY"):
            dec = _decrypt_key(key)
            if not dec or len(dec) != 64 or not all(c in "0123456789abcdefABCDEF" for c in dec):
                messagebox.showwarning("格式错误", "粘贴的密钥内容无法识别，请确认复制自 wx_key*.txt 文件（而非其它文件）。")
                return
            key = dec
        if len(key) != 64 or not all(c in "0123456789abcdefABCDEF" for c in key):
            messagebox.showwarning("格式错误", "密钥应为 64 位十六进制字符串。")
            return
        try:
            path = self._store_key(key)
        except Exception as e:
            messagebox.showerror("保存失败", f"密钥保存失败：{e}")
            return
        self.var_adv_key.set("")
        self._refresh_advanced_status()
        messagebox.showinfo("成功", f"密钥已保存！\n\n重启程序后启用消息内容显示。\n保存路径：{path}")

    def _store_key(self, key):
        """把 64 位密钥按当前账号落盘：wx_key_<wxid>.txt + 通用 wx_key.txt。
        先写 .tmp 再 rename——崩溃时不会留下半截密钥文件。返回账号文件名（无账号时返回通用文件）。"""
        account = _find_active_account(config["data_root"])
        common = os.path.join(APP_DIR, "wx_key.txt")
        path = (os.path.join(APP_DIR, f"wx_key_{os.path.basename(account)}.txt")
                if account else common)
        enc = _encrypt_key(key)
        for p in ([path, common] if account else [path]):
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(enc)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        return path

    # ---------- 内嵌密钥提取（v4.4：不再另开独立工具进程） ----------
    def _set_step(self, i, state):
        """步骤指示：pending 灰圈数字 / active 绿圈数字 / done 深绿圈白勾。"""
        P = self.P
        cv, lab = self._ext_steps[i]
        cv.delete("all")
        pending = state == "pending"
        fill = P["CARD_BG"] if pending else (P["ACCENT"] if state == "active" else P["DEEP"])
        cv.create_oval(15, 2, 31, 18, fill=fill,
                       outline=P["BORDER"] if pending else "", width=2)
        if state == "done":
            cv.create_line(19, 10, 22, 13, fill="#FFFFFF", width=2)
            cv.create_line(22, 13, 27, 7, fill="#FFFFFF", width=2)
        else:
            cv.create_text(23, 10, text=str(i + 1),
                           fill=P["PH"] if pending else "#FFFFFF",
                           font=(FONT, 8, "bold"))
        lab.configure(fg=P["PH"] if pending else P["DEEP"])

    def _ext_log(self, msg):
        """引擎线程回调：切回 Tk 主线程再动界面。"""
        try:
            self.win.after(0, self._ext_append, msg)
        except Exception:
            pass

    def _ext_append(self, msg):
        try:
            self.ext_log.configure(state="normal")
            self.ext_log.insert("end", msg + "\n")
            self.ext_log.see("end")
            self.ext_log.configure(state="disabled")
        except Exception:
            return
        # 日志里的 [n/4] 表示进入第 n 步（之前的步骤补勾）；出现"成功"即末步完成
        for i in range(4):
            if f"[{i + 1}/4]" in msg:
                for k in range(i):
                    self._set_step(k, "done")
                self._set_step(i, "active")
        if "成功" in msg:
            self._set_step(3, "done")

    def _start_key_extract(self):
        if not _HAS_KEY_ENGINE:
            messagebox.showwarning("密钥提取",
                                   f"提取引擎缺失：找不到 {os.path.join(DLL_DIR, 'wx_key.dll')}。")
            return
        if self._extractor:
            messagebox.showinfo("密钥提取", "上一次提取还在进行中，请先等它结束或点「取消」。")
            return
        ok, why = preflight()
        if not ok:
            # 依赖不全时**先说、再动手**——原来会先把微信杀掉再发现 DLL 不在，
            # 用户白被关一次客户端还要重新登录
            self._ext_append("[错误] " + why)
            messagebox.showerror("密钥提取", why + "\n\n可改用上方「手动输入密钥」。")
            return
        if not messagebox.askyesno(
                "密钥提取",
                "提取会关闭并重启微信，你需要在重新弹出的二维码上扫码登录。\n\n现在继续吗？"):
            return
        log("[设置] 开始密钥提取（内嵌）")
        for i in range(4):
            self._set_step(i, "pending")
        try:
            self.ext_log.configure(state="normal")
            self.ext_log.delete("1.0", "end")
            self.ext_log.configure(state="disabled")
        except Exception:
            pass
        self.ext_start.set_enabled(False)
        self.ext_cancel.set_enabled(True)
        self._extractor = KeyExtractor(self._ext_log, self._ext_done)
        threading.Thread(target=self._extractor.run, daemon=True).start()

    def _cancel_key_extract(self):
        try:
            if self._extractor:
                self._extractor.stop()
        except Exception:
            pass
        self._extractor = None
        log("[设置] 密钥提取已取消")
        self._ext_log("已取消")
        self.ext_start.set_enabled(True)
        self.ext_cancel.set_enabled(False)

    def _ext_done(self, success, key):
        """引擎完成回调（后台线程）→ 回主线程保存密钥并刷新状态。"""
        try:
            self.win.after(0, self._on_extract_done, bool(success), key)
        except Exception:
            pass

    def _on_extract_done(self, success, key):
        self._extractor = None
        self.ext_start.set_enabled(True)
        self.ext_cancel.set_enabled(False)
        if not success or not key:
            log("[设置] 密钥提取失败")
            self._ext_append("提取失败：可重试，或改用上方「手动输入密钥」。")
            messagebox.showerror("密钥提取", "提取失败。请重试，或改用手动输入密钥。")
            return
        try:
            path = self._store_key(key)
        except Exception as e:
            self._ext_append(f"密钥保存失败：{e}")
            messagebox.showerror("保存失败",
                                 f"密钥已提取但保存失败：{e}\n\n"
                                 f"可手动复制到输入框：\n{key}")
            return
        self._ext_append(f"密钥已保存：{path}")
        try:
            self.var_adv_key.set(key)  # 回填明文，可直接复制或修改
        except Exception:
            pass
        self._refresh_advanced_status()
        log("[设置] 密钥提取成功，已保存并回填")
        messagebox.showinfo(
            "密钥提取",
            f"提取成功，已保存到：\n{path}\n\n"
            f"密钥前 8 位：{key[:8]}…（完整密钥已回填到上方输入框，需要复制请从那里取）\n\n"
            "重启微信提醒后即可显示发件人和消息内容。")
    def _clear_key(self):
        """清除当前账号的密钥"""
        account = _find_active_account(config["data_root"])
        paths = []
        if account:
            paths.append(os.path.join(APP_DIR, f"wx_key_{os.path.basename(account)}.txt"))
        paths.append(os.path.join(APP_DIR, "wx_key.txt"))
        removed = 0
        for p in paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed += 1
                except Exception:
                    pass
        # v4.0.1-7: 同步清空整个解密缓存目录（含历史残留子目录），
        # 否则清除密钥后仍能读到已解密的消息内容
        try:
            cache_root = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                      "微信提醒", "decrypted_cache")
            if os.path.isdir(cache_root):
                import shutil
                shutil.rmtree(cache_root, ignore_errors=True)
                removed += 1
        except Exception:
            pass
        self._refresh_advanced_status()
        if removed > 0:
            messagebox.showinfo("已清除", f"已清除 {removed} 个密钥/缓存项。\n重启程序后回退到普通模式。")
        else:
            messagebox.showinfo("无密钥", "当前没有已配置的密钥。")

    # ---------- 关于页 ----------
    # ---------- 关于我们 ----------
    def _about_card(self, card, lines):
        """一段一行、自动换行的说明文字（关于我们页专用）"""
        P = self.P
        for i, (text, fg_key, bold) in enumerate(lines):
            lb = self._dyn_wrap(card, tk.Label(
                card, text=text, bg=self.CARD_BG, fg=P[fg_key],
                font=(FONT, 9, "bold" if bold else "normal"), anchor="w",
                justify="left", wraplength=HINT_FULL))
            lb.pack(fill="x", pady=(2 if i == 0 else 4, 2))

    def _build_about(self, page):
        P = self.P
        CARD_BG = self.CARD_BG
        col, _ = self._columns(page)

        card = self._section(col, "应用简介")
        tk.Label(card, text=f"微信来消息提醒 v{VERSION}", bg=CARD_BG, fg=self.P["DEEP"],
                 font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 4))
        self._about_card(card, [
            ("收到微信新消息时在屏幕弹出横幅提醒；点击横幅打开微信，右上角 — 隐藏横幅。"
             "支持显示发件人昵称与消息内容、提示音、免打扰时段、关键词与联系人过滤规则。",
             "TEXT", False),
        ])

        card = self._section(col, "非官方声明")
        self._about_card(card, [
            ("本软件是第三方独立开发的免费小工具，与腾讯公司以及\u201c微信 / WeChat\u201d产品"
             "之间不存在任何隶属、授权、赞助、认证或背书关系，也不是腾讯公司的官方或衍生产品。",
             "TEXT", False),
            ("\u201c微信\u201d\u201cWeChat\u201d及相关名称、标识、图标的全部权利归深圳市腾讯计算机系统"
             "有限公司所有。本软件仅在说明用途时以文字方式提及这些商标，不构成授权或品牌使用。",
             "SUB", False),
        ])

        card = self._section(col, "数据与隐私")
        self._about_card(card, [
            ("只读取本机当前登录账号自己的微信本地数据库，用于在本机弹出提醒；"
             "解密结果仅缓存在本机目录，不写入微信数据目录。", "TEXT", False),
            ("不联网上传、不转发、不统计任何聊天内容与联系人信息；数据库密钥仅保存在本机"
             "配置目录的文件里。", "TEXT", False),
            ("需要清除全部痕迹时：退出本程序，删除 %APPDATA%\\微信提醒 目录"
             "（配置、日志、解密缓存、密钥）即可。", "TEXT", False),
        ])

        card = self._section(col, "使用限制与免责声明")
        self._about_card(card, [
            ("本软件仅供用户本人对自己账号的消息提醒用途。禁止用于监控他人账号、获取他人数据、"
             "批量抓取消息等任何未授权或违法场景；使用者应对自己的行为负责。", "TEXT", False),
            ("微信随时可能更新存储方式导致功能失效或读取异常。本软件按\u201c现状\u201d提供，"
             "不作任何明示或暗示的担保；因使用本软件导致的任何后果由使用者自行承担。", "SUB", False),
            ("本软件免费分发，请勿用于商业用途：禁止售卖、打包收费、捆绑推广或冒充官方产品。",
             "SUB", False),
            ("本软件构建于 Python 运行时与 Tkinter 之上，并使用 PyCryptodome、python-zstandard、"
             "Pillow、pystray 等开源组件；这些组件的版权归各自作者所有，按各自的开源许可证条款"
             "分发与使用，本软件不主张对它们的任何权利。", "SUB", False),
            ("© 2026 微信来消息提醒 · 保留所有权利", "SUB", False),
        ])

        card = self._section(col, "联系与本机信息")
        r = tk.Frame(card, bg=CARD_BG)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="问题反馈 / 功能建议：", bg=CARD_BG, fg=P["TEXT"],
                 font=(FONT, 10)).pack(side="left")
        mail = tk.Label(r, text=CONTACT_EMAIL, bg=CARD_BG, fg=P["DEEP"],
                        font=(FONT, 10, "bold"), cursor="hand2")
        mail.pack(side="left")
        mail.bind("<Button-1>", lambda e: self._copy_text(CONTACT_EMAIL, mail))
        tk.Label(card, text="点击邮箱地址即可复制到剪贴板。", bg=CARD_BG, fg=P["SUB"],
                 font=(FONT, 9), anchor="w").pack(fill="x", pady=(4, 2))
        resolved_root = _resolve_data_root()
        self._dyn_wrap(card, tk.Label(card, text=f"数据目录：{resolved_root}", bg=CARD_BG,
                                      fg=self.P["SUB"], font=(FONT, 9),
                                      justify="left", wraplength=HINT_FULL)
                       ).pack(anchor="w", pady=(2, 2))
        self._dyn_wrap(card, tk.Label(card, text=f"日志文件：{LOG_FILE}", bg=CARD_BG,
                                      fg=self.P["SUB"], font=(FONT, 9),
                                      justify="left", wraplength=HINT_FULL)
                       ).pack(anchor="w", pady=(2, 2))

    def _copy_text(self, text, lbl):
        """把邮箱复制到剪贴板并就地给一次反馈（不弹对话框打断操作）"""
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            old = lbl.cget("text")
            lbl.configure(text="已复制 ✓")
            self.win.after(1200, lambda: lbl.configure(text=old))
        except Exception:
            pass

    # ---------- 动作 ----------
    def _open_log(self):
        try:
            os.startfile(LOG_FILE)
        except Exception as e:
            log(f"[日志] 打开失败: {e}")

    def _pick_sound(self):
        """v3.6.2: 选择自定义提示音（wav）
        v4.0.1: 仅支持 wav——PlaySound 不支持 mp3，选 mp3 会静默无声"""
        p = filedialog.askopenfilename(title="选择提示音（wav）",
                                       filetypes=[("WAV 音频", "*.wav")])
        if p:
            if not p.lower().endswith(".wav"):
                messagebox.showwarning("格式不支持", "仅支持 .wav 格式音频（系统播放接口限制）。")
                return
            config["custom_sound"] = p
            self.var_csound.set(p)
            save_config()
            log(f"[设置] 自定义提示音: {p}")
            messagebox.showinfo("已设置", "提示音已设置，收到消息时将播放该音频。")

    def _clear_sound(self):
        """清除自定义提示音，恢复系统默认"""
        config["custom_sound"] = ""
        config["sound_source"] = "default"
        self.var_csound.set("")
        self.var_ssrc.set("default")
        self.csound_card.pack_forget()
        save_config()
        log("[设置] 已清除自定义提示音")
        self._play_current_sound("default")

    def _auto_scan_dataroot(self):
        """v4.0.1-3: 自动扫描微信数据目录——遍历注册表/配置/文档/OneDrive/各盘符
        候选位置，找到含账号数据的目录后询问是否应用（不懂技术的朋友无需手动找路径）。"""
        import tkinter.messagebox as _mb
        found = []
        seen = set()
        for root in _candidate_roots():
            root = os.path.normpath(root)
            if not root or root in seen:
                continue
            seen.add(root)
            if os.path.isdir(root) and _root_has_account(root):
                found.append(root)
        if not found:
            _mb.showinfo("自动扫描", "未自动找到微信数据目录。\n"
                                      "请点击「选择」手动指定：\n"
                                      "微信文档目录 = xwechat_files 所在的那一层文件夹（能看到 wxid_xxx 账号文件夹的那一层）。")
            return
        # 多个候选时按最近活跃优先
        def _act(r):
            try:
                return max(os.path.getmtime(os.path.join(r, n)) for n in os.listdir(r)
                           if os.path.isdir(os.path.join(r, n)))
            except Exception:
                return 0.0
        found.sort(key=_act, reverse=True)
        target = found[0]
        if not _mb.askyesno("自动扫描", f"已找到微信数据目录：\n\n{target}\n\n是否应用？"):
            return
        config["data_root"] = target
        save_config()
        log(f"[设置] 自动扫描应用数据目录: {target}")
        self.adv_dataroot_lbl.config(text=target)
        # 刷新账号与密钥状态
        try:
            self._refresh_advanced_status()
        except Exception:
            pass
        _mb.showinfo("已应用", "数据目录已更新，微信来消息提醒将按新目录监控。")

    def _pick_dataroot(self):
        """v3.6.2: 手动指定微信数据目录（xwechat_files 所在文件夹）

        反馈场景：微信装在 D 盘、文档存在 F 盘时自动检测失败，
        %USERPROFILE%\\Documents 下没有 xwechat_files。用户可手动指定。"""
        p = filedialog.askdirectory(title="选择微信数据目录（xwechat_files 所在文件夹）")
        if p:
            config["data_root"] = p
            if hasattr(self, "adv_dataroot_lbl"):
                self.adv_dataroot_lbl.config(text=p)
            save_config()
            self._refresh_advanced_status()
            messagebox.showinfo("已设置",
                                "数据目录已更新，正在重新检测账号…\n"
                                "如密钥未配置请重新输入/提取。")

    def _save(self):
        config["sound"] = bool(self.var_sound.get())
        config["skip_if_wechat_active"] = bool(self.var_skip.get())
        config["include_biz_messages"] = bool(self.var_biz.get())
        config["wecom_on"] = bool(self.var_wecom.get())
        try:
            config["popup_duration"] = max(2, min(20, int(self.var_dur.get() or 6)))
        except Exception:
            config["popup_duration"] = 6.0
        config["quiet_enabled"] = bool(self.var_quiet.get())
        qs = self.var_qstart.get().strip()
        qe = self.var_qend.get().strip()
        if re.match(r"^\d{1,2}:\d{2}$", qs) and re.match(r"^\d{1,2}:\d{2}$", qe):
            config["quiet_start"] = qs
            config["quiet_end"] = qe
        else:
            self.var_qstart.set(config["quiet_start"])
            self.var_qend.set(config["quiet_end"])
        # v4.2.0: 提醒规则 + 提示音音量
        try:
            config["banner_mode"] = self.var_bmode.get()
        except Exception:
            pass
        for key, var in (("alert_keywords", "var_alert_kw"),
                         ("alert_contacts", "var_alert_ct"),
                         ("mute_keywords", "var_mute_kw"),
                         ("mute_contacts", "var_mute_ct")):
            v = getattr(self, var, None)
            if v is not None:
                config[key] = v.get().strip()
        v = getattr(self, "var_vol", None)
        if v is not None:
            try:
                config["sound_volume"] = max(0, min(100, int(v.get())))
            except Exception:
                pass
        save_config()
        log("[设置] 已保存")

    def _save_quick_replies(self):
        """话术库存盘：去首尾空格、丢掉空条、单条截到 QR_LEN、最多留 QR_MAX 条。
        清空中间一条时后面自动补位——横幅按顺序画按钮，留个洞会显得像坏了。"""
        config["quick_reply_on"] = bool(self.var_qr_on.get())
        texts = []
        for var in getattr(self, "var_qr", []):
            t = var.get().strip()[:QR_LEN]
            if len(t) != len(var.get()):
                var.set(t)              # 输入框里留的就是真正存下来的那条
            if t:
                texts.append(t)
        config["quick_replies"] = texts[:QR_MAX]
        save_config()
        log(f"[设置] 快捷回复已保存（{len(config['quick_replies'])} 条）")

    def _save_auto_reply(self):
        """「自动回复」开关（默认关）：点横幅跳会话；点话术跳过去核对后再填入。
        到输入框为止，绝不替你按回车发送。"""
        config["auto_reply"] = bool(self.var_auto_reply.get())
        save_config()
        log(f"[设置] 自动回复：{config['auto_reply']}")

    def _save_drive_on(self):
        """驱动总闸（v4.5.2）：关掉后跳会话/填入/自检一步都不动微信。
        话术仍然复制进剪贴板——关的是"动微信"，不是快捷回复本身。"""
        config["drive_on"] = bool(self.var_drive_on.get())
        if config["drive_on"]:
            wx_rate_reset()      # 关回来那一刻不该还背着上一段的配额
        save_config()
        log(f"[设置] 驱动总闸：{'开（配额已清零）' if config['drive_on'] else '关'}")
        try:
            self.drive_stat.config(text=wx_drive_stat_text())
        except Exception:
            pass

    def _toggle_quiet(self, save=True):
        """免打扰开关：切换两个时间输入框的可用态。
        构建期也会调一次来复原输入框状态，那时不能顺手写盘（save=False）。"""
        state = "normal" if self.var_quiet.get() else "disabled"
        self.ent_qstart.configure_state(state)
        self.ent_qend.configure_state(state)
        if save:
            self._save()

    def _on_vertical(self):
        """v4.6.2: 点顶部/中部/底部时退出自定义坐标模式，让预设位置真正生效。"""
        if self.var_pos.get() == "custom":
            self.var_pos.set("center")
        self._apply_banner()

    def _on_pos_xy(self):
        """v4.6.2: 输入自定义坐标后自动切到「自定义」并立即应用。"""
        try:
            self.var_pos.set("custom")
        except Exception:
            pass
        self._apply_banner()

    def _apply_banner(self):
        config["banner_pos"] = self.var_pos.get()
        config["banner_theme"] = self.var_btheme.get()
        config["banner_title"] = self.var_btitle.get().strip() or "微信 · 新消息"
        try:
            config["banner_radius"] = max(0, min(60, int(self.var_radius.get())))
        except Exception:
            config["banner_radius"] = 18
        # v4.1.0: 新外观项——字号 / 不透明度 / 垂直位置
        try:
            config["banner_font_size"] = max(9, min(16, int(self.var_bfont.get())))
        except Exception:
            pass
        try:
            config["banner_opacity"] = max(0.3, min(1.0, float(self.var_bopacity.get()) / 100))
        except Exception:
            pass
        if hasattr(self, "var_bvertical"):
            config["banner_vertical"] = self.var_bvertical.get()
        # v4.6.2: 自定义左上角坐标（X / Y）
        for _var, _key in (("var_px", "banner_pos_x"), ("var_py", "banner_pos_y")):
            if hasattr(self, _var):
                try:
                    config[_key] = max(0, int(getattr(self, _var).get()))
                except (TypeError, ValueError):
                    pass
        save_config()
        try:
            self.banner.rebuild()
        except Exception as e:
            log(f"[banner] 重建失败: {e}")
        log("[设置] 横幅外观已更新")

    def _pick_custom_color(self, part):
        """v3.6.3: 自定义横幅配色——背景 / 标题 / 文字颜色选择"""
        key = {"bg": "banner_custom_bg", "title": "banner_custom_title",
               "text": "banner_custom_text"}[part]
        cur = str(config.get(key, "#202126"))
        rgb, hexv = colorchooser.askcolor(color=cur, title="选择颜色")
        if not hexv:
            return
        config[key] = hexv
        save_config()
        self._apply_banner()
        log(f"[设置] 自定义配色 {part} -> {hexv}")

    def _on_autostart(self):
        ok = set_auto_start(self.var_autostart.get())
        if not ok:
            self.var_autostart.set(False)
            self.status_lbl.config(text="开机自启设置失败", fg="#e5484d")

    def _on_theme_change(self):
        """v4.0.1: 切换界面主题——保存配置、全局换 UI 字典、重建设置窗口"""
        theme = self.var_theme.get()
        config["theme"] = theme
        try:
            save_config()
        except Exception:
            pass
        _apply_theme(theme)
        self.P = UI
        self._rebuild()

    def _rebuild(self):
        """按当前主题重建设置窗口（保留窗口位置与当前页）"""
        try:
            g = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
        except Exception:
            g = ""
        cur = getattr(self, "_active_tab", "basic")
        try:
            self.win.destroy()
        except Exception:
            pass
        self._build()
        if g:
            try:
                config["win_geometry"] = g
            except Exception:
                pass
        try:
            self._switch_tab(cur, first=True)
        except Exception:
            self._switch_tab("basic", first=True)
        self.show()

    def _test_banner(self):
        self.banner.show("new")

    def update_status(self, text, color="#07C160"):
        if self.status_lbl:
            self.status_lbl.config(text=text, fg=color)
            # 运行状态指示灯：监控中绿点，错误由调用方传红色
            if getattr(self, "status_dot", None):
                try:
                    self.status_dot.itemconfig(self._dot, fill=color)
                except Exception:
                    pass

    def show(self):
        """v4.1.0: 增强健壮性——窗口不存在/已销毁/显示异常时自动重建
        （社区反馈：右键托盘打不开设置界面，重启程序也一样，弹窗却正常）。"""
        try:
            if not self.win or not self.win.winfo_exists():
                log("[设置] show: 窗口不存在，重建")
                self._build()
            g = config.get("win_geometry", "")
            if g and g.startswith("+"):
                self.win.geometry(g)
        except Exception as e:
            log(f"[设置] show 准备异常，重建窗口: {e}")
            try:
                if self.win:
                    self.win.destroy()
            except Exception:
                pass
            self._build()
        try:
            self._reveal()
        except Exception as e:
            log(f"[设置] show 显示异常，二次重建: {e}")
            try:
                if self.win:
                    self.win.destroy()
            except Exception:
                pass
            try:
                self._build()
                self._reveal()
            except Exception as e2:
                log(f"[设置] show 二次重建失败: {e2}")

    def _reveal(self):
        """显示窗口并提到最前：最小化后从托盘/任务栏唤回时，窗口不能只是
        "存在"，必须盖在当前前台应用之上。"""
        self.win.deiconify()
        self.win.lift()
        self._raise_foreground()
        # 延迟执行：窗口刚显示时布局未完成，立即测量高度会得到 1
        self.win.after(30, self._refit)

    def _raise_foreground(self):
        """v4.4：设置窗口不再置顶，光靠 lift 有时只在本进程内提升层级，
        仍会被浏览器之类压在下面——这里显式抢一次前台。"""
        try:
            hwnd = self._taskbar_hwnd()
            if user32.GetForegroundWindow() != hwnd:
                if not user32.SetForegroundWindow(hwnd):
                    # 前台锁未放开：模拟一次 ALT 按下/松开再试（Windows 常规绕法）
                    user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
                    user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up
                    user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        except Exception:
            pass

    def _end_move(self, e):
        self._dragging = False
        try:
            if self.win and self.win.winfo_exists():
                config["win_geometry"] = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
                save_config()
        except Exception:
            pass

    def hide(self):
        try:
            if self.win and self.win.winfo_exists():
                self.win.withdraw()
                log("[设置] 窗口已隐藏到托盘")
            else:
                log("[设置] hide: 窗口不存在")
        except Exception as e:
            log(f"[设置] hide 异常: {e}")


# ============================================================
# 托盘图标
# ============================================================
def start_tray(on_open, on_quit):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        log("[tray] pystray 不可用，跳过托盘图标")
        return None
    try:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4, 10, 60, 52), radius=14, fill=(7, 193, 96, 255))
        d.polygon([(18, 50), (18, 62), (32, 50)], fill=(7, 193, 96, 255))
        d.ellipse((21, 24, 29, 32), fill="white")
        d.ellipse((35, 24, 43, 32), fill="white")
        menu = pystray.Menu(
            # default=True：托盘左键单击直接打开设置（pystray 的左键会派发到
            # 菜单里标记为默认的那一项），右键仍是菜单
            pystray.MenuItem("打开设置", lambda i, it: on_open(), default=True),
            pystray.MenuItem("测试弹窗", lambda i, it: on_open(test=True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda i, it: on_quit()),
        )
        icon = pystray.Icon(APP_NAME, img,
                           f"微信来消息提醒 v{VERSION}（左键打开设置，右键菜单）", menu)
        threading.Thread(target=icon.run, daemon=True).start()
        log("[tray] 托盘图标已启动")
        return icon
    except Exception as e:
        log(f"[tray] 启动失败: {e}")
        return None


# ============================================================
# v4.2.0: 提醒规则系统（关键词/联系人过滤 + 屏蔽列表）
# ============================================================
def _parse_rules(s):
    """把逗号分隔的规则字符串解析为去空白非空列表"""
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]


def _rule_match(keys, *fields):
    """任一规则关键词命中任一字段（不区分大小写）"""
    if not keys:
        return False
    text = " ".join(str(f or "") for f in fields).lower()
    return any(k.lower() in text for k in keys)


def check_alert_rules(msg_info, cfg):
    """提醒规则判定。返回 True=提醒 / False=静默。

    优先级（自上而下）：
    ① 软件屏蔽列表（联系人/群 + 关键词）——全局生效，最高优先；
    ② 仅提醒模式（filtered）：仅提醒联系人/群白名单 + 关键词白名单。
    屏蔽与白名单均支持逗号分隔多值；联系人匹配昵称/群名/会话 key。"""
    if not msg_info:
        return True
    sender = msg_info.get("sender", "") or ""
    group = msg_info.get("group_name", "") or ""
    session = msg_info.get("session_key", "") or ""
    content = msg_info.get("content", "") or ""

    # ① 屏蔽列表
    if _rule_match(_parse_rules(cfg.get("mute_contacts")), session, sender, group):
        log(f"[规则] 命中屏蔽联系人/群（{sender or group or session}），静默")
        return False
    if _rule_match(_parse_rules(cfg.get("mute_keywords")), content):
        log("[规则] 命中屏蔽关键词，静默")
        return False

    # ② 仅提醒模式（filtered）
    if cfg.get("banner_mode", "all") == "filtered":
        ac = _parse_rules(cfg.get("alert_contacts"))
        if ac and not _rule_match(ac, session, sender, group):
            log(f"[规则] 不在仅提醒联系人/群名单（{sender or group or session}），静默")
            return False
        ak = _parse_rules(cfg.get("alert_keywords"))
        if ak and not _rule_match(ak, content):
            log("[规则] 未命中提醒关键词，静默")
            return False
    return True


# ============================================================
# v4.2.0: 提示音音量（动态缩放 wav 样本，不调节系统音量）
# ============================================================
def _play_sound_file(path, vol):
    """按音量（0-100）播放 wav 文件。

    winsound 不支持单文件音量，这里读取 16-bit PCM wav，按比例缩放样本后
    缓存到 %APPDATA%\\微信提醒\\sound_cache，避免每次弹窗都重新生成。
    非 16-bit PCM 或异常时回退到原文件/系统默认音。"""
    try:
        import winsound
        if not path or not os.path.exists(path):
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        vol = max(0, min(100, int(vol or 70)))
        if vol <= 0:
            return
        if vol >= 100:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        cache_dir = os.path.join(APP_DIR, "sound_cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            cache_dir = None
        cf = None
        if cache_dir:
            cf = os.path.join(cache_dir,
                              f"{os.path.splitext(os.path.basename(path))[0]}_{vol}.wav")
        if not (cf and os.path.exists(cf)):
            import wave as _wave
            import array as _array
            with _wave.open(path, "rb") as w:
                p = w.getparams()
                n = w.getnframes()
                frames = w.readframes(n)
            if w.getsampwidth() == 2 and cf:
                a = _array.array("h")
                a.frombytes(frames)
                scale = vol / 100.0
                for i in range(len(a)):
                    a[i] = int(a[i] * scale)
                with _wave.open(cf, "wb") as w2:
                    w2.setparams(p)
                    w2.writeframes(a.tobytes())
            else:
                cf = path  # 非 16-bit PCM：直接播放原文件（无音量调节）
        winsound.PlaySound(cf, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass


def system_sound_file():
    """「系统默认音」实际播放的 wav 路径。

    MessageBeep 放的就是系统事件 SystemAsterisk 注册的那个文件；把它解析出来，
    音量滑块才能真的起作用（MessageBeep 本身不认音量）。解析不到就返回 None，
    由调用方回退 MessageBeep——保持"系统音被关掉就是没声"的原语义，不擅自换文件。"""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"AppEvents\Schemes\Apps\.Default\SystemAsterisk\.current") as k:
                v = winreg.QueryValue(k, None)
        except OSError:
            continue
        return v if v and os.path.exists(v) else None
    return None


def resolve_sound(src=None):
    """提示音来源 → wav 路径（None=用系统事件音）。"""
    src = src or config.get("sound_source", "default")
    if src == "custom":
        return (config.get("custom_sound") or "").strip() or None
    if src.startswith("builtin"):
        names = ["dingdong.wav", "crisp.wav", "soft.wav", "pop.wav"]
        try:
            idx = int(src[len("builtin"):]) - 1
        except ValueError:
            return None
        if 0 <= idx < len(names):
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            return os.path.join(base, "sounds", names[idx])
        return None
    return system_sound_file()


def play_notify_sound(src=None, volume=None):
    """提示音统一入口：横幅弹窗和设置界面「试听」都走这里。

    以前这两处各写了一份来源解析（横幅那份连音量都是复制的），改一处漏一处。"""
    path = resolve_sound(src)
    if path and not os.path.exists(path):
        log(f"[提示音] 文件不存在，改用系统默认音: {path}")
        path = None
    _play_sound_file(path, config.get("sound_volume", 70) if volume is None else volume)


# ============================================================
# 监控线程
# ============================================================
class Monitor:
    def __init__(self, q, wxdb=None):
        self.q = q
        self.wxdb = wxdb
        self.dbs = {}
        self.prev = {}
        self.last_show = 0.0
        # v3.6: 去重按会话分桶——自己发消息会触发 WAL 变化并读回旧消息，
        # 全局单指纹会把其他会话的旧消息重弹；按会话记录已弹指纹可避免。
        self._last_msg_fp_map = {}   # {session_key: 最近弹窗指纹}
        self._last_msg_time_map = {}  # {session_key: 最近弹窗时间}
        self._front_skip_until = 0.0  # 前台跳过水位：微信前台期间收到的消息视为已读，不再补弹
        self._last_refresh = time.time()  # 上次刷新监控库列表的时间（定期刷新）
        self._no_disturb = set()      # v4.0.1-7: 消息免打扰会话缓存（私聊+群，flag&512）
        self._no_disturb_ts = 0.0     # 免打扰集合刷新时间
        # v4.1.0: 通用（无内容）横幅去重指纹——内容读取失败/普通模式时用
        # 触发文件路径 + 20s 窗口去重，防止微信二次写库造成重复弹窗
        self._last_generic_fp = None
        self._last_generic_t = 0.0
        if self.wxdb:
            # v4.4: 预探测消息库 WAL 能否直读，否则启动后的第一条消息仍会等满
            # 3.1s checkpoint（合并状态只有读过一次才知道）
            try:
                self.wxdb.probe_wal()
            except Exception:
                pass
        self._refresh()

    def _bound_account(self):
        """当前已绑定的账号目录（规范化小写 + 结尾分隔符，便于前缀比较）。

        普通模式（没有密钥、wxdb 为 None）返回 None——那种模式本来就只能靠
        "任意 WAL 变了"来弹通用横幅，不能按账号过滤，否则彻底收不到提醒。"""
        if not self.wxdb:
            return None
        try:
            return os.path.normpath(os.path.dirname(self.wxdb.db_root)).lower() + os.sep
        except Exception:
            return None

    def _maybe_switch_account(self):
        """微信换了登录账号时，把库和密钥一起跟过去。

        v4.4.1: 库/密钥在启动时只绑一个账号，而监控列表扫的是数据根下**所有**
        账号；另一账号来消息会用错账号的库去读（读不到新消息），还会污染按会话
        分桶的去重指纹。现在活跃账号一变就重建绑定，监控范围也随它收窄。"""
        if not self.wxdb:
            return
        try:
            acct = _find_active_account(_resolve_data_root())
        except Exception:
            return
        bound = self._bound_account()
        if not acct or not bound:
            return
        if os.path.normpath(acct).lower() + os.sep == bound:
            return
        wxid = os.path.basename(os.path.normpath(acct))
        # 只用"这个账号自己的"密钥。_load_master_key 会退回通用 wx_key.txt，
        # 那多半是别的账号的 key——拿它去解新账号的库比不切更糟（解出乱码/失败，
        # 还把错账号的会话写进免打扰与去重表）。
        if not _has_account_key(wxid):
            log(f"[监控] 活跃账号已切到 {wxid}，没有该账号的密钥文件，仍按原账号监控")
            return
        key = _load_master_key(wxid)
        if not key:
            log(f"[监控] 活跃账号已切到 {wxid}，密钥不可用，仍按原账号监控")
            return
        try:
            new = WeChatDB(key, os.path.join(acct, "db_storage"))
            new.probe_wal()
        except Exception as e:
            log(f"[监控] 切换到账号 {wxid} 失败: {e}，仍按原账号监控")
            return
        self.wxdb = new
        self._no_disturb = set()
        self._last_msg_fp_map.clear()
        self._last_msg_time_map.clear()
        self._last_generic_fp = None
        self._front_skip_until = 0.0
        log(f"[监控] 已跟随活跃账号 {wxid} 重建库绑定（密钥/免打扰/去重一并复位）")

    def _refresh(self):
        self._maybe_switch_account()
        dbs = find_message_dbs(_resolve_data_root())
        acct = self._bound_account()
        if acct:
            # 只监控"解得开"的那个账号，读不到的库不触发弹窗
            dbs = {p: v for p, v in dbs.items()
                   if os.path.normpath(p).lower().startswith(acct)}
        self.dbs = dbs
        # 不能整表重拍快照：_refresh 末尾还要解密级地刷新免打扰集合（可能耗时秒级），
        # 这期间到达的新消息已经把 WAL 改掉了，重拍会把它一并"当作基线"，那条消息
        # 就永远检测不到、永远不弹。已监控的库沿用旧值，只给新增的库拍基线，
        # 未处理完的变化留给下一轮比对。
        fresh = snapshot(self.dbs)
        self.prev = {p: (self.prev[p] if p in self.prev else v)
                     for p, v in fresh.items()}
        # v4.0.1: 同步刷新免打扰会话集合（用户改了群免打扰后 30s 内生效）
        try:
            if self.wxdb:
                new_set = self.wxdb.get_no_disturb_set()
                # 每 30s 刷新一次，集合未变时不记日志，否则日志被无效行冲满
                if new_set != self._no_disturb:
                    log(f"[监控] 免打扰会话 {len(self._no_disturb)} → {len(new_set)} 个")
                    self._no_disturb = new_set
        except Exception as e:
            log(f"[监控] 免打扰集合刷新失败: {e}")

    def file_count(self):
        return len(self.dbs)

    def _msg_fingerprint(self, msg_info):
        """计算消息指纹，用于去重。

        v4.0.1: 指纹改用**稳定标识**——session_key + sender_wxid + local_id +
        create_time，剔除展示昵称与展示文案。此前用展示昵称/文案参与指纹，
        微信状态更新（二次写库）或昵称解析先后不一致会导致指纹失配，
        同一条消息分钟级重复弹横幅（社区反馈的那条重复横幅即此根因）。"""
        if not msg_info:
            return None
        sess = msg_info.get("session_key", "") or msg_info.get("sender", "")
        wxid = msg_info.get("sender_wxid", "") or ""
        ts = msg_info.get("timestamp", 0)
        lid = msg_info.get("local_id", "")
        return f"{sess}|{wxid}|{lid}|{ts}"

    def _avatar_for(self, msg):
        """取这条消息该显示的头像：会话头像（群聊就是群的头像），拿不到就 None。

        查不到不是错误——微信只给"显示过"的头像落本地缓存，陌生发送者常常没有。"""
        try:
            who = (msg.get("session_key") or msg.get("sender_wxid") or "").strip()
            return self.wxdb.get_avatar(who) if who else None
        except Exception:
            return None

    def _fetch_latest_message(self):
        """获取最新消息的发件人和内容（v3.0 增强）。
        v3.1: 加入超时保护——解密大库可能耗时数秒到数十秒，
        在子线程执行并限时，超时跳过本次内容显示，避免监控线程被拖死。
        v3.4: 若上次读取线程超时后仍在运行，本次不再新开线程（防止线程累积）。"""
        if not self.wxdb:
            return None
        if getattr(self, "_fetch_thread", None) and self._fetch_thread.is_alive():
            log("[监控] 上次内容读取仍在进行，跳过本次读取（防线程堆积）")
            return None
        result = {}
        def _work():
            try:
                self.wxdb.refresh()
                msg = self.wxdb.get_latest_message()
                if msg and msg.get("self_sent"):
                    # 最新一条是自己刚发出的（WAL 变化由"我发消息"引起），
                    # 不是别人来消息：静默跳过。不能落到下面的"读不到内容"
                    # 兜底去弹通用横幅，也不能翻出上一条收到的旧消息重弹。
                    log(f"[监控] 最新消息是自己发出的（{msg.get('session_key') or '未知会话'}），跳过提醒")
                    result["info"] = {"suppressed": True,
                                      "session_key": msg.get("session_key") or ""}
                    return
                # v4.0.1: 消息记录存在即返回 info（内容可能为空串）——
                # 空内容也需按稳定指纹去重、按 session 判定免打扰，
                # 避免"内容读取失败 → 通用横幅无指纹 → 10s 后成功读取再弹一次"
                if msg and (msg.get("content") is not None or msg.get("local_id")):
                    # v4.0.1: 会话设置了「消息免打扰」（群）→ 不弹窗
                    sk = msg.get("session_key") or ""
                    if sk and sk in self._no_disturb:
                        log(f"[免打扰] 会话 {sk} 已设置消息免打扰，跳过弹窗")
                        # 必须显式标记"被免打扰压掉"：以前这里返回 None，和"读取
                        # 失败"长得一模一样，调用方就把它当成兜底场景去弹通用横幅，
                        # 结果微信里开了免打扰的会话照样弹。
                        result["info"] = {"suppressed": True, "session_key": sk}
                        return
                    result["info"] = {
                        "sender": msg.get("sender_nick") or msg.get("sender_wxid") or "",
                        "sender_wxid": msg.get("sender_wxid") or "",
                        "content": msg.get("content", ""),
                        "timestamp": msg.get("timestamp", 0),
                        "local_id": msg.get("local_id", 0),
                        "is_group": msg.get("is_group", False),
                        "group_name": msg.get("group_name", "") or "",
                        "session_key": msg.get("session_key", "") or "",
                        # 群消息是否 @ 了当前账号（换账号后 wxdb 重建、身份跟着变）
                        "at_me": _compute_at_me(msg, self.wxdb),
                        # 头像只在横幅上用、约 10KB，在这里（监控线程）取好塞进消息，
                        # 别放到 UI 线程去碰数据库——首次解密 head_image 库要 0.2s 左右
                        "avatar": self._avatar_for(msg),
                    }
            except Exception as e:
                log(f"[监控] 读取消息内容失败: {e}")
        t = threading.Thread(target=_work, daemon=True)
        self._fetch_thread = t
        t.start()
        t.join(timeout=6.0)
        if t.is_alive():
            log("[监控] 读取消息内容超时（数据库解密较慢），本次仅显示未读提示")
            return None
        return result.get("info")

    def loop(self):
        log(f"[监控] 已监控 {len(self.dbs)} 个微信数据库文件")
        if self.wxdb:
            log("[监控] v3.0 增强模式：已启用消息内容读取")
        while True:
            try:
                # v3.5.1: 加锁复制配置副本，避免设置界面写配置时读到中间状态
                # （DeepSeek 审查：监控线程直接读 config 多键可能脏读）
                with _config_lock:
                    cfg = dict(config)
                time.sleep(cfg["poll_interval"])
                # 定期刷新监控库列表：微信可能新建会话库（message_N.db / media_N.db），
                # 不刷新会导致新库的消息永远监控不到
                # v4.1.0-审查: _refresh() 内部已同步刷新免打扰会话集合（含日志），
                # 这里不再重复 get_no_disturb_set（避免每 30s 重复解密 contact 库）
                if time.time() - self._last_refresh > 30:
                    self._last_refresh = time.time()
                    self._refresh()
                cur = snapshot(self.dbs)
                changed = [p for p in self.dbs if cur.get(p) != self.prev.get(p)]
                self.prev = cur
                if not changed:
                    continue
                if cfg["skip_if_wechat_active"] and foreground_is_wechat() \
                        and not foreground_fullscreen():
                    # 微信在前台（且非全屏，如全屏看视频仍照常弹窗）：本次不弹窗，
                    # 并记录水位。
                    # 2026-09-08 修复：若只跳过而不记水位，最小化后微信触发
                    # checkpoint 会让 WAL 再次变化，把前台期间已看到的旧消息
                    # 补弹出来；这里记录时间戳水位，弹窗前过滤旧消息。
                    self._front_skip_until = time.time()
                    continue
                # 免打扰时段：不弹窗不响铃
                if cfg["quiet_enabled"] and in_quiet_time(cfg["quiet_start"],
                                                           cfg["quiet_end"]):
                    log(f"[免打扰] {time.strftime('%H:%M')} 处于免打扰时段，跳过弹窗")
                    continue
                # v3.0: 智能等待 WAL checkpoint 到主数据库
                # 微信新消息先写 WAL，需等 checkpoint 后解密主数据库才能读到新消息
                # 轮询检查主数据库 mtime 变化 + WAL 文件清空（checkpoint 完成的直接信号），
                # 一旦就绪立即读取，最多等 3 秒
                # v4.4: wechat_db 已能直读 WAL 里已提交的事务，合并被验证可用时
                # 不必再等 checkpoint（实测旧逻辑 99% 都等满 3.1s，端到端横幅延迟
                # 中位数 18s）。改为只等"本次事务写完"（WAL 连续 0.15s 不再变化，
                # 上限 1.2s）——防止读到未提交事务、内容仍是旧消息而被去重吞掉漏弹。
                # 合并不可用（无密钥 / 非 4096 页 / 结构异常）时完全保持原等待。
                account_dir = _find_active_account(cfg["data_root"])
                main_db = os.path.join(account_dir, "db_storage", "message", "message_0.db") if account_dir else None
                wait_start = time.time()
                fast_wal = bool(self.wxdb and self.wxdb.wal_merge_ready())
                if fast_wal:
                    sizes = {}
                    for p in changed:
                        try:
                            sizes[p] = os.path.getsize(p)
                        except OSError:
                            sizes[p] = -1
                    while time.time() - wait_start < 1.2:
                        time.sleep(0.15)
                        grown = False
                        for p in changed:
                            try:
                                s = os.path.getsize(p)
                            except OSError:
                                s = -1
                            if s != sizes.get(p):
                                sizes[p] = s
                                grown = True
                        if not grown:
                            break
                else:
                    last_mtime = 0
                    try:
                        if main_db and os.path.exists(main_db):
                            last_mtime = os.path.getmtime(main_db)
                    except Exception:
                        pass
                    # 记录触发本次检测的所有 WAL 文件大小（checkpoint 完成后 WAL 会被截断/清空）
                    last_wal_sizes = {}
                    for p in changed:
                        try:
                            last_wal_sizes[p] = os.path.getsize(p)
                        except OSError:
                            last_wal_sizes[p] = -1
                    # 先快速等待 0.3 秒（大多数消息在这个时间内 checkpoint）
                    time.sleep(0.3)
                    # 轮询检查主数据库 mtime 变化或任一变化 WAL 清空（多个 WAL 同时变化时
                    # 逐个检查，任一完成 checkpoint 即可读取，避免只等第一个文件）
                    while time.time() - wait_start < 3.0:
                        try:
                            if main_db and os.path.exists(main_db):
                                cur_mtime = os.path.getmtime(main_db)
                                if cur_mtime != last_mtime:
                                    break  # checkpoint 完成，立即读取
                        except Exception:
                            pass
                        # WAL 被截断/删除 = checkpoint 已把数据写回主库
                        any_done = True
                        for p in changed:
                            try:
                                if os.path.exists(p):
                                    if os.path.getsize(p) < last_wal_sizes.get(p, -1) - 10:
                                        continue  # 该文件已完成 checkpoint
                                    any_done = False
                            except OSError:
                                continue  # 视为已完成
                        if any_done:
                            break
                        time.sleep(0.2)
                waited = time.time() - wait_start
                now = time.time()
                acc = account_of(changed[0])
                log(f"[监控] 检测到新消息 ({acc}) : {os.path.basename(changed[0])} "
                    f"(等待{waited:.2f}s {'WAL直读' if fast_wal else 'checkpoint'})")
                # v3.0: 尝试获取最新消息内容
                msg_info = self._fetch_latest_message()
                # v4.4.1: 免打扰命中要当场跳过，绝不能落到下面的"读不到内容"兜底分支
                if msg_info and msg_info.get("suppressed"):
                    continue
                # v4.0.1: 消息免打扰——该会话在微信里开启了免打扰则不弹窗不响铃
                if msg_info and msg_info.get("session_key") in self._no_disturb:
                    log(f"[监控] 会话 {msg_info.get('session_key')} 已开启消息免打扰，跳过弹窗")
                    continue
                # v4.2.0: 提醒规则系统——屏蔽列表/仅提醒关键词/仅提醒联系人（软件内）
                if msg_info and not check_alert_rules(msg_info, cfg):
                    continue
                if msg_info:
                    log(f"[监控] 发件人: {msg_info['sender']}, 内容: {msg_info['content'][:40]}...")
                    # v4.0.1: 横幅时间显示消息原始到达时间，而非检测/弹出时间
                    _mt = msg_info.get("timestamp") or 0
                    ts = time.strftime("%H:%M:%S", time.localtime(_mt)) if _mt > 0 \
                        else time.strftime("%H:%M:%S")
                else:
                    # v4.1.0: 内容读取失败/普通模式时不再完全跳过——否则无密钥用户
                    # （普通模式）与持续解析失败的用户将永远收不到任何提醒（社区反馈
                    # "别人发消息弹窗没弹出来"）。改弹无内容通用横幅，并用触发文件
                    # 路径 + 20s 窗口去重，防微信二次写库（到达时 + 10s 后状态更新）
                    # 造成重复弹窗（原报的那两条重复横幅问题）。
                    if self.wxdb is None:
                        log("[监控] 普通模式（未启用内容读取），弹通用新消息提醒")
                    else:
                        log("[监控] 消息内容读取失败/超时，弹通用新消息提醒（防完全静默）")
                    _gfp = changed[0]
                    if self._last_generic_fp == _gfp and (now - self._last_generic_t) < 20.0:
                        log("[监控] 通用提醒重复（20s 内同一触发文件），跳过弹窗")
                        continue
                    self._last_generic_fp = _gfp
                    self._last_generic_t = now
                    msg_info = None
                    ts = time.strftime("%H:%M:%S")
                # 前台水位过滤：消息时间戳早于"微信前台跳过时刻"= 前台期间已看到的
                # 旧消息（最小化后 checkpoint 触发误报），不弹窗
                if self._front_skip_until > 0:
                    msg_ts = (msg_info or {}).get("timestamp") or 0
                    if msg_ts > 0 and msg_ts < self._front_skip_until:
                        log(f"[监控] 前台期间的消息（时间 {msg_ts:.0f} < 水位 {self._front_skip_until:.0f}），不弹窗")
                        continue
                    if not msg_info:
                        # 读不到内容无法判断新旧（可能是前台消息的 checkpoint 触发），
                        # 保守不弹，避免补弹前台旧消息
                        log("[监控] 前台水位期内且读取超时，跳过（避免补弹前台消息）")
                        continue
                # 消息去重：微信收消息会写库两次（到达时 + 约10秒后状态更新），
                # 第二次 WAL 变化会再次检测到同一条消息。指纹含 create_time，
                # 同一条消息重复检测（间隔 <20 秒）直接跳过；真正的新消息指纹必然不同。
                fp = self._msg_fingerprint(msg_info)
                # v3.6: 按会话去重（会话维度指纹），避免其他会话旧消息重弹
                sess_key = (msg_info or {}).get("session_key", "") or (msg_info or {}).get("sender", "")
                last_fp = self._last_msg_fp_map.get(sess_key)
                last_tm = self._last_msg_time_map.get(sess_key, 0.0)
                # v4.0.1: 去重窗口 20s -> 24h：微信稍后 checkpoint/整理 DB 会再次
                # 触发同一条消息检测，20s 窗口外会重复弹横幅（用户反馈 11 点的消息
                # 之后又弹了一次）；local_id 在同一会话内唯一递增，指纹相同必为
                # 同一条消息，长窗口不会误杀真正的新消息
                if fp and last_fp and fp == last_fp and (now - last_tm) < 86400.0:
                    log(f"[监控] 消息重复（{now - last_tm:.1f}秒内），跳过弹窗")
                    continue
                self._last_msg_fp_map[sess_key] = fp
                self._last_msg_time_map[sess_key] = now
                if now - self.last_show < cfg["merge_window"]:
                    # 合并窗口内：通知横幅计数 +1（不重弹）
                    self.q.put(("merge", ts, msg_info))
                else:
                    self.last_show = now
                    self.q.put(("new", ts, msg_info))
            except Exception as e:
                log(f"[监控] 循环异常: {e}")
                time.sleep(1)


# ============================================================
# 微信数据目录解析（v3.6.1：支持自定义存储位置/多用户/OneDrive）
# ============================================================
def _wechat_save_path_from_registry():
    """从注册表读取微信文件保存路径（HKCU/Software/Tencent/WeChat/FileSavePath）"""
    for sub in (r"Software\Tencent\WeChat", r"Software\Tencent\Weixin",
                r"Software\Tencent\Weixin\FilePath"):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub)
            for vname in ("FileSavePath", "FilePath", "DataPath"):
                try:
                    val, _ = winreg.QueryValueEx(key, vname)
                    if val and os.path.isdir(val):
                        winreg.CloseKey(key)
                        return val
                except Exception:
                    continue
            winreg.CloseKey(key)
        except Exception:
            continue
    return None


def _wechat_save_path_from_config():
    """从微信配置目录解析文件保存路径（%APPDATA%/Tencent/WeChat/All Users/config/*.ini）"""
    try:
        cfg_dir = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "WeChat",
                               "All Users", "config")
        if os.path.isdir(cfg_dir):
            for fn in os.listdir(cfg_dir):
                if not fn.lower().endswith((".ini", ".data", ".cfg")):
                    continue
                p = os.path.join(cfg_dir, fn)
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            m = re.search(r"FileSavePath\s*=\s*(.+)", line, re.I)
                            if m:
                                val = m.group(1).strip().strip('"')
                                if val and os.path.isdir(val):
                                    return val
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _candidate_roots():
    """候选微信数据根目录列表（按优先级）"""
    roots = []
    if config.get("data_root"):
        roots.append(config["data_root"])
    for p in (_wechat_save_path_from_registry(), _wechat_save_path_from_config()):
        if p:
            roots.append(p)
            roots.append(os.path.join(p, "xwechat_files"))
    roots.append(os.path.expandvars(r"%USERPROFILE%\Documents\xwechat_files"))
    roots.append(os.path.expandvars(r"%USERPROFILE%\xwechat_files"))
    try:
        users = os.path.join(os.environ.get("SystemDrive", "C:"), "Users")
        if os.path.isdir(users):
            for u in os.listdir(users):
                roots.append(os.path.join(users, u, "Documents", "xwechat_files"))
                roots.append(os.path.join(users, u, "xwechat_files"))
    except Exception:
        pass
    try:
        od = os.environ.get("OneDrive")
        if od:
            roots.append(os.path.join(od, "文档", "xwechat_files"))
            roots.append(os.path.join(od, "Documents", "xwechat_files"))
    except Exception:
        pass
    # v3.6.2: Windows"文档"文件夹实际位置（可能被重定向到 D/E/F 盘）：
    # 反馈场景——微信装在 D 盘、文档存 F 盘时，%USERPROFILE%\Documents 不存在，
    # 需按系统记录的文档真实位置找 xwechat_files
    try:
        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
        FOLDERID_Documents = _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                                   (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        sh = ctypes.windll.shell32
        p = ctypes.c_wchar_p()
        if sh.SHGetKnownFolderPath(ctypes.byref(FOLDERID_Documents), 0, None,
                                   ctypes.byref(p)) == 0:
            doc = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            if doc:
                roots.append(os.path.join(doc, "xwechat_files"))
    except Exception:
        pass
    # v4.0.1-4: 自动扫描扩展到所有固定盘符（GetLogicalDrives 枚举，含 C/D/E/F…），
    # 每个盘扫常见路径；不做全盘递归遍历（太慢），注册表+配置已覆盖 95% 场景
    try:
        import ctypes
        _mask = ctypes.windll.kernel32.GetLogicalDrives()
        _drives = []
        for _i in range(3, 26):  # C: 开始（A/B 为软驱）
            if _mask & (1 << _i):
                _drives.append(chr(ord("A") + _i) + ":")
        for drive in _drives:
            for _sub in ("xwechat_files",
                         os.path.join("WeChatFiles", "xwechat_files"),
                         os.path.join("WeChat Files", "xwechat_files"),
                         os.path.join("WeChat", "xwechat_files"),
                         os.path.join("Tencent", "xwechat_files")):
                roots.append(os.path.join(drive + "\\", _sub))
    except Exception:
        for drive in ("D:", "E:", "F:"):
            roots.append(os.path.join(drive + "\\", "xwechat_files"))
            roots.append(os.path.join(drive + "\\", "WeChatFiles", "xwechat_files"))
    return roots


def _root_has_account(root):
    """目录中是否存在微信账号数据目录（wxid_/openim_/v3_ 开头，或含 db_storage）"""
    try:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isdir(full):
                if is_account_dir(name):
                    return True
                if os.path.exists(os.path.join(full, "db_storage")):
                    return True
    except Exception:
        pass
    return False


def _resolve_data_root():
    """解析真实存在的微信数据根目录；找不到返回 config 配置值"""
    seen = set()
    for r in _candidate_roots():
        r = os.path.normpath(r)
        if not r or r in seen:
            continue
        seen.add(r)
        if os.path.isdir(r) and _root_has_account(r):
            return r
    for r in _candidate_roots():
        r = os.path.normpath(r)
        if os.path.isdir(r):
            return r
    return config["data_root"]


# ============================================================
# 主程序
# ============================================================
def _find_active_account(data_root=None):
    """找到活跃的微信账号目录（wxid_xxxxxxxx_xxxx 等格式）。
    v3.6.1: data_root 为空/找不到时自动解析真实数据目录。"""
    roots = []
    if data_root:
        roots.append(data_root)
    resolved = _resolve_data_root()
    if resolved not in roots:
        roots.append(resolved)
    for root in roots:
        try:
            # v4.1.0-审查: 设置里直接选中账号目录（wxid_xxx 层 / 任意含 db_storage
            # 的目录）时，listdir(root) 不再有账号子目录，原逻辑会返回 None 导致
            # 增强模式（密钥加载）失效——与 find_message_dbs 的账号目录兼容配套。
            if os.path.exists(os.path.join(root, "db_storage")):
                return root
            cands = []
            # 第一优先：账号目录（wxid_/openim_/v3_ 前缀）
            # v4.0.1 修复：原代码残留一个"只认本机账号目录后缀"的调试条件，
            # 导致其它用户的账号全部检测不到——已删除（后缀属于用户数据，不进源码）
            # v4.0.1-2: 多个账号时取 db_storage 最近活跃（最新写入）的目录，
            # 避免 listdir 顺序恰为旧账号时读错库
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isdir(full) and is_account_dir(name)                         and os.path.exists(os.path.join(full, "db_storage")):
                    cands.append(full)
            if cands:
                return max(cands, key=lambda d: _dir_recent_mtime(
                    os.path.join(d, "db_storage")))
            # 第二优先：任意含 db_storage 的目录（账号目录命名/前缀变化时兜底）
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isdir(full) and os.path.exists(os.path.join(full, "db_storage")):
                    cands.append(full)
            if cands:
                return max(cands, key=lambda d: _dir_recent_mtime(
                    os.path.join(d, "db_storage")))
        except Exception:
            continue
    return None


def _dir_recent_mtime(d):
    """目录内最近一次文件修改时间（用于多账号活跃判定）
    v4.5.2: 改为递归查找，因为 db_storage 顶层目录 mtime 不变，
    真正更新的是 message/ 子目录里的 .db-wal 文件。"""
    latest = 0.0
    try:
        for root, dirs, files in os.walk(d):
            for n in files:
                try:
                    m = os.path.getmtime(os.path.join(root, n))
                    if m > latest:
                        latest = m
                except OSError:
                    continue
    except OSError:
        pass
    return latest


# v4.0.1: 密钥文件加密存储（XOR 混淆 + base64，防明文泄露；旧明文密钥兼容读取）
_KEY_SALT = b"\x9f\x1c\x4a\x77\xe2\xb8\x3d\x5f\xa1\x0c\x6b\x94\xd7\x2e\x58\xc3"
# 便携密钥格式（WXKEY3）的主密钥材料：由程序内置常量派生，**不绑机器、不绑
# Windows 用户**——软件要发给别人用、也要能换电脑换账号继续用，DPAPI 那种"只有
# 本机当前用户可解"的存储在这里不成立。代价：安全性等于"拿到本程序就能解"，
# 属混淆级别而非访问控制；但比旧 XOR 强得多（AES-256-GCM + 每文件随机
# salt/nonce + 认证标签，改一个字节就解不开）。
_KEY_APP_SECRET = "wxnotify-portable-key-2026"
_KEY_APP_SALT = b"\x27\xbe\x51\xf4\x08\xa6\xd9\x3c\x75\x1e\xc2\x8b\x4a\xf7\x60\x93"


def _portable_key_bytes(salt):
    """便携格式的主密钥（32 字节）：只由程序内置常量 + 文件自带 salt 决定。

    不绑机器、不绑 Windows 用户，所以任何装了本程序的机器都能算出同一个值；
    salt 取自文件本身，既让每个文件有独立密钥，也让"改 salt"会被 GCM 认证标签
    当场发现（不参与派生的话它就是没人管的装饰字节）。"""
    return hashlib.pbkdf2_hmac("sha256", _KEY_APP_SECRET.encode("utf-8"),
                               _KEY_APP_SALT + salt, 150000, 32)


def _encrypt_key(k):
    """WXKEY3：便携加密（AES-256-GCM，每文件随机 salt+nonce）。"""
    raw = k.encode("utf-8")
    try:
        from Crypto.Cipher import AES
        salt, nonce = os.urandom(16), os.urandom(12)
        ct, tag = AES.new(_portable_key_bytes(salt), AES.MODE_GCM,
                          nonce=nonce).encrypt_and_digest(raw)
        return "WXKEY3:" + base64.b64encode(salt + nonce + tag + ct).decode("ascii")
    except Exception as e:
        log(f"[密钥] 便携加密不可用（{e}），退回旧混淆格式")
    x = bytes(b ^ _KEY_SALT[i % len(_KEY_SALT)] for i, b in enumerate(raw))
    return "WXKEY1:" + base64.b64encode(x).decode("ascii")


def _decrypt_key(s):
    s = (s or "").strip()
    if s.startswith("WXKEY3:"):
        try:
            from Crypto.Cipher import AES
            blob = base64.b64decode(s[7:])
            salt, nonce, tag, ct = blob[:16], blob[16:28], blob[28:44], blob[44:]
            d = AES.new(_portable_key_bytes(salt), AES.MODE_GCM,
                        nonce=nonce).decrypt_and_verify(ct, tag)
            return d.decode("utf-8")
        except Exception:
            # 认证标签不符（被改过/被截断）：明确失败，绝不把密文当明文往下传
            return None
    if s.startswith("WXKEY1:"):
        try:
            x = base64.b64decode(s[7:])
            d = bytes(b ^ _KEY_SALT[i % len(_KEY_SALT)] for i, b in enumerate(x))
            return d.decode("utf-8")
        except Exception:
            return None
    return s  # 旧明文兼容


def _has_account_key(account_wxid):
    """该账号有没有**自己的**密钥文件（通用 wx_key.txt 不算，那可能是别人的）。"""
    if not account_wxid:
        return False
    project_dir = os.path.dirname(os.path.abspath(__file__))
    return any(os.path.exists(p) for p in (
        os.path.join(APP_DIR, f"wx_key_{account_wxid}.txt"),
        os.path.join(project_dir, "wx_extract", f"key_{account_wxid}.txt")))


def _load_master_key(account_wxid=None):
    """加载微信数据库主密钥（v3.0，支持多账号）。
    优先加载指定账号的密钥 wx_key_<wxid>.txt，其次通用 wx_key.txt"""
    candidates = []
    if account_wxid:
        candidates.append(os.path.join(APP_DIR, f"wx_key_{account_wxid}.txt"))
    candidates.append(os.path.join(APP_DIR, "wx_key.txt"))
    # 项目目录兜底
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if account_wxid:
        candidates.append(os.path.join(project_dir, "wx_extract", f"key_{account_wxid}.txt"))
    candidates.append(os.path.join(project_dir, "wx_extract", "key.txt"))
    for p in candidates:
        try:
            if os.path.exists(p):
                raw = open(p, "r", encoding="utf-8").read().strip()
                key = _decrypt_key(raw) or ""
                if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key):
                    return key
        except Exception:
            pass
    return None


def main():
    load_config()
    # v4.0.1: 应用界面主题（浅色/深色）
    _apply_theme(config.get("theme", "light"))

    # v3.1: 全局异常钩子——任何未捕获异常都写入日志，便于定位偶发退出
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log(f"[致命异常] {tb}")
    sys.excepthook = _excepthook
    try:
        threading.excepthook = lambda args: log(
            f"[线程异常] {args.exc_type.__name__}: {args.exc_value}")
    except Exception:
        pass

    # 单实例保护：已有实例运行则本实例退出
    if not acquire_single_instance():
        log("[启动] 检测到已有实例在运行，本实例自动退出")
        return 0
    # 以注册表为准同步自启状态（注册表是权威来源）
    config["auto_start"] = auto_start_enabled()

    root = tk.Tk()
    root.withdraw()
    # 防止 root 被意外销毁导致 mainloop 退出
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    # v3.0: 初始化微信数据库读取（增强模式）
    wxdb = None
    if _HAS_WXDB:
        account_dir = _find_active_account(config["data_root"])
        account_wxid = os.path.basename(account_dir) if account_dir else None
        master_key = _load_master_key(account_wxid)
        if master_key and account_dir:
            try:
                db_root = os.path.join(account_dir, "db_storage")
                wxdb = WeChatDB(master_key, db_root)
                log(f"[启动] v3.0 增强模式已启用（账号: {account_wxid}）")
            except Exception as e:
                log(f"[启动] WeChatDB 初始化失败: {e}，回退到普通模式")
                wxdb = None
        else:
            if not master_key:
                log(f"[启动] 未找到账号 {account_wxid} 的数据库密钥，使用普通模式")
                log("[启动] 运行 wx_extract_key.py 可提取当前账号密钥以启用增强模式")
            if not account_dir:
                log("[启动] 未找到微信账号目录")

    q = queue.Queue()
    monitor = Monitor(q, wxdb)
    threading.Thread(target=monitor.loop, daemon=True).start()

    # v4.6.0: 企业微信提醒（原生未读红点）。和微信共用队列，横幅按 source 区分。
    wecom_monitor = None
    if _HAS_WECOM:
        try:
            wecom_monitor = WeComMonitor(q, config, log_fn=log,
                                         quiet_fn=in_quiet_time,
                                         rules_fn=check_alert_rules)
            threading.Thread(target=wecom_monitor.loop, daemon=True).start()
            log(f"[启动] 企业微信提醒：{'开' if config.get('wecom_on') else '关'}")
        except Exception as e:
            log(f"[企微] 启动监控失败: {e}")
    else:
        log("[企微] 缺少 wecom_notify 模块，企业微信提醒不可用")

    banner = Banner(root, q)
    win_holder = {}

    def open_settings(test=False):
        # v4.1.0: 设置窗口丢失/销毁时自动重建（社区反馈：右键托盘打不开
        # 设置界面，重启程序也一样，弹窗却正常）
        try:
            w = win_holder.get("win")
            if w is None:
                w = SettingsWindow(root, banner, quit_app)
                win_holder["win"] = w
            w.show()
        except Exception as e:
            log(f"[设置] open_settings 异常，重建窗口: {e}")
            try:
                w = SettingsWindow(root, banner, quit_app)
                win_holder["win"] = w
                w.show()
            except Exception as e2:
                log(f"[设置] open_settings 重建失败: {e2}")
        if test:
            banner.show("new")

    def quit_app():
        log("[退出] 托盘菜单触发退出")
        save_config()
        try:
            if win_holder.get("icon"):
                win_holder["icon"].stop()
        except Exception:
            pass
        try:
            root.after(200, root.destroy)
        except Exception:
            pass

    # 托盘回调跑在 pystray 自己的线程里，绝不能在那个线程直接动 Tk 控件
    # （主线程不在 mainloop 时 after() 会抛 "main thread is not in main loop"，
    # 表现就是"右键托盘打不开设置界面"）——一律投递回主线程执行。
    def _to_tk(fn):
        try:
            root.after(0, fn)
        except Exception as e:
            log(f"[tray] 投递主线程失败: {e}")

    tray = start_tray(lambda test=False: _to_tk(lambda: open_settings(test=test)),
                      lambda: _to_tk(quit_app))
    win_holder["icon"] = tray

    # v4.0.1: 按配置应用界面主题（浅色/深色），再创建设置窗口
    _apply_theme(config.get("theme", "light"))
    win = SettingsWindow(root, banner, quit_app)
    win_holder["win"] = win

    def status_tick():
        # 切主题会销毁旧窗口并重建（win_holder["win"] 指向新的），这里若闭包
        # 死攥着创建时的 win，定时刷新就会对着已销毁的窗口写状态
        w = win_holder.get("win")
        if w is not None:
            try:
                w.update_status(f"监控中（{monitor.file_count()} 个数据文件）")
            except Exception:
                pass
        root.after(3000, status_tick)

    root.after(1500, status_tick)

    if "--minimized" not in sys.argv:
        win.show()

    log(f"[启动] v{VERSION} 已运行（托盘：{'有' if tray else '无'}）")
    root.mainloop()
    log("[main] 主循环已退出，程序结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
