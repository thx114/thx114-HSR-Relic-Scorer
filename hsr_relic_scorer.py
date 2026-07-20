from __future__ import annotations

import base64
import ctypes
import copy
import io
import json
import math
import os
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter import simpledialog

import requests
try:
    from mss import mss
    from PIL import Image, ImageTk
    from pynput import keyboard, mouse
    import numpy as np
except ImportError as exc:
    mss = None
    Image = None
    ImageTk = None
    keyboard = None
    mouse = None
    np = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


OCR_URL = "http://127.0.0.1:1224/api/ocr"

RELIC_SLOTS = ["头部", "手部", "躯干", "脚部", "位面球", "连结绳"]
RELIC_SLOT_ALIASES = {
    "躯平": "躯干",
    "躯干球": "位面球",
    "位面": "位面球",
    "连绳": "连结绳",
    "连结": "连结绳",
    "绳": "连结绳",
    "头": "头部",
    "手": "手部",
    "脚": "脚部",
}


def identify_slot(text: str) -> str:
    for slot in RELIC_SLOTS:
        if slot in text:
            return slot
    for alias, target in RELIC_SLOT_ALIASES.items():
        if alias in text:
            return target
    return "未知"


VALID_STATS = {
    "生命值",
    "攻击力",
    "防御力",
    "生命值%",
    "攻击力%",
    "防御力%",
    "速度",
    "暴击率",
    "暴击伤害",
    "击破特攻",
    "效果命中",
    "效果抵抗",
    "属性加伤",
    "物理属性伤害提高",
    "火属性伤害提高",
    "冰属性伤害提高",
    "雷属性伤害提高",
    "风属性伤害提高",
    "量子属性伤害提高",
    "虚数属性伤害提高",
    "能量恢复",
    "治疗加成",
}

# 各属性伤害主词条统一映射为"属性加伤"
ELEMENTAL_DAMAGE_ALIASES = {
    "物理属性伤害提高": "属性加伤",
    "火属性伤害提高": "属性加伤",
    "冰属性伤害提高": "属性加伤",
    "雷属性伤害提高": "属性加伤",
    "风属性伤害提高": "属性加伤",
    "量子属性伤害提高": "属性加伤",
    "虚数属性伤害提高": "属性加伤",
    # OCR 常见错误别名
    "物理伤害提高": "属性加伤",
    "火伤害提高": "属性加伤",
    "冰伤害提高": "属性加伤",
    "雷伤害提高": "属性加伤",
    "风伤害提高": "属性加伤",
    "量子伤害提高": "属性加伤",
    "虚数伤害提高": "属性加伤",
}

# 姬子「启行」权重。权重为 0 的词条可被识别、展示，但不会计入有效分。
HIMEKO_QIXING_WEIGHTS = {
    "生命值": 0.0,
    "生命值%": 0.0,
    "攻击力": 0.75,
    "攻击力%": 0.75,
    "防御力": 0.0,
    "防御力%": 0.0,
    "速度": 0.75,
    "暴击率": 1.0,
    "暴击伤害": 1.0,
    "属性加伤": 1.0,
    "击破特攻": 0.0,
    "能量恢复": 1.0,
    "治疗加成": 0.0,
    "效果命中": 0.0,
    "效果抵抗": 0.0,
}

# 角色可选择的副词条全集（用于"有效副词条"多选框）
ALL_SUB_STATS = [
    "生命值", "生命值%", "攻击力", "攻击力%",
    "防御力", "防御力%", "速度",
    "暴击率", "暴击伤害", "击破特攻",
    "效果命中", "效果抵抗", "属性加伤",
]

# 伤害来源下拉框选项：value -> 显示文本
DAMAGE_SOURCES = {
    "ATK": "攻击力",
    "HP": "生命值",
    "DEF": "防御力",
    "SPD": "速度",
    "BREAK": "击破特攻",
    "CUSTOM": "自定义公式",
}

# 转模公式可写入的目标属性
CONVERSION_TARGETS = {
    "atk_flat": "攻击力(固定)",
    "atk_pct": "攻击力%",
    "hp_flat": "生命值(固定)",
    "hp_pct": "生命值%",
    "def_flat": "防御力(固定)",
    "def_pct": "防御力%",
    "spd": "速度",
    "crit_rate": "暴击率%",
    "crit_dmg": "暴击伤害%",
    "break_dmg": "击破特攻%",
    "dmg_bonus": "属性加伤%",
    "energy": "能量恢复%",
}

# 转模公式 / 伤害公式中可用的变量名（在公式编辑对话框中作为注释显示）
FORMULA_AVAILABLE_VARS = """可用变量（区分大小写）：
  baseATK        基础攻击力（白值）
  ATK            最终攻击力
  baseHP         基础生命值
  HP             最终生命值
  baseDEF        基础防御力
  DEF            最终防御力
  SPD            速度
  CR             暴击率（0~1）
  CD             暴击伤害（0~1）
  DMG            属性伤害加成（0~1）
  ENERGY         能量恢复（0~1）
  BREAK          击破特攻（0~1）
可用函数：min(a,b), max(a,b), abs(x), sqrt(x)
注意：返回百分比类目标时直接填小数（如 0.1 表示 10%）
示例：ATK * 0.001   # 每点攻击力增加 0.1% 爆伤
"""

_DEFAULT_VALID_SUBS = [name for name, w in HIMEKO_QIXING_WEIGHTS.items() if w > 0]

# 队友拐力 / 自拐的输入字段。生命值、生命值%、击破特攻% 对姬子伤害无影响，仅保存与展示。
BUFF_FIELDS = ["atk", "atk_pct", "crit_rate", "crit_dmg", "hp", "hp_pct", "break_dmg"]
BUFF_LABELS = {
    "atk": "攻击力",
    "atk_pct": "攻击力%",
    "crit_rate": "暴击率%",
    "crit_dmg": "暴击伤害%",
    "hp": "生命值",
    "hp_pct": "生命值%",
    "break_dmg": "击破特攻%",
}

DEFAULT_DETAIL_REGION = "0,0 | 1350,500"
DEFAULT_RELIC_REGION = "0,0 | 560,390"
# 打包后 config/log 需写入 exe 同级目录，而不是临时解压目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "hsr_relic_config.json"
LOG_PATH = BASE_DIR / "hsr_relic_scorer.log"


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


@dataclass
class StatLine:
    name: str
    value: float
    percent: bool
    box: list | None = None
    score: float = 0.0
    delta: float = 0.0
    theoretical_score: float = 0.0  # 暴击率超100%时的理论分
    weight: float = 0.0  # 词条权重，>0 表示有效词条（即使 score=0 也显示）


@dataclass
class Relic:
    slot: str
    main_name: str | None = None
    main_value: float = 0.0
    main_box: list | None = None  # 主词条 OCR box，用于 debug 对齐
    subs: list[StatLine] = field(default_factory=list)
    raw_text: str = ""
    total_score: float = 0.0
    total_delta: float = 0.0
    theoretical_total: float = 0.0  # 暴击率超100%时的理论总分
    removed_stats: list[tuple[str, float]] = field(default_factory=list)  # 替换时被删除的有效词条 (name, score)

    def signature(self) -> str:
        payload = {
            "slot": self.slot,
            "main_name": self.main_name,
            "main_value": round(float(self.main_value), 4),
            "subs": [(s.name, round(float(s.value), 4), s.percent) for s in self.subs],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


@dataclass
class CharacterStats:
    hp_base: float = 0.0
    hp_bonus: float = 0.0
    atk_base: float = 0.0
    atk_bonus: float = 0.0
    def_base: float = 0.0
    def_bonus: float = 0.0
    speed: float = 0.0
    crit_rate: float = 0.0
    crit_dmg: float = 0.0


@dataclass
class CharacterConfig:
    """单个角色的完整配置：有效副词条、转模公式、伤害来源、拐力、基础值、遗器。"""
    name: str
    valid_subs: list[str] = field(default_factory=lambda: list(_DEFAULT_VALID_SUBS))
    # 转模公式列表：每项 {"target": "crit_dmg", "formula": "ATK * 0.001", "desc": "..."}
    conversion_formulas: list[dict] = field(default_factory=list)
    damage_source: str = "ATK"  # ATK|HP|DEF|SPD|BREAK|CUSTOM
    damage_formula: str = "ATK"  # damage_source == CUSTOM 时使用
    ally_buffs: dict[str, str] = field(default_factory=dict)
    self_buffs: dict[str, str] = field(default_factory=dict)
    base_stats: CharacterStats = field(default_factory=CharacterStats)
    relics: dict[str, Relic] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "valid_subs": list(self.valid_subs),
            "conversion_formulas": [dict(f) for f in self.conversion_formulas],
            "damage_source": self.damage_source,
            "damage_formula": self.damage_formula,
            "ally_buffs": dict(self.ally_buffs),
            "self_buffs": dict(self.self_buffs),
            "base_stats": character_stats_to_dict(self.base_stats),
            "relics": {slot: relic_to_dict(r) for slot, r in self.relics.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CharacterConfig":
        return cls(
            name=str(data.get("name", "未命名")),
            valid_subs=list(data.get("valid_subs", _DEFAULT_VALID_SUBS)),
            conversion_formulas=[dict(f) for f in data.get("conversion_formulas", [])],
            damage_source=str(data.get("damage_source", "ATK")),
            damage_formula=str(data.get("damage_formula", "ATK")),
            ally_buffs=dict(data.get("ally_buffs", {})),
            self_buffs=dict(data.get("self_buffs", {})),
            base_stats=character_stats_from_dict(data.get("base_stats", {})),
            relics={slot: relic_from_dict(r) for slot, r in data.get("relics", {}).items()},
        )

    @classmethod
    def default_himeko(cls) -> "CharacterConfig":
        """默认角色：姬子「启行」。"""
        return cls(name="姬子")


_FORMULA_GLOBALS = {
    "__builtins__": {},
    "min": min,
    "max": max,
    "abs": abs,
    "sqrt": math.sqrt,
    "pow": pow,
    "round": round,
}


def _build_formula_locals(totals: dict[str, float]) -> dict[str, float]:
    """从 build_totals 结果构造公式可用变量字典。"""
    return {
        "baseATK": totals.get("atk_base", 0.0),
        "ATK": totals.get("atk", 0.0),
        "baseHP": totals.get("hp_base", 0.0),
        "HP": totals.get("hp", 0.0),
        "baseDEF": totals.get("def_base", 0.0),
        "DEF": totals.get("def", 0.0),
        "SPD": totals.get("speed", 0.0),
        "CR": totals.get("crit_rate_raw", totals.get("crit_rate", 0.0)),
        "CD": totals.get("crit_dmg", 0.0),
        "DMG": totals.get("dmg_bonus", 0.0),
        "ENERGY": totals.get("energy", 0.0),
        "BREAK": totals.get("break_dmg", 0.0),
    }


def safe_eval_formula(formula: str, totals: dict[str, float]) -> float:
    """在受限环境下求值公式。失败返回 0.0 并记日志。"""
    if not formula or not formula.strip():
        return 0.0
    try:
        locals_dict = _build_formula_locals(totals)
        return float(eval(formula, dict(_FORMULA_GLOBALS), locals_dict))
    except Exception as exc:
        log_to_file(f"公式求值失败: {formula!r} -> {exc}", "ERROR")
        return 0.0


def stat_to_dict(stat: StatLine) -> dict:
    return {
        "name": stat.name,
        "value": stat.value,
        "percent": stat.percent,
        "score": stat.score,
        "delta": stat.delta,
    }


def stat_from_dict(data: dict) -> StatLine:
    return StatLine(
        name=str(data.get("name", "")),
        value=float(data.get("value", 0.0)),
        percent=bool(data.get("percent", False)),
        score=float(data.get("score", 0.0)),
        delta=float(data.get("delta", 0.0)),
    )


def relic_to_dict(relic: Relic) -> dict:
    return {
        "slot": relic.slot,
        "main_name": relic.main_name,
        "main_value": relic.main_value,
        "subs": [stat_to_dict(stat) for stat in relic.subs],
        "raw_text": relic.raw_text,
        "total_score": relic.total_score,
        "total_delta": relic.total_delta,
    }


def relic_from_dict(data: dict) -> Relic:
    return Relic(
        slot=str(data.get("slot", "未知")),
        main_name=data.get("main_name"),
        main_value=float(data.get("main_value", 0.0)),
        subs=[stat_from_dict(item) for item in data.get("subs", [])],
        raw_text=str(data.get("raw_text", "")),
        total_score=float(data.get("total_score", 0.0)),
        total_delta=float(data.get("total_delta", 0.0)),
    )


def character_stats_to_dict(stats: CharacterStats) -> dict:
    return {
        "hp_base": stats.hp_base,
        "hp_bonus": stats.hp_bonus,
        "atk_base": stats.atk_base,
        "atk_bonus": stats.atk_bonus,
        "def_base": stats.def_base,
        "def_bonus": stats.def_bonus,
        "speed": stats.speed,
        "crit_rate": stats.crit_rate,
        "crit_dmg": stats.crit_dmg,
    }


def character_stats_from_dict(data: dict) -> CharacterStats:
    return CharacterStats(
        hp_base=float(data.get("hp_base", 0.0)),
        hp_bonus=float(data.get("hp_bonus", 0.0)),
        atk_base=float(data.get("atk_base", 0.0)),
        atk_bonus=float(data.get("atk_bonus", 0.0)),
        def_base=float(data.get("def_base", 0.0)),
        def_bonus=float(data.get("def_bonus", 0.0)),
        speed=float(data.get("speed", 0.0)),
        crit_rate=float(data.get("crit_rate", 0.0)),
        crit_dmg=float(data.get("crit_dmg", 0.0)),
    )


def parse_plus_expr(text: str) -> float:
    """解析拐力输入。允许输入中文/符号作为注释，仅累加其中的数字部分。

    支持格式：
      "100+200"           -> 300
      "100（鸟）+200（停云）" -> 300
      "攻击力 100"         -> 100
      "5.5% 暴击"          -> 5.5
      "无"                -> 0
    百分号会被剥离，不参与数值（与原逻辑一致）。
    """
    if not text:
        return 0.0
    clean = text.strip().replace("％", "%").replace("，", ",")
    clean = clean.replace("%", "")
    total = 0.0
    for part in clean.split("+"):
        part = part.strip()
        if not part:
            continue
        # 提取第一个数字（支持小数和负号），其余字符作为注释忽略
        m = re.search(r"-?\d+(?:\.\d+)?", part)
        if m:
            total += float(m.group())
    return total


def normalize_stat_name(name: str, value_has_percent: bool) -> str:
    name = name.replace(" ", "").replace("暴击伤害", "暴击伤害")
    name = name.replace("暴击率", "暴击率").replace("擊破特攻", "击破特攻")
    # 各属性伤害提高统一归为"属性加伤"
    if name in ELEMENTAL_DAMAGE_ALIASES:
        return ELEMENTAL_DAMAGE_ALIASES[name]
    if name in {"生命值", "攻击力", "防御力"} and value_has_percent:
        return name + "%"
    return name


def pct_to_ratio(value: float) -> float:
    return value / 100.0


class UmiOcrClient:
    def __init__(self, url: str = OCR_URL):
        self.url = url
        # 屏蔽区域（相对图片坐标），用于屏蔽强化标记①②等，格式 [[x1,y1],[x2,y2]]
        self.ignore_area: list | None = None

    def image_to_lines(self, image: Image.Image) -> list[dict]:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        payload = {
            "base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            # 提高大图识别精度：不压缩边长，保留更多像素细节
            "ocr.limit_side_len": 4320,
        }
        if self.ignore_area:
            payload["tbpu.ignoreArea"] = [self.ignore_area]
        response = requests.post(self.url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 100:
            raise RuntimeError(str(data.get("data", data)))
        lines = data.get("data") or []
        return sorted(lines, key=lambda item: (box_y(item.get("box")), box_x(item.get("box"))))


def box_x(box) -> float:
    if not box:
        return 0.0
    return min(p[0] for p in box)


def box_y(box) -> float:
    if not box:
        return 0.0
    return min(p[1] for p in box)


def box_mid_y(box) -> float:
    if not box:
        return 0.0
    return sum(p[1] for p in box) / len(box)


def grab_region(region_text: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
    if mss is None or Image is None:
        raise RuntimeError(f"缺少依赖：{IMPORT_ERROR}。请先运行 pip install -r requirements.txt")
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
    # 格式化后若为 0.0/0 则不显示差值，避免出现 "+ 0.0"
    if delta_text in ("0.0", "0"):
        return score_text
    sign = "+" if delta >= 0 else "-"
    return f"{score_text} {sign} {delta_text}"


def flatten_ocr_text(lines: list[dict]) -> str:
    return "\n".join(str(item.get("text", "")) for item in lines if item.get("text"))


def _find_percent_after(text: str, keyword: str, next_keywords: list[str] | None = None) -> float | None:
    """在 text 中找 keyword 之后、下一个词条名之前的第一个百分比数字。
    next_keywords 指定可能紧跟在 keyword 后的词条名，用于限定搜索范围，避免越过下一个词条。"""
    pos = text.find(keyword)
    if pos < 0:
        return None
    start = pos + len(keyword)
    # 限定搜索范围：找到下一个词条名的位置
    end = len(text)
    if next_keywords:
        nk_pos = len(text)
        for nk in next_keywords:
            p = text.find(nk, start)
            if 0 <= p < nk_pos:
                nk_pos = p
        if nk_pos < end:
            end = nk_pos
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text[start:end])
    if not m:
        return None
    return float(m.group(1))


def _find_flat_after(text: str, keyword: str, next_keywords: list[str] | None = None) -> tuple[float, float | None] | None:
    """在 text 中找 keyword 之后、下一个词条名之前的数值。
    返回 (base_value, bonus_value_or_None)。next_keywords 用于限定搜索范围。"""
    pos = text.find(keyword)
    if pos < 0:
        return None
    start = pos + len(keyword)
    end = len(text)
    if next_keywords:
        nk_pos = len(text)
        for nk in next_keywords:
            p = text.find(nk, start)
            if 0 <= p < nk_pos:
                nk_pos = p
        if nk_pos < end:
            end = nk_pos
    after = text[start:end]
    matches = list(re.finditer(r"([+＋]?)(\d+(?:\.\d+)?)", after))
    if not matches:
        return None
    base_val = None
    bonus_val = None
    for m in matches:
        is_plus = bool(m.group(1))
        val = float(m.group(2))
        if not is_plus and base_val is None:
            base_val = val
        elif is_plus and base_val is not None and bonus_val is None:
            bonus_val = val
        if base_val is not None and bonus_val is not None:
            break
    if base_val is None:
        return None
    return (base_val, bonus_val)


def parse_detail_stats(text: str) -> CharacterStats:
    matches = list(re.finditer(r"[+＋]?\d+(?:\.\d+)?%?", text))
    nums = []
    for match in matches:
        raw = match.group(0).replace("＋", "+")
        nums.append(
            {
                "raw": raw,
                "value": float(raw.replace("+", "").replace("%", "")),
                "plus": raw.startswith("+"),
                "percent": "%" in raw,
            }
        )
    stats = CharacterStats()

    # 优先用词条名定位（抗 OCR 漏行/多行），失败则回退到原有顺序解析
    # next_keywords 指定每个属性后可能紧跟的下一个属性名，用于限定搜索范围
    hp_res = _find_flat_after(text, "生命值", ["攻击力", "防御力", "速度", "暴击率", "暴击伤害"])
    atk_res = _find_flat_after(text, "攻击力", ["防御力", "速度", "暴击率", "暴击伤害"])
    def_res = _find_flat_after(text, "防御力", ["速度", "暴击率", "暴击伤害"])
    spd_res = _find_flat_after(text, "速度", ["暴击率", "暴击伤害", "击破特攻", "进阶属性"])
    crit_rate_val = _find_percent_after(text, "暴击率", ["暴击伤害", "击破特攻"])
    crit_dmg_val = _find_percent_after(text, "暴击伤害", ["击破特攻"])

    used_name_based = (hp_res and atk_res and def_res and spd_res
                       and crit_rate_val is not None and crit_dmg_val is not None)
    if used_name_based:
        stats.hp_base, stats.hp_bonus = hp_res[0], (hp_res[1] or 0.0)
        stats.atk_base, stats.atk_bonus = atk_res[0], (atk_res[1] or 0.0)
        stats.def_base, stats.def_bonus = def_res[0], (def_res[1] or 0.0)
        stats.speed = spd_res[0]
        stats.crit_rate = pct_to_ratio(crit_rate_val)
        stats.crit_dmg = pct_to_ratio(crit_dmg_val)
        return stats

    # 回退：原有顺序解析
    percent_nums = [item for item in nums if item["percent"]]
    flat_nums = [item for item in nums if not item["percent"]]
    if len(flat_nums) >= 4 and len(percent_nums) >= 2:
        idx = 0
        stats.hp_base = flat_nums[idx]["value"]
        idx += 1
        if idx < len(flat_nums) and flat_nums[idx]["plus"]:
            stats.hp_bonus = flat_nums[idx]["value"]
            idx += 1

        if idx < len(flat_nums):
            stats.atk_base = flat_nums[idx]["value"]
            idx += 1
        if idx < len(flat_nums) and flat_nums[idx]["plus"]:
            stats.atk_bonus = flat_nums[idx]["value"]
            idx += 1

        if idx < len(flat_nums):
            stats.def_base = flat_nums[idx]["value"]
            idx += 1
        if idx < len(flat_nums) and flat_nums[idx]["plus"]:
            stats.def_bonus = flat_nums[idx]["value"]
            idx += 1

        if idx < len(flat_nums):
            stats.speed = flat_nums[idx]["value"]
        stats.crit_rate = pct_to_ratio(percent_nums[-2]["value"])
        stats.crit_dmg = pct_to_ratio(percent_nums[-1]["value"])
    return stats


def detect_enhance_marker_by_color(image) -> list | None:
    """通过颜色 #6EE0B6 检测强化标记①②等在图片中的位置，
    返回相对图片的屏蔽区域 [[x1,y1],[x2,y2]]。所有遗器强化标记位置固定，首次检测后可复用。"""
    if Image is None or np is None:
        return None
    try:
        arr = np.array(image.convert("RGB"))
        # 目标颜色 #6EE0B6 = (110, 224, 182)
        target = np.array([110, 224, 182])
        # 允许一定容差，匹配相近的绿色
        diff = np.abs(arr.astype(int) - target).sum(axis=2)
        mask = diff < 30  # 总差值小于30视为匹配
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return None
        x1, x2 = int(xs.min()), int(xs.max())
        y1, y2 = int(ys.min()), int(ys.max())
        # 外扩 8px，覆盖标记数字本身（数字可能不是纯绿色）
        return [[max(0, x1 - 8), max(0, y1 - 8)], [x2 + 8, y2 + 8]]
    except Exception as exc:
        log_to_file(f"detect_enhance_marker_by_color failed: {exc}", "ERROR")
        return None


def parse_relic(lines: list[dict]) -> Relic:
    text = flatten_ocr_text(lines)
    compact = text.replace(" ", "")
    slot = identify_slot(compact)
    log_to_file(f"parse_relic: slot={slot}, raw_text=\n{text}", "DEBUG")

    line_groups: list[dict] = []
    for item in lines:
        line_text = str(item.get("text", "")).strip()
        if not line_text:
            continue
        # 提前清理所有带圈数字，避免干扰数值识别
        circled_digits = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
        for c in circled_digits:
            line_text = line_text.replace(c, "")
        # OCR 偶尔把 "11" 识成 "1.1"，导致 "11.6%" 变成 "1.1.6%"。
        # 检测多小数点异常（X.Y.Z%），去掉第一个小数点拼成 XY.Z%。
        # 仅当拼接后的数值在遗器副词条合理范围内才修正。
        m_multi_dot = re.search(r"(\d+)\.(\d+)\.(\d+)%?", line_text)
        if m_multi_dot:
            merged = f"{m_multi_dot.group(1)}{m_multi_dot.group(2)}.{m_multi_dot.group(3)}"
            if m_multi_dot.group(0).endswith("%"):
                merged += "%"
            try:
                merged_val = float(merged.replace("%", ""))
                # 副词条百分比合理范围 1.0~40.0
                if 1.0 <= merged_val <= 40.0:
                    line_text = line_text[:m_multi_dot.start()] + merged + line_text[m_multi_dot.end():]
                    log_to_file(f"parse_relic: fixed multi-dot '{m_multi_dot.group(0)}' -> '{merged}'", "DEBUG")
            except ValueError:
                pass
        box = item.get("box")
        names = find_stat_names(line_text)
        values = []
        for match in re.finditer(r"-?\d+(?:\.\d+)?\s*%?", line_text):
            vt = match.group(0).replace(" ", "").replace("-", "")
            if vt.startswith("."):
                vt = vt[1:]
            if vt:
                val = float(vt.replace("%", ""))
                hpct = "%" in match.group(0)
                if not hpct and val > 100 and val < 1000:
                    s = str(val)
                    if len(s) >= 2 and s[-1] in "0123456789":
                        last_digit = int(s[-1])
                        if last_digit <= 5:
                            new_val = float(s[:-1])
                            if 30 <= new_val <= 200:
                                val = new_val
                                log_to_file(f"parse_relic: fixed value {vt} -> {new_val}", "DEBUG")
                values.append((val, hpct))
        if names or values:
            line_groups.append({
                "y": box_mid_y(box) if box else 0,
                "x": box_x(box) if box else 0,
                "box": box,
                "names": names,
                "values": values,
                "text": line_text,
            })
    line_groups.sort(key=lambda g: (g["y"], g["x"]))
    log_to_file(f"parse_relic: line_groups={line_groups}", "DEBUG")

    # 每个 pending 项额外带 box，便于后续对齐位置
    pending_names: list[tuple[str, float, list | None]] = []
    pending_values: list[tuple[float, bool, float, list | None]] = []
    pairs: list[tuple[str, float, bool, list | None]] = []

    for group in line_groups:
        line_y = group["y"]
        line_box = group.get("box")

        for name in group["names"]:
            found = False
            for i, (val, hpct, vy, _vbox) in enumerate(pending_values):
                if abs(line_y - vy) > 40:
                    continue
                if is_value_compatible(name, val, hpct):
                    # name 与 value 跨行时，优先用 name 所在行的 box（更稳定）
                    pairs.append((name, val, hpct, line_box))
                    del pending_values[i]
                    found = True
                    break
            if not found:
                pending_names.append((name, line_y, line_box))

        for val, hpct in group["values"]:
            found = False
            for i, (name, ny, _nbox) in enumerate(pending_names):
                if abs(line_y - ny) > 40:
                    continue
                if is_value_compatible(name, val, hpct):
                    # 跨行配对时用 name 所在行的 box
                    pairs.append((name, val, hpct, _nbox))
                    del pending_names[i]
                    found = True
                    break
            if not found:
                if not hpct and val <= 30 and len(group["names"]) == 0 and "＋" in group["text"]:
                    log_to_file(f"parse_relic: filtered noise value={val} (line={group['text']})", "DEBUG")
                    continue
                pending_values.append((val, hpct, line_y, line_box))

    log_to_file(f"parse_relic: before cross-line - pending_names={pending_names}, pending_values={pending_values}", "DEBUG")

    for name, ny, nbox in pending_names:
        for i, (val, hpct, vy, _vbox) in enumerate(pending_values):
            if abs(ny - vy) <= 50 and is_value_compatible(name, val, hpct):
                pairs.append((name, val, hpct, nbox))
                del pending_values[i]
                break

    log_to_file(f"parse_relic: pairs before dedup={pairs}", "DEBUG")

    seen_names = set()
    unique_pairs = []
    for name, val, hpct, box in pairs:
        key = (name, hpct, round(val, 2))
        if key not in seen_names:
            seen_names.add(key)
            unique_pairs.append((name, val, hpct, box))
        else:
            log_to_file(f"parse_relic: deduplicated {name}={val}%={hpct}", "DEBUG")

    log_to_file(f"parse_relic: unique_pairs={unique_pairs}", "DEBUG")

    main_name = None
    main_value = 0.0
    subs: list[StatLine] = []
    if unique_pairs:
        first_name, first_value, first_pct, first_box = unique_pairs[0]
        main_name = normalize_stat_name(first_name, first_pct)
        main_value = first_value
        main_box = first_box
        for name, value, has_pct, box in unique_pairs[1:]:
            stat_name = normalize_stat_name(name, has_pct)
            subs.append(StatLine(stat_name, value, has_pct, box))

    if main_name is None:
        for name in find_stat_names(compact):
            main_name = name
            break
    log_to_file(f"parse_relic: result - main={main_name}:{main_value}, subs={[(s.name, s.value, s.percent) for s in subs]}", "DEBUG")
    return Relic(slot=slot, main_name=main_name, main_value=main_value, main_box=main_box, subs=subs[:4], raw_text=text)


def is_value_compatible(raw_name: str, value: float, has_percent: bool) -> bool:
    if raw_name in {"暴击率", "暴击伤害", "击破特攻", "效果命中", "效果抵抗", "属性加伤"}:
        return has_percent
    if raw_name in {"物理属性伤害提高", "火属性伤害提高", "冰属性伤害提高", "雷属性伤害提高",
                    "风属性伤害提高", "量子属性伤害提高", "虚数属性伤害提高"}:
        return has_percent
    if raw_name == "速度":
        return not has_percent
    if raw_name in {"生命值", "攻击力", "防御力"}:
        return True
    return value > 0


def extract_line_tokens(line: str, box) -> list[tuple[str, list | None]]:
    pieces = re.findall(
        r"生命值|攻击力|防御力|速度|暴击率|暴击伤害|击破特攻|效果命中|效果抵抗|"
        r"物理属性伤害提高|火属性伤害提高|冰属性伤害提高|雷属性伤害提高|"
        r"风属性伤害提高|量子属性伤害提高|虚数属性伤害提高|"
        r"\d+(?:\.\d+)?\s*%?",
        line,
    )
    if not pieces:
        return [(line, box)]
    return [(piece, box) for piece in pieces]


def find_stat_names(text: str) -> list[str]:
    names = ["暴击伤害", "击破特攻", "效果命中", "效果抵抗", "暴击率", "生命值", "攻击力", "防御力", "速度",
             "物理属性伤害提高", "火属性伤害提高", "冰属性伤害提高", "雷属性伤害提高",
             "风属性伤害提高", "量子属性伤害提高", "虚数属性伤害提高"]
    result = []
    for name in names:
        count = text.count(name)
        result.extend([name] * count)
    return result


def build_totals(base: CharacterStats, relics: dict[str, Relic], buffs: dict[str, float]) -> dict[str, float]:
    atk_base = base.atk_base
    atk_bonus = base.atk_bonus
    atk_percent = 0.0
    hp_base = base.hp_base
    hp_bonus = base.hp_bonus
    hp_percent = 0.0
    def_base = base.def_base
    def_bonus = base.def_bonus
    def_percent = 0.0
    crit_rate_base = base.crit_rate
    crit_dmg_base = base.crit_dmg
    speed = base.speed
    break_dmg = 0.0
    dmg_bonus = pct_to_ratio(buffs.get("dmg_bonus", 0.0))
    energy = pct_to_ratio(buffs.get("energy", 0.0))

    relic_atk_flat = 0.0
    relic_atk_percent = 0.0
    relic_hp_flat = 0.0
    relic_hp_percent = 0.0
    relic_def_flat = 0.0
    relic_def_percent = 0.0
    crit_rate = crit_rate_base
    crit_dmg = crit_dmg_base
    relic_crit_rate_by_slot: dict[str, float] = {}
    relic_crit_dmg_by_slot: dict[str, float] = {}

    for slot, relic in relics.items():
        slot_crit_rate = 0.0
        slot_crit_dmg = 0.0
        if relic.main_name:
            (atk_base, atk_bonus, atk_percent, relic_atk_flat, relic_atk_percent,
             hp_base, hp_bonus, hp_percent, relic_hp_flat, relic_hp_percent,
             def_base, def_bonus, def_percent, relic_def_flat, relic_def_percent,
             _, _, speed, dmg_bonus, energy, break_dmg) = apply_stat_to_totals(
                relic.main_name, relic.main_value,
                atk_base, atk_bonus, atk_percent, relic_atk_flat, relic_atk_percent,
                hp_base, hp_bonus, hp_percent, relic_hp_flat, relic_hp_percent,
                def_base, def_bonus, def_percent, relic_def_flat, relic_def_percent,
                0.0, 0.0, speed, dmg_bonus, energy, break_dmg,
            )
            cr, cd = _extract_crit(relic.main_name, relic.main_value)
            slot_crit_rate += cr
            slot_crit_dmg += cd
        for stat in relic.subs:
            (atk_base, atk_bonus, atk_percent, relic_atk_flat, relic_atk_percent,
             hp_base, hp_bonus, hp_percent, relic_hp_flat, relic_hp_percent,
             def_base, def_bonus, def_percent, relic_def_flat, relic_def_percent,
             _, _, speed, dmg_bonus, energy, break_dmg) = apply_stat_to_totals(
                stat.name, stat.value,
                atk_base, atk_bonus, atk_percent, relic_atk_flat, relic_atk_percent,
                hp_base, hp_bonus, hp_percent, relic_hp_flat, relic_hp_percent,
                def_base, def_bonus, def_percent, relic_def_flat, relic_def_percent,
                0.0, 0.0, speed, dmg_bonus, energy, break_dmg,
            )
            cr, cd = _extract_crit(stat.name, stat.value)
            slot_crit_rate += cr
            slot_crit_dmg += cd
        relic_crit_rate_by_slot[slot] = slot_crit_rate
        relic_crit_dmg_by_slot[slot] = slot_crit_dmg
        crit_rate += slot_crit_rate
        crit_dmg += slot_crit_dmg

    # 拐力作为最后一项额外叠加
    buff_atk_flat = buffs.get("atk", 0.0)
    buff_atk_pct = pct_to_ratio(buffs.get("atk_pct", 0.0))
    buff_crit_rate = pct_to_ratio(buffs.get("crit_rate", 0.0))
    buff_crit_dmg = pct_to_ratio(buffs.get("crit_dmg", 0.0))
    buff_hp_flat = buffs.get("hp", 0.0)
    buff_hp_pct = pct_to_ratio(buffs.get("hp_pct", 0.0))
    buff_break_dmg = pct_to_ratio(buffs.get("break_dmg", 0.0))

    atk_percent_total = 1.0 + atk_percent + relic_atk_percent + buff_atk_pct
    atk = atk_base * atk_percent_total + atk_bonus + relic_atk_flat + buff_atk_flat
    hp_percent_total = 1.0 + hp_percent + relic_hp_percent + buff_hp_pct
    hp = hp_base * hp_percent_total + hp_bonus + relic_hp_flat + buff_hp_flat
    def_percent_total = 1.0 + def_percent + relic_def_percent
    def_ = def_base * def_percent_total + def_bonus + relic_def_flat
    crit_rate_raw = crit_rate + buff_crit_rate
    crit_rate_capped = max(0.0, min(1.0, crit_rate_raw))
    crit_dmg_total = crit_dmg + buff_crit_dmg
    break_dmg_total = break_dmg + buff_break_dmg

    return {
        "atk": atk,
        "atk_base": atk_base,
        "atk_bonus": atk_bonus,
        "atk_percent": atk_percent,
        "relic_atk_flat": relic_atk_flat,
        "relic_atk_percent": relic_atk_percent,
        "buff_atk_flat": buff_atk_flat,
        "buff_atk_pct": buff_atk_pct,
        "hp": hp,
        "hp_base": hp_base,
        "hp_bonus": hp_bonus,
        "hp_percent": hp_percent,
        "relic_hp_flat": relic_hp_flat,
        "relic_hp_percent": relic_hp_percent,
        "buff_hp_flat": buff_hp_flat,
        "buff_hp_pct": buff_hp_pct,
        "def": def_,
        "def_base": def_base,
        "def_bonus": def_bonus,
        "def_percent": def_percent,
        "relic_def_flat": relic_def_flat,
        "relic_def_percent": relic_def_percent,
        "crit_rate": crit_rate_capped,
        "crit_rate_raw": crit_rate_raw,
        "crit_rate_base": crit_rate_base,
        "crit_dmg": crit_dmg_total,
        "crit_dmg_base": crit_dmg_base,
        "buff_crit_rate": buff_crit_rate,
        "buff_crit_dmg": buff_crit_dmg,
        "relic_crit_rate_by_slot": relic_crit_rate_by_slot,
        "relic_crit_dmg_by_slot": relic_crit_dmg_by_slot,
        "speed": speed,
        "dmg_bonus": dmg_bonus,
        "energy": energy,
        "break_dmg": break_dmg_total,
    }


def _extract_crit(name: str, value: float) -> tuple[float, float]:
    if name == "暴击率":
        return pct_to_ratio(value), 0.0
    if name == "暴击伤害":
        return 0.0, pct_to_ratio(value)
    return 0.0, 0.0


def apply_stat_to_totals(
    name: str,
    value: float,
    atk_base: float,
    atk_bonus: float,
    atk_percent: float,
    relic_atk_flat: float,
    relic_atk_percent: float,
    hp_base: float,
    hp_bonus: float,
    hp_percent: float,
    relic_hp_flat: float,
    relic_hp_percent: float,
    def_base: float,
    def_bonus: float,
    def_percent: float,
    relic_def_flat: float,
    relic_def_percent: float,
    crit_rate: float,
    crit_dmg: float,
    speed: float,
    dmg_bonus: float,
    energy: float,
    break_dmg: float,
) -> tuple:
    if name == "攻击力":
        relic_atk_flat += value
    elif name == "攻击力%":
        relic_atk_percent += pct_to_ratio(value)
    elif name == "生命值":
        relic_hp_flat += value
    elif name == "生命值%":
        relic_hp_percent += pct_to_ratio(value)
    elif name == "防御力":
        relic_def_flat += value
    elif name == "防御力%":
        relic_def_percent += pct_to_ratio(value)
    elif name == "暴击率":
        crit_rate += pct_to_ratio(value)
    elif name == "暴击伤害":
        crit_dmg += pct_to_ratio(value)
    elif name == "速度":
        speed += value
    elif name == "属性加伤":
        dmg_bonus += pct_to_ratio(value)
    elif name == "能量恢复":
        energy += pct_to_ratio(value)
    elif name == "击破特攻":
        break_dmg += pct_to_ratio(value)
    return (atk_base, atk_bonus, atk_percent, relic_atk_flat, relic_atk_percent,
            hp_base, hp_bonus, hp_percent, relic_hp_flat, relic_hp_percent,
            def_base, def_bonus, def_percent, relic_def_flat, relic_def_percent,
            crit_rate, crit_dmg, speed, dmg_bonus, energy, break_dmg)


def apply_conversion_formulas(totals: dict[str, float], formulas: list[dict]) -> dict[str, float]:
    """应用转模公式，将公式结果加到目标属性上。返回更新后的 totals 副本。"""
    result = dict(totals)
    for formula in formulas:
        target = formula.get("target", "")
        expr = formula.get("formula", "")
        if not target or not expr:
            continue
        value = safe_eval_formula(expr, result)
        if target == "atk_flat":
            result["atk"] = result.get("atk", 0.0) + value
        elif target == "atk_pct":
            # 攻击力%转模按基础攻击力换算为固定值
            result["atk"] = result.get("atk", 0.0) + result.get("atk_base", 0.0) * value
        elif target == "hp_flat":
            result["hp"] = result.get("hp", 0.0) + value
        elif target == "hp_pct":
            result["hp"] = result.get("hp", 0.0) + result.get("hp_base", 0.0) * value
        elif target == "def_flat":
            result["def"] = result.get("def", 0.0) + value
        elif target == "def_pct":
            result["def"] = result.get("def", 0.0) + result.get("def_base", 0.0) * value
        elif target == "spd":
            result["speed"] = result.get("speed", 0.0) + value
        elif target == "crit_rate":
            # 暴击率转模同时影响 raw 和 capped
            result["crit_rate_raw"] = result.get("crit_rate_raw", 0.0) + value
            result["crit_rate"] = max(0.0, min(1.0, result["crit_rate_raw"]))
        elif target == "crit_dmg":
            result["crit_dmg"] = result.get("crit_dmg", 0.0) + value
        elif target == "break_dmg":
            result["break_dmg"] = result.get("break_dmg", 0.0) + value
        elif target == "dmg_bonus":
            result["dmg_bonus"] = result.get("dmg_bonus", 0.0) + value
        elif target == "energy":
            result["energy"] = result.get("energy", 0.0) + value
    return result


def compute_damage_base(totals: dict[str, float], config: CharacterConfig) -> float:
    """根据角色配置的"伤害来源"计算伤害基数。"""
    src = config.damage_source
    if src == "ATK":
        return totals.get("atk", 0.0)
    if src == "HP":
        return totals.get("hp", 0.0)
    if src == "DEF":
        return totals.get("def", 0.0)
    if src == "SPD":
        return totals.get("speed", 0.0)
    if src == "BREAK":
        return totals.get("break_dmg", 0.0)
    if src == "CUSTOM":
        return safe_eval_formula(config.damage_formula, totals)
    return totals.get("atk", 0.0)


def _crit_dmg_factor(config: CharacterConfig | None, totals: dict[str, float]) -> float:
    """爆伤系数：爆伤为有效副词条时使用实际爆伤，否则按 0 计算。"""
    if config is not None and "暴击伤害" not in config.valid_subs:
        return 0.0
    return totals["crit_dmg"]


def _crit_rate_factor(config: CharacterConfig | None, totals: dict[str, float], use_raw: bool = False) -> float:
    """暴击率系数：暴击率为有效副词条时使用实际暴击率，否则按 0 计算。"""
    if config is not None and "暴击率" not in config.valid_subs:
        return 0.0
    return totals.get("crit_rate_raw", totals["crit_rate"]) if use_raw else totals["crit_rate"]


def expected_damage(totals: dict[str, float], config: CharacterConfig | None = None) -> float:
    """期望伤害。传入 config 时按角色配置的伤害来源和转模公式计算。"""
    if config is not None:
        totals = apply_conversion_formulas(totals, config.conversion_formulas)
        base = compute_damage_base(totals, config)
    else:
        base = totals.get("atk", 0.0)
    cr = _crit_rate_factor(config, totals, use_raw=False)
    cd = _crit_dmg_factor(config, totals)
    return base * (1.0 + cr * cd)


def expected_damage_theoretical(totals: dict[str, float], config: CharacterConfig | None = None) -> float:
    """理论期望伤害：使用未截断的暴击率（即假设暴击率可以超过100%）。"""
    if config is not None:
        totals = apply_conversion_formulas(totals, config.conversion_formulas)
        base = compute_damage_base(totals, config)
    else:
        base = totals.get("atk", 0.0)
    crit_rate = _crit_rate_factor(config, totals, use_raw=True)
    cd = _crit_dmg_factor(config, totals)
    return base * (1.0 + crit_rate * cd)


def expected_damage_crit_100(totals: dict[str, float], config: CharacterConfig | None = None) -> float:
    """假设暴击率为 100% 时的期望伤害（用于爆伤理论分计算）。"""
    if config is not None:
        totals = apply_conversion_formulas(totals, config.conversion_formulas)
        base = compute_damage_base(totals, config)
    else:
        base = totals.get("atk", 0.0)
    cd = _crit_dmg_factor(config, totals)
    return base * (1.0 + 1.0 * cd)


def _stat_weight(stat_name: str, config: CharacterConfig | None) -> float:
    """获取词条权重：在角色配置的有效副词条中权重为 1.0，否则 0.0。"""
    if config is None:
        return HIMEKO_QIXING_WEIGHTS.get(stat_name, 0.0)
    return 1.0 if stat_name in config.valid_subs else 0.0


def score_relic_lines(base: CharacterStats, relics: dict[str, Relic], buffs: dict[str, float], slot: str,
                      config: CharacterConfig | None = None) -> None:
    totals_with = build_totals(base, relics, buffs)
    with_all = expected_damage(totals_with, config)
    if with_all <= 0 or slot not in relics:
        return
    relic = relics[slot]
    crit_rate_raw = totals_with["crit_rate_raw"]
    is_overflow = crit_rate_raw > 1.0
    # 预计算理论伤害（暴击率=100%场景）
    with_crit_100 = expected_damage_crit_100(totals_with, config)
    for idx, stat in enumerate(relic.subs):
        weight = _stat_weight(stat.name, config)
        stat.weight = weight
        if weight <= 0:
            stat.score = 0.0
            stat.theoretical_score = 0.0
            continue
        reduced = copy.deepcopy(relics)
        reduced[slot] = copy.deepcopy(relic)
        reduced[slot].subs.pop(idx)
        reduced_totals = build_totals(base, reduced, buffs)
        without = expected_damage(reduced_totals, config)
        stat.score = max(0.0, (with_all - without) / with_all * 100.0 * weight)
        # 理论分：暴击率溢出时暴击率词条用未截断暴击率；爆伤词条用暴击率=100%
        if stat.name == "暴击率" and is_overflow:
            without_theoretical = expected_damage_theoretical(reduced_totals, config)
            with_theoretical = expected_damage_theoretical(totals_with, config)
            stat.theoretical_score = max(0.0, (with_theoretical - without_theoretical) / with_theoretical * 100.0 * weight)
        elif stat.name == "暴击伤害":
            without_100 = expected_damage_crit_100(reduced_totals, config)
            stat.theoretical_score = max(0.0, (with_crit_100 - without_100) / with_crit_100 * 100.0 * weight)
        else:
            stat.theoretical_score = stat.score
    relic.total_score = sum(stat.score for stat in relic.subs)
    relic.theoretical_total = sum(stat.theoretical_score for stat in relic.subs)


def apply_score_deltas(new_relic: Relic, old_relic: Relic | None, config: CharacterConfig | None = None) -> None:
    old_scores: dict[tuple[str, bool], list[float]] = {}
    if old_relic:
        for stat in old_relic.subs:
            old_scores.setdefault((stat.name, stat.percent), []).append(stat.score)

    matched_keys: set[tuple[str, bool]] = set()
    for stat in new_relic.subs:
        key = (stat.name, stat.percent)
        queue = old_scores.get(key) or []
        old_score = queue.pop(0) if queue else 0.0
        stat.delta = stat.score - old_score
        if old_score > 0 or queue or old_scores.get(key):
            matched_keys.add(key)
    # 找出被删除的有效词条（旧遗器有，新遗器没有或剩余的）
    removed: list[tuple[str, float]] = []
    if old_relic:
        # 重新统计旧遗器中每个词条出现的次数
        old_counts: dict[tuple[str, bool], int] = {}
        for stat in old_relic.subs:
            if _stat_weight(stat.name, config) > 0:
                old_counts[(stat.name, stat.percent)] = old_counts.get((stat.name, stat.percent), 0) + 1
        # 统计新遗器中每个词条出现的次数
        new_counts: dict[tuple[str, bool], int] = {}
        for stat in new_relic.subs:
            if _stat_weight(stat.name, config) > 0:
                new_counts[(stat.name, stat.percent)] = new_counts.get((stat.name, stat.percent), 0) + 1
        # 旧遗器中数量超过新遗器的部分，就是被删除的
        old_score_queues: dict[tuple[str, bool], list[float]] = {}
        for stat in old_relic.subs:
            if _stat_weight(stat.name, config) > 0:
                old_score_queues.setdefault((stat.name, stat.percent), []).append(stat.score)
        for key, old_cnt in old_counts.items():
            new_cnt = new_counts.get(key, 0)
            if old_cnt > new_cnt:
                removed_cnt = old_cnt - new_cnt
                queue = old_score_queues.get(key, [])
                for _ in range(removed_cnt):
                    if queue:
                        score_val = queue.pop(0)
                        removed.append((key[0], score_val))
    new_relic.removed_stats = removed
    old_total = old_relic.total_score if old_relic else 0.0
    new_relic.total_delta = new_relic.total_score - old_total


class Overlay(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.92)
        self.configure(bg="#101820")
        s = self._scale()
        self.line1_label = tk.Label(self, text="等待记录", bg="#101820", fg="#ffffff",
                                    font=scaled_font(14, s, bold=True))
        self.line1_label.pack(ipadx=18, ipady=3)
        self.line2_label = tk.Label(self, text="", bg="#101820", fg="#a0a0a0",
                                    font=scaled_font(10, s))
        self.line2_label.pack(ipadx=18, ipady=1)
        self.line3_label = tk.Label(self, text="", bg="#101820", fg="#a0a0a0",
                                    font=scaled_font(10, s))
        self.line3_label.pack(ipadx=18, ipady=1)
        self.line4_label = tk.Label(self, text="", bg="#101820", fg="#a0a0a0",
                                    font=scaled_font(10, s))
        self.line4_label.pack(ipadx=18, ipady=1)
        # 错误信息显示在最底部一行
        self.error_label = tk.Label(self, text="", bg="#101820", fg="#ff5b5b",
                                    font=scaled_font(10, s, bold=True), wraplength=600, justify="center")
        self.error_label.pack(ipadx=18, ipady=1)
        self._error_clear_job = None
        self._center_top()
        set_window_clickthrough(self)
        self.stat_windows: list[tk.Toplevel] = []
        self.debug_windows: list[tk.Toplevel] = []

    def _scale(self) -> float:
        """读取当前 overlay 缩放系数（从 App 的 overlay_scale 变量）"""
        try:
            return float(self.master.overlay_scale.get())
        except Exception:
            return 1.0

    def apply_scale(self):
        """滑条改变时调用：更新主 overlay 4行文字+错误标签的字体。"""
        s = self._scale()
        self.line1_label.configure(font=scaled_font(14, s, bold=True))
        self.line2_label.configure(font=scaled_font(10, s))
        self.line3_label.configure(font=scaled_font(10, s))
        self.line4_label.configure(font=scaled_font(10, s))
        self.error_label.configure(font=scaled_font(10, s, bold=True))
        self._center_top()

    def _text_window(self, x: int, center_y: int, text: str, bg: str, fg: str,
                     font_spec: tuple, alpha: float, window_list: list) -> tk.Toplevel:
        """创建 overlay 文本窗口，使窗口垂直中心精确对齐 center_y。
        关键：用 label.winfo_reqwidth/reqheight（label 自身请求尺寸，不含 Toplevel 边距）
        显式设置窗口 w×h，确保窗口紧凑包裹文字，不会因 Toplevel 默认尺寸导致相邻色块重叠。"""
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", alpha)
        win.configure(bg=bg)
        win.withdraw()
        label = tk.Label(win, text=text, anchor="w", bg=bg, fg=fg, font=font_spec,
                         padx=6, pady=2)
        label.pack()
        win.update_idletasks()
        label_w = label.winfo_reqwidth()
        label_h = label.winfo_reqheight()
        correct_y = max(0, int(center_y - label_h / 2))
        win.geometry(f"{label_w}x{label_h}+{x}+{correct_y}")
        win.deiconify()
        set_window_clickthrough(win)
        window_list.append(win)
        return win

    def _center_top(self):
        self.update_idletasks()
        w = self.winfo_width()
        sw = self.winfo_screenwidth()
        x = max(0, (sw - w) // 2)
        self.geometry(f"+{x}+0")

    def set_damage(self, text: str):
        self.line1_label.configure(text=text)
        self._center_top()

    def set_panel(self, line2: str = "", line3: str = "", line4: str = ""):
        self.line2_label.configure(text=line2)
        self.line3_label.configure(text=line3)
        self.line4_label.configure(text=line4)
        self._center_top()

    def show_error(self, msg: str, auto_clear_ms: int = 6000):
        """在 overlay 底部显示错误信息，超时后自动清除。"""
        self.error_label.configure(text=msg)
        self._center_top()
        if self._error_clear_job is not None:
            try:
                self.after_cancel(self._error_clear_job)
            except Exception:
                pass
        self._error_clear_job = self.after(auto_clear_ms, self.clear_error)

    def clear_error(self):
        self.error_label.configure(text="")
        if self._error_clear_job is not None:
            try:
                self.after_cancel(self._error_clear_job)
            except Exception:
                pass
            self._error_clear_job = None
        self._center_top()

    def _clear_windows(self, wins: list[tk.Toplevel]):
        for win in wins:
            win.destroy()
        wins.clear()

    def set_stat_scores(self, region: tuple[int, int, int, int], relic: Relic):
        self._clear_windows(self.stat_windows)
        x, y, _, _ = region
        sx = max(0, x - 180)
        # 词条名位置（与调试模式词条名同列，但无视调试开关）
        nx = max(0, x - 380)
        if relic.subs and relic.subs[0].box:
            first_sub_center = int(y + box_mid_y(relic.subs[0].box))
        else:
            first_sub_center = y + 170
        # 总分中线在第一条副词条中线上方 120px
        total_center = max(0, first_sub_center - 120)
        if relic.total_score > 0 or relic.total_delta != 0:
            # "遗器总分：" 标签（白字，无视调试模式），左边与调试词条名同列对齐，与总分同中线
            self._static_label_window(nx, total_center, "遗器总分：")
            # 总分字体放大
            self._score_window(sx, total_center, format_score_delta(relic.total_score, relic.total_delta),
                               relic.total_delta, font_size=18)
            # 理论总分另起一行（格式化后与实际分相同时隐藏）
            if relic.theoretical_total > 0 and round(relic.theoretical_total, 1) != round(relic.total_score, 1):
                self._score_window(sx, total_center + 48, f"({relic.theoretical_total:.1f})", 0.0, font_size=14)
        for idx, stat in enumerate(relic.subs):
            if stat.box:
                stat_center = int(y + box_mid_y(stat.box))
            else:
                stat_center = y + 170 + idx * 42
            if stat.weight > 0 or stat.delta != 0:
                stat_text = format_score_delta(stat.score, stat.delta)
                # 副词条理论值用括号显示在同一行（格式化后与实际分相同时隐藏）
                width = 8
                if stat.theoretical_score > 0 and round(stat.theoretical_score, 1) != round(stat.score, 1):
                    stat_text += f" ({stat.theoretical_score:.1f})"
                    width = 12
                self._score_window(sx, stat_center, stat_text, stat.delta, width=width)
        # 已删除词条：词条名（红字，无视调试模式）+ 差值（红字 -score），接在最后一条副词条下方
        if relic.removed_stats:
            if relic.subs and relic.subs[-1].box:
                last_center = int(y + box_mid_y(relic.subs[-1].box))
            elif relic.subs:
                last_center = y + 170 + (len(relic.subs) - 1) * 42
            else:
                last_center = y + 170
            start_center = last_center + 42
            for i, (name, score) in enumerate(relic.removed_stats):
                line_center = start_center + i * 42
                # 词条名和差值共享同一个中线，_text_window 按各自字号自动居中
                self._removed_name_window(nx, line_center, name)
                delta_text = f"{abs(score):.0f}" if abs(score) >= 10 else f"{abs(score):.1f}"
                self._score_window(sx, line_center, f"-{delta_text}", -score)

    def set_debug_overlay(self, region: tuple[int, int, int, int] | None, relic: Relic | None, stats: CharacterStats | None = None):
        """调试模式：在分数窗口更左侧显示 OCR 解析出的词条名:词条值"""
        self._clear_windows(self.debug_windows)
        if stats:
            x, y = 520, 100
            dx = x - 380
            # 基础值面板以 center_y 定位，每行中线间隔 24px
            self._debug_window(dx, y + 12, f"基础值识别结果:")
            self._debug_window(dx, y + 36, f"生命值: {stats.hp_base:.0f} +{stats.hp_bonus:.0f}")
            self._debug_window(dx, y + 60, f"攻击力: {stats.atk_base:.0f} +{stats.atk_bonus:.0f}")
            self._debug_window(dx, y + 84, f"防御力: {stats.def_base:.0f} +{stats.def_bonus:.0f}")
            self._debug_window(dx, y + 108, f"速度: {stats.speed:.0f}")
            self._debug_window(dx, y + 132, f"暴击率: {stats.crit_rate*100:.1f}%")
            self._debug_window(dx, y + 156, f"暴击伤害: {stats.crit_dmg*100:.1f}%")
            return
        if not region or not relic:
            return
        x, y, _, _ = region
        dx = max(0, x - 380)
        # 主词条中线对齐 OCR box 中线
        if relic.main_box:
            main_center = int(y + box_mid_y(relic.main_box))
        else:
            main_center = max(0, y + 20)
        main_text = f"{relic.main_name or '?'}: {relic.main_value:g}"
        self._debug_window(dx, main_center, main_text)
        for idx, stat in enumerate(relic.subs):
            if stat.box:
                stat_center = int(y + box_mid_y(stat.box))
            else:
                stat_center = y + 170 + idx * 42
            unit = "%" if stat.percent else ""
            self._debug_window(dx, stat_center, f"{stat.name}: {stat.value:g}{unit}")

    def _removed_name_window(self, x: int, center_y: int, name: str):
        self._text_window(x, center_y, name, "#101820", "#ff5b5b",
                          scaled_font(12, self._scale(), bold=True), 0.9, self.stat_windows)

    def _static_label_window(self, x: int, center_y: int, text: str, font_size: int = 18):
        """固定文本标签（白字），无视调试模式，与总分同字号对齐。"""
        self._text_window(x, center_y, text, "#101820", "#ffffff",
                          scaled_font(font_size, self._scale(), bold=True), 0.9, self.stat_windows)

    def clear_debug(self):
        self._clear_windows(self.debug_windows)

    def _score_window(self, x: int, center_y: int, text: str, delta: float,
                      width: int = 8, font_size: int = 14):
        color = "#65f2a5" if delta >= 0 else "#ff5b5b"
        self._text_window(x, center_y, text, "#101820", color,
                          scaled_font(font_size, self._scale(), bold=True), 0.9, self.stat_windows)

    def _debug_window(self, x: int, center_y: int, text: str):
        self._text_window(x, center_y, text, "#1a1a2e", "#ffcc66",
                          scaled_font(12, self._scale()), 0.85, self.debug_windows)


class CollapsibleFrame(ttk.Frame):
    """可折叠面板：点击标题栏展开/收起内容。"""
    def __init__(self, master, title: str = "", start_expanded: bool = True, **kwargs):
        super().__init__(master, **kwargs)
        self._expanded = start_expanded
        self._title_var = tk.StringVar(value=("▼ " if start_expanded else "▶ ") + title)
        self._toggle_btn = ttk.Button(self, textvariable=self._title_var, style="Toggle.TButton",
                                       command=self._toggle)
        self._toggle_btn.pack(fill="x")
        self._content_frame = ttk.Frame(self)
        if start_expanded:
            self._content_frame.pack(fill="x", padx=2, pady=(2, 0))

    @property
    def content(self) -> ttk.Frame:
        return self._content_frame

    def _toggle(self):
        if self._expanded:
            self._content_frame.pack_forget()
            self._expanded = False
        else:
            self._content_frame.pack(fill="x", padx=2, pady=(2, 0))
            self._expanded = True
        title = self._title_var.get()
        prefix = "▼ " if self._expanded else "▶ "
        self._title_var.set(prefix + title[2:])

    def set_title(self, title: str):
        prefix = "▼ " if self._expanded else "▶ "
        self._title_var.set(prefix + title)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        if IMPORT_ERROR is not None:
            messagebox.showerror("缺少依赖", f"{IMPORT_ERROR}\n\n请在项目目录运行：pip install -r requirements.txt")
            raise SystemExit(1)
        self.title("崩铁遗器实时计分")
        self.geometry("700x780")
        self.minsize(600, 600)
        self.resizable(True, True)
        self._setup_style()
        self.ocr = UmiOcrClient()
        # 角色配置：按角色名保存一份完整配置
        self.character_configs: dict[str, CharacterConfig] = {}
        self.current_character: str = "姬子"
        # 默认创建姬子
        self.character_configs["姬子"] = CharacterConfig.default_himeko()
        # 以下是当前角色的"展开视图"——实际持久化存储在 self.character_configs[self.current_character] 中
        self.base_stats = CharacterStats()
        self.relics: dict[str, Relic] = {}
        self.undo_stack: list[dict[str, Relic]] = []
        self.last_signature_by_slot: dict[str, str] = {}
        self.current_relic_region = (0, 0, 560, 390)
        self.ally_buff_vars = {field: tk.StringVar(value="0") for field in BUFF_FIELDS}
        self.self_buff_vars = {field: tk.StringVar(value="0") for field in BUFF_FIELDS}
        self.debug_mode = tk.BooleanVar(value=True)
        # overlay 字体缩放系数（1.0 = 100%）。独立于 Windows DPI 缩放，
        # 用于适配 2K/150% 等高分辨率环境，让 overlay 字体在游戏画面上比例合适。
        self.overlay_scale = tk.DoubleVar(value=1.0)
        # 角色配置相关的 UI 变量
        self.character_name_var = tk.StringVar(value="姬子")
        self.valid_sub_vars: dict[str, tk.BooleanVar] = {name: tk.BooleanVar(value=False) for name in ALL_SUB_STATS}
        self.damage_source_var = tk.StringVar(value="ATK")
        self.damage_formula_var = tk.StringVar(value="ATK")
        self.conversion_formulas: list[dict] = []
        self._loading = False  # 加载配置时禁用 trace 回调，避免重复重算
        self.overlay = Overlay(self)
        self.sampling_target: tk.StringVar | None = None
        self.sampling_first_point: tuple[int, int] | None = None
        self.preview_photo = None
        self.mouse_ctl = mouse.Controller()
        self._build_ui()
        self.load_config()
        self.bind_config_traces()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._start_keyboard_listener()

    def _setup_style(self):
        style = ttk.Style(self)
        # 使用系统默认主题（Windows 上为 vista/xpnative，呈现 Win11 原生外观）
        style.configure("TLabelframe", padding=6)
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", padding=4)
        style.configure("TCheckbutton", padding=1)
        style.configure("TRadiobutton", padding=1)
        style.configure("TEntry", padding=2)
        style.configure("Toggle.TButton", anchor="w", padding=(6, 4),
                         font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self):
        main = ttk.Frame(self, padding=6)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)

        # ========== 第1页：主操作面板 ==========
        page1 = ttk.Frame(notebook, padding=6)
        page1.columnconfigure(0, weight=1)
        page1.rowconfigure(2, weight=1)
        notebook.add(page1, text="主面板")

        # --- 角色配置 ---
        char_frame = ttk.LabelFrame(page1, text="角色配置")
        char_frame.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        char_frame.columnconfigure(1, weight=1)

        # 角色选择行
        ttk.Label(char_frame, text="当前角色：").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        self.character_combo = ttk.Combobox(char_frame, textvariable=self.character_name_var, width=12, state="readonly")
        self.character_combo.grid(row=0, column=1, sticky="w", padx=4)
        self.character_combo["values"] = list(self.character_configs.keys())
        self.character_combo.bind("<<ComboboxSelected>>", lambda e: self._on_character_switch())
        btn_row = ttk.Frame(char_frame)
        btn_row.grid(row=0, column=2, sticky="e", padx=4)
        ttk.Button(btn_row, text="新增", command=self._add_character, width=5).pack(side="left", padx=1)
        ttk.Button(btn_row, text="删除", command=self._delete_character, width=5).pack(side="left", padx=1)
        ttk.Button(btn_row, text="改名", command=self._rename_character, width=5).pack(side="left", padx=1)

        # 有效副词条
        subs_frame = ttk.Frame(char_frame)
        subs_frame.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        ttk.Label(subs_frame, text="有效副词条：").pack(anchor="w")
        sub_row1 = ttk.Frame(subs_frame); sub_row1.pack(fill="x")
        sub_row2 = ttk.Frame(subs_frame); sub_row2.pack(fill="x")
        for name in ["生命值", "生命值%", "攻击力", "攻击力%", "防御力", "防御力%", "速度"]:
            ttk.Checkbutton(sub_row1, text=name, variable=self.valid_sub_vars[name],
                            command=self._on_valid_subs_change).pack(side="left", padx=(0,6))
        for name in ["暴击率", "暴击伤害", "击破特攻", "效果命中", "效果抵抗", "属性加伤"]:
            ttk.Checkbutton(sub_row2, text=name, variable=self.valid_sub_vars[name],
                            command=self._on_valid_subs_change).pack(side="left", padx=(0,6))

        # 伤害来源
        dmg_frame = ttk.Frame(char_frame)
        dmg_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=2)
        ttk.Label(dmg_frame, text="伤害来源：").pack(side="left")
        dmg_combo = ttk.Combobox(dmg_frame, textvariable=self.damage_source_var, width=8, state="readonly",
                                  values=list(DAMAGE_SOURCES.keys()))
        dmg_combo.pack(side="left", padx=(2,4))
        self._dmg_source_label = ttk.Label(dmg_frame, text="攻击力", width=6, anchor="w")
        self._dmg_source_label.pack(side="left")
        dmg_combo.bind("<<ComboboxSelected>>", lambda e: self._on_damage_source_change())
        ttk.Label(dmg_frame, text="公式：").pack(side="left", padx=(8,0))
        ttk.Entry(dmg_frame, textvariable=self.damage_formula_var, width=16).pack(side="left", padx=2, fill="x", expand=True)
        ttk.Button(dmg_frame, text="应用", command=self._on_damage_formula_change, width=5).pack(side="left", padx=(2,0))

        # 转模公式
        conv_frame = ttk.Frame(char_frame)
        conv_frame.grid(row=3, column=0, columnspan=3, sticky="ew", padx=4, pady=(4,2))
        ttk.Label(conv_frame, text="转模公式：").pack(anchor="w")
        self.conv_listbox = tk.Listbox(conv_frame, height=3, font=("Consolas", 9))
        self.conv_listbox.pack(fill="x", pady=2)
        conv_btn = ttk.Frame(conv_frame); conv_btn.pack(fill="x")
        ttk.Button(conv_btn, text="添加", command=lambda: self._edit_conversion(None), width=5).pack(side="left", padx=(0,2))
        ttk.Button(conv_btn, text="编辑", command=lambda: self._edit_conversion_from_selection(), width=5).pack(side="left", padx=2)
        ttk.Button(conv_btn, text="删除", command=self._delete_conversion, width=5).pack(side="left", padx=2)
        self._conv_preview_label = ttk.Label(conv_frame, text="", foreground="#666666", font=("Microsoft YaHei UI", 8))
        self._conv_preview_label.pack(fill="x", pady=(2,0))

        # --- 拐力区（左右并排，各自可折叠）---
        buffs_row = ttk.Frame(page1)
        buffs_row.grid(row=1, column=0, sticky="ew", pady=2)
        buffs_row.columnconfigure(0, weight=1)
        buffs_row.columnconfigure(1, weight=1)

        self._ally_collapse = CollapsibleFrame(buffs_row, "队友拐力（支持 100+200 格式）", start_expanded=True)
        self._ally_collapse.grid(row=0, column=0, sticky="nsew", padx=(0,3))
        ally_inner = self._ally_collapse.content
        ally_inner.columnconfigure(1, weight=1)
        self._buff_grid(ally_inner, self.ally_buff_vars, compact=True)

        self._self_collapse = CollapsibleFrame(buffs_row, "自拐（角色自身提供的拐力）", start_expanded=True)
        self._self_collapse.grid(row=0, column=1, sticky="nsew", padx=(3,0))
        self_inner = self._self_collapse.content
        self_inner.columnconfigure(1, weight=1)
        self._buff_grid(self_inner, self.self_buff_vars, compact=True)

        # 占据中间空间的弹性占位
        spacer = ttk.Frame(page1)
        spacer.grid(row=2, column=0, sticky="nsew")

        # --- 操作按钮（底部，三个大按钮横排）---
        action_frame = ttk.Frame(page1)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(4,2))
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)
        action_frame.columnconfigure(2, weight=1)
        ttk.Button(action_frame, text="记录基础值 (1)", command=self.capture_base_async).grid(
            row=0, column=0, sticky="ew", padx=2, ipady=6)
        ttk.Button(action_frame, text="记录/替换遗器 (空格)", command=self.capture_relic_async).grid(
            row=0, column=1, sticky="ew", padx=2, ipady=6)
        ttk.Button(action_frame, text="撤回 (退格)", command=self.undo).grid(
            row=0, column=2, sticky="ew", padx=2, ipady=6)

        # 状态提示
        admin_note = "非管理员；若游戏以管理员运行，热键可能无效。" if not is_admin() else ""
        self.status = tk.StringVar(value=f"先填区域和拐力；按 1 记录基础值，按空格记录遗器。{admin_note}")
        ttk.Label(page1, textvariable=self.status, wraplength=650, font=("Microsoft YaHei UI", 8),
                  foreground="#555555").grid(row=4, column=0, sticky="ew", pady=(2,0))

        # ========== 第2页：设置 & 预览 ==========
        page2 = ttk.Frame(notebook, padding=6)
        page2.columnconfigure(0, weight=1)
        page2.rowconfigure(2, weight=1)
        page2.rowconfigure(3, weight=1)
        notebook.add(page2, text="设置 / 预览")

        # 截图区域 & 调试
        region_frame = ttk.LabelFrame(page2, text="截图区域")
        region_frame.grid(row=0, column=0, sticky="ew", pady=(0,4))
        region_frame.columnconfigure(1, weight=1)
        self.detail_region = tk.StringVar(value=DEFAULT_DETAIL_REGION)
        self.relic_region = tk.StringVar(value=DEFAULT_RELIC_REGION)
        self._region_row(region_frame, "脱装备详情", self.detail_region, 0)
        self._region_row(region_frame, "遗器详情", self.relic_region, 1)
        dbg_row = ttk.Frame(region_frame)
        dbg_row.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(2,2))
        ttk.Label(dbg_row, text="调试：").pack(side="left")
        ttk.Radiobutton(dbg_row, text="关", variable=self.debug_mode, value=False,
                        command=self._on_debug_toggle).pack(side="left", padx=2)
        ttk.Radiobutton(dbg_row, text="开", variable=self.debug_mode, value=True,
                        command=self._on_debug_toggle).pack(side="left", padx=2)

        # Overlay 字体缩放滑条（独立于 Windows DPI 缩放）
        scale_row = ttk.Frame(region_frame)
        scale_row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=4, pady=(2,2))
        ttk.Label(scale_row, text="Overlay缩放：").pack(side="left")
        self.scale_label = ttk.Label(scale_row, text="100%", width=6)
        self.scale_label.pack(side="left", padx=2)
        self.scale_slider = ttk.Scale(scale_row, from_=0.5, to=2.0,
                                      variable=self.overlay_scale, command=self._on_scale_change)
        self.scale_slider.pack(side="left", fill="x", expand=True, padx=4)
        # 初始化显示
        self._update_scale_label()

        # 测试 & 预览按钮
        test_btns = ttk.Frame(page2)
        test_btns.grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(test_btns, text="测试 Umi-OCR", command=self.test_ocr, width=14).pack(side="left", padx=(0,3))
        ttk.Button(test_btns, text="截图详情预览", command=lambda: self.preview_region_async(self.detail_region), width=14).pack(side="left", padx=3)
        ttk.Button(test_btns, text="截图遗器预览", command=lambda: self.preview_region_async(self.relic_region), width=14).pack(side="left", padx=3)

        # 截图预览
        preview_frame = ttk.LabelFrame(page2, text="最近一次截图")
        preview_frame.grid(row=2, column=0, sticky="nsew", pady=4)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(preview_frame, text="点击截图预览后显示", anchor="center")
        self.preview_label.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._preview_original_image: Image.Image | None = None
        self._preview_region_text: str = ""
        self.preview_label.bind("<Configure>", lambda e: self._redraw_preview())

        # 日志
        log_frame = ttk.LabelFrame(page2, text="日志")
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(log_frame, height=6, font=("Consolas", 9), relief="flat",
                            bg="#f8f8f8", bd=0, highlightthickness=0)
        self.text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.log("热键：1=记录基础值；空格=记录/替换遗器；退格=撤回。")

    def _buff_grid(self, parent, vars_dict: dict[str, tk.StringVar], compact: bool = False):
        """compact=True 时为纵向单列布局（左右分栏使用），否则为三列布局。"""
        if compact:
            for idx, field in enumerate(BUFF_FIELDS):
                ttk.Label(parent, text=BUFF_LABELS[field]).grid(row=idx, column=0, sticky="w", padx=2, pady=1)
                ttk.Entry(parent, textvariable=vars_dict[field]).grid(row=idx, column=1, sticky="ew", padx=(2,4), pady=1)
        else:
            for idx, field in enumerate(BUFF_FIELDS):
                row = idx // 3
                col = (idx % 3) * 2
                ttk.Label(parent, text=BUFF_LABELS[field]).grid(row=row, column=col, sticky="w", padx=(2,2), pady=2)
                ttk.Entry(parent, textvariable=vars_dict[field], width=10).grid(row=row, column=col + 1, sticky="ew", padx=(0,8), pady=2)

    def _on_debug_toggle(self):
        if not self.debug_mode.get():
            self.overlay.clear_debug()
        self.save_config()

    def _update_scale_label(self):
        """更新缩放百分比文本显示"""
        pct = int(round(self.overlay_scale.get() * 100))
        self.scale_label.configure(text=f"{pct}%")

    def _on_scale_change(self, *_):
        """滑条拖动时实时更新 overlay 字体并保存配置"""
        self._update_scale_label()
        # 实时更新主 overlay 的4行文字字体
        self.overlay.apply_scale()
        # stat_windows / debug_windows 是临时窗口，下次扫描时会按新缩放重新创建，
        # 这里无需重建，避免拖动时频繁闪烁。
        self.save_config()

    def _region_row(self, parent, label, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(parent, text="采样", command=lambda: self.begin_sampling(var), width=5).grid(row=row, column=2, padx=4, pady=2)

    def bind_config_traces(self):
        for var in list(self.ally_buff_vars.values()) + list(self.self_buff_vars.values()):
            var.trace_add("write", lambda *_: self._on_buff_change())
        for var in (self.detail_region, self.relic_region):
            var.trace_add("write", lambda *_: self.save_config())
        self.debug_mode.trace_add("write", lambda *_: self.save_config())
        self.damage_formula_var.trace_add("write", lambda *_: self._on_damage_formula_change())
        self.damage_source_var.trace_add("write", lambda *_: self._on_damage_source_change())

    # ===== 角色管理 =====
    def _sync_ui_to_current_config(self):
        """把当前 UI 状态同步到 self.character_configs[self.current_character]。"""
        if self.current_character not in self.character_configs:
            return
        cfg = self.character_configs[self.current_character]
        cfg.valid_subs = [name for name in ALL_SUB_STATS if self.valid_sub_vars[name].get()]
        cfg.conversion_formulas = list(self.conversion_formulas)
        cfg.damage_source = self.damage_source_var.get()
        cfg.damage_formula = self.damage_formula_var.get()
        cfg.ally_buffs = {field: self.ally_buff_vars[field].get() for field in BUFF_FIELDS}
        cfg.self_buffs = {field: self.self_buff_vars[field].get() for field in BUFF_FIELDS}
        cfg.base_stats = self.base_stats
        cfg.relics = self.relics

    def _load_config_to_ui(self):
        """从 self.character_configs[self.current_character] 加载到 UI。"""
        cfg = self.character_configs.get(self.current_character)
        if not cfg:
            return
        self._loading = True
        try:
            # 有效副词条
            for name in ALL_SUB_STATS:
                self.valid_sub_vars[name].set(name in cfg.valid_subs)
            # 伤害来源
            self.damage_source_var.set(cfg.damage_source)
            self.damage_formula_var.set(cfg.damage_formula)
            self._dmg_source_label.configure(text=DAMAGE_SOURCES.get(cfg.damage_source, ""))
            # 转模公式
            self.conversion_formulas = [dict(f) for f in cfg.conversion_formulas]
            self._refresh_conversion_listbox()
            # 拐力
            for field in BUFF_FIELDS:
                self.ally_buff_vars[field].set(str(cfg.ally_buffs.get(field, "0")))
                self.self_buff_vars[field].set(str(cfg.self_buffs.get(field, "0")))
            # 基础值和遗器
            self.base_stats = cfg.base_stats
            self.relics = cfg.relics
            self.last_signature_by_slot = {slot: relic.signature() for slot, relic in self.relics.items()}
            self.undo_stack.clear()
            # 更新角色下拉框
            self.character_combo["values"] = list(self.character_configs.keys())
            self.character_name_var.set(self.current_character)
        finally:
            self._loading = False
        # 重算分数并刷新显示
        self.recalculate_all_relic_scores()
        self.refresh_damage()

    def _on_character_switch(self):
        new_name = self.character_name_var.get()
        if new_name == self.current_character:
            return
        if new_name not in self.character_configs:
            return
        # 保存当前角色
        self._sync_ui_to_current_config()
        # 切换到新角色
        self.current_character = new_name
        self._load_config_to_ui()
        # 清除 overlay
        self.overlay._clear_windows(self.overlay.stat_windows)
        self.overlay.clear_debug()
        self.save_config()
        self.log(f"已切换到角色：{new_name}")

    def _add_character(self):
        name = simpledialog.askstring("新增角色", "请输入角色名称：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.character_configs:
            messagebox.showwarning("提示", f"角色 '{name}' 已存在", parent=self)
            return
        # 保存当前角色，再创建新角色
        self._sync_ui_to_current_config()
        self.character_configs[name] = CharacterConfig(name=name)
        self.current_character = name
        self._load_config_to_ui()
        self.save_config()
        self.log(f"已新增角色：{name}")

    def _delete_character(self):
        if len(self.character_configs) <= 1:
            messagebox.showwarning("提示", "至少保留一个角色", parent=self)
            return
        name = self.current_character
        if not messagebox.askyesno("确认", f"确认删除角色 '{name}' 及其所有遗器配置？", parent=self):
            return
        del self.character_configs[name]
        self.current_character = next(iter(self.character_configs))
        self._load_config_to_ui()
        self.save_config()
        self.log(f"已删除角色：{name}")

    def _rename_character(self):
        old_name = self.current_character
        new_name = simpledialog.askstring("重命名角色", "请输入新名称：", initialvalue=old_name, parent=self)
        if not new_name or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        if new_name in self.character_configs:
            messagebox.showwarning("提示", f"角色 '{new_name}' 已存在", parent=self)
            return
        self._sync_ui_to_current_config()
        cfg = self.character_configs.pop(old_name)
        cfg.name = new_name
        self.character_configs[new_name] = cfg
        self.current_character = new_name
        self.character_combo["values"] = list(self.character_configs.keys())
        self.character_name_var.set(new_name)
        self.save_config()
        self.log(f"角色已重命名：{old_name} -> {new_name}")

    # ===== 有效副词条 =====
    def _on_valid_subs_change(self):
        if self._loading:
            return
        if self.current_character not in self.character_configs:
            return
        cfg = self.character_configs[self.current_character]
        cfg.valid_subs = [name for name in ALL_SUB_STATS if self.valid_sub_vars[name].get()]
        self.recalculate_all_relic_scores()
        self.refresh_damage()
        if self.current_relic_region and self.relics:
            latest_slot = None
            for slot in self.relics:
                if self.relics[slot].total_score > 0:
                    latest_slot = slot
                    break
            if latest_slot:
                self.overlay.set_stat_scores(self.current_relic_region, self.relics[latest_slot])
        self.save_config()

    # ===== 伤害来源 =====
    def _on_damage_source_change(self):
        if self._loading:
            return
        src = self.damage_source_var.get()
        self._dmg_source_label.configure(text=DAMAGE_SOURCES.get(src, ""))
        if self.current_character in self.character_configs:
            self.character_configs[self.current_character].damage_source = src
            self.recalculate_all_relic_scores()
            self.refresh_damage()
            self.save_config()

    def _on_damage_formula_change(self):
        if self._loading:
            return
        if self.current_character in self.character_configs:
            self.character_configs[self.current_character].damage_formula = self.damage_formula_var.get()
            self.recalculate_all_relic_scores()
            self.refresh_damage()
            self.save_config()

    # ===== 转模公式 =====
    def _refresh_conversion_listbox(self):
        self.conv_listbox.delete(0, tk.END)
        for f in self.conversion_formulas:
            target_text = CONVERSION_TARGETS.get(f.get("target", ""), "?")
            formula = f.get("formula", "")
            self.conv_listbox.insert(tk.END, f"{target_text} <- {formula}")
        # 单行预览
        if self.conversion_formulas:
            preview_parts = []
            for f in self.conversion_formulas:
                target_text = CONVERSION_TARGETS.get(f.get("target", ""), "?")
                formula = f.get("formula", "")
                preview_parts.append(f"{target_text} += {formula}")
            self._conv_preview_label.configure(text="  |  ".join(preview_parts))
        else:
            self._conv_preview_label.configure(text="")

    def _edit_conversion_from_selection(self):
        sel = self.conv_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选择一条公式", parent=self)
            return
        idx = sel[0]
        if idx >= len(self.conversion_formulas):
            return
        self._edit_conversion(self.conversion_formulas[idx], idx)

    def _edit_conversion(self, existing: dict | None, index: int | None = None):
        """打开公式编辑对话框。existing=None 表示新增。"""
        dialog = tk.Toplevel(self)
        dialog.title("编辑转模公式" if existing else "添加转模公式")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("560x460")

        # 顶部：目标属性下拉
        top = ttk.Frame(dialog, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="目标属性：").pack(side="left")
        target_var = tk.StringVar(value=existing.get("target", "crit_dmg") if existing else "crit_dmg")
        target_combo = ttk.Combobox(top, textvariable=target_var, width=20, state="readonly",
                                     values=list(CONVERSION_TARGETS.keys()))
        target_combo.pack(side="left", padx=4)
        target_label = ttk.Label(top, text=CONVERSION_TARGETS.get(target_var.get(), ""))
        target_label.pack(side="left", padx=4)
        def _on_target_change(e=None):
            target_label.configure(text=CONVERSION_TARGETS.get(target_var.get(), ""))
        target_combo.bind("<<ComboboxSelected>>", _on_target_change)

        # 公式输入区
        mid = ttk.Frame(dialog, padding=8)
        mid.pack(fill="both", expand=True)
        ttk.Label(mid, text="公式（Python 表达式）：").pack(anchor="w")
        formula_text = tk.Text(mid, height=14, font=("Consolas", 10))
        formula_text.pack(fill="both", expand=True, pady=4)
        # 默认内容：注释 + 原公式
        default_content = "# " + FORMULA_AVAILABLE_VARS.replace("\n", "\n# ") + "\n"
        if existing:
            default_content += existing.get("formula", "")
        else:
            default_content += "ATK * 0.001"
        formula_text.insert("1.0", default_content)

        # 预览行
        preview_var = tk.StringVar(value="")
        ttk.Label(mid, textvariable=preview_var, foreground="#666666").pack(anchor="w")

        def _update_preview(*_):
            # 提取最后一行非注释作为公式
            content = formula_text.get("1.0", "end-1c")
            lines = [ln.strip() for ln in content.split("\n") if ln.strip() and not ln.strip().startswith("#")]
            formula = lines[-1] if lines else ""
            tgt_text = CONVERSION_TARGETS.get(target_var.get(), "?")
            preview_var.set(f"预览：{tgt_text} += {formula}")

        formula_text.bind("<KeyRelease>", _update_preview)
        target_combo.bind("<<ComboboxSelected>>", lambda e: _update_preview())
        _update_preview()

        # 底部按钮
        bot = ttk.Frame(dialog, padding=8)
        bot.pack(fill="x")

        def _on_ok():
            content = formula_text.get("1.0", "end-1c")
            lines = [ln.strip() for ln in content.split("\n") if ln.strip() and not ln.strip().startswith("#")]
            formula = lines[-1] if lines else ""
            if not formula:
                messagebox.showwarning("提示", "请输入公式", parent=dialog)
                return
            # 验证公式可解析
            try:
                test_totals = {"atk": 1000, "atk_base": 800, "hp": 5000, "hp_base": 4000,
                               "def": 500, "def_base": 400, "speed": 120,
                               "crit_rate": 0.5, "crit_rate_raw": 0.5, "crit_dmg": 1.0,
                               "dmg_bonus": 0.0, "energy": 0.0, "break_dmg": 0.0}
                _ = safe_eval_formula(formula, test_totals)
            except Exception as exc:
                if not messagebox.askyesno("公式校验", f"公式测试失败：{exc}\n仍要保存？", parent=dialog):
                    return
            new_entry = {"target": target_var.get(), "formula": formula}
            if existing is not None and index is not None:
                self.conversion_formulas[index] = new_entry
            else:
                self.conversion_formulas.append(new_entry)
            # 同步到当前角色
            if self.current_character in self.character_configs:
                self.character_configs[self.current_character].conversion_formulas = list(self.conversion_formulas)
            self._refresh_conversion_listbox()
            self.recalculate_all_relic_scores()
            self.refresh_damage()
            self.save_config()
            dialog.destroy()

        ttk.Button(bot, text="确定", command=_on_ok).pack(side="right", padx=4)
        ttk.Button(bot, text="取消", command=dialog.destroy).pack(side="right", padx=4)

    def _delete_conversion(self):
        sel = self.conv_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选择一条公式", parent=self)
            return
        idx = sel[0]
        if idx < len(self.conversion_formulas):
            del self.conversion_formulas[idx]
            if self.current_character in self.character_configs:
                self.character_configs[self.current_character].conversion_formulas = list(self.conversion_formulas)
            self._refresh_conversion_listbox()
            self.recalculate_all_relic_scores()
            self.refresh_damage()
            self.save_config()

    def _on_buff_change(self):
        if self._loading:
            return
        self.recalculate_all_relic_scores()
        self.refresh_damage()
        if self.current_relic_region and self.relics:
            latest_slot = None
            for slot in self.relics:
                if self.relics[slot].total_score > 0:
                    latest_slot = slot
                    break
            if latest_slot:
                self.overlay.set_stat_scores(self.current_relic_region, self.relics[latest_slot])
                if self.debug_mode.get():
                    self.overlay.set_debug_overlay(self.current_relic_region, self.relics[latest_slot])
        # 实时同步拐力到当前角色配置
        if self.current_character in self.character_configs:
            cfg = self.character_configs[self.current_character]
            cfg.ally_buffs = {field: self.ally_buff_vars[field].get() for field in BUFF_FIELDS}
            cfg.self_buffs = {field: self.self_buff_vars[field].get() for field in BUFF_FIELDS}
        self.save_config()

    def load_config(self):
        if not CONFIG_PATH.exists():
            # 首次启动：加载默认姬子到 UI
            self._load_config_to_ui()
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 全局配置
            regions = data.get("regions", {})
            self.detail_region.set(regions.get("detail", DEFAULT_DETAIL_REGION))
            self.relic_region.set(regions.get("relic", DEFAULT_RELIC_REGION))
            self.debug_mode.set(bool(data.get("debug_mode", False)))
            # overlay 字体缩放系数
            self.overlay_scale.set(float(data.get("overlay_scale", 1.0)))

            # 角色配置
            characters_data = data.get("characters", {})
            if characters_data:
                self.character_configs = {
                    name: CharacterConfig.from_dict(cdata)
                    for name, cdata in characters_data.items()
                }
            else:
                # 兼容旧版单角色配置
                legacy_cfg = CharacterConfig(name="姬子")
                legacy_buffs = data.get("buffs", {})
                ally_buffs = data.get("ally_buffs", {})
                self_buffs = data.get("self_buffs", {})
                if not ally_buffs and legacy_buffs:
                    ally_buffs = legacy_buffs
                legacy_cfg.ally_buffs = ally_buffs
                legacy_cfg.self_buffs = self_buffs
                legacy_cfg.base_stats = character_stats_from_dict(data.get("base_stats", {}))
                legacy_cfg.relics = {
                    slot: relic_from_dict(r) for slot, r in data.get("relics", {}).items()
                }
                self.character_configs = {"姬子": legacy_cfg}

            self.current_character = data.get("current_character", "姬子")
            if self.current_character not in self.character_configs:
                self.current_character = next(iter(self.character_configs))
            self._load_config_to_ui()
            self.log(f"已读取配置：{CONFIG_PATH.name}")
        except Exception as exc:
            self.log(f"读取配置失败：{exc}")

    def save_config(self):
        try:
            # 同步当前 UI 状态到角色配置
            self._sync_ui_to_current_config()
            data = {
                "regions": {
                    "detail": self.detail_region.get(),
                    "relic": self.relic_region.get(),
                },
                "debug_mode": bool(self.debug_mode.get()),
                "overlay_scale": float(self.overlay_scale.get()),
                "current_character": self.current_character,
                "characters": {name: cfg.to_dict() for name, cfg in self.character_configs.items()},
            }
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"保存配置失败：{exc}")

    def current_config(self) -> CharacterConfig | None:
        return self.character_configs.get(self.current_character)

    def recalculate_all_relic_scores(self, old_relics: dict[str, Relic] | None = None):
        """重新计算所有遗器分数。

        old_relics: 替换之前的遗器副本。若提供，则计算每个遗器相对于旧状态的分数差值；
                    若不提供（如启动时），则所有 delta 归零。
        """
        cfg = self.current_config()
        # 收集每个遗器的旧分数（按 (name, percent) 队列匹配，类似 apply_score_deltas）
        old_scores_by_slot: dict[str, dict[tuple[str, bool], list[float]]] = {}
        old_totals: dict[str, float] = {}
        if old_relics:
            for slot, relic in old_relics.items():
                queue_map: dict[tuple[str, bool], list[float]] = {}
                for stat in relic.subs:
                    queue_map.setdefault((stat.name, stat.percent), []).append(stat.score)
                old_scores_by_slot[slot] = queue_map
                old_totals[slot] = relic.total_score

        for slot in list(self.relics):
            score_relic_lines(self.base_stats, self.relics, self.buffs(), slot, cfg)
            relic = self.relics[slot]
            if old_relics and slot in old_scores_by_slot:
                # 对每个副词条，按顺序从旧队列中取分数比较
                old_queue_map = old_scores_by_slot[slot]
                # 复制一份以免修改原数据
                old_queue_map_copy = {k: list(v) for k, v in old_queue_map.items()}
                for stat in relic.subs:
                    key = (stat.name, stat.percent)
                    queue = old_queue_map_copy.get(key)
                    if queue:
                        old_score = queue.pop(0)
                    else:
                        old_score = 0.0
                    stat.delta = stat.score - old_score
                old_total = old_totals.get(slot, 0.0)
                relic.total_delta = relic.total_score - old_total
            else:
                relic.total_delta = 0.0
                for stat in relic.subs:
                    stat.delta = 0.0

    def on_close(self):
        self.save_config()
        self.destroy()

    def begin_sampling(self, target: tk.StringVar):
        self.sampling_target = target
        self.sampling_first_point = None
        self.status.set("采样模式：把鼠标移动到区域左上角，按回车；再移动到右下角，按回车结束。")

    def _start_keyboard_listener(self):
        def on_press(key):
            try:
                if key == keyboard.Key.enter and self.sampling_target:
                    x, y = self.mouse_ctl.position
                    if self.sampling_first_point is None:
                        self.sampling_first_point = (int(x), int(y))
                        self.after(0, lambda: self.status.set("已记录左上角；移动到区域右下角，再按回车结束采样。"))
                    else:
                        x1, y1 = self.sampling_first_point
                        self.sampling_target.set(f"{x1},{y1} | {int(x)},{int(y)}")
                        self.sampling_target = None
                        self.sampling_first_point = None
                        self.after(0, self.save_config)
                        self.after(0, lambda: self.status.set("采样完成。"))
                elif key == keyboard.Key.space:
                    if is_star_rail_active():
                        self.after(0, self.capture_relic_async)
                elif key == keyboard.Key.backspace:
                    if is_star_rail_active():
                        self.after(0, self.undo)
                elif getattr(key, "char", None) == "1":
                    if is_star_rail_active():
                        self.after(0, self.capture_base_async)
            except Exception as exc:
                self.after(0, lambda: self.log(f"热键错误：{exc}"))

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()

    def buffs(self) -> dict[str, float]:
        merged: dict[str, float] = {}
        for field in BUFF_FIELDS:
            total = parse_plus_expr(self.ally_buff_vars[field].get()) + parse_plus_expr(self.self_buff_vars[field].get())
            merged[field] = total
        return merged

    def capture_base_async(self):
        self.run_async(self.capture_base)

    def capture_relic_async(self):
        self.run_async(self.capture_relic)

    def preview_region_async(self, region_var: tk.StringVar):
        self.run_async(lambda: self.preview_region(region_var))

    def run_async(self, func):
        threading.Thread(target=lambda: self.safe_call(func), daemon=True).start()

    def safe_call(self, func):
        try:
            func()
        except Exception as exc:
            log_to_file(f"ERROR: {exc}", "ERROR")
            import traceback
            log_to_file(f"TRACEBACK:\n{traceback.format_exc()}", "ERROR")
            err_msg = str(exc) or exc.__class__.__name__
            self.after(0, lambda: self.overlay.show_error(err_msg))
            self.after(0, lambda: self.log(f"错误：{exc}"))

    def test_ocr(self):
        def work():
            response = requests.get(OCR_URL.replace("/api/ocr", "/api/ocr/get_options"), timeout=5)
            response.raise_for_status()
            self.after(0, lambda: self.log("Umi-OCR 已连接：" + response.text[:300]))
        self.run_async(work)

    def preview_region(self, region_var: tk.StringVar):
        image, region = grab_region(region_var.get())
        self.after(0, lambda: self.show_preview(image, region))
        self.after(0, lambda: self.log(f"截图预览完成：{format_region(region)}"))

    def show_preview(self, image: Image.Image, region: tuple[int, int, int, int]):
        self._preview_original_image = image
        self._preview_region_text = format_region(region)
        self._redraw_preview()

    def _redraw_preview(self):
        """按 preview_label 当前尺寸重绘图片，保持原比例。"""
        if self._preview_original_image is None:
            return
        self.preview_label.update_idletasks()
        avail_w = max(50, self.preview_label.winfo_width() - 8)
        avail_h = max(50, self.preview_label.winfo_height() - 8)
        display = self._preview_original_image.copy()
        # thumbnail 保持比例缩放到不超过可用区域
        display.thumbnail((avail_w, avail_h))
        self.preview_photo = ImageTk.PhotoImage(display)
        self.preview_label.configure(image=self.preview_photo, text=self._preview_region_text, compound="bottom")

    def ask_relaunch_admin(self):
        if messagebox.askyesno("以管理员重启", "游戏若以管理员权限运行，本工具也需要管理员权限才能收到全局热键。现在重启为管理员？"):
            self.save_config()
            relaunch_as_admin()
            self.destroy()

    def capture_base(self):
        self.after(0, self.overlay.clear_error)
        self.overlay.withdraw()
        time.sleep(0.1)
        image, _ = grab_region(self.detail_region.get())
        self.overlay.deiconify()
        self.after(0, lambda: self.show_preview(image, parse_region(self.detail_region.get())))
        lines = self.ocr.image_to_lines(image)
        text = flatten_ocr_text(lines)
        log_to_file(f"capture_base: raw_text=\n{text}", "DEBUG")
        stats = parse_detail_stats(text)
        log_to_file(f"capture_base: parsed stats - hp_base={stats.hp_base}, atk_base={stats.atk_base}, def_base={stats.def_base}, speed={stats.speed}, crit_rate={stats.crit_rate*100:.1f}%, crit_dmg={stats.crit_dmg*100:.1f}%", "DEBUG")
        if stats.atk_base <= 0:
            raise RuntimeError("未能解析攻击力基础值，请检查脱装备详情区域。")
        self.base_stats = stats
        # 同步到当前角色配置
        if self.current_character in self.character_configs:
            self.character_configs[self.current_character].base_stats = stats
        self.after(0, lambda: self.log(f"基础值已记录：攻击 {stats.atk_base}+{stats.atk_bonus}，暴击 {stats.crit_rate*100:.1f}%，爆伤 {stats.crit_dmg*100:.1f}%\nOCR:\n{text}"))
        if self.debug_mode.get():
            self.after(0, lambda: self.overlay.set_debug_overlay(None, None, stats=stats))
        self.after(0, self.refresh_damage)
        self.after(0, self.save_config)

    def _filter_relic_image(self, image: Image.Image) -> Image.Image:
        if np is None:
            return image
        img_array = np.array(image)
        r, g, b = img_array[..., 0], img_array[..., 1], img_array[..., 2]
        green_mask = (np.abs(r - 110) < 60) & (np.abs(g - 224) < 60) & (np.abs(b - 182) < 60)
        dark_mask = (r < 40) & (g < 40) & (b < 40)
        combined_mask = green_mask | dark_mask
        img_array[combined_mask] = [0, 0, 0]
        return Image.fromarray(img_array)

    def _ocr_missing_values(self, image: Image.Image, lines: list[dict]) -> list[dict]:
        """对缺失数值的词条名，裁剪右侧区域并重新 OCR（放大 3 倍提升小字识别）。"""
        stat_names = ["生命值", "攻击力", "防御力", "速度", "暴击率", "暴击伤害",
                      "击破特攻", "效果命中", "效果抵抗",
                      "物理属性伤害提高", "火属性伤害提高", "冰属性伤害提高",
                      "雷属性伤害提高", "风属性伤害提高", "量子属性伤害提高", "虚数属性伤害提高"]
        augmented = list(lines)

        for item in lines:
            text = str(item.get("text", "")).strip()
            box = item.get("box")
            if not box:
                continue
            # 仅处理纯词条名行
            if text not in stat_names:
                continue

            name_y = box_mid_y(box)

            # 检查同一行是否已有数值
            has_value = False
            for other in lines:
                if other is item:
                    continue
                other_text = str(other.get("text", "")).strip()
                other_box = other.get("box")
                if not other_box:
                    continue
                other_y = box_mid_y(other_box)
                if abs(other_y - name_y) <= 15 and re.search(r"\d", other_text):
                    has_value = True
                    break
            if has_value:
                continue

            # 裁剪词条名右侧区域
            name_right = max(p[0] for p in box)
            crop_x1 = name_right + 80
            crop_x2 = min(image.width, crop_x1 + 250)
            crop_y1 = max(0, int(name_y) - 18)
            crop_y2 = min(image.height, int(name_y) + 18)
            if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
                continue

            crop = image.crop((crop_x1, crop_y1, crop_x2, crop_y2))
            # 放大 3 倍提升小字识别
            crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)

            try:
                new_lines = self.ocr.image_to_lines(crop)
                log_to_file(f"_ocr_missing_values: re-OCR for '{text}' at y={name_y:.0f}, crop=({crop_x1},{crop_y1},{crop_x2},{crop_y2}), results={[nl.get('text') for nl in new_lines]}", "DEBUG")
                for nl in new_lines:
                    nl_box = nl.get("box")
                    if nl_box:
                        # 缩放回原始坐标并偏移
                        nl["box"] = [[p[0] / 3 + crop_x1, p[1] / 3 + crop_y1] for p in nl_box]
                    augmented.append(nl)
            except Exception as exc:
                log_to_file(f"_ocr_missing_values: re-OCR failed for '{text}': {exc}", "ERROR")

        return augmented

    def capture_relic(self):
        self.after(0, self.overlay.clear_error)
        image, region = grab_region(self.relic_region.get())
        image = self._filter_relic_image(image)
        self.after(0, lambda: self.show_preview(image, region))
        self.current_relic_region = region
        # 首次扫描时通过绿色 #6EE0B6 检测强化标记位置，缓存后后续 OCR 屏蔽该区域
        if not self.ocr.ignore_area:
            marker_area = detect_enhance_marker_by_color(image)
            if marker_area:
                self.ocr.ignore_area = marker_area
                log_to_file(f"capture_relic: detected enhance marker area={marker_area}", "DEBUG")
        lines = self.ocr.image_to_lines(image)
        # 对缺失数值的词条，重新 OCR 右侧数值区域
        lines = self._ocr_missing_values(image, lines)
        relic = parse_relic(lines)
        if relic.slot == "未知":
            raise RuntimeError("未能识别遗器位置，请检查遗器区域。")

        old_relic = self.relics.get(relic.slot)
        if old_relic and self._is_same_relic(old_relic, relic):
            # OCR 数值在容差内，视为同一遗器，不更新评分，但仍刷新 overlay
            # 同步新 OCR 的 box 到 old_relic（配置加载的 old_relic.box 是 None）
            old_relic.main_box = relic.main_box
            for old_stat, new_stat in zip(old_relic.subs, relic.subs):
                old_stat.box = new_stat.box
            self.after(0, lambda: self.overlay.set_stat_scores(region, old_relic))
            if self.debug_mode.get():
                self.after(0, lambda: self.overlay.set_debug_overlay(region, relic))
            else:
                self.after(0, self.overlay.clear_debug)
            self.after(0, lambda: self.log(f"{relic.slot} OCR 数值在容差内，未更新。"))
            return

        cfg = self.current_config()
        old_relics = copy.deepcopy(self.relics)
        if old_relic:
            score_relic_lines(self.base_stats, old_relics, self.buffs(), relic.slot, cfg)
        old_damage = expected_damage(build_totals(self.base_stats, self.relics, self.buffs()), cfg)
        self.undo_stack.append(copy.deepcopy(self.relics))
        self.relics[relic.slot] = relic
        self.last_signature_by_slot[relic.slot] = relic.signature()
        # 传入 old_relics，让所有遗器的 delta 都实时更新（其他遗器分数可能因暴击率变化而改变）
        self.recalculate_all_relic_scores(old_relics)
        apply_score_deltas(self.relics[relic.slot], old_relic, cfg)
        new_damage = expected_damage(build_totals(self.base_stats, self.relics, self.buffs()), cfg)
        delta = new_damage - old_damage
        self.after(0, lambda: self.overlay.set_stat_scores(region, self.relics[relic.slot]))
        if self.debug_mode.get():
            self.after(0, lambda: self.overlay.set_debug_overlay(region, self.relics[relic.slot]))
        else:
            self.after(0, self.overlay.clear_debug)
        self.after(0, self.refresh_damage)
        self.after(0, self.save_config)
        self.after(0, lambda: self.log_relic(relic.slot, old_damage, new_damage, delta, flatten_ocr_text(lines)))

    def _is_same_relic(self, old: Relic, new: Relic, rel_tol: float = 0.02) -> bool:
        """判断两个遗器是否为同一件（OCR 数值在容差内）。"""
        if old.main_name != new.main_name:
            return False
        if len(old.subs) != len(new.subs):
            return False
        mv_tol = max(0.5, abs(old.main_value) * rel_tol)
        if abs(old.main_value - new.main_value) > mv_tol:
            return False
        for old_sub, new_sub in zip(old.subs, new.subs):
            if old_sub.name != new_sub.name:
                return False
            sv_tol = max(0.3, abs(old_sub.value) * rel_tol)
            if abs(old_sub.value - new_sub.value) > sv_tol:
                return False
        return True

    def undo(self):
        if not self.undo_stack:
            self.log("没有可撤回的更替。")
            return
        self.relics = self.undo_stack.pop()
        self.last_signature_by_slot = {slot: relic.signature() for slot, relic in self.relics.items()}
        # 同步到当前角色配置
        if self.current_character in self.character_configs:
            self.character_configs[self.current_character].relics = self.relics
        self.recalculate_all_relic_scores()
        self.refresh_damage()
        self.save_config()
        self.log("已撤回上次更替。")

    def refresh_damage(self):
        cfg = self.current_config()
        totals = build_totals(self.base_stats, self.relics, self.buffs())
        dmg = expected_damage(totals, cfg)
        dmg_theoretical = expected_damage_theoretical(totals, cfg)
        # 伤害来源对应的中文名和 totals 字段
        source_name = DAMAGE_SOURCES.get(cfg.damage_source, "攻击力") if cfg else "攻击力"
        # 第1行：期望伤害（含理论值）、伤害来源、暴击率（显示原始值，不截断100%）
        dmg_text = f"期望 {dmg:,.0f}"
        if abs(dmg_theoretical - dmg) > 1:
            dmg_text += f" ({dmg_theoretical:,.0f})"
        dmg_text += f" | {source_name} {self._source_value(cfg, totals):.0f}"
        # 仅当暴击率是有效副词条时显示暴击率
        if cfg and "暴击率" in cfg.valid_subs:
            dmg_text += f" | 暴击率 {totals['crit_rate_raw']*100:.1f}%"
        self.overlay.set_damage(dmg_text)

        # 后续行：根据伤害来源动态展示对应词条的计算
        lines = []
        if cfg and cfg.damage_source == "ATK":
            lines.append(self._format_atk_line(totals))
        elif cfg and cfg.damage_source == "HP":
            lines.append(self._format_hp_line(totals))
        elif cfg and cfg.damage_source == "DEF":
            lines.append(self._format_def_line(totals))
        elif cfg and cfg.damage_source == "SPD":
            lines.append(self._format_spd_line(totals))
        elif cfg and cfg.damage_source == "BREAK":
            lines.append(self._format_break_line(totals))
        elif cfg and cfg.damage_source == "CUSTOM":
            lines.append(f"自定义公式：{cfg.damage_formula} = {self._source_value(cfg, totals):.0f}")

        # 暴击率/爆伤仅当为有效副词条时显示
        if cfg and "暴击率" in cfg.valid_subs:
            lines.append(self._format_crit_rate_line(totals))
        if cfg and "暴击伤害" in cfg.valid_subs:
            lines.append(self._format_crit_dmg_line(totals))
        # 最多 4 行（避免 overlay 过长）
        self.overlay.set_panel(*lines[:4])

    def _source_value(self, cfg: CharacterConfig | None, totals: dict[str, float]) -> float:
        """获取伤害来源对应的数值。"""
        if cfg is None:
            return totals.get("atk", 0.0)
        return compute_damage_base(totals, cfg)

    def _format_atk_line(self, totals: dict[str, float]) -> str:
        atk_base = totals["atk_base"]
        atk_bonus = totals["atk_bonus"]
        relic_atk_percent_val = totals["relic_atk_percent"] * 100
        relic_atk_flat = totals["relic_atk_flat"]
        relic_atk_percent_conv = atk_base * totals["relic_atk_percent"]
        buff_atk_flat = totals["buff_atk_flat"]
        buff_atk_pct_val = totals["buff_atk_pct"] * 100
        buff_atk_pct_conv = atk_base * totals["buff_atk_pct"]
        return (f"攻击力：{atk_base:.0f} + {atk_bonus:.0f} + "
                f"{relic_atk_percent_conv:.0f}({relic_atk_percent_val:.1f}%) + "
                f"{relic_atk_flat:.0f} + {buff_atk_flat:.0f} + "
                f"{buff_atk_pct_conv:.0f}({buff_atk_pct_val:.1f}%)")

    def _format_hp_line(self, totals: dict[str, float]) -> str:
        hp_base = totals["hp_base"]
        hp_bonus = totals["hp_bonus"]
        relic_hp_percent_val = totals["relic_hp_percent"] * 100
        relic_hp_flat = totals["relic_hp_flat"]
        relic_hp_percent_conv = hp_base * totals["relic_hp_percent"]
        buff_hp_flat = totals["buff_hp_flat"]
        buff_hp_pct_val = totals["buff_hp_pct"] * 100
        buff_hp_pct_conv = hp_base * totals["buff_hp_pct"]
        return (f"生命值：{hp_base:.0f} + {hp_bonus:.0f} + "
                f"{relic_hp_percent_conv:.0f}({relic_hp_percent_val:.1f}%) + "
                f"{relic_hp_flat:.0f} + {buff_hp_flat:.0f} + "
                f"{buff_hp_pct_conv:.0f}({buff_hp_pct_val:.1f}%)")

    def _format_def_line(self, totals: dict[str, float]) -> str:
        def_base = totals["def_base"]
        def_bonus = totals["def_bonus"]
        relic_def_percent_val = totals["relic_def_percent"] * 100
        relic_def_flat = totals["relic_def_flat"]
        relic_def_percent_conv = def_base * totals["relic_def_percent"]
        return (f"防御力：{def_base:.0f} + {def_bonus:.0f} + "
                f"{relic_def_percent_conv:.0f}({relic_def_percent_val:.1f}%) + "
                f"{relic_def_flat:.0f}")

    def _format_spd_line(self, totals: dict[str, float]) -> str:
        return f"速度：{totals['speed']:.1f}"

    def _format_break_line(self, totals: dict[str, float]) -> str:
        return f"击破特攻：{totals['break_dmg']*100:.1f}%"

    def _format_crit_rate_line(self, totals: dict[str, float]) -> str:
        cr_parts = [f"{totals['crit_rate_base']*100:.1f}"]
        for slot in ["头部", "手部", "躯干", "脚部", "位面球", "连结绳"]:
            cr = totals["relic_crit_rate_by_slot"].get(slot, 0.0)
            if cr > 0:
                cr_parts.append(f"{cr*100:.1f}")
        if totals["buff_crit_rate"] > 0:
            cr_parts.append(f"{totals['buff_crit_rate']*100:.1f}")
        return f"暴击率：{' + '.join(cr_parts)} = {totals['crit_rate_raw']*100:.1f}%"

    def _format_crit_dmg_line(self, totals: dict[str, float]) -> str:
        cd_parts = [f"{totals['crit_dmg_base']*100:.1f}"]
        for slot in ["头部", "手部", "躯干", "脚部", "位面球", "连结绳"]:
            cd = totals["relic_crit_dmg_by_slot"].get(slot, 0.0)
            if cd > 0:
                cd_parts.append(f"{cd*100:.1f}")
        if totals["buff_crit_dmg"] > 0:
            cd_parts.append(f"{totals['buff_crit_dmg']*100:.1f}")
        return f"暴击伤害：{' + '.join(cd_parts)} = {totals['crit_dmg']*100:.1f}%"

    def log_relic(self, slot, old_damage, new_damage, delta, text):
        relic = self.relics[slot]
        cfg = self.current_config()
        self.log(f"{slot} 已记录，期望伤害 {old_damage:,.0f} -> {new_damage:,.0f}，差值 {delta:+,.0f}，总分 {relic.total_score:.2f} ({relic.total_delta:+.2f})")
        for stat in relic.subs:
            valid = "有效" if _stat_weight(stat.name, cfg) > 0 else "无效"
            unit = "%" if stat.percent else ""
            self.log(f"  {stat.name} {stat.value:g}{unit}：{stat.score:.2f} 分，差值 {stat.delta:+.2f} ({valid})")
        self.log("OCR:\n" + text)

    def log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        self.text.insert("end", f"[{stamp}] {msg}\n")
        self.text.see("end")


if __name__ == "__main__":
    enable_dpi_awareness()
    if not is_admin():
        relaunch_as_admin()
        raise SystemExit(0)
    App().mainloop()
