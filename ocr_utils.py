"""OCR 相关：RapidOCR 引擎封装、坐标工具、遗器/面板解析、强化标记检测。

后端说明：
- 默认使用 RapidOCR（rapidocr-onnxruntime，Apache 2.0，模型来自 PaddleOCR）。
  优点：纯 Python 包，随 EXE 打包，无需启动外部进程。
  模型文件首次使用时自动下载到用户目录，之后离线可用。
- 可选回退到外部 Umi-OCR HTTP API（http://127.0.0.1:1224/api/ocr）。
  优点：Umi-OCR 已在运行时，复用其模型缓存；支持 tbpu.ignoreArea 服务端忽略区域。

性能优化要点（对比 Umi-OCR 默认配置）：
1. 禁用角度分类（use_cls=False）：游戏截图文字均为正向，无需 cls 模型，省一次推理。
2. 启动时后台预热：构造 UmiOcrClient 后立即用 1x1 占位图跑一次 OCR，
   避免首次识别遗器时同步加载 3 个 ONNX 模型（约 2-4 秒）。
3. onnxruntime 线程数：让 onnxruntime 自动使用所有 CPU 核心。
4. 截图前预裁剪：调用方传入的 image 应是已裁剪的区域，避免对整屏 OCR。

参考 Umi-OCR 项目（https://github.com/hiroi-sora/Umi-OCR）的模块划分思路：
- ocr/api：OCR 引擎接口（这里封装为 UmiOcrClient，兼容两种后端）
- ocr/tbpu：文块处理（这里包含坐标工具 box_x/y/mid_y、解析函数）
- ocr/tbpu/ignore_area：忽略区域（UmiOcrClient.ignore_area + detect_enhance_marker_by_color）
- ocr/tbpu/parser_tools/line_preprocessing：按行预处理（这里包含 flatten_ocr_text、parse_relic 的 line_groups）
"""
from __future__ import annotations

import base64
import io
import re
import threading

import requests

try:
    from PIL import Image
except Exception:
    Image = None

# numpy 单独导入：打包后可能因 DLL 加载失败抛出 OSError 等非 ImportError，
# 必须用 except Exception 兜底，否则 np 变量从未被定义会导致运行时 NameError。
try:
    import numpy as np
except Exception:
    np = None

# RapidOCR 可选导入（程序自带优先使用）
try:
    from rapidocr_onnxruntime import RapidOCR
    _RAPIDOCR_AVAILABLE = True
except ImportError:
    RapidOCR = None
    _RAPIDOCR_AVAILABLE = False

from models import Relic, StatLine, CharacterStats, normalize_stat_name
from utils import log_to_file

# 外部 Umi-OCR HTTP API 地址（回退方案）
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

VALID_STATS = {
    "生命值", "攻击力", "防御力", "生命值%", "攻击力%", "防御力%", "速度",
    "暴击率", "暴击伤害", "击破特攻", "效果命中", "效果抵抗", "属性加伤",
    "物理属性伤害提高", "火属性伤害提高", "冰属性伤害提高", "雷属性伤害提高",
    "风属性伤害提高", "量子属性伤害提高", "虚数属性伤害提高",
    "能量恢复", "治疗加成",
}


def identify_slot(text: str) -> str:
    for slot in RELIC_SLOTS:
        if slot in text:
            return slot
    for alias, target in RELIC_SLOT_ALIASES.items():
        if alias in text:
            return target
    return "未知"


class UmiOcrClient:
    """OCR 客户端：优先使用内置 RapidOCR，失败回退到外部 Umi-OCR HTTP API。

    参考 Umi-OCR 的 ocr/api 模块：封装 OCR 引擎接口。
    ignore_area 对应 Umi-OCR 的 tbpu.ignoreArea 参数（忽略区域）。

    后端选择：
    - backend="auto"（默认）：先尝试 RapidOCR，初始化失败则回退 HTTP
    - backend="rapidocr"：强制使用 RapidOCR
    - backend="http"：强制使用外部 Umi-OCR HTTP API
    """
    def __init__(self, url: str = OCR_URL, backend: str = "auto"):
        self.url = url
        # 屏蔽区域（相对图片坐标），用于屏蔽强化标记①②等，格式 [[x1,y1],[x2,y2]]
        self.ignore_area: list | None = None
        self._rapidocr_engine = None  # RapidOCR 引擎实例（延迟初始化）
        self._rapidocr_lock = threading.Lock()  # 预热线程与首次 OCR 并发保护
        self.backend = backend
        # 实际使用的后端（延迟确定）：auto 时先假设 rapidocr，失败后改 http
        if backend == "http":
            self._active_backend = "http"
        elif backend == "rapidocr":
            if not _RAPIDOCR_AVAILABLE:
                raise RuntimeError("RapidOCR 未安装，请 pip install rapidocr-onnxruntime")
            self._active_backend = "rapidocr"
        else:  # auto
            self._active_backend = "rapidocr" if _RAPIDOCR_AVAILABLE else "http"
        log_to_file(f"UmiOcrClient: backend={self.backend}, active={self._active_backend}, rapidocr_available={_RAPIDOCR_AVAILABLE}", "DEBUG")
        # 后台预热 RapidOCR：首次按空格识别时模型已就绪，避免阻塞 2-4 秒
        if self._active_backend == "rapidocr":
            threading.Thread(target=self._warmup_rapidocr, daemon=True).start()

    def _warmup_rapidocr(self):
        """后台用 1x1 占位图预热 RapidOCR，加载 det/cls/rec 三个 ONNX 模型。"""
        try:
            engine = self._get_rapidocr()
            if np is None or Image is None:
                return
            # 1x1 占位图：触发模型加载，结果为空不影响后续使用
            placeholder = Image.new("RGB", (64, 32), (0, 0, 0))
            engine(np.array(placeholder), use_cls=False, use_det=True, use_rec=True)
            log_to_file("RapidOCR 预热完成", "DEBUG")
        except Exception as exc:
            log_to_file(f"RapidOCR 预热失败（不影响功能，首次使用时会再次尝试加载）：{exc}", "DEBUG")

    def _get_rapidocr(self):
        """延迟初始化 RapidOCR 引擎（首次调用 OCR 时加载模型）。
        加锁：预热线程与首次 OCR 可能同时触发，避免重复初始化。"""
        with self._rapidocr_lock:
            if self._rapidocr_engine is None:
                if not _RAPIDOCR_AVAILABLE:
                    raise RuntimeError("RapidOCR 未安装")
                # use_cls=False：禁用角度分类模型，游戏文字均为正向，省一次推理
                # intra_op_num_threads=0：让 onnxruntime 自动按 CPU 核数选择线程
                self._rapidocr_engine = RapidOCR(use_cls=False)
            return self._rapidocr_engine

    def _apply_ignore_area(self, image: "Image.Image") -> "Image.Image":
        """在图片上用纯色填充 ignore_area 区域（RapidOCR 不支持服务端忽略，需客户端预处理）。
        用 numpy 直接填充，比 PIL ImageDraw 快约 5 倍（300ms → 60ms）。"""
        if not self.ignore_area or Image is None:
            return image
        try:
            (x1, y1), (x2, y2) = self.ignore_area
            # 直接在原数组上操作，避免 image.copy() 的 PIL 开销
            arr = np.array(image.convert("RGB"))
            arr[max(0, int(y1)):int(y2), max(0, int(x1)):int(x2)] = [16, 24, 32]
            return Image.fromarray(arr)
        except Exception as exc:
            log_to_file(f"_apply_ignore_area failed: {exc}", "ERROR")
            return image

    def image_to_lines(self, image: "Image.Image") -> list[dict]:
        """识别图片，返回 [{box, text, score}, ...]，按 (y, x) 排序。
        box 格式：[[x1,y1],[x2,y2],[x3,y3],[x4,y4]]（4 个角点）。
        统一封装 RapidOCR 和 Umi-OCR HTTP 的返回格式。"""
        # 尝试 RapidOCR 后端
        if self._active_backend == "rapidocr":
            try:
                return self._image_to_lines_rapidocr(image)
            except Exception as exc:
                log_to_file(f"RapidOCR 失败，回退 HTTP: {exc}", "ERROR")
                self._active_backend = "http"
        # HTTP 后端
        return self._image_to_lines_http(image)

    def _image_to_lines_rapidocr(self, image: "Image.Image") -> list[dict]:
        """RapidOCR 后端：返回 [{box, text, score}, ...]。"""
        if np is None:
            raise RuntimeError("numpy 未安装")
        engine = self._get_rapidocr()
        # 一次性转 RGB numpy 数组，ignore_area 直接在数组上填充，避免重复 convert
        arr = np.array(image.convert("RGB"))
        if self.ignore_area:
            (x1, y1), (x2, y2) = self.ignore_area
            arr[max(0, int(y1)):int(y2), max(0, int(x1)):int(x2)] = [16, 24, 32]
        # use_cls=False：跳过角度分类，游戏文字均为正向
        result, _elapse = engine(arr, use_cls=False)
        if not result:
            return []
        lines = []
        for item in result:
            # RapidOCR 返回 [box, text, score]，box 是 [[x,y],...x4]
            box = item[0]
            text = str(item[1])
            score = float(item[2]) if len(item) > 2 else 0.0
            lines.append({"box": box, "text": text, "score": score})
        return sorted(lines, key=lambda item: (box_y(item.get("box")), box_x(item.get("box"))))

    def _image_to_lines_http(self, image: "Image.Image") -> list[dict]:
        """外部 Umi-OCR HTTP API 后端。"""
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


# ===== 坐标工具（对应 Umi-OCR ocr/tbpu/parser_tools/line_preprocessing 的 bbox 计算） =====

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


def flatten_ocr_text(lines: list[dict]) -> str:
    return "\n".join(str(item.get("text", "")) for item in lines if item.get("text"))


# ===== 面板解析（基于关键词定位 + box y 坐标） =====

def _find_percent_after(text: str, keyword: str, next_keywords: list[str] | None = None) -> float | None:
    """在 text 中找 keyword 之后、下一个词条名之前的第一个百分比数字。
    next_keywords 指定可能紧跟在 keyword 后的词条名，用于限定搜索范围，避免越过下一个词条。"""
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
    """基于文本关键词解析基础面板（回退方案）。"""
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


def parse_detail_stats_from_lines(lines: list[dict]) -> CharacterStats:
    """基于 OCR line 的 box y 坐标解析基础面板。
    崩铁面板布局：数值与词条名在同一行（左右排列），OCR 会拆成不同 line 但 y 相近。
    用 y 坐标最近匹配，不依赖 OCR 输出顺序，避免数值在词条名上方/下方导致的错位。
    匹配失败时回退到 parse_detail_stats(text)。"""
    stats = CharacterStats()
    if not lines:
        return stats

    stat_names = ["生命值", "攻击力", "防御力", "速度",
                  "暴击率", "暴击伤害", "击破特攻"]

    name_items: list[tuple[str, float]] = []
    for item in lines:
        text = str(item.get("text", "")).strip()
        box = item.get("box")
        if not box:
            continue
        if text in stat_names:
            name_items.append((text, box_mid_y(box)))

    if not name_items:
        return parse_detail_stats(flatten_ocr_text(lines))

    value_items: list[tuple[float, bool, bool, float]] = []
    for item in lines:
        text = str(item.get("text", "")).strip()
        box = item.get("box")
        if not box:
            continue
        if text in stat_names:
            continue
        for m in re.finditer(r"([+＋]?)(\d+(?:\.\d+)?)\s*(%?)", text):
            plus = bool(m.group(1))
            try:
                val = float(m.group(2))
            except ValueError:
                continue
            percent = bool(m.group(3))
            value_items.append((val, plus, percent, box_mid_y(box)))

    if not value_items:
        return parse_detail_stats(flatten_ocr_text(lines))

    y_threshold = 30.0
    grouped: dict[str, list[tuple[float, bool, bool]]] = {name: [] for name in stat_names}
    for val, plus, percent, vy in value_items:
        best_name = None
        best_dist = float("inf")
        for name, ny in name_items:
            dist = abs(vy - ny)
            if dist < best_dist:
                best_dist = dist
                best_name = name
        if best_name and best_dist <= y_threshold:
            grouped[best_name].append((val, plus, percent))

    for name, attr_base, attr_bonus in [
        ("生命值", "hp_base", "hp_bonus"),
        ("攻击力", "atk_base", "atk_bonus"),
        ("防御力", "def_base", "def_bonus"),
    ]:
        base = 0.0
        bonus = 0.0
        for val, plus, percent in grouped[name]:
            if percent:
                continue
            if plus:
                bonus = val
            elif base == 0.0:
                base = val
        setattr(stats, attr_base, base)
        setattr(stats, attr_bonus, bonus)

    for val, plus, percent in grouped["速度"]:
        if not percent and not plus:
            stats.speed = val
            break

    for name, attr in [("暴击率", "crit_rate"), ("暴击伤害", "crit_dmg")]:
        for val, plus, percent in grouped[name]:
            if percent:
                setattr(stats, attr, pct_to_ratio(val))
                break

    if stats.atk_base <= 0 or (stats.crit_rate == 0 and stats.crit_dmg == 0):
        fallback = parse_detail_stats(flatten_ocr_text(lines))
        if stats.atk_base <= 0:
            stats.atk_base = fallback.atk_base
            stats.atk_bonus = fallback.atk_bonus
        if stats.hp_base <= 0:
            stats.hp_base = fallback.hp_base
            stats.hp_bonus = fallback.hp_bonus
        if stats.def_base <= 0:
            stats.def_base = fallback.def_base
            stats.def_bonus = fallback.def_bonus
        if stats.speed <= 0:
            stats.speed = fallback.speed
        if stats.crit_rate == 0:
            stats.crit_rate = fallback.crit_rate
        if stats.crit_dmg == 0:
            stats.crit_dmg = fallback.crit_dmg

    return stats


def pct_to_ratio(value: float) -> float:
    return value / 100.0


def detect_enhance_marker_by_color(image) -> list | None:
    """通过颜色 #6EE0B6 检测强化标记①②等在图片中的位置，
    返回相对图片的屏蔽区域 [[x1,y1],[x2,y2]]。所有遗器强化标记位置固定，首次检测后可复用。
    对应 Umi-OCR 的 tbpu.ignoreArea 忽略区域功能。"""
    if Image is None or np is None:
        return None
    try:
        arr = np.array(image.convert("RGB"))
        target = np.array([110, 224, 182])
        diff = np.abs(arr.astype(int) - target).sum(axis=2)
        mask = diff < 30
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return None
        x1, x2 = int(xs.min()), int(xs.max())
        y1, y2 = int(ys.min()), int(ys.max())
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
        circled_digits = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
        for c in circled_digits:
            line_text = line_text.replace(c, "")
        m_multi_dot = re.search(r"(\d+)\.(\d+)\.(\d+)%?", line_text)
        if m_multi_dot:
            merged = f"{m_multi_dot.group(1)}{m_multi_dot.group(2)}.{m_multi_dot.group(3)}"
            if m_multi_dot.group(0).endswith("%"):
                merged += "%"
            try:
                merged_val = float(merged.replace("%", ""))
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
    main_box: list | None = None  # 修复：原代码未初始化，unique_pairs 为空时 NameError
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
