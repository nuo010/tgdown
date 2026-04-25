from telethon import TelegramClient, events
try:
    from croniter import croniter  # type: ignore
except Exception:  # pragma: no cover
    croniter = None  # type: ignore
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
    from lib.ai import generate_video_filename_from_text
except ImportError:
    generate_video_filename_from_text = None
from fastapi.responses import FileResponse
import uvicorn

# ---------- 数据目录（日志路径依赖） ----------
SCRIPT_DIR = Path(__file__).resolve().parent
_data_root = Path("/data")
DATA_DIR = _data_root if (_data_root / "config.json").exists() else (SCRIPT_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA_DIR / "tgdown.log"

# 统一日志：stdout（便于 docker logs）+ 数据目录内 tgdown.log
_log_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.handlers.clear()
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_log_fmt)
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(_log_fmt)
_root.addHandler(_sh)
_root.addHandler(_fh)
log = logging.getLogger(__name__)

# ---------- 配置（从文件读取） ----------
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
TEMP_PATH = _config.get("temp_path", "./temp_downloads")
WEB_PORT = int(_config.get("web_port", 8765))
WEB_BIND = _config.get("web_bind", "0.0.0.0")  # 可选：仅本机访问填 127.0.0.1
TARGET_GROUP_NAME = _config.get("target_group_name", "downapp")
CONCURRENT_DOWNLOADS = max(1, int(_config.get("concurrent_downloads", 3)))
PUSH_STATUS_TO_GROUP = _config.get("push_status_to_group", True)
DOWNLOAD_RETRIES = max(0, int(_config.get("download_retries", 2)))  # 下载失败或卡住时重试次数，默认 2
# 若连续多少秒没有新的下载进度则判定为卡住并重试；0 表示不检测（大文件友好）
DOWNLOAD_STALL_SECONDS = max(0, int(_config.get("download_stall_seconds", 600)))

# Telegram 登录后在「设置 → 设备」里显示的设备名（Telethon: device_model）；兼容旧键 device_model
TG_DEVICE_MODEL = str(_config.get("tg_device_name") or _config.get("device_model") or "tgdown").strip() or "tgdown"
# 可选：在会话里进一步区分系统/应用版本（留空则使用 Telethon 默认）
TG_SYSTEM_VERSION = str(_config.get("tg_system_version") or "").strip() or None
TG_APP_VERSION = str(_config.get("tg_app_version") or "").strip() or None
# 发往目标群的消息前加标识行，便于与人工消息区分；留空则不加
TG_MESSAGE_PREFIX = str(_config.get("tg_message_prefix", "[tgdown]")).strip()


def _apply_outgoing_message_prefix(text: str) -> str:
    """脚本发往群的消息统一加前缀（首行标识，与正文换行分隔）。"""
    if not TG_MESSAGE_PREFIX:
        return text
    return f"{TG_MESSAGE_PREFIX}\n{text}"


def _telegram_client_extra_kwargs() -> dict:
    kw: dict = {"device_model": TG_DEVICE_MODEL}
    if TG_SYSTEM_VERSION:
        kw["system_version"] = TG_SYSTEM_VERSION
    if TG_APP_VERSION:
        kw["app_version"] = TG_APP_VERSION
    return kw


# ---------- 定时发送（cron） ----------
# 支持 5 字段或 6 字段 cron：
# - 5 字段：分钟 小时 日 月 星期（例如：*/5 * * * *）
# - 6 字段：秒 分钟 小时 日 月 星期（例如：*/10 * * * * *）
CRON_SEND_CURRENT_TIME_CRON = str(_config.get("cron_send_current_time_cron", "")).strip()
CRON_SEND_CURRENT_TIME_ENABLED = bool(CRON_SEND_CURRENT_TIME_CRON)
CRON_PUSH_DOWNLOAD_PROGRESS_CRON = str(_config.get("cron_push_download_progress_cron", "")).strip()
CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED = bool(CRON_PUSH_DOWNLOAD_PROGRESS_CRON)

# 为了让配置文件尽量“只放 cron 表达式”，时间格式与文案在代码里使用默认值。
CRON_SEND_CURRENT_TIME_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CRON_SEND_CURRENT_TIME_MESSAGE_TEMPLATE = "corn定时发送 - 当前时间：{time}"

if CRON_SEND_CURRENT_TIME_ENABLED and croniter is None:
    log.error("已配置 cron_send_current_time_cron，但缺少 croniter 依赖，请运行: pip install croniter")
    CRON_SEND_CURRENT_TIME_ENABLED = False
if CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED and croniter is None:
    log.error("已配置 cron_push_download_progress_cron，但缺少 croniter 依赖，请运行: pip install croniter")
    CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED = False


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


def _resolve_storage_dir(path_value: str, default_name: str) -> Path:
    """把配置目录解析为绝对路径；相对路径基于 data 的父目录。"""
    p = Path(path_value or default_name)
    if not p.is_absolute():
        p = DATA_DIR.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p


# 下载目录、临时目录：与 data 同级（相对路径基于 DATA_DIR 的父目录）
DOWNLOAD_PATH = _resolve_storage_dir(str(DOWNLOAD_PATH or ""), "downloads")
TEMP_PATH = _resolve_storage_dir(str(TEMP_PATH or ""), "temp_downloads")
if DOWNLOAD_PATH.resolve() == TEMP_PATH.resolve():
    adjusted_temp = DOWNLOAD_PATH.parent / "temp_downloads"
    if adjusted_temp.resolve() == DOWNLOAD_PATH.resolve():
        adjusted_temp = DOWNLOAD_PATH.parent / f"{DOWNLOAD_PATH.name}_temp"
    adjusted_temp.mkdir(parents=True, exist_ok=True)
    log.warning("temp_path 与 download_path 相同，临时目录自动调整为: %s", adjusted_temp)
    TEMP_PATH = adjusted_temp

DOWNLOAD_PATH = str(DOWNLOAD_PATH)
TEMP_PATH = str(TEMP_PATH)

SESSION_PATH = str(DATA_DIR / "session")
_TG_PROXY = _build_tg_proxy_from_config()
_tg_extra = _telegram_client_extra_kwargs()
if _TG_PROXY:
    client = TelegramClient(SESSION_PATH, api_id, api_hash, proxy=_TG_PROXY, **_tg_extra)
else:
    client = TelegramClient(SESSION_PATH, api_id, api_hash, **_tg_extra)
log.info(
    "Telegram 会话设备标识: device_model=%s system_version=%s app_version=%s",
    TG_DEVICE_MODEL,
    TG_SYSTEM_VERSION or "(默认)",
    TG_APP_VERSION or "(默认)",
)

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


def _add_pending(chat_id: int, message_id: int, sender_name: str, file_name: str = "", added_at: str | None = None, status: str = "waiting"):
    with _state_lock:
        if any(p["chat_id"] == chat_id and p["message_id"] == message_id for p in _pending_list):
            return
        _pending_list.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_name": sender_name,
            "file_name": file_name or "",
            "added_at": added_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
        })


def _set_pending_status(chat_id: int, message_id: int, status: str):
    with _state_lock:
        for p in _pending_list:
            if p["chat_id"] == chat_id and p["message_id"] == message_id:
                p["status"] = status
                break
    _update_task_status(chat_id, message_id, status)


def _set_pending_file_name(chat_id: int, message_id: int, file_name: str):
    with _state_lock:
        for p in _pending_list:
            if p["chat_id"] == chat_id and p["message_id"] == message_id:
                p["file_name"] = file_name or ""
                break
    _update_task_file_name(chat_id, message_id, file_name)


def _remove_pending(chat_id: int, message_id: int):
    with _state_lock:
        global _pending_list
        _pending_list = [p for p in _pending_list if not (p["chat_id"] == chat_id and p["message_id"] == message_id)]
    _delete_task(chat_id, message_id)


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


# ---------- SQLite 成功记录 / 待下载任务 ----------
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                file_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'waiting',
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(chat_id, message_id)
            )
        """)
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_download_task_status_id ON download_task(status, id)")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()


def _save_task(chat_id: int, message_id: int, sender_name: str, status: str = "waiting"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            """
            INSERT INTO download_task (chat_id, message_id, sender_name, status, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                sender_name = excluded.sender_name,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (chat_id, message_id, sender_name or "未知", status, now, now),
        )
        conn.commit()
        conn.close()


def _update_task_status(chat_id: int, message_id: int, status: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "UPDATE download_task SET status = ?, updated_at = ? WHERE chat_id = ? AND message_id = ?",
            (status, now, chat_id, message_id),
        )
        conn.commit()
        conn.close()


def _update_task_file_name(chat_id: int, message_id: int, file_name: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "UPDATE download_task SET file_name = ?, updated_at = ? WHERE chat_id = ? AND message_id = ?",
            (file_name or "", now, chat_id, message_id),
        )
        conn.commit()
        conn.close()


def _delete_task(chat_id: int, message_id: int):
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "DELETE FROM download_task WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        conn.commit()
        conn.close()


def _load_unfinished_tasks():
    """恢复上次退出前未完成的任务；downloading 视为被中断，启动后重新排队。"""
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE download_task SET status = 'waiting', updated_at = ? WHERE status = 'downloading'",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = conn.execute(
            """
            SELECT chat_id, message_id, sender_name, file_name, added_at, status
            FROM download_task
            WHERE status = 'waiting'
            ORDER BY id ASC
            """
        ).fetchall()
        conn.commit()
        conn.close()
    return [dict(r) for r in rows]


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


def _get_download_records_total() -> int:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        total = conn.execute("SELECT COUNT(*) FROM download_record").fetchone()[0]
        conn.close()
    return total


def _get_download_records(limit: int = 20, offset: int = 0):
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, file_path, file_name, file_size, username, message_time, download_time, duration_sec FROM download_record ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
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
    return data


def _format_progress_line(item: dict) -> str:
    file_name = str(item.get("file_name") or "未知文件").strip()
    sender = str(item.get("sender") or "未知").strip()
    current_mb = item.get("current_mb", 0) or 0
    total_mb = item.get("total_mb", 0) or 0
    progress_pct = item.get("progress_pct", 0) or 0
    elapsed_sec = int(item.get("elapsed_sec", 0) or 0)
    speed_text = _format_speed(item.get("speed_bytes_per_sec"))
    remain_text = _estimate_remaining_text(item)
    return (
        f"文件: {file_name}\n"
        f"发送者: {sender}\n"
        f"进度: {progress_pct}% ({current_mb:.2f} MB / {total_mb:.2f} MB)\n"
        f"速度: {speed_text}\n"
        f"预计剩余: {remain_text}\n"
        f"已用时: {_format_duration(elapsed_sec)}"
    )


def _build_download_progress_status_text() -> str | None:
    with _state_lock:
        active_items = list(_active_downloads.values())
        queue_size = _queue_size
        pending_total = len(_pending_list)
    if not active_items:
        return None
    waiting_count = max(0, pending_total - len(active_items))
    lines = [
        "📊 下载进度播报",
        f"队列中数量: {queue_size}",
        f"下载中数量: {len(active_items)}",
        f"等待中数量: {waiting_count}",
    ]
    for idx, item in enumerate(active_items, start=1):
        lines.append("")
        lines.append(f"【{idx}】")
        lines.append(_format_progress_line(item))
    return "\n".join(lines)


def _build_status_command_reply() -> str:
    """群内「状态」查询：队列数量、待办列表、各下载中任务的进度。"""
    with _state_lock:
        active_items = list(_active_downloads.values())
        queue_size = _queue_size
        pending_list = list(_pending_list)
    pending_total = len(pending_list)
    waiting_count = max(0, pending_total - len(active_items))
    lines = [
        "📊 当前状态",
        f"队列中数量: {queue_size}",
        f"待办任务数: {pending_total}",
        f"下载中数量: {len(active_items)}",
        f"等待中数量: {waiting_count}",
    ]
    if active_items:
        for idx, item in enumerate(active_items, start=1):
            lines.append("")
            lines.append(f"【下载中 {idx}】")
            lines.append(_format_progress_line(item))
    else:
        lines.append("")
        lines.append("当前无进行中的下载。")
    if pending_list:
        lines.append("")
        lines.append("── 待办列表 ──")
        max_rows = 40
        for i, p in enumerate(pending_list[:max_rows], start=1):
            fn = (p.get("file_name") or "").strip() or "(文件名待定)"
            st = (p.get("status") or "").strip() or "?"
            who = (p.get("sender_name") or "").strip() or "?"
            lines.append(f"{i}. {who} | {st} | {fn}")
        if len(pending_list) > max_rows:
            lines.append(f"... 共 {len(pending_list)} 条，仅显示前 {max_rows} 条")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "..."
    return text


def _format_speed(speed_bps) -> str:
    if speed_bps is None:
        return "-"
    speed_bps = float(speed_bps)
    if speed_bps <= 0:
        return "-"
    if speed_bps >= 1 << 20:
        return f"{speed_bps / (1 << 20):.2f} MB/s"
    if speed_bps >= 1 << 10:
        return f"{speed_bps / (1 << 10):.2f} KB/s"
    return f"{speed_bps:.0f} B/s"


def _estimate_remaining_text(item: dict) -> str:
    speed_bps = item.get("speed_bytes_per_sec")
    current_bytes = item.get("current_bytes", 0) or 0
    total_mb = item.get("total_mb", 0) or 0
    total_bytes = int(float(total_mb) * 1024 * 1024) if total_mb else 0
    if speed_bps is None:
        return "-"
    try:
        speed_bps = float(speed_bps)
    except (TypeError, ValueError):
        return "-"
    if speed_bps <= 0 or total_bytes <= 0 or current_bytes >= total_bytes:
        return "-"
    remain_sec = int(max(0, (total_bytes - current_bytes) / speed_bps))
    return _format_duration(remain_sec)


def _is_in_pending(chat_id: int, message_id: int) -> bool:
    with _state_lock:
        return any(p["chat_id"] == chat_id and p["message_id"] == message_id for p in _pending_list)


def _is_in_active(chat_id: int, message_id: int) -> bool:
    download_id = _download_id(chat_id, message_id)
    with _state_lock:
        return download_id in _active_downloads


def _is_already_queued(chat_id: int, message_id: int) -> bool:
    return _is_in_pending(chat_id, message_id) or _is_in_active(chat_id, message_id)


async def _send_to_target_group(text: str, *, kind: str = "status") -> tuple[bool, str | None]:
    """向目标群发送文本。返回 (是否成功, 失败原因)；成功时第二项为 None。"""
    if not PUSH_STATUS_TO_GROUP:
        reason = "push_status_to_group 已关闭"
        log.debug("群消息未发送 kind=%s: %s", kind, reason)
        return False, reason
    if _target_chat_id is None:
        reason = "目标群 chat_id 未就绪"
        log.warning("群消息发送失败 kind=%s: %s", kind, reason)
        return False, reason
    try:
        out = _apply_outgoing_message_prefix(text)
        sent = await client.send_message(_target_chat_id, out)
        mid = getattr(sent, "id", None)
        log.info(
            "群消息发送成功 kind=%s chat_id=%s sent_msg_id=%s text_len=%d",
            kind,
            _target_chat_id,
            mid,
            len(out or ""),
        )
        return True, None
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        log.warning("群消息发送失败 kind=%s: %s", kind, reason)
        return False, reason


async def _push_status(text: str):
    """把状态消息发到 downapp 群"""
    await _send_to_target_group(text, kind="status")


async def _ensure_target_chat():
    """确保 _target_chat_id 已指向目标群（按名称/标题）。"""
    global _target_chat_id
    if _target_chat_id is not None:
        return
    target_name = str(TARGET_GROUP_NAME or "").strip()
    target_name_lc = target_name.lower()
    try:
        chat = await client.get_entity(target_name)
        _target_chat_id = chat.id
        log.info("目标群 chat_id 初始化为 %s", _target_chat_id)
    except Exception as e:
        log.warning("根据名称 %s 获取目标群失败，尝试按群标题遍历匹配: %s", target_name, e)
        try:
            async for dialog in client.iter_dialogs():
                title = str(getattr(dialog, "name", None) or getattr(dialog.entity, "title", None) or "").strip()
                if not title:
                    continue
                title_lc = title.lower()
                if title == target_name or title_lc == target_name_lc:
                    _target_chat_id = dialog.id
                    log.info("通过群标题匹配到目标群 chat_id: %s", _target_chat_id)
                    return
            log.warning("按群标题未匹配到目标群: %s", target_name)
        except Exception as e2:
            log.warning("遍历会话匹配目标群失败: %s", e2)


async def _notify_startup_ready():
    """进程启动完成后向目标群推送一次上线通知（含当前时间）。受 push_status_to_group 控制。"""
    if not PUSH_STATUS_TO_GROUP:
        log.debug("跳过启动推送: push_status_to_group 已关闭")
        return
    try:
        await _ensure_target_chat()
        time_str = datetime.now().strftime(CRON_SEND_CURRENT_TIME_TIME_FORMAT)
        text = f"✅ tgdown 已启动完成\n当前时间: {time_str}"
        ok, err = await _send_to_target_group(text, kind="startup")
        if ok:
            log.info("已向目标群推送启动完成通知")
        else:
            log.warning("启动完成推送未成功: %s", err)
    except Exception as e:
        log.warning("启动完成推送异常: %s", e)


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


async def _push_raw_text(msg, raw_text: str | None = None) -> tuple[bool, str | None]:
    """只将消息的正文/字幕发到群。若传 raw_text 则优先使用（避免事件里未带全）。"""
    raw = (raw_text if raw_text is not None else _get_message_raw_text(msg)) or ""
    return await _send_to_target_group(raw, kind="received_echo")


def _build_received_reply_text(has_video: bool, raw_text: str) -> str:
    receive_text = "收到" if has_video else "收到。（本条消息无视频，未加入下载）"
    raw = (raw_text or "").strip() or "(无)"
    return f"{receive_text}\n\n📋 消息原文:\n{raw}"


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
        if _is_already_queued(entity.id, msg_id):
            log.info("链接对应消息已在队列或下载中，跳过重复: %s #%s", chat_ref, msg_id)
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


def _normalize_media_ext(ext: str) -> str:
    ext = unicodedata.normalize("NFKC", str(ext or "")).strip().lower()
    if not ext:
        return ".mp4"
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def _strip_duplicate_suffix(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    if len(s) >= 3 and s.endswith(")") and "(" in s:
        left = s.rfind("(")
        inner = s[left + 1:-1].strip()
        if left > 0 and inner.isdigit():
            s = s[:left].rstrip()
    return s


def _resolve_conflict_path(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return str(p)
    stem = p.stem
    suffix = p.suffix
    parent = p.parent
    index = 2
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return str(candidate)
        index += 1


def _cleanup_temp_file(path: str | None):
    """删除下载失败时残留的临时文件（仅清理 TEMP_PATH 目录内的 temp_* 文件）。"""
    if not path:
        return
    try:
        p = Path(path)
        if not p.exists():
            return
        if p.resolve().parent == Path(TEMP_PATH).resolve() and p.name.startswith("temp_"):
            p.unlink()
    except Exception as e:
        log.warning("清理临时文件失败: %s (%s)", path, e)


def _safe_temp_path_for_final(final_path: str) -> str:
    """临时文件统一落到 TEMP_PATH，文件名规则：temp_<最终文件名>。"""
    p = Path(final_path)
    return str(Path(TEMP_PATH) / f"temp_{p.name}")


def _validate_temp_and_final_paths(temp_path: str, final_path: str) -> bool:
    """校验：临时文件仅允许落在 TEMP_PATH，最终文件仅允许落在 DOWNLOAD_PATH。"""
    temp_obj = Path(temp_path)
    final_obj = Path(final_path)
    t = temp_obj.name
    f = final_obj.name
    if f.startswith("temp_"):
        log.error("最终保存路径不得以 temp_ 开头: %s", final_path)
        return False
    if not t.startswith("temp_"):
        log.error("临时路径必须以 temp_ 开头: %s", temp_path)
        return False
    if t != f"temp_{f}":
        log.error("临时文件名应为 temp_<最终文件名>，实际 temp=%s final=%s", t, f)
        return False
    if temp_obj.resolve().parent != Path(TEMP_PATH).resolve():
        log.error("临时文件必须位于 TEMP_PATH 目录内: %s", temp_path)
        return False
    if final_obj.resolve().parent != Path(DOWNLOAD_PATH).resolve():
        log.error("最终文件必须位于 DOWNLOAD_PATH 目录内: %s", final_path)
        return False
    return True


async def _build_final_basename(message, original_base: str, ts_str: str, ext: str) -> str:
    cleaned_base = _sanitize_basename(_strip_duplicate_suffix(original_base or "video"))
    msg_text = (getattr(message, "text", None) or getattr(message, "message", None) or "").strip()

    if msg_text and generate_video_filename_from_text:
        try:
            log.info("根据消息文本生成文件名，文本长度: %d", len(msg_text))
            name_from_ai = await asyncio.to_thread(generate_video_filename_from_text, msg_text)
            name_from_ai = _sanitize_basename(name_from_ai)
            if name_from_ai and name_from_ai != "video":
                return f"ai_{name_from_ai}_{ts_str}{ext}"
        except Exception as e:
            log.warning("根据文案生成文件名失败，将使用原文件名规则: %s", e)

    if _has_chinese(cleaned_base) and generate_video_filename_from_text:
        try:
            log.info("根据原文件名中文生成文件名，原名: %s", cleaned_base)
            name_from_ai2 = await asyncio.to_thread(generate_video_filename_from_text, cleaned_base)
            name_from_ai2 = _sanitize_basename(name_from_ai2)
            if name_from_ai2 and name_from_ai2 != "video":
                return f"ai_{name_from_ai2}_{ts_str}{ext}"
        except Exception as e:
            log.warning("根据原文件名生成文件名失败，使用清洗后的原名: %s", e)

    return f"{cleaned_base}_{ts_str}{ext}"


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
                continue

            file_name = _get_media_file_name(message)
            name_no_ext, ext = os.path.splitext(file_name)
            ext = _normalize_media_ext(ext)
            ts_str = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
            final_basename = await _build_final_basename(message, name_no_ext, ts_str, ext)
            final_path = _resolve_conflict_path(os.path.join(DOWNLOAD_PATH, final_basename))
            temp_path = _safe_temp_path_for_final(final_path)
            if not _validate_temp_and_final_paths(temp_path, final_path):
                _remove_pending(chat_id, message_id)
                _remove_active(download_id)
                await _push_status(f"❌ 内部错误：临时/最终路径校验失败\n发送者: {sender_name}")
                log.error("临时/最终路径校验失败: temp=%s final=%s", temp_path, final_path)
                continue

            _set_pending_file_name(chat_id, message_id, os.path.basename(final_path))
            _update_active(download_id, file_name=os.path.basename(final_path), temp_path=temp_path, final_path=final_path)

            last_error = None
            for attempt in range(DOWNLOAD_RETRIES + 1):
                try:
                    _update_active(download_id, last_progress_time=time.time())
                    try:
                        if os.path.isfile(temp_path):
                            os.unlink(temp_path)
                    except OSError as e:
                        log.warning("删除残留临时文件失败（将尝试覆盖下载）: %s (%s)", temp_path, e)

                    download_coro = message.download_media(
                        file=temp_path,
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
                        downloaded = await download_task
                    finally:
                        if watchdog_task is not None:
                            watchdog_task.cancel()
                            try:
                                await watchdog_task
                            except asyncio.CancelledError:
                                pass

                    got = str(Path(downloaded).resolve()) if downloaded else str(Path(temp_path).resolve())
                    if got != str(Path(temp_path).resolve()) and os.path.isfile(got):
                        log.warning("下载落盘路径与预期 temp 不一致，将移动到 temp: got=%s expect=%s", got, temp_path)
                        shutil.move(got, temp_path)
                    if not os.path.isfile(temp_path):
                        raise FileNotFoundError(f"临时文件未生成: {temp_path}")
                    if not _validate_temp_and_final_paths(temp_path, final_path):
                        raise RuntimeError("下载完成后临时/最终路径校验失败")
                    shutil.move(temp_path, final_path)
                    file_path = str(Path(final_path).resolve())

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
                    log.info("下载完成: %s (%s bytes)（已从 temp 落盘到最终路径）", file_path, file_size)
                    break
                except asyncio.CancelledError:
                    _cleanup_temp_file(temp_path)
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
                    _cleanup_temp_file(temp_path)
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


async def _enqueue(chat_id: int, message_id: int, sender_name: str, *, persist: bool = True, file_name: str = "", added_at: str | None = None) -> bool:
    global _queue_size
    if _is_already_queued(chat_id, message_id):
        log.info("任务已在队列或下载中，跳过重复入队: chat_id=%s msg_id=%s", chat_id, message_id)
        return False
    if persist:
        _save_task(chat_id, message_id, sender_name, status="waiting")
    _add_pending(chat_id, message_id, sender_name, file_name=file_name, added_at=added_at, status="waiting")
    with _state_lock:
        _queue_size += 1
    await _download_queue.put((chat_id, message_id, sender_name))
    return True


async def _restore_unfinished_tasks():
    """把重启前未完成的任务恢复到内存队列。"""
    if _download_queue is None:
        return
    tasks = _load_unfinished_tasks()
    if not tasks:
        log.info("未发现需要恢复的下载任务")
        return
    restored = 0
    for task in tasks:
        chat_id = int(task["chat_id"])
        message_id = int(task["message_id"])
        ok = await _enqueue(
            chat_id,
            message_id,
            task.get("sender_name") or "未知",
            persist=False,
            file_name=task.get("file_name") or "",
            added_at=task.get("added_at") or None,
        )
        if ok:
            restored += 1
    log.info("已恢复未完成下载任务: %d/%d", restored, len(tasks))
    if restored:
        if _target_chat_id is None:
            await _ensure_target_chat()
        await _push_status(f"♻️ 已恢复上次未完成的下载任务：{restored} 个")


# ---------- Telegram 事件处理 ----------
@client.on(events.NewMessage)
async def handler(event):
    global _download_queue, _target_chat_id, _queue_size
    if not event.is_group:
        return
    chat = await event.get_chat()
    chat_title = str(getattr(chat, "title", "") or "").strip().lower()
    target_title = str(TARGET_GROUP_NAME or "").strip().lower()
    if chat_title != target_title:
        return
    if _target_chat_id is None:
        _target_chat_id = event.chat_id

    sender = await event.get_sender()
    sender_label = getattr(sender, "username", None) or getattr(sender, "first_name", "未知")

    # 转发消息时事件里可能未带完整 media，先拉取完整消息再判断是否为视频
    msg_to_check = event.message
    fetch_full_ok: bool | None = None
    fetch_full_err: str | None = None
    if getattr(event.message, "media", None) and not _message_has_video(event.message):
        try:
            full = await client.get_messages(event.chat_id, ids=event.message.id)
            if full:
                msg_to_check = full
                fetch_full_ok = True
            else:
                fetch_full_ok = False
                fetch_full_err = "get_messages 返回空"
        except Exception as e:
            fetch_full_ok = False
            fetch_full_err = f"{type(e).__name__}: {e}"
            log.warning("拉取完整消息用于视频检测失败: %s", fetch_full_err)
    fetch_note = "跳过"
    if fetch_full_ok is True:
        fetch_note = "成功"
    elif fetch_full_ok is False:
        fetch_note = f"失败({fetch_full_err})"

    # 取消息正文：事件里的 message 有时未带全，空且是视频时用已拉取的 full
    has_video = _message_has_video(msg_to_check)
    raw = _get_message_raw_text(event.message) or (getattr(event, "text", None) or "")
    if not raw and has_video:
        raw = _get_message_raw_text(msg_to_check) or ""

    is_status_query = "状态" in raw
    only_status_word = raw.strip() == "状态"

    if is_status_query:
        await _send_to_target_group(_build_status_command_reply(), kind="status_command")
        log.info(
            "已响应群内「状态」查询: chat_id=%s msg_id=%s sender=%s",
            event.chat_id,
            event.message.id,
            sender_label,
        )

    if only_status_word:
        echo_ok, echo_err = True, None
        echo_note = "跳过(纯状态查询)"
    else:
        echo_ok, echo_err = await _push_raw_text(
            event.message, raw_text=_build_received_reply_text(has_video, raw)
        )
        if echo_ok:
            echo_note = "成功"
        elif echo_err == "push_status_to_group 已关闭":
            echo_note = "未发送(功能已关闭)"
        else:
            echo_note = f"失败({echo_err})"
    log.info(
        "收到目标群消息: chat_id=%s msg_id=%s sender=%s has_video=%s text_len=%s 拉取完整消息=%s 回显发送=%s",
        event.chat_id,
        event.message.id,
        sender_label,
        has_video,
        len(raw),
        fetch_note,
        echo_note,
    )

    # 顺便在文本里查找是否有 Telegram 链接，如果有则尝试按链接下载对应视频
    await _handle_links_in_text(raw)
    if not has_video:
        log.info("群消息处理结束（无视频）: chat_id=%s msg_id=%s", event.chat_id, event.message.id)
        return
    if _download_queue is None:
        log.warning("群消息含视频但未入队: download_queue 未初始化 chat_id=%s msg_id=%s", event.chat_id, event.message.id)
        return

    name = sender_label
    if _is_already_queued(event.chat_id, event.message.id):
        log.info("已在队列或下载中，跳过重复: chat_id=%s msg_id=%s", event.chat_id, event.message.id)
        return
    log.info("收到群 [%s] 里 %s 的视频，加入队列", TARGET_GROUP_NAME, name)
    await _enqueue(event.chat_id, event.message.id, name)
    await _push_status(f"📥 已加入下载队列：{name} 的视频（当前队列共 {_queue_size} 个）")
    log.info("群消息处理结束（已入队视频）: chat_id=%s msg_id=%s sender=%s", event.chat_id, event.message.id, name)


# ---------- FastAPI ----------
app = FastAPI()


@app.get("/api/status")
def api_status():
    return _get_status()


@app.get("/api/download-records")
def api_download_records(page: int = 1, per_page: int = 20):
    """分页查询下载成功记录。page 从 1 开始，per_page 默认 20。"""
    page = max(1, page)
    per_page = min(max(1, per_page), 100)
    try:
        total = _get_download_records_total()
        offset = (page - 1) * per_page
        items = _get_download_records(limit=per_page, offset=offset)
        return {"items": items, "total": total, "page": page, "per_page": per_page}
    except Exception as e:
        log.warning("分页读取下载记录失败: %s", e)
        return {"items": [], "total": 0, "page": page, "per_page": per_page}


@app.get("/")
def index():
    return FileResponse(SCRIPT_DIR / "index.html")


# ---------- 启动 ----------
def run_web():
    uvicorn.run(app, host=WEB_BIND, port=WEB_PORT, log_level="warning")


async def _cron_send_current_time_once(send_dt: datetime):
    """向目标群发送一次“当前时间”。"""
    if not CRON_SEND_CURRENT_TIME_ENABLED:
        return
    try:
        if _target_chat_id is None:
            await _ensure_target_chat()
        if _target_chat_id is None:
            log.warning("cron 发送失败：未能解析到目标群 chat_id（%s）", TARGET_GROUP_NAME)
            return

        time_str = send_dt.strftime(CRON_SEND_CURRENT_TIME_TIME_FORMAT)
        text = CRON_SEND_CURRENT_TIME_MESSAGE_TEMPLATE.format(time=time_str)
        ok, err = await _send_to_target_group(text, kind="cron_current_time")
        if ok:
            log.info("cron 已发送当前时间: %s", time_str)
        else:
            log.warning("cron 发送当前时间未成功: %s", err)
    except Exception as e:
        log.warning("cron 发送当前时间到群失败: %s", e)


async def _cron_push_download_progress_once(send_dt: datetime):
    """定时推送下载进度；无下载任务时跳过。"""
    if not CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED:
        return
    try:
        if _target_chat_id is None:
            await _ensure_target_chat()
        if _target_chat_id is None:
            log.warning("下载进度播报失败：未能解析到目标群 chat_id（%s）", TARGET_GROUP_NAME)
            return
        text = _build_download_progress_status_text()
        if not text:
            log.info("cron 下载进度播报跳过：当前没有进行中的下载任务")
            return
        ok, err = await _send_to_target_group(text, kind="cron_download_progress")
        if ok:
            log.info("cron 已推送下载进度: %s", send_dt.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            log.warning("cron 推送下载进度未成功: %s", err)
    except Exception as e:
        log.warning("cron 推送下载进度到群失败: %s", e)


async def _cron_send_current_time_loop():
    """根据 cron 表达式定时发送当前时间。"""
    if not CRON_SEND_CURRENT_TIME_ENABLED:
        return
    if croniter is None:  # 理论上不会发生（上面已禁用）
        log.error("croniter 依赖缺失，cron 任务已禁用")
        return
    expr = CRON_SEND_CURRENT_TIME_CRON
    fields = expr.split()
    if len(fields) not in (5, 6):
        log.error("cron 表达式字段数不合法: %r（支持 5 字段或 6 字段）", expr)
        return

    second_at_beginning = len(fields) == 6
    try:
        ci = croniter(
            expr,
            datetime.now(),
            ret_type=datetime,
            second_at_beginning=second_at_beginning,
        )
        next_dt = ci.get_next(datetime)
        log.info("cron 定时发送已启用: expr=%s, next=%s", expr, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        log.error("cron 表达式解析失败: %r，错误: %s", expr, e)
        return

    while True:
        now = datetime.now()
        delay = (next_dt - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            # 如果系统时间变动导致已过期，直接立即发送一次
            await asyncio.sleep(0)

        await _cron_send_current_time_once(next_dt)
        try:
            next_dt = ci.get_next(datetime)
            log.info("cron 下一次触发时间: %s", next_dt.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log.error("cron 计算下一次触发时间失败: %s", e)
            return


async def _cron_push_download_progress_loop():
    """根据 cron 表达式定时推送下载进度。"""
    if not CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED:
        return
    if croniter is None:
        log.error("croniter 依赖缺失，下载进度播报任务已禁用")
        return
    expr = CRON_PUSH_DOWNLOAD_PROGRESS_CRON
    fields = expr.split()
    if len(fields) not in (5, 6):
        log.error("下载进度播报 cron 表达式字段数不合法: %r（支持 5 字段或 6 字段）", expr)
        return

    second_at_beginning = len(fields) == 6
    try:
        ci = croniter(
            expr,
            datetime.now(),
            ret_type=datetime,
            second_at_beginning=second_at_beginning,
        )
        next_dt = ci.get_next(datetime)
        log.info("cron 下载进度播报已启用: expr=%s, next=%s", expr, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        log.error("下载进度播报 cron 表达式解析失败: %r，错误: %s", expr, e)
        return

    while True:
        now = datetime.now()
        delay = (next_dt - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)

        await _cron_push_download_progress_once(next_dt)
        try:
            next_dt = ci.get_next(datetime)
            log.info("下载进度播报 cron 下一次触发时间: %s", next_dt.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log.error("下载进度播报 cron 计算下一次触发时间失败: %s", e)
            return


if __name__ == "__main__":
    _init_db()

    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    log.info("日志文件: %s", LOG_PATH)
    log.info("下载目录: %s", DOWNLOAD_PATH)
    log.info("临时目录: %s", TEMP_PATH)
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
        await _restore_unfinished_tasks()
        if CRON_SEND_CURRENT_TIME_ENABLED:
            asyncio.create_task(_cron_send_current_time_loop())
        if CRON_PUSH_DOWNLOAD_PROGRESS_ENABLED:
            asyncio.create_task(_cron_push_download_progress_loop())

    with client:
        client.loop.run_until_complete(_start())
        client.loop.run_until_complete(_notify_startup_ready())
        client.run_until_disconnected()
