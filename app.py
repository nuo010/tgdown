from telethon import TelegramClient, events
import os
import asyncio
import threading
import json
import shutil
import logging
import sys
import sqlite3
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI

try:
    import socks  # type: ignore
except ImportError:  # pragma: no cover - 仅在未安装 PySocks 时触发
    socks = None  # type: ignore

try:
    from ai import generate_video_filename_from_text
except ImportError:
    generate_video_filename_from_text = None
from fastapi.responses import FileResponse
import uvicorn

# 统一日志：输出到 stdout，便于 docker logs 查看
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger(__name__)

# ---------- 配置（从文件读取） ----------
# 数据目录：脚本同级下的 data（本地）；Docker 挂载时为 /data
SCRIPT_DIR = Path(__file__).resolve().parent
_data_root = Path("/data")
DATA_DIR = _data_root if (_data_root / "config.json").exists() else (SCRIPT_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "downloads.db"

# 若 data 下没有配置且脚本同目录有 config.json，则复制过去便于迁移
if not CONFIG_PATH.exists() and (SCRIPT_DIR / "config.json").exists():
    shutil.copy2(SCRIPT_DIR / "config.json", CONFIG_PATH)


def load_config():
    if not CONFIG_PATH.exists():
        log.error("配置文件不存在: %s（请将 config.json 放入 data 目录）", CONFIG_PATH)
        sys.exit(1)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
    except json.JSONDecodeError as e:
        log.error("配置文件 JSON 格式错误: %s", e)
        sys.exit(1)
    for key in ("api_id", "api_hash"):
        if key not in c:
            log.error("配置文件缺少必填项: %s", key)
            sys.exit(1)
    return c


_config = load_config()
api_id = int(_config["api_id"])
api_hash = str(_config["api_hash"]).strip()
DOWNLOAD_PATH = _config.get("download_path", "./downloads")
WEB_PORT = int(_config.get("web_port", 8765))
WEB_BIND = _config.get("web_bind", "0.0.0.0")  # 可选：仅本机访问填 127.0.0.1
TARGET_GROUP_NAME = _config.get("target_group_name", "downapp")
CONCURRENT_DOWNLOADS = max(1, int(_config.get("concurrent_downloads", 3)))
PUSH_STATUS_TO_GROUP = _config.get("push_status_to_group", True)
DOWNLOAD_RETRIES = max(0, int(_config.get("download_retries", 2)))  # 下载失败或卡住时重试次数，默认 2
# 若连续多少秒没有新的下载进度则判定为卡住并重试；0 表示不检测（大文件友好）
DOWNLOAD_STALL_SECONDS = max(0, int(_config.get("download_stall_seconds", 600)))


def _build_tg_proxy_from_config():
    """根据 config.json 里的 tg_proxy_* 字段构造 Telethon 代理配置。"""
    t = str(_config.get("tg_proxy_type") or "").lower().strip()
    host = str(_config.get("tg_proxy_host") or "").strip()
    port = _config.get("tg_proxy_port") or 0
    username = _config.get("tg_proxy_username") or None
    password = _config.get("tg_proxy_password") or None
    if not t or not host or not port:
        return None
    if socks is None:
        log.error("已配置 tg_proxy_* 但未安装 PySocks，忽略 Telegram 代理。请运行: pip install pysocks")
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        log.error("tg_proxy_port 配置非法: %r", port)
        return None
    tmap = {
        "socks5": getattr(socks, "SOCKS5", None),
        "socks4": getattr(socks, "SOCKS4", None),
        "http": getattr(socks, "HTTP", None),
        "https": getattr(socks, "HTTP", None),
    }
    proxy_type = tmap.get(t)
    if proxy_type is None:
        log.error("不支持的 tg_proxy_type: %s（可选值：socks5/socks4/http）", t)
        return None
    log.info("使用 Telegram 代理: %s://%s:%s", t, host, port)
    return (proxy_type, host, int(port), True, username, password)


# 下载目录：与 data 同级（相对路径基于 DATA_DIR 的父目录）
DOWNLOAD_PATH = Path(DOWNLOAD_PATH)
if not DOWNLOAD_PATH.is_absolute():
    DOWNLOAD_PATH = DATA_DIR.parent / DOWNLOAD_PATH
DOWNLOAD_PATH = str(DOWNLOAD_PATH)
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

SESSION_PATH = str(DATA_DIR / "session")
_TG_PROXY = _build_tg_proxy_from_config()
if _TG_PROXY:
    client = TelegramClient(SESSION_PATH, api_id, api_hash, proxy=_TG_PROXY)
else:
    client = TelegramClient(SESSION_PATH, api_id, api_hash)

# ---------- 状态与持久化（线程安全） ----------
_state_lock = threading.Lock()
_active_downloads = {}  # {(chat_id, msg_id): {status, progress_pct, sender, ...}}
_download_history = []
_history_max = 100
_pending_list = []  # [{chat_id, message_id, sender_name, added_at, status}]
_download_queue = None
_queue_size = 0
_target_chat_id = None  # downapp 群的 chat_id，用于推送状态


def _download_id(chat_id: int, message_id: int) -> str:
    return f"{chat_id}_{message_id}"


def _add_pending(chat_id: int, message_id: int, sender_name: str):
    with _state_lock:
        _pending_list.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_name": sender_name,
            "file_name": "",
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "waiting",
        })


def _set_pending_status(chat_id: int, message_id: int, status: str):
    with _state_lock:
        for p in _pending_list:
            if p["chat_id"] == chat_id and p["message_id"] == message_id:
                p["status"] = status
                break


def _set_pending_file_name(chat_id: int, message_id: int, file_name: str):
    with _state_lock:
        for p in _pending_list:
            if p["chat_id"] == chat_id and p["message_id"] == message_id:
                p["file_name"] = file_name or ""
                break


def _remove_pending(chat_id: int, message_id: int):
    with _state_lock:
        global _pending_list
        _pending_list = [p for p in _pending_list if not (p["chat_id"] == chat_id and p["message_id"] == message_id)]


def _update_active(download_id: str, **kwargs):
    with _state_lock:
        if download_id not in _active_downloads:
            _active_downloads[download_id] = {}
        _active_downloads[download_id].update(kwargs)


def _remove_active(download_id: str):
    with _state_lock:
        _active_downloads.pop(download_id, None)


def _add_history(record):
    with _state_lock:
        _download_history.insert(0, record)
        if len(_download_history) > _history_max:
            _download_history.pop()


# ---------- SQLite 成功记录 ----------
_db_lock = threading.Lock()


def _init_db():
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                username TEXT NOT NULL,
                message_time TEXT,
                download_time TEXT NOT NULL,
                duration_sec INTEGER DEFAULT 0
            )
        """)
        try:
            conn.execute("ALTER TABLE download_record ADD COLUMN duration_sec INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()


def _save_download_record(file_path: str, file_size: int, username: str, message_time: str, download_time: str, duration_sec: int = 0):
    file_name = os.path.basename(file_path)
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO download_record (file_path, file_name, file_size, username, message_time, download_time, duration_sec) VALUES (?,?,?,?,?,?,?)",
            (file_path, file_name, file_size, username or "未知", message_time or "", download_time, duration_sec),
        )
        conn.commit()
        conn.close()


def _get_download_records(limit: int = 200):
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, file_path, file_name, file_size, username, message_time, download_time, duration_sec FROM download_record ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
    return [dict(r) for r in rows]


def _get_status():
    with _state_lock:
        data = {
            "active_downloads": [{"id": k, **v} for k, v in _active_downloads.items()],
            "pending_list": list(_pending_list),
            "history": list(_download_history),
            "queue_size": _queue_size,
            "concurrent_downloads": CONCURRENT_DOWNLOADS,
        }
    try:
        data["download_records"] = _get_download_records()
    except Exception as e:
        log.warning("读取下载记录失败: %s", e)
        data["download_records"] = []
    return data


def _is_in_pending(chat_id: int, message_id: int) -> bool:
    with _state_lock:
        return any(p["chat_id"] == chat_id and p["message_id"] == message_id for p in _pending_list)


async def _push_status(text: str):
    """把状态消息发到 downapp 群"""
    if not PUSH_STATUS_TO_GROUP or _target_chat_id is None:
        return
    try:
        await client.send_message(_target_chat_id, text)
    except Exception as e:
        log.warning("推送状态到群失败: %s", e)


async def _ensure_target_chat():
    """确保 _target_chat_id 已指向目标群（按名称 TARGET_GROUP_NAME 查找）。"""
    global _target_chat_id
    if _target_chat_id is not None:
        return
    try:
        chat = await client.get_entity(TARGET_GROUP_NAME)
        _target_chat_id = chat.id
        log.info("目标群 chat_id 初始化为 %s", _target_chat_id)
    except Exception as e:
        log.warning("根据名称 %s 获取目标群失败: %s", TARGET_GROUP_NAME, e)


def _message_has_video(msg) -> bool:
    """判断消息是否包含视频（含 .video 或以文档形式发送的视频）。"""
    if getattr(msg, "video", None):
        return True
    try:
        media = getattr(msg, "media", None)
        doc = getattr(media, "document", None) if media else None
        if not doc:
            return False
        for attr in getattr(doc, "attributes", []) or []:
            if type(attr).__name__ == "DocumentAttributeVideo":
                return True
        mime = getattr(doc, "mime_type", None) or ""
        if isinstance(mime, str) and mime.startswith("video/"):
            return True
    except Exception:
        pass
    return False


def _get_message_raw_text(msg) -> str:
    """从 Telethon Message 取正文/字幕，兼容多种属性（有时事件里未带全）。"""
    if msg is None:
        return ""
    raw = (
        getattr(msg, "raw_text", None)
        or getattr(msg, "text", None)
        or getattr(msg, "message", None)
    )
    return (raw or "").strip()


async def _push_raw_text(msg, raw_text: str | None = None):
    """只将消息的正文/字幕发到群。若传 raw_text 则优先使用（避免事件里未带全）。"""
    if not PUSH_STATUS_TO_GROUP or _target_chat_id is None:
        return
    raw = (raw_text if raw_text is not None else _get_message_raw_text(msg)) or ""
    try:
        await client.send_message(_target_chat_id, "📋 消息原文:\n" + (raw or "(无)"))
    except Exception as e:
        log.warning("推送消息原文到群失败: %s", e)


def _parse_tg_link(url: str):
    """解析 https://t.me/<用户名>/<消息ID> 形式的链接，返回 (chat_ref, message_id)。"""
    if not url:
        return None
    s = url.strip()
    # 允许用户只粘贴 t.me/...，没有协议时补上
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    try:
        u = urlparse(s)
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if "t.me" not in host and "telegram.me" not in host:
        return None
    parts = [p for p in (u.path or "").split("/") if p]
    if len(parts) < 2:
        return None
    # 目前仅支持 https://t.me/<用户名>/<消息ID> 这种形式
    if parts[0] == "c":
        # /c/<内部id>/<msgid> 形式暂不支持
        return None
    chat_ref = parts[0]
    try:
        msg_id = int(parts[1])
    except ValueError:
        return None
    return chat_ref, msg_id


async def _handle_links_in_text(text: str):
    """在群消息正文中查找 Telegram 链接并尝试加入下载队列。"""
    global _download_queue
    if not text or _download_queue is None:
        return
    seen = set()
    # 简单按空白分割，然后挑出包含 t.me/ 或 telegram.me/ 的片段
    for raw_part in text.split():
        if "t.me" not in raw_part and "telegram.me" not in raw_part:
            continue
        part = raw_part.strip(" <>()（）[]【】,.，。")
        if not part or part in seen:
            continue
        seen.add(part)
        parsed = _parse_tg_link(part)
        if not parsed:
            continue
        chat_ref, msg_id = parsed
        try:
            entity = await client.get_entity(chat_ref)
            message = await client.get_messages(entity, ids=msg_id)
        except Exception as e:
            log.warning("通过链接获取消息失败: %s (%s #%s)", e, chat_ref, msg_id)
            await _push_status(
                f"❌ 通过链接获取消息失败：{chat_ref} #{msg_id}\n错误: {e}"
            )
            continue
        if not message or not _message_has_video(message):
            log.info("链接对应的消息不是视频或已失效: %s #%s", chat_ref, msg_id)
            await _push_status(
                f"⚠️ 链接对应的消息不是视频或已失效：{chat_ref} #{msg_id}"
            )
            continue
        sender = await message.get_sender()
        sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", "未知")
        await _enqueue(entity.id, msg_id, sender_name)
        await _ensure_target_chat()
        await _push_status(
            f"🔗 通过群内链接加入下载队列：{chat_ref} #{msg_id}（当前队列共 {_queue_size} 个）"
        )


def _format_duration(sec: int) -> str:
    """把秒数格式化为「X分Y秒」。"""
    if sec < 0:
        return "0秒"
    if sec < 60:
        return f"{sec}秒"
    m, s = divmod(sec, 60)
    return f"{m}分{s}秒" if s else f"{m}分"


def _format_size(size_bytes: int) -> str:
    """把字节数格式化为可读大小。"""
    if size_bytes < 0:
        return "0 B"
    if size_bytes >= 1 << 30:
        return f"{size_bytes / (1 << 30):.2f} GB"
    if size_bytes >= 1 << 20:
        return f"{size_bytes / (1 << 20):.2f} MB"
    if size_bytes >= 1 << 10:
        return f"{size_bytes / (1 << 10):.2f} KB"
    return f"{size_bytes} B"


# 原始文件名清洗：尽量只保留中英文、数字和少量安全符号，标点一律改为下划线
_BASENAME_BAD_CHARS = set(' /\\:*?"<>|()（）')
_BASENAME_PUNCT = set("，。、；：！？,.;:!?\'\"\t\n\r")  # 中英文标点与空白
_BASENAME_EXTRA_ALLOWED = set("._-")


def _sanitize_basename(name: str) -> str:
    if not name:
        return "video"
    # 先做兼容性归一化，统一全角/半角等
    s = unicodedata.normalize("NFKC", str(name))
    cleaned = []
    for ch in s:
        if ch in _BASENAME_BAD_CHARS or ch in _BASENAME_PUNCT:
            cleaned.append("_")
            continue
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("L", "N"):  # 字母或数字（含中文）
            cleaned.append(ch)
        elif ch in _BASENAME_EXTRA_ALLOWED:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    s = "".join(cleaned)
    s = s.replace(" ", "_")
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("._")
    return s or "video"


def _has_chinese(s: str) -> bool:
    """判断字符串中是否包含中文字符。"""
    if not s:
        return False
    for ch in str(s):
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _get_media_file_name(message) -> str:
    """从 Telegram 消息中取媒体文件名（视频等）。"""
    try:
        if getattr(message, "media", None) and getattr(message.media, "document", None):
            for attr in getattr(message.media.document, "attributes", []) or []:
                if getattr(attr, "file_name", None):
                    return attr.file_name
        if getattr(message, "file", None) and getattr(message.file, "name", None):
            return message.file.name
    except Exception:
        pass
    return "视频"


def _make_progress_callback(download_id: str):
    def cb(current, total):
        now = time.time()
        with _state_lock:
            rec = _active_downloads.get(download_id, {})
            start = rec.get("start_time") or now
            last_t = rec.get("last_speed_time")
            last_b = rec.get("last_speed_bytes", 0)
        elapsed_sec = max(0, now - start)
        speed_bps = None
        if last_t is not None and (now - last_t) >= 0.3:
            speed_bps = (current - last_b) / (now - last_t)
        elif last_t is not None and rec.get("speed_bytes_per_sec") is not None:
            speed_bps = rec.get("speed_bytes_per_sec")
        if total and total > 0:
            pct = round(100 * current / total, 1)
            cur_mb = round(current / (1024 * 1024), 2)
            total_mb = round(total / (1024 * 1024), 2)
            kw = {
                "progress_pct": pct,
                "current_mb": cur_mb,
                "total_mb": total_mb,
                "last_progress_time": now,
                "current_bytes": current,
                "elapsed_sec": round(elapsed_sec, 1),
                "last_speed_time": now,
                "last_speed_bytes": current,
            }
            if speed_bps is not None:
                kw["speed_bytes_per_sec"] = round(speed_bps, 2)
            _update_active(download_id, **kw)
    return cb


# ---------- 下载工作协程（可多实例并发） ----------
async def _download_worker():
    global _download_queue, _queue_size, _target_chat_id
    while True:
        item = await _download_queue.get()
        chat_id, message_id, sender_name = item
        if _target_chat_id is None:
            _target_chat_id = chat_id
        with _state_lock:
            _queue_size = max(0, _queue_size - 1)

        download_id = _download_id(chat_id, message_id)
        _set_pending_status(chat_id, message_id, "downloading")
        _update_active(download_id, status="downloading", progress_pct=0, current_mb=0, total_mb=0, sender=sender_name, start_time=time.time())
        await _push_status(f"⏬ 开始下载：{sender_name} 的视频")

        try:
            message = await client.get_messages(chat_id, ids=message_id)
            if not message or not _message_has_video(message):
                _remove_pending(chat_id, message_id)
                _remove_active(download_id)
                await _push_status(f"⚠️ 跳过：{sender_name} 的消息（非视频或已失效）")
                log.info("跳过非视频或已失效消息: chat_id=%s msg_id=%s", chat_id, message_id)
                _download_queue.task_done()
                continue

            file_name = _get_media_file_name(message)
            _set_pending_file_name(chat_id, message_id, file_name)
            _update_active(download_id, file_name=file_name)

            last_error = None
            for attempt in range(DOWNLOAD_RETRIES + 1):
                try:
                    _update_active(download_id, last_progress_time=time.time())
                    download_coro = message.download_media(
                        file=DOWNLOAD_PATH,
                        progress_callback=_make_progress_callback(download_id),
                    )
                    download_task = asyncio.create_task(download_coro)

                    async def _stall_watchdog():
                        while not download_task.done():
                            await asyncio.sleep(15)
                            if download_task.done():
                                break
                            with _state_lock:
                                rec = _active_downloads.get(download_id, {})
                                last = rec.get("last_progress_time") or 0
                            if last and (time.time() - last) >= DOWNLOAD_STALL_SECONDS:
                                log.warning(
                                    "下载进度 %d 秒无变化，判定卡住，取消并重试",
                                    DOWNLOAD_STALL_SECONDS,
                                )
                                download_task.cancel()
                                break

                    watchdog_task = asyncio.create_task(_stall_watchdog()) if DOWNLOAD_STALL_SECONDS > 0 else None
                    try:
                        file_path = await download_task
                    finally:
                        if watchdog_task is not None:
                            watchdog_task.cancel()
                            try:
                                await watchdog_task
                            except asyncio.CancelledError:
                                pass

                    file_path = str(Path(file_path).resolve())
                    dirname, basename = os.path.split(file_path)
                    name_no_ext, ext = os.path.splitext(basename)
                    if not ext:
                        ext = ".mp4"
                    # 立即重命名为临时安全名，避免保留 Telegram/发送者文件名中的标点等
                    safe_temp_basename = f"{download_id}_temp{ext}"
                    temp_path = os.path.join(dirname, safe_temp_basename)
                    if file_path != temp_path:
                        shutil.move(file_path, temp_path)
                        file_path = temp_path
                    now = datetime.now()
                    ts_str = now.strftime("%Y_%m_%d_%H_%M_%S")
                    # 有文案：用 AI 生成关键词文件名，拼接在前，时间戳在后
                    # 例如: 川普_史诗狂怒_行动_持续_4至5周_2026_03_01_15_15_15.mp4
                    msg_text = (getattr(message, "text", None) or getattr(message, "message", None) or "").strip()
                    new_basename = None
                    if msg_text and generate_video_filename_from_text:
                        try:
                            log.info("根据消息文本生成文件名，文本长度: %d", len(msg_text))
                            name_from_ai = await asyncio.to_thread(
                                generate_video_filename_from_text, msg_text
                            )
                            name_from_ai = _sanitize_basename(name_from_ai)
                            new_basename = f"{name_from_ai}_{ts_str}{ext}"
                        except Exception as e:
                            log.warning("根据文案生成文件名失败，将使用无文案规则: %s", e)

                    # 无文案或上一步失败：根据原文件名是否包含中文来命名
                    # 1) 原文件名含中文：用 AI 整理中文，再拼接到时间戳后
                    #    例如: 2026_03_01_15_15_15_整理后中文名.mp4
                    # 2) 原文件名不含中文：直接用 “空”
                    #    例如: 2026_03_01_15_15_15_空.mp4
                    if new_basename is None:
                        base = name_no_ext or "video"
                        if _has_chinese(base):
                            if generate_video_filename_from_text:
                                try:
                                    log.info("根据原文件名中文生成文件名，原名: %s", base)
                                    name_from_ai2 = await asyncio.to_thread(
                                        generate_video_filename_from_text, base
                                    )
                                    name_from_ai2 = _sanitize_basename(name_from_ai2)
                                    new_basename = f"{ts_str}_{name_from_ai2}{ext}"
                                except Exception as e:
                                    log.warning("根据原文件名生成文件名失败，使用清洗后的原名: %s", e)
                                    safe = _sanitize_basename(base)
                                    new_basename = f"{ts_str}_{safe}{ext}"
                            else:
                                safe = _sanitize_basename(base)
                                new_basename = f"{ts_str}_{safe}{ext}"
                        else:
                            new_basename = f"{ts_str}_空{ext}"

                    new_path = os.path.join(dirname, new_basename)
                    if file_path != new_path:
                        shutil.move(file_path, new_path)
                        file_path = new_path
                    file_size = os.path.getsize(file_path)
                    message_time_str = ""
                    if getattr(message, "date", None):
                        try:
                            message_time_str = message.date.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass
                    download_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with _state_lock:
                        rec = _active_downloads.get(download_id, {})
                        duration_sec = int(time.time() - rec.get("start_time", time.time()))
                    _save_download_record(file_path, file_size, sender_name, message_time_str, download_time_str, duration_sec=duration_sec)
                    _remove_active(download_id)
                    _remove_pending(chat_id, message_id)
                    _add_history({
                        "status": "success",
                        "file_path": file_path,
                        "sender": sender_name,
                        "time": download_time_str,
                        "duration_sec": duration_sec,
                    })
                    await _push_status(
                        f"✅ 下载完成\n"
                        f"发送者: {sender_name}\n"
                        f"文件: {os.path.basename(file_path)}\n"
                        f"大小: {_format_size(file_size)}\n"
                        f"用时: {_format_duration(duration_sec)}"
                    )
                    log.info("下载完成: %s (%s bytes)", file_path, file_size)
                    break
                except asyncio.CancelledError:
                    # 由看门狗因“进度无变化”取消，视为卡住
                    last_error = None
                    if attempt < DOWNLOAD_RETRIES:
                        log.warning("下载卡住（%ds 无进度）第 %d 次，重试中…", DOWNLOAD_STALL_SECONDS, attempt + 1)
                        await _push_status(f"⏱️ 下载卡住（进度无变化），正在第 {attempt + 2} 次重试…")
                        await asyncio.sleep(2)
                    else:
                        _remove_active(download_id)
                        _remove_pending(chat_id, message_id)
                        _add_history({"status": "failed", "error": "下载卡住", "sender": sender_name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                        await _push_status(f"❌ 下载卡住（{DOWNLOAD_STALL_SECONDS}s 无进度）\n发送者: {sender_name}\n已重试 {DOWNLOAD_RETRIES + 1} 次")
                        log.error("下载卡住，已重试 %d 次", DOWNLOAD_RETRIES + 1)
                except Exception as e:
                    last_error = e
                    if attempt < DOWNLOAD_RETRIES:
                        log.warning("下载失败第 %d 次，重试中: %s", attempt + 1, e)
                        await asyncio.sleep(2)
                    else:
                        _remove_active(download_id)
                        _remove_pending(chat_id, message_id)
                        _add_history({
                            "status": "failed",
                            "error": str(e),
                            "sender": sender_name,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        await _push_status(f"❌ 下载失败\n发送者: {sender_name}\n错误: {e}")
                        log.error("下载失败: %s", e)
        finally:
            _download_queue.task_done()


async def _enqueue(chat_id: int, message_id: int, sender_name: str):
    global _queue_size
    await _download_queue.put((chat_id, message_id, sender_name))
    with _state_lock:
        _queue_size += 1
    _add_pending(chat_id, message_id, sender_name)


# ---------- Telegram 事件处理 ----------
@client.on(events.NewMessage)
async def handler(event):
    global _download_queue, _target_chat_id, _queue_size
    if not event.is_group:
        return
    chat = await event.get_chat()
    if chat.title != TARGET_GROUP_NAME:
        return
    if _target_chat_id is None:
        _target_chat_id = event.chat_id

    # 转发消息时事件里可能未带完整 media，先拉取完整消息再判断是否为视频
    msg_to_check = event.message
    if getattr(event.message, "media", None) and not _message_has_video(event.message):
        try:
            full = await client.get_messages(event.chat_id, ids=event.message.id)
            if full:
                msg_to_check = full
        except Exception as e:
            log.debug("拉取完整消息用于视频检测失败: %s", e)

    # 无论什么消息都回复「收到」，便于确认程序已处理；无视频时说明未加入下载
    try:
        has_video = _message_has_video(msg_to_check)
        if has_video:
            await event.reply("收到")
        else:
            await event.reply("收到。（本条消息无视频，未加入下载）")
    except Exception as e:
        log.warning("回复「收到」失败: %s", e)

    # 取消息正文：事件里的 message 有时未带全，空且是视频时用已拉取的 full
    raw = _get_message_raw_text(event.message) or (getattr(event, "text", None) or "")
    if not raw and has_video:
        raw = _get_message_raw_text(msg_to_check) or ""
    # 把消息原文推送到 downapp 群
    await _push_raw_text(event.message, raw_text=raw or None)
    # 顺便在文本里查找是否有 Telegram 链接，如果有则尝试按链接下载对应视频
    await _handle_links_in_text(raw)
    if not has_video:
        return
    if _download_queue is None:
        return

    sender = await event.get_sender()
    name = getattr(sender, "username", None) or getattr(sender, "first_name", "未知")

    # if _is_in_pending(event.chat_id, event.message.id):
    #     log.info("已在队列中，跳过重复: chat_id=%s msg_id=%s", event.chat_id, event.message.id)
    #     return
    log.info("收到群 [%s] 里 %s 的视频，加入队列", TARGET_GROUP_NAME, name)
    await _enqueue(event.chat_id, event.message.id, name)
    await _push_status(f"📥 已加入下载队列：{name} 的视频（当前队列共 {_queue_size} 个）")


# ---------- FastAPI ----------
app = FastAPI()


@app.get("/api/status")
def api_status():
    return _get_status()


@app.get("/")
def index():
    return FileResponse(SCRIPT_DIR / "index.html")


# ---------- 启动 ----------
def run_web():
    uvicorn.run(app, host=WEB_BIND, port=WEB_PORT, log_level="warning")


if __name__ == "__main__":
    _init_db()

    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    log.info("Web 已启动: port=%s bind=%s", WEB_PORT, WEB_BIND)
    log.info(
        "并发数: %s，卡住检测: %ss 无进度则重试，失败重试: %s 次",
        CONCURRENT_DOWNLOADS,
        DOWNLOAD_STALL_SECONDS or "关闭",
        DOWNLOAD_RETRIES + 1,
    )

    async def _start():
        global _download_queue, _queue_size
        _download_queue = asyncio.Queue()
        for _ in range(CONCURRENT_DOWNLOADS):
            asyncio.create_task(_download_worker())

    with client:
        client.loop.run_until_complete(_start())
        client.run_until_disconnected()






