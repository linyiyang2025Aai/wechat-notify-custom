"""
微信数据库读取模块 v3.0
- 主密钥 + PBKDF2 派生 per-DB enc_key
- AES-256-CBC 逐页解密 SQLCipher 4 数据库
- sqlite3 查询会话和消息
- zstd 解压消息内容
"""
import hashlib, struct, hmac as hmac_mod, os, sqlite3, tempfile, threading, re, shutil, time, xml.etree.ElementTree as ET
from Crypto.Cipher import AES

try:
    import zstandard
    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def derive_enc_key(master_key: bytes, salt: bytes) -> bytes:
    """从主密钥 + 数据库 salt 派生 per-DB enc_key (PBKDF2-HMAC-SHA512, 256000)"""
    return hashlib.pbkdf2_hmac('sha512', master_key, salt, 256000, dklen=KEY_SZ)


def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    """解密单个 4096 字节页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def decrypt_database(src_path: str, dst_path: str, enc_key: bytes, workers: int = None):
    """解密整个数据库文件到 dst_path。

    v3.1.1: 并行解密——SQLCipher 4 每页使用独立 IV（CBC 无页间依赖），
    且 PyCryptodome 加解密时释放 GIL，多线程可显著提速（4核约3-5倍）。
    批量读取控制内存占用（每批 32 页 ≈ 128KB）。"""
    if workers is None:
        import os as _os
        workers = min(8, _os.cpu_count() or 2)
    from concurrent.futures import ThreadPoolExecutor
    file_size = os.path.getsize(src_path)
    BATCH = workers * 4
    with open(src_path, 'rb') as fin, open(dst_path, 'wb') as fout, \
            ThreadPoolExecutor(max_workers=workers) as ex:
        pgno = 1
        while True:
            batch = []
            for _ in range(BATCH):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                batch.append(page)
            if not batch:
                break
            # enumerate(start=pgno)：第 1 页走 header 特殊处理
            for dec in ex.map(lambda p: _decrypt_page(enc_key, p[1], p[0]),
                              enumerate(batch, start=pgno)):
                fout.write(dec)
            pgno += len(batch)


WAL_MAGIC = 0x377f0682      # 末位 0/1 表示校验和字序；x86 写出的通常是 0x377f0682
WAL_HDR_SZ = 32
WAL_FRAME_HDR_SZ = 24
SQLITE_DB_PAGES_OFFSET = 28  # 解密后页 1 里"数据库页数"字段（大端 4 字节）
# 注意：24 是 file change counter，28 才是 size-in-pages。
# 用三个真实解密样本核对过（@28 分别等于 文件大小/页大小 = 65 / 2308 / 26）。


def _wal_cksum(buf, s1, s2, be_words=False):
    """SQLite walChecksumBytes：8 字节一组成对累加（模 2^32）。

    be_words 由 WAL magic 末位决定：本机为小端（Windows x86），末位 0 →
    按小端字取值，末位 1 → 按大端字取值。计算结果始终**以大端**存进帧头
    （SQLite 规范：校验和字段一律大端）。"""
    fmt = '>II' if be_words else '<II'
    end = len(buf) - (len(buf) % 8)
    for i in range(0, end, 8):
        w1, w2 = struct.unpack_from(fmt, buf, i)
        s1 = (s1 + w1 + s2) & 0xFFFFFFFF
        s2 = (s2 + w2 + s1) & 0xFFFFFFFF
    return s1, s2


def read_wal_pages(wal_path: str, enc_key: bytes, page_sz: int = PAGE_SZ):
    """解密 WAL 里**已提交事务**的页，返回 (页号 -> 明文页, 提交后数据库页数)。

    v4.4 低延迟读取的核心：微信收到消息只写 -wal，checkpoint（实测常在
    10~30s 之后）才回写主库，旧实现只解密主库 → 横幅延迟中位数 18s。
    WAL 帧本身就是主库页的密文副本，可以直接用同一 enc_key 逐页解密。

    严格按 SQLite 的读法取舍，保证只会读到完整事务：
      - 帧的 salt1/salt2 必须与头一致，否则说明头已换代（checkpoint 后重启），
        其后的帧属于旧代，停止扫描；
      - 逐帧校验 checksum（输入 = 帧头前 8 字节 + 整页），失败即撕裂写入，停止；
      - 只有遇到 nTruncate != 0 的提交帧才把该事务的页并入结果，
        未提交的尾部事务一律忽略。

    结构合法但没有已提交页 → 返回 ({}, 0)（checkpoint 刚完成时就是这个状态，
    数据都在主库里）；格式不支持或读坏 → 返回 None，调用方退回"只读主库"，
    行为与旧版一致。"""
    try:
        with open(wal_path, 'rb') as f:
            data = f.read()
    except Exception:
        return None
    if len(data) == 0:
        return {}, 0                   # 空 WAL：合法，无帧
    if len(data) < WAL_HDR_SZ + WAL_FRAME_HDR_SZ + page_sz:
        return None
    magic, _ver, sz_page, _ckpt, salt1, salt2, ck1, ck2 = struct.unpack_from('>8I', data, 0)
    if (magic & 0xFFFFFFFE) != WAL_MAGIC or sz_page != page_sz:
        return None
    be_words = (magic & 1) == 1
    if _wal_cksum(data[:24], 0, 0, be_words) != (ck1, ck2):
        return None                      # 头撕裂：整代不可信
    s1, s2 = ck1, ck2
    pages, pending, db_pages = {}, {}, 0
    off = WAL_HDR_SZ
    frame_sz = WAL_FRAME_HDR_SZ + page_sz
    while off + frame_sz <= len(data):
        fh = data[off:off + WAL_FRAME_HDR_SZ]
        pgno, ntrunc, fs1, fs2 = struct.unpack_from('>4I', fh, 0)
        fc1, fc2 = struct.unpack_from('>2I', fh, 16)
        if (fs1, fs2) != (salt1, salt2):
            break
        body = data[off + WAL_FRAME_HDR_SZ:off + frame_sz]
        a1, a2 = _wal_cksum(fh[:8], s1, s2, be_words)
        b1, b2 = _wal_cksum(body, a1, a2, be_words)
        if (b1, b2) != (fc1, fc2):
            break
        s1, s2 = b1, b2
        if 1 <= pgno < 0x80000000:
            try:
                pending[pgno] = _decrypt_page(enc_key, body, pgno)
            except Exception:
                return None
        if ntrunc:                       # 提交帧：本事务生效
            pages.update(pending)
            pending.clear()
            db_pages = ntrunc
        off += frame_sz
    return pages, db_pages


def decode_content(data) -> str:
    """解码消息内容：二进制zstd / hex+zstd / 明文"""
    if data is None:
        return ''
    if isinstance(data, bytes):
        if len(data) >= 4 and data[:4] == ZSTD_MAGIC and _HAS_ZSTD:
            try:
                dctx = zstandard.ZstdDecompressor()
                return dctx.decompress(data).decode('utf-8', errors='replace')
            except Exception:
                pass
        try:
            return data.decode('utf-8', errors='replace')
        except Exception:
            return ''
    if isinstance(data, str):
        if len(data) > 10 and all(c in '0123456789abcdefABCDEF' for c in data[:20]):
            try:
                raw = bytes.fromhex(data)
                if raw[:4] == ZSTD_MAGIC and _HAS_ZSTD:
                    dctx = zstandard.ZstdDecompressor()
                    return dctx.decompress(raw).decode('utf-8', errors='replace')
                # 解出来不是 zstd：那它多半就是一条"全是十六进制字符"的普通消息
                # （订单号、验证码、色值…），按原样返回。以前会硬解成字节 → 乱码
                return data
            except Exception:
                return data  # hex 解析失败：按明文完整返回（不截断）
        return data  # 明文消息：完整返回（不截断，横幅自行做像素级截断）
    return str(data)


# 群消息把发送者 id 塞在 message_content 首行，形如 'alice_alias01:\n正文'。
# 这串 id 可能是 wxid_xxx、xxx@chatroom、xxx@openim，也可能是对方的微信号
# （纯字母数字别名）——v4.4.0 及之前只认前三类，别名前缀会漏剥，显示成"多出来的拼音"。
GROUP_SENDER_PREFIX = re.compile(r'^([A-Za-z0-9_\-.@]{2,48}):[ \t]*\r?\n')


def split_group_sender(content: str, is_group: bool):
    """剥掉群消息正文首行的发送者前缀，返回 (发送者 id 或空串, 去掉前缀的内容)。

    只在群聊里剥：私聊的 message_content 没有这一行。形状卡得很死——首行必须是
    纯 ASCII 的 id 样式且以半角冒号结尾、后面紧跟换行；带中文或空格的首行（比如
    真的 @ 了某人再补一句）原样保留，压缩过的二进制内容也进不来这个形状。"""
    if not content or not is_group:
        return '', content
    m = GROUP_SENDER_PREFIX.match(content)
    if not m:
        return '', content
    return m.group(1), content[m.end():]


def parse_sender_from_source(source_data) -> str:
    """从 msgsource XML 解析发送者 wxid"""
    if source_data is None:
        return ''
    text = decode_content(source_data)
    if not text:
        return ''
    try:
        root = ET.fromstring(text)
        # 群消息：从 atuserlist 或 strid 解析
        atuser = root.find('atuserlist')
        if atuser is not None and atuser.text:
            return atuser.text.strip()
        # 私聊：source 可能直接是 wxid
    except ET.ParseError:
        pass


ATUSERS_RE = re.compile(
    r'<atuserlist>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+(?:\s*,\s*[^<\]\s]+)*)\s*(?:\]\]>)?\s*</atuserlist>')


def parse_atusers(source_data) -> list:
    """从 msgsource XML 取被 @ 的成员 id 列表（wxid 或微信号别名，逗号分隔）。

    真库实测三种形状都要能读：<atuserlist>wxid_x</atuserlist>、
    <atuserlist><![CDATA[wxid_x]]></atuserlist>、以及多人的逗号列表。
    没有 atuserlist（没人被 @ 或 source 缺失）返回空列表。"""
    if source_data is None:
        return []
    text = decode_content(source_data)
    if not text:
        return []
    try:
        m = ATUSERS_RE.search(text)
        if not m:
            return []
        return [x.strip() for x in m.group(1).split(',') if x.strip()]
    except Exception:
        return []


def extract_xml_text(content: str):
    """从微信 XML 卡片消息中提取可读文本，返回 (kind, text)。

    kind: 'quote' 引用消息（优先展示被引用原文）
          'card'  卡片消息（title > des）
          ''      无可提取内容
    微信 XML 常含 CDATA 与非法转义，用正则而非 XML 解析器。
    """
    if not content:
        return ('', '')
    import re

    def clean(s):
        s = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', s, flags=re.DOTALL)
        s = re.sub(r'<[^>]+>', '', s)
        s = re.sub(r'\s+', ' ', s)
        return s.strip()

    # 1. 引用消息：refermsg 中被引用的原文
    m = re.search(r'<refermsg>.*?<content>(.*?)</content>', content, re.DOTALL)
    quoted = clean(m.group(1)) if m else ''
    # 2. title（回复内容 / 卡片标题）
    m = re.search(r'<title>(.*?)</title>', content, re.DOTALL)
    title = clean(m.group(1)) if m else ''
    # 3. des（描述）
    m = re.search(r'<des>(.*?)</des>', content, re.DOTALL)
    des = clean(m.group(1)) if m else ''

    if quoted:
        # 引用消息：原文为主，附带回复内容
        if title and title != quoted:
            return ('quote', f'{quoted[:24]}… {title[:16]}'.rstrip())
        return ('quote', quoted)
    if title:
        return ('card', title)
    if des:
        return ('card', des)
    return ('', '')  # 非 XML 且无可读标题时返回空


def log_if_any(msg):
    """免打扰列探测失败时的静默日志（独立于主程序日志系统）"""
    try:
        import logging
        logging.getLogger("wechat_db").warning(msg)
    except Exception:
        pass


CACHE_KEEP_DAYS = 7


def prune_cache(dirpath, keep_days=CACHE_KEEP_DAYS):
    """删掉 keep_days 天内没再碰过的解密明文副本，返回删除个数。

    缓存文件按库名固定命名，每次重解都会刷新 mtime，所以"还在用的副本"永远
    年轻，留不下来的只有换账号/删库之后没人再读的那些明文——恰恰是最该清掉的。
    正被其它连接使用的文件在 Windows 上删不掉，忽略即可（下次启动再试）。"""
    removed = 0
    try:
        cutoff = time.time() - keep_days * 86400
    except Exception:
        return 0
    for root, _dirs, files in os.walk(dirpath):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
    return removed


class WeChatDB:
    """微信数据库读取器"""

    def __init__(self, master_key_hex: str, db_root: str, temp_dir: str = None):
        self.master_key = bytes.fromhex(master_key_hex)
        self.db_root = db_root
        if temp_dir is None:
            # v3.1.1: 解密缓存持久化——存到固定目录（按账号隔离），
            # 程序重启后复用已解密的库（源文件 mtime 未变时不重解），避免每次启动全量解密
            cache_root = os.path.join(
                os.environ.get('APPDATA', os.path.expanduser('~')),
                '微信提醒', 'decrypted_cache')
            account = os.path.basename(os.path.dirname(os.path.normpath(db_root)))
            temp_dir = os.path.join(cache_root, account)
        os.makedirs(temp_dir, exist_ok=True)
        self.temp_dir = temp_dir
        try:
            n = prune_cache(temp_dir)
            if n:
                log_if_any(f"[缓存] 已清理 {n} 个 {CACHE_KEEP_DAYS} 天未使用的解密明文副本")
        except Exception:
            pass
        self._lock = threading.Lock()
        self._decrypted = {}  # db_rel -> (读取路径, 主库标识, WAL标识, 纯主库解密路径)
        self._nick_map = {}
        self._alias_map = {}   # 微信号别名 -> 显示名（群消息正文前缀给的可能就是别名）
        self._self_extra = set()  # 我自己那一行的 微信号/昵称（群消息 @我 判定用）
        # v4.4: WAL 直读合并状态 db_rel -> bool（监控线程据此缩短等待）
        self._wal_merge = {}
        # v4.0.1: 本机微信账号 wxid（过滤自己发出的消息用）
        self.wxid = os.path.basename(os.path.dirname(os.path.normpath(db_root)))
        self._msg_cols_cache = {}  # 表名 -> 列集合（消息表结构探测缓存）
        self._load_nicknames()

    def _db_path(self, *parts):
        return os.path.join(self.db_root, *parts)

    def wal_merge_ready(self) -> bool:
        """消息库的 WAL 直读合并是否已确认可用。"""
        return bool(self._wal_merge.get(os.path.join('message', 'message_0.db')))

    def probe_wal(self) -> bool:
        """低成本预探测：只校验消息库 WAL 头（magic / 页大小 / 头校验和），
        不解密任何页。合并状态原本要读过一次才知道，导致启动后的第一条消息
        仍等满 3.1s checkpoint；这里提前判定，第一条就走快速路径。"""
        rel = os.path.join('message', 'message_0.db')
        wal = self._db_path(rel) + '-wal'
        ok = False
        try:
            with open(wal, 'rb') as f:
                hdr = f.read(WAL_HDR_SZ)
            if len(hdr) == WAL_HDR_SZ:
                magic, _v, psz, _c, _s1, _s2, c1, c2 = struct.unpack_from('>8I', hdr, 0)
                if (magic & 0xFFFFFFFE) == WAL_MAGIC and psz == PAGE_SZ:
                    ok = _wal_cksum(hdr[:24], 0, 0, (magic & 1) == 1) == (c1, c2)
        except Exception:
            ok = False
        self._wal_merge[rel] = ok
        return ok

    def _merge_wal(self, base_path, dst_path, wal_path, enc_key):
        """把 WAL 已提交页并入「解密主库的副本」。

        返回 (副本路径或 None, 格式是否支持)。supported=False 表示这不是
        「刚 checkpoint 完所以 WAL 没内容」而是结构/密钥不匹配，调用方
        应保持旧的等 checkpoint 路径。绝不返回半成品库。"""
        try:
            got = read_wal_pages(wal_path, enc_key)
            if got is None:
                return None, False
            pages, db_pages = got
            if not pages:
                return None, True        # 没有已提交页（数据都在主库）
            shutil.copyfile(base_path, dst_path)
            # 合并产物按路径复用，若上次跑留下过 -wal/-shm，SQLite 会把残留 WAL
            # 当成更新的页盖在刚并入的数据上（读到旧消息）。先清掉自己这两个兄弟文件。
            for junk in (dst_path + '-wal', dst_path + '-shm'):
                try:
                    os.remove(junk)
                except OSError:
                    pass
            with open(dst_path, 'r+b') as f:
                for pgno, page in pages.items():
                    f.seek((pgno - 1) * PAGE_SZ)
                    f.write(page)
                if db_pages > 0:
                    if 1 not in pages:
                        # WAL 没带页 1：头里的页数仍是主库旧值，按提交页数纠正，
                        # 否则 SQLite 可能认为库比头声明的更大/更小
                        f.seek(SQLITE_DB_PAGES_OFFSET)
                        f.write(struct.pack('>I', db_pages))
                    f.seek(0, os.SEEK_END)
                    if f.tell() < db_pages * PAGE_SZ:
                        f.truncate(db_pages * PAGE_SZ)
            # 能读出对象定义才算可用（撕裂/密钥不符在这里就会被发现）
            try:
                conn = sqlite3.connect(f"file:{self._uri_path(dst_path)}?mode=ro", uri=True)
                conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return dst_path, True
        except Exception:
            try:
                os.remove(dst_path)
            except OSError:
                pass
            return None, False

    @staticmethod
    def _uri_path(p):
        """Windows 绝对路径转 file: URI（C:\\x\\y → /C:/x/y），反斜杠在 URI 里非法。"""
        p = os.path.abspath(p).replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return p.replace("%", "%25")

    def _get_decrypted(self, db_rel_path: str) -> str:
        """获取可查询的解密数据库路径（主库 + WAL 已提交页）。

        v4.4: 缓存失效条件从「主库 mtime」扩展为「主库 mtime+大小」和
        「WAL mtime_ns+大小」——消息只写 WAL 时主库不动，旧缓存键看不出变化，
        只能等 checkpoint；现在 WAL 一变就重新合并，秒级读到新消息。
        只有 WAL 变、主库没变时**跳过全量解密**，直接复用上次的解密主库做合并。
        """
        src = self._db_path(db_rel_path)
        try:
            st = os.stat(src)
        except OSError:
            return None
        src_id = (st.st_mtime_ns, st.st_size)
        wal_path = src + '-wal'
        wal_sig = None
        try:
            if os.path.exists(wal_path):
                ws = os.stat(wal_path)
                wal_sig = (ws.st_mtime_ns, ws.st_size)
        except OSError:
            wal_sig = None
        with self._lock:
            cached = self._decrypted.get(db_rel_path)
            if cached and cached[1] == src_id and cached[2] == wal_sig \
                    and os.path.exists(cached[0]):
                return cached[0]
            with open(src, 'rb') as f:
                salt = f.read(PAGE_SZ)[:SALT_SZ]
            enc_key = derive_enc_key(self.master_key, salt)
            dst = os.path.join(self.temp_dir, db_rel_path.replace('\\', '_').replace('/', '_'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            base = dst
            if cached and cached[1] == src_id and cached[3] and os.path.exists(cached[3]):
                base = cached[3]          # 主库没变，只是 WAL 变了：不重解
            else:
                decrypt_database(src, dst, enc_key)
            final = base
            merged, supported = (None, False)
            if wal_sig:
                merged, supported = self._merge_wal(
                    base, os.path.splitext(dst)[0] + '.walmerge.db', wal_path, enc_key)
                if merged:
                    final = merged
            self._wal_merge[db_rel_path] = supported
            self._decrypted[db_rel_path] = (final, src_id, wal_sig, base)
            return final

    def _load_nicknames(self):
        """加载联系人显示名映射：按 wxid 一份、按微信号别名一份。

        群消息正文首行的发送者 id 有时是 wxid，有时是对方设置的微信号别名
        （contact.alias，例如 alice_alias01），两种都得能换回可读名字。"""
        try:
            db = self._get_decrypted(os.path.join('contact', 'contact.db'))
            if not db:
                return
            conn = sqlite3.connect(db)
            rows = conn.execute('SELECT username, nick_name, remark, alias FROM contact').fetchall()
            self._self_extra = set()
            base = getattr(self, "wxid", "") or ""
            pure = re.sub(r'_[0-9a-fA-F]{4}$', '', base) if base else ''
            for wxid, nick, remark, alias in rows:
                show = remark or nick or ''
                if wxid and show:
                    self._nick_map[wxid] = show
                if alias and show:
                    self._alias_map.setdefault(alias, show)
                # 我自己那一行（username == 目录名或剥掉 _xxxx 设备后缀的纯 wxid）：
                # 它的 alias 是微信号、nick 是微信昵称——都是群消息 @我 的候选身份
                if wxid and wxid in (base, pure):
                    self._self_extra.update(x for x in (alias, nick, remark) if x)
            conn.close()
        except Exception:
            pass

    def _pure_wxid(self):
        """目录名剥掉尾部 _xxxx 设备后缀后的纯 wxid（没有后缀就原样返回）。"""
        base = getattr(self, "wxid", "") or ""
        m = re.match(r'^(.+)_[0-9a-fA-F]{4}$', base)
        return m.group(1) if m else base

    def self_names(self):
        """当前账号自己的身份串集合：目录名、纯 wxid、微信号别名、微信昵称。

        群消息"@我"判定用它；换账号后对象重建，集合自动跟着新账号走。"""
        names = {x for x in (self.wxid or '', self._pure_wxid()) if x}
        names.update(x for x in self._self_extra if x)
        names.discard('')
        return names

    def display_name(self, sender_id: str) -> str:
        """把发送者 id（wxid 或微信号别名）换成可读名字，换不到返回空串。

        刻意不返回 id 本身——拿 id 顶替名字，就是横幅上那串拼音的来源。"""
        if not sender_id:
            return ''
        nm = self._nick_map.get(sender_id) or self._alias_map.get(sender_id) or ''
        return '' if nm == sender_id else nm

    def get_latest_session(self) -> dict:
        """获取最新的会话（按 sort_timestamp 排序）"""
        try:
            db = self._get_decrypted(os.path.join('session', 'session.db'))
            if not db:
                return None
            conn = sqlite3.connect(db)
            row = conn.execute('''SELECT username, unread_count, summary, last_sender_display_name,
                                   last_msg_sender, last_timestamp, last_msg_type
                                   FROM SessionTable ORDER BY sort_timestamp DESC LIMIT 1''').fetchone()
            conn.close()
            if row:
                return {
                    'username': row[0],
                    'unread_count': row[1],
                    'summary': row[2] or '',
                    'last_sender_display': row[3] or '',
                    'last_msg_sender': row[4] or '',
                    'timestamp': row[5],
                    'msg_type': row[6],
                }
        except Exception:
            pass
        return None

    def get_unread(self, username: str):
        """读单个会话的未读数。跳会话之后用它核对"是不是真把这个会话打开了"——
        微信一打开会话就会把未读清零，这是不靠肉眼、不靠 OCR 的硬证据。

        读不到返回 None，**不是 0**：0 是合法未读数，拿 0 当哨兵会把
        "已经打开"和"库打不开"混成一回事。"""
        if not username:
            return None
        try:
            db = self._get_decrypted(os.path.join('session', 'session.db'))
            if not db:
                return None
            conn = sqlite3.connect(db)
            row = conn.execute(
                'SELECT unread_count FROM SessionTable WHERE username=?', (username,)).fetchone()
            conn.close()
            return None if row is None else int(row[0] or 0)
        except Exception:
            pass
        return None

    def get_no_disturb_set(self):
        """返回设置了「消息免打扰」的会话集合（私聊+群）。

        v4.0.1-7: 实测核验（当时仅对某一个联系人开了免打扰，contact 表里该联系人
        flag=515=0x203，含 0x200 位；其余未开免打扰的联系人 flag 均不含
        0x200）确认：Contact.flag 的 bit9（0x200）才是消息免打扰标志，
        覆盖私聊与群聊。旧实现用 chat_room_notify=0 判断完全错误——
        该列仅是"群/非群"标记（群全为 1），导致免打扰集合恒空。
        低版本/非 4.x 库无 flag 列时记日志并返回空，避免静默失效。"""
        try:
            db = self._get_decrypted(os.path.join('contact', 'contact.db'))
            if not db:
                return set()
            conn = sqlite3.connect(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(contact)").fetchall()}
            if 'flag' not in cols:
                log_if_any("contact 表无 flag 列，免打扰检测不可用（当前微信版本不支持）")
                conn.close()
                return set()
            rows = conn.execute(
                "SELECT username FROM contact WHERE (flag & 512) != 0"
            ).fetchall()
            conn.close()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def get_avatar(self, username):
        """按会话名取本地头像图片字节（JPEG/PNG），取不到返回 None。

        head_image 库与消息库共用同一个主密钥，走现成的"解密 + WAL 合并"路径，
        不需要额外的图片密钥——image_key.json 只管聊天图片（msg\\attach\\ 下的
        V2 密文），头像缓存里存的是明文 JPEG。
        微信只给"显示过"的头像落缓存，所以陌生发送者查不到行是常态，不是错误。"""
        if not username:
            return None
        try:
            db = self._get_decrypted(os.path.join('head_image', 'head_image.db'))
            if not db:
                return None
            conn = sqlite3.connect(f"file:{self._uri_path(db)}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT image_buffer FROM head_image WHERE username=?",
                    (username,)).fetchone()
            finally:
                conn.close()
            return bytes(row[0]) if row and row[0] else None
        except Exception:
            return None

    def _msg_cols(self, conn, table):
        """探测消息表的列集合（按表缓存）。"""
        cache = getattr(self, "_msg_cols_cache", None)
        if cache is None:
            cache = self._msg_cols_cache = {}
        if table not in cache:
            try:
                cache[table] = {r[1] for r in
                                conn.execute("PRAGMA table_info(" + table + ")").fetchall()}
            except Exception:
                cache[table] = set()
        return cache[table]

    def _self_rowid(self, conn):
        """我自己在该消息库 Name2Id 里的 rowid（real_sender_id 指向它）。

        Name2Id.user_name 存的是纯 wxid，而账号目录名带 _xxxx 设备后缀，
        两种形态都试；查不到返回 None（调用方按"不判"处理）。"""
        for name in (self._pure_wxid(), getattr(self, "wxid", "")):
            if not name:
                continue
            try:
                row = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 1",
                    (name,)).fetchone()
            except Exception:
                return None
            if row:
                return row[0]
        return None

    def _latest_row(self, conn, table):
        """该表按 local_id 的最新一行，并判定它是不是自己发出的。

        返回 (row, sent)。row 固定六列：
        (local_type, source, message_content, compress_content, create_time, local_id)。

        微信 4.x 的 message 表没有 isSend 列，也没有可靠的 upload_status：
        2026-10-28 真库核对，收到的和自己发的 upload_status 全是 0——旧的
        "自己发的 upload_status 非 0"启发式等于没过滤，自己发的消息会被
        当成别人发来的弹横幅。真正的判据是 real_sender_id：它指向该库
        Name2Id 的 rowid，自己发出的消息恒等于"我"的 rowid。
        isSend / real_sender_id 都探测不到时不判（sent=False），宁可照旧
        提醒，也不吞掉别人的消息。"""
        cols = self._msg_cols(conn, table)
        base = "local_type, source, message_content, compress_content, create_time, local_id"
        if "isSend" in cols:
            row = conn.execute(
                f"SELECT {base}, isSend FROM {table} ORDER BY local_id DESC LIMIT 1").fetchone()
            if not row:
                return None, False
            return row[:6], bool(row[6])
        if "real_sender_id" in cols:
            row = conn.execute(
                f"SELECT {base}, real_sender_id FROM {table} ORDER BY local_id DESC LIMIT 1").fetchone()
            if not row:
                return None, False
            sid = self._self_rowid(conn)
            return row[:6], (sid is not None and row[6] == sid)
        row = conn.execute(
            f"SELECT {base} FROM {table} ORDER BY local_id DESC LIMIT 1").fetchone()
        return row, False

    def get_latest_message(self, session_username: str = None) -> dict:
        """获取最新一条消息（发件人+内容）。
        先从 session.db 找到最新会话，再只查该会话对应的消息表（Msg_<md5(username)>）。
        这样避免获取到其他会话的旧消息（不同会话的 local_id 是独立的）。
        传入 session_username 则点名读那个会话（以前一进函数就被覆盖成空串，
        等于这个参数不存在）。"""
        try:
            import hashlib
            # 1. 从 session.db 获取最新会话
            session_info = self.get_latest_session()
            session_sender = ''
            session_sender_display = ''
            if not session_username and session_info:
                session_sender = session_info.get('last_msg_sender', '')
                session_sender_display = session_info.get('last_sender_display', '')
                session_username = session_info.get('username', '')

            # 2. 从 message_0.db 获取最新消息
            db = self._get_decrypted(os.path.join('message', 'message_0.db'))
            if not db:
                if session_sender_display or session_sender:
                    # 这条是"消息库读不出来、只能靠会话表"的退路：显示名同样不许拿 id 顶
                    who = session_sender or session_username or ''
                    return {
                        'type': 0,
                        'sender_wxid': who,
                        'sender_nick': (session_sender_display or self.display_name(who)
                                        or ('群成员' if '@chatroom' in (session_username or '')
                                            else '微信好友')),
                        'content': '',
                        'timestamp': session_info.get('timestamp', 0) if session_info else 0,
                    }
                return None
            conn = sqlite3.connect(db)

            # 3. 优先查找最新会话对应的消息表（Msg_<md5(username)>）
            best = None
            if session_username:
                md5 = hashlib.md5(session_username.encode()).hexdigest()
                try:
                    row, sent = self._latest_row(conn, f"Msg_{md5}")
                except Exception:
                    row, sent = None, False
                if sent:
                    # 最新一条是自己刚发出的：本次 WAL 变化来自"我发消息"，
                    # 不是别人来消息。直接标记静默——绝不把上一条收到的旧消息
                    # 翻出来再弹一次（那会张冠李戴成"对方又发了一条"）。
                    return {"self_sent": True,
                            "session_key": session_username,
                            "local_id": row[5] if row else 0,
                            "timestamp": row[4] if row else 0}
                best = row

            # 4. 如果目标表没有消息，回退到全局搜索（各表里 create_time 最新的）
            if not best:
                tables = [t[0] for t in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'").fetchall()]
                for t in tables:
                    try:
                        row, sent = self._latest_row(conn, t)
                        # 自己发出的不参与回退：否则给 A 发消息会翻出 B 的旧消息
                        if sent or not row:
                            continue
                        # v4.0.1-2: 跨表比较用 create_time（各表 local_id 独立递增，
                        # 跨表比 local_id 会选中"id 大但时间旧"的消息）
                        if best is None or row[4] > best[4]:
                            best = row
                    except Exception:
                        continue
            conn.close()
            if not best:
                return None

            local_type, source, msg_content, compress_content, create_time, local_id = best
            content = decode_content(msg_content) if msg_content else decode_content(compress_content)

            # 群消息 content 首行带发送者前缀（'wxid_xxx:\n'、对方微信号别名如
            # 'alice_alias01:\n'、'xxx@chatroom'/'xxx@openim'），先剥掉再判类型：
            # 既避免 XML 检测与提取失败，也避免这串 id 被当成正文显示到横幅上
            is_group = bool(session_username and '@chatroom' in session_username)
            content_sender, pure_content = split_group_sender(content, is_group)
            if content_sender and local_type == 1:
                content = pure_content

            # 根据消息类型转换显示内容
            type_names = {
                1: "文本", 3: "图片", 34: "语音", 37: "好友确认",
                42: "名片", 43: "视频", 47: "表情", 48: "位置",
                49: "链接", 50: "视频通话", 10000: "系统", 10002: "撤回",
            }
            display_content = content
            # 转账/红包/付款识别（微信 4.x：appmsg type=2000 微信转账 / 2001 微信红包 / title 含"付款给"）
            if pure_content:
                if '<type>2000</type>' in pure_content or '微信转账' in pure_content:
                    m = re.search(r'收到转账([\d.]+)元', pure_content)
                    display_content = f"[转账 {m.group(1)}元]" if m else "[转账]"
                elif '<type>2001</type>' in pure_content or '微信红包' in pure_content:
                    display_content = "[红包]"
                elif '付款给' in pure_content:
                    m = re.search(r'付款给\s*(?:<!\[CDATA\[)?([^<\]]+)', pure_content)
                    display_content = f"[转账] 付款给 {m.group(1).strip()[:12]}" if m else "[转账]"
            if local_type == 3:
                display_content = "[图片]"
            elif local_type == 34:
                # 语音消息：解析 voicelength（毫秒）
                display_content = "[语音]"
                if content:
                    m = re.search(r'voicelength="(\d+)"', content)
                    if m:
                        ms = int(m.group(1))
                        sec = ms // 1000
                        if sec >= 60:
                            display_content = f"[语音 {sec//60}:{sec%60:02d}]"
                        else:
                            display_content = f"[语音 {sec}\"]"
            elif local_type == 43:
                display_content = "[视频]"
                if content:
                    m = re.search(r'playlength="(\d+)"', content)
                    if m:
                        sec = int(m.group(1))
                        display_content = f"[视频 {sec//60}:{sec%60:02d}]"
            elif local_type == 47:
                display_content = "[表情]"
            elif local_type == 48:
                display_content = "[位置]"
                if content:
                    m = re.search(r'label="([^"]+)"', content)
                    if m:
                        display_content = f"[位置] {m.group(1)[:20]}"
            elif local_type == 42:
                display_content = "[个人名片]"
            elif local_type == 49:
                # 链接/文件消息：尝试从 XML 解析标题
                display_content = "[链接]"
                if content and "<title>" in content:
                    m = re.search(r"<title>(.*?)</title>", content, re.DOTALL)
                    if m:
                        title = m.group(1).strip()[:30]
                        # 判断是文件还是链接
                        if "<attach" in content or "fileext" in content:
                            display_content = f"[文件] {title}"
                        else:
                            display_content = f"[链接] {title}"
            elif local_type == 10002:
                display_content = "[撤回了一条消息]"
            elif local_type == 1:
                # 文本消息：如果内容是 XML（引用回复/小程序卡片等），提取可读文本
                if pure_content and (pure_content.strip().startswith("<?xml") or pure_content.strip().startswith("<msg")):
                    kind, text = extract_xml_text(pure_content)
                    if kind == 'quote':
                        display_content = f"[引用] {text}"
                    elif kind == 'card':
                        display_content = f"[卡片] {text}"
                    else:
                        display_content = "[消息]"
            else:
                # 未知类型：若是 XML 卡片消息，尝试提取标题/引用原文
                if pure_content and (pure_content.strip().startswith("<?xml") or pure_content.strip().startswith("<msg")):
                    kind, text = extract_xml_text(pure_content)
                    if kind == 'quote':
                        display_content = f"[引用] {text}"
                    elif kind == 'card':
                        display_content = f"[卡片] {text}"
                    else:
                        display_content = f"[{type_names.get(local_type, '消息')}]"
                else:
                    display_content = f"[{type_names.get(local_type, '消息')}]"

            # 3. 发送者显示名：群聊要的是"群里的昵称"，私聊要的是"备注 / 本来昵称"，
            #    任何情况下都不拿 wxid、微信号别名这串 id 去顶替名字。
            #    正文前缀里的 id 才是"这一条"的发送者；session.db 的显示名只描述
            #    "该会话的最后一条"，两边都是可比较形式却对不上时不能信它。
            #    （前缀是微信号别名、session 是 wxid 时比不了，只能采信 session 的显示名，
            #     这也是微信自己会话列表用的那个字段）
            actual_content = display_content
            source_sender = parse_sender_from_source(source)
            comparable = re.match(r'^(?:wxid_[A-Za-z0-9]+|[^@\s]+@(?:chatroom|openim))$',
                                  content_sender or '')
            session_comparable = re.match(r'^(?:wxid_[A-Za-z0-9]+|[^@\s]+@(?:chatroom|openim))$',
                                          session_sender or '')
            same_sender = bool(content_sender and session_sender and content_sender == session_sender)
            not_comparable = bool(content_sender and not comparable)   # 前缀是微信号别名
            actual_sender = session_sender or content_sender or source_sender or session_username or ''
            nick = ''
            if session_sender_display and (not content_sender or same_sender or not_comparable):
                nick = session_sender_display
            if not nick:
                nick = (self.display_name(content_sender) or self.display_name(session_sender)
                        or self.display_name(actual_sender))
            if not nick:
                nick = '群成员' if is_group else '微信好友'

            return {
                'type': local_type,
                'sender_wxid': actual_sender,
                'sender_nick': nick,
                'content': actual_content.strip(),
                'timestamp': create_time,
                'local_id': local_id,
                # v3.1: 会话分类——群聊 vs 私聊（用于横幅显示"xx 在 xx 群里发了消息"）
                'is_group': is_group,
                # 群名同理：查不到就留空，让横幅走别的文案，别把 12345678901@chatroom
                # 这种 id 当群名显示出去
                'group_name': self.display_name(session_username) if is_group else '',
                # v3.6: 会话唯一标识（按会话去重用）
                'session_key': session_username or session_sender or '',
                # 被 @ 的成员 id 列表（wxid/微信号别名，来自 source XML 的 atuserlist；
                # 没人被 @ 或 source 缺失时是空列表）——群消息"@我"判定用
                'mentioned': parse_atusers(source),
            }
        except Exception:
            pass
        return None

    def refresh(self, message_db_only=True):
        """WAL 变化后调用（保持旧接口）。

        v4.4: 不再失效消息库缓存——缓存键已包含 WAL 的 mtime+大小，变化时
        _get_decrypted 会自动重新合并，主库没变还会跳过全量解密。旧的 pop
        每次检测都强制重解 14MB 消息库（实测占内容读取 1s 里的 ~0.35s）。
        message_db_only=False 仍全量清空并重读昵称（账号切换用）。"""
        if message_db_only:
            return
        with self._lock:
            self._decrypted.clear()
            self._wal_merge.clear()
        self._load_nicknames()
