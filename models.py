"""数据模型：遗器、角色属性、角色配置等数据类，以及序列化与公式求值。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from utils import log_to_file

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

_DEFAULT_VALID_SUBS = [name for name, w in HIMEKO_QIXING_WEIGHTS.items() if w > 0]

# 各属性伤害主词条统一映射为"属性加伤"
ELEMENTAL_DAMAGE_ALIASES = {
    "物理属性伤害提高": "属性加伤",
    "火属性伤害提高": "属性加伤",
    "冰属性伤害提高": "属性加伤",
    "雷属性伤害提高": "属性加伤",
    "风属性伤害提高": "属性加伤",
    "量子属性伤害提高": "属性加伤",
    "虚数属性伤害提高": "属性加伤",
    "物理伤害提高": "属性加伤",
    "火伤害提高": "属性加伤",
    "冰伤害提高": "属性加伤",
    "雷伤害提高": "属性加伤",
    "风伤害提高": "属性加伤",
    "量子伤害提高": "属性加伤",
    "虚数伤害提高": "属性加伤",
}


def normalize_stat_name(name: str, value_has_percent: bool) -> str:
    name = name.replace(" ", "").replace("暴击伤害", "暴击伤害")
    name = name.replace("暴击率", "暴击率").replace("擊破特攻", "击破特攻")
    if name in ELEMENTAL_DAMAGE_ALIASES:
        return ELEMENTAL_DAMAGE_ALIASES[name]
    if name in {"生命值", "攻击力", "防御力"} and value_has_percent:
        return name + "%"
    return name


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
