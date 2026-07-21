"""通用工具：日志、路径、系统工具、区域/坐标/字体格式化、截图。"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

try:
    from mss import mss
    from PIL import Image
except ImportError:
    mss = None
    Image = None


# 打包后 config/log 需写入 exe 同级目录，而不是临时解压目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "hsr_relic_config.json"
LOG_PATH = BASE_DIR / "hsr_relic_scorer.log"
# 精简日志：只记录角色基础值+遗器识别结果+评分，方便发给AI分析。每次启动清空。
STATS_LOG_PATH = BASE_DIR / "hsr_relic_stats.log"

DEFAULT_DETAIL_REGION = "0,0 | 1350,500"
DEFAULT_RELIC_REGION = "0,0 | 560,390"


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def log_to_file(msg: str, level: str = "INFO") -> None:
    """记录日志到文件"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def log_stats(msg: str) -> None:
    """精简日志：只记角色值+遗器值+评分，方便发给AI分析。每次启动自动清空。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}\n"
    try:
        with STATS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def clear_logs_on_startup() -> None:
    """启动时清空日志文件，并写入启动信息便于排查路径问题。"""
    startup_msg = (
        f"=== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        f"__file__ = {__file__}\n"
        f"sys.executable = {sys.executable}\n"
        f"sys.frozen = {getattr(sys, 'frozen', False)}\n"
        f"BASE_DIR = {BASE_DIR}\n"
        f"LOG_PATH = {LOG_PATH}\n"
        f"STATS_LOG_PATH = {STATS_LOG_PATH}\n"
        f"cwd = {os.getcwd()}\n"
    )
    try:
        LOG_PATH.write_text(startup_msg, encoding="utf-8")
    except Exception as exc:
        print(f"LOG_PATH 写入失败: {exc}", file=sys.stderr)
    try:
        STATS_LOG_PATH.write_text(startup_msg, encoding="utf-8")
    except Exception as exc:
        print(f"STATS_LOG_PATH 写入失败: {exc}", file=sys.stderr)


def scaled_font(base_size: int, scale: float, bold: bool = False) -> tuple:
    """根据缩放系数生成 overlay 字体规格。
    独立于 Windows DPI 缩放，让 overlay 字体在高分辨率下保持合适大小。"""
    actual = max(6, int(round(base_size * scale)))
    return ("Microsoft YaHei UI", actual, "bold") if bold else ("Microsoft YaHei UI", actual)


def is_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_star_rail_active() -> bool:
    """检查当前活动窗口是否为星穹铁道（StarRail.exe）"""
    if sys.platform != "win32":
        return True
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return False
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "StarRail" in title or "星穹铁道" in title:
            return True
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        try:
            process = psutil.Process(pid.value)
            if process.name().lower() == "starrail.exe":
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def relaunch_as_admin() -> None:
    if sys.platform != "win32":
        return
    params = " ".join(f'"{arg}"' for arg in sys.argv)
    exe = sys.executable
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, os.getcwd(), 1)


def set_window_clickthrough(win) -> None:
    """让窗口鼠标可穿透（仅 Windows）。需在窗口已 realize 后调用。"""
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        else:
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception as exc:
        log_to_file(f"set_window_clickthrough failed: {exc}", "ERROR")


def grab_region(region_text: str) -> tuple["Image.Image", tuple[int, int, int, int]]:
    if mss is None or Image is None:
        raise RuntimeError("缺少依赖：mss/PIL。请先运行 pip install -r requirements.txt")
    region = parse_region(region_text)
    x, y, w, h = region
    with mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    return image, region


def parse_region(text: str) -> tuple[int, int, int, int]:
    normalized = text.replace("，", ",").replace("｜", "|")
    if "|" in normalized:
        left_text, right_text = normalized.split("|", 1)
        x1, y1 = parse_point(left_text)
        x2, y2 = parse_point(right_text)
        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        if w <= 0 or h <= 0:
            raise ValueError("两个采样点不能相同，区域宽高必须大于 0")
        return x, y, w, h
    nums = [int(float(v.strip())) for v in normalized.split(",") if v.strip()]
    if len(nums) == 4:
        x, y, w, h = nums
        if w <= 0 or h <= 0:
            raise ValueError("区域宽高必须大于 0")
        return x, y, w, h
    raise ValueError('区域格式必须是 "x,y | x,y"')


def parse_point(text: str) -> tuple[int, int]:
    nums = [int(float(v.strip())) for v in text.split(",") if v.strip()]
    if len(nums) != 2:
        raise ValueError('坐标点格式必须是 "x,y"')
    return nums[0], nums[1]


def format_region(region: tuple[int, int, int, int]) -> str:
    x, y, w, h = region
    return f"{x},{y} | {x + w},{y + h}"


def format_score_delta(score: float, delta: float) -> str:
    score_text = f"{score:.0f}" if abs(score) >= 10 else f"{score:.1f}"
    delta_text = f"{abs(delta):.0f}" if abs(delta) >= 10 else f"{abs(delta):.1f}"
    if delta_text in ("0.0", "0"):
        return score_text
    sign = "+" if delta >= 0 else "-"
    return f"{score_text} {sign} {delta_text}"
