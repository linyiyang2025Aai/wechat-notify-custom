# -*- coding: utf-8 -*-
"""把改过的 wechat_notify.py 注入原件，生成一个新的单文件 exe。

背景
----
微信提醒是用 PyInstaller（onefile 模式）打包的。归档里主模块 `wechat_notify` 那一条
存的是一段 zlib 压缩的 marshal code object。所以改功能不一定非要重新打包：把新代码
编译成 code object、追加进归档、改掉目录里那一条的偏移和长度即可，其余 1000 多个
条目一个字节都不用动。

用法
----
    python tools/patch_exe.py --src-exe "D:\\path\\微信提醒 v4.6.1.exe" \\
                              --out-exe "D:\\path\\微信提醒 v4.6.1 自定义位置.exe"

    --src-py 默认取本仓库根目录的 wechat_notify.py。

要求
----
* 编译用的 Python 版本要和 exe 内置的 Python 主次版本一致（本项目的 exe 是 3.13）。
  脚本会读归档 cookie 里的 pyvers 和当前解释器对比，不一致直接报错。
* 不需要安装 PyInstaller。
"""
import argparse
import marshal
import os
import struct
import sys

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_SIZE = 88          # magic(8) + package_len(4) + toc_off(4) + 保留(4) + pyvers(4) + pylibname(64)
TARGET_NAME = "wechat_notify"
TARGET_TYPCD = b"s"       # 主脚本


def read_archive(data):
    """解析 CArchive：返回 (cookie 起点, package_len, 归档起点, 条目列表, TOC 结束位置)。"""
    magic_pos = data.rfind(MAGIC)
    if magic_pos < 0:
        raise SystemExit("找不到 PyInstaller 归档 cookie，这个 exe 不是 PyInstaller 打包的")
    cookie = data[magic_pos:magic_pos + COOKIE_SIZE]
    if len(cookie) != COOKIE_SIZE:
        raise SystemExit("cookie 长度异常（文件被截断？）")
    pkg_len, toc_off = struct.unpack("!II", cookie[8:16])
    pyvers = struct.unpack("!i", cookie[20:24])[0]
    archive_start = len(data) - pkg_len          # cookie 也算在 package_len 内
    entries, pos = [], archive_start + toc_off
    while pos + 18 <= magic_pos:
        entrylen, offset, cmplen, unlen, cflag, typcd = struct.unpack("!IIIIBc", data[pos:pos + 18])
        # 目录之后可能跟着自定义追加的数据（本工具生成的新 exe 就是这样），
        # 所以遇到不合法条目就视作目录结束，而不是报错
        if entrylen < 18 or pos + entrylen > magic_pos or typcd not in b"bmosxzdl":
            break
        name = data[pos + 18:pos + entrylen].decode("utf-8", "replace").rstrip("\x00")
        if not name or any(ord(ch) < 32 or ord(ch) > 126 for ch in name):
            break
        entries.append(dict(tocpos=pos, entrylen=entrylen, offset=offset, cmplen=cmplen,
                            unlen=unlen, cflag=cflag, typcd=typcd, name=name))
        pos += entrylen
    if len(entries) < 50:
        raise SystemExit("只解析到 %d 个条目，不像正常的 PyInstaller 归档，已中止" % len(entries))
    return magic_pos, cookie, pkg_len, archive_start, pyvers, entries, pos


def has_const(code, needle, depth=0):
    """递归扫代码对象常量表，确认改动确实编进了 code object。"""
    if depth > 25:
        return False
    for c in code.co_consts:
        if isinstance(c, str) and needle in c:
            return True
        if hasattr(c, "co_consts") and has_const(c, needle, depth + 1):
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="把改过的源码注入 PyInstaller onefile exe")
    ap.add_argument("--src-exe", required=True, help="原版 exe（不会被修改）")
    ap.add_argument("--out-exe", required=True, help="输出的新 exe 路径")
    ap.add_argument("--src-py", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wechat_notify.py"),
        help="改过的源码，默认仓库根目录的 wechat_notify.py")
    args = ap.parse_args()

    if not os.path.isfile(args.src_exe):
        raise SystemExit("找不到原版 exe: %s" % args.src_exe)
    if not os.path.isfile(args.src_py):
        raise SystemExit("找不到源码: %s" % args.src_py)

    src = open(args.src_py, "rb").read().decode("utf-8-sig")
    code = compile(src, TARGET_NAME + ".py", "exec")
    payload = marshal.dumps(code)

    data = open(args.src_exe, "rb").read()
    magic_pos, cookie, pkg_len, archive_start, pyvers, entries, toc_end = read_archive(data)

    py_major, py_minor = sys.version_info[:2]
    if (pyvers // 100) != py_major or (pyvers % 100) != py_minor:
        raise SystemExit("版本不匹配：归档里的 Python 是 %d.%d，当前解释器是 %d.%d。"
                         "marshal 出来的 code object 必须与 exe 内置的 Python 同主次版本，"
                         "请换用 Python %d.%d 再跑本脚本。"
                         % (pyvers // 100, pyvers % 100, py_major, py_minor,
                            pyvers // 100, pyvers % 100))

    if toc_end != magic_pos:
        raise SystemExit("归档目录与 cookie 不连续（可能已经是被改过的 exe），为了安全已中止")

    target = [e for e in entries if e["name"] == TARGET_NAME and e["typcd"] == TARGET_TYPCD]
    if len(target) != 1:
        raise SystemExit("定位主模块条目失败，找到 %d 个候选" % len(target))
    t = target[0]
    print("归档：%d 个条目，归档起点 %d，主模块 offset=%d cmplen=%d cflag=%d"
          % (len(entries), archive_start, t["offset"], t["cmplen"], t["cflag"]))

    # 新数据放在归档目录之后、cookie 之前：其它条目的偏移全部不受影响
    new_offset = magic_pos - archive_start
    buf = bytearray(data[:magic_pos])
    struct.pack_into("!IIIIBc", buf, t["tocpos"], t["entrylen"], new_offset,
                     len(payload), len(payload), 0, TARGET_TYPCD)
    buf += payload
    new_cookie = bytearray(cookie)
    struct.pack_into("!I", new_cookie, 8, len(buf) + len(cookie) - archive_start)
    buf += new_cookie

    open(args.out_exe, "wb").write(bytes(buf))
    print("已写出 %s（%d 字节，原 %d）" % (args.out_exe, os.path.getsize(args.out_exe), len(data)))

    # 自检：重新解析生成的 exe，把主模块解回来
    v = open(args.out_exe, "rb").read()
    _m, _c, _p, as2, pyv2, entries2, _t2 = read_archive(v)
    e2 = [e for e in entries2 if e["name"] == TARGET_NAME and e["typcd"] == TARGET_TYPCD][0]
    blob = v[as2 + e2["offset"]: as2 + e2["offset"] + e2["cmplen"]]
    if e2["cflag"] & 1:
        import zlib
        blob = zlib.decompress(blob)
    code2 = marshal.loads(blob)
    ok = has_const(code2, "banner_pos_x") if "banner_pos_x" in src else True
    print("自检：条目 %d 个，解出模块 %s，长度一致=%s，改动已写入=%s"
          % (len(entries2), code2.co_name, e2["unlen"] == len(blob), ok))
    if not (e2["unlen"] == len(blob) and ok):
        raise SystemExit("自检未通过，生成的 exe 不可用")


if __name__ == "__main__":
    main()
