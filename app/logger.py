import builtins
import time
import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional

original_print = builtins.print
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# ============================================================
# 日志时间显示：默认北京时间（容器默认 UTC 也不受影响）
# - 优先用 zoneinfo（安装 tzdata 时支持任意 IANA 时区）
# - 没有 tzdata 时回退到固定偏移（Asia/Shanghai = UTC+8，永远可用）
# - log_timezone="local" 则跟随服务器本地时区
# ============================================================

DEFAULT_LOG_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOG_TIME_FORMAT = "time"

_FIXED_TZ_HOURS = {
    "Asia/Shanghai": 8, "Asia/Chongqing": 8, "Asia/Harbin": 8,
    "Asia/Hong_Kong": 8, "Asia/Macau": 8, "Asia/Taipei": 8,
    "Asia/Singapore": 8, "Asia/Kuala_Lumpur": 8,
    "UTC": 0, "Etc/UTC": 0,
    "Asia/Tokyo": 9, "Asia/Seoul": 9,
    "Europe/London": 0, "America/New_York": -5, "America/Los_Angeles": -8,
}

LOCAL_TZ_ALIASES = {"", "local", "server", "system", "系统", "本地"}


def _log_prefs() -> tuple:
    """读取控制台保存的日志时区/格式（读不到就用默认值）。"""
    try:
        from runtime_state import app_state
        tz = app_state.get_setting("log_timezone", DEFAULT_LOG_TIMEZONE)
        fmt = app_state.get_setting("log_time_format", DEFAULT_LOG_TIME_FORMAT)
        return str(tz or DEFAULT_LOG_TIMEZONE), str(fmt or DEFAULT_LOG_TIME_FORMAT)
    except Exception:
        return DEFAULT_LOG_TIMEZONE, DEFAULT_LOG_TIME_FORMAT


def _resolve_timezone(name: str):
    """返回 tzinfo；None 表示跟随系统本地时区。"""
    raw = (name or "").strip()
    if raw.lower() in LOCAL_TZ_ALIASES:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(raw)
    except Exception:
        pass
    hours = _FIXED_TZ_HOURS.get(raw)
    if hours is None:
        return None
    return timezone(timedelta(hours=hours))


def format_timestamp(ts: Optional[float] = None) -> str:
    """按控制台设置格式化日志时间戳。"""
    tz_name, fmt = _log_prefs()
    tz = _resolve_timezone(tz_name)
    stamp = time.time() if ts is None else ts
    dt = datetime.fromtimestamp(stamp, tz)
    return dt.strftime("%m-%d %H:%M:%S") if str(fmt).lower() in ("datetime", "full", "date") \
        else dt.strftime("%H:%M:%S")


def describe_log_timezone() -> dict:
    """给控制台展示用：当前生效时区 + 一个实时样例。"""
    tz_name, fmt = _log_prefs()
    tz = _resolve_timezone(tz_name)
    sample = datetime.fromtimestamp(time.time(), tz)
    offset = sample.strftime("%z")
    offset_txt = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    label = {
        "Asia/Shanghai": "北京时间",
        "Asia/Hong_Kong": "香港时间",
        "Asia/Taipei": "台北时间",
        "Asia/Singapore": "新加坡时间",
        "Asia/Tokyo": "东京时间",
        "UTC": "UTC",
    }.get(tz_name, "服务器本地时区" if tz is None else tz_name)
    return {
        "log_timezone": tz_name,
        "log_time_format": fmt,
        "log_timezone_label": f"{label} ({offset_txt})" if offset_txt else label,
        "log_time_preview": format_timestamp(),
    }



class ProxyStats:
    def __init__(self):
        self.start_time = time.time()
        self.total_requests = 0
        self.success_requests = 0
        self.error_requests = 0
        self.retry_counts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.lock = threading.Lock()

    def increment_total(self):
        with self.lock:
            self.total_requests += 1

    def add_error(self):
        with self.lock:
            self.error_requests += 1

    def add_retry(self):
        with self.lock:
            self.retry_counts += 1

    def add_success(self):
        """直接计一次成功请求（Cookie 通道不产生 token 统计行，成功数单独计入）。"""
        with self.lock:
            self.success_requests += 1

    def add_tokens(self, p_tokens, c_tokens):
        with self.lock:
            self.prompt_tokens += p_tokens
            self.completion_tokens += c_tokens
            self.success_requests += 1

    def get_json_stats(self):
        with self.lock:
            return {
                "uptime": round(time.time() - self.start_time, 2),
                "total": self.total_requests,
                "success": self.success_requests,
                "error": self.error_requests,
                "retries": self.retry_counts,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }


stats = ProxyStats()


class SSELogger:
    """把运行日志推给控制台的 SSE 订阅者。

    P1-5 修复的三个隐患：
      - `asyncio.Queue.put_nowait` 不是线程安全的，而 push() 可能从任意线程被调用
        （图片压缩线程、to_thread 里的消息转换等）。改为 loop.call_soon_threadsafe。
      - 订阅者列表会被事件循环并发增删，遍历时可能 RuntimeError。改为持锁取快照。
      - 队列原本无界，慢客户端会把内存撑爆。改为有界 + 满则丢最旧。
    """

    def __init__(self, max_history: int = 100, queue_size: int = 500):
        self.max_history = max_history
        self.queue_size = queue_size
        self.history: List[str] = []
        self._subscribers: List[tuple] = []       # [(queue, loop)]
        self._lock = threading.Lock()

    # ---- 订阅管理 ----

    def subscribe(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        loop = loop or asyncio.get_running_loop()
        with self._lock:
            self._subscribers.append((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(sq, sl) for (sq, sl) in self._subscribers if sq is not q]

    def snapshot_history(self) -> List[str]:
        with self._lock:
            return list(self.history)

    # ---- 推送 ----

    @staticmethod
    def _offer(q: asyncio.Queue, msg: str) -> None:
        """在事件循环线程里执行：队列满时丢最旧的一条，保证新日志能进来。"""
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except Exception:
                pass

    def push(self, plain_text: str) -> None:
        timestamp = format_timestamp()
        formatted_msg = f"[{timestamp}] {plain_text}"

        with self._lock:
            self.history.append(formatted_msg)
            if len(self.history) > self.max_history:
                self.history.pop(0)
            subscribers = list(self._subscribers)

        for q, loop in subscribers:
            try:
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(self._offer, q, formatted_msg)
            except RuntimeError:
                # 事件循环已停止，忽略即可
                pass


rt_logger = SSELogger()

# 防止 print 钩子内部再触发 print 导致递归
_in_hook = threading.local()


def custom_print(*args, **kwargs):
    """把业务 print 同时推到控制台日志流。

    说明（P1-5 第一步）：仍保留对 builtins.print 的接管以兼容现有 6000 行代码里的
    print 调用，但已经**移除了用正则从日志文本反解 token 数**的逻辑——
    那与中文文案强耦合，改一个字就静默失效。现在由 api_helpers._record_usage()
    直接调用 stats.add_tokens()。后续可分批把 print 迁移到 logging。
    """
    if getattr(_in_hook, "active", False):
        original_print(*args, **kwargs)
        return

    _in_hook.active = True
    try:
        import io
        buf = io.StringIO()
        kwargs_for_buffer = dict(kwargs)
        kwargs_for_buffer["file"] = buf

        raw_msg = ""
        try:
            original_print(*args, **kwargs_for_buffer)
            raw_msg = buf.getvalue().strip()
        except Exception:
            pass

        try:
            original_print(*args, **kwargs)
        except Exception:
            pass

        if not raw_msg:
            return

        try:
            rt_logger.push(ANSI_ESCAPE.sub('', raw_msg))
        except Exception:
            pass
    finally:
        _in_hook.active = False


builtins.print = custom_print
