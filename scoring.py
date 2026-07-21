"""评分逻辑：伤害计算、完美遗器基准、归一化评分、评分等级。"""
from __future__ import annotations

import copy

from models import (
    CharacterConfig, CharacterStats, HIMEKO_QIXING_WEIGHTS, Relic, StatLine,
    _DEFAULT_VALID_SUBS, safe_eval_formula,
)
from ocr_utils import pct_to_ratio

# 金遗器副词条满强化最大值（大档，+15时5次强化全中）。
# 用于归一化评分：完美遗器（4个有效副词条全最大值）= 100分。
STAT_MAX_VALUES = {
    "生命值": 254.0,       # 固定值
    "攻击力": 127.0,       # 固定值
    "防御力": 127.0,       # 固定值
    "生命值%": 25.92,
    "攻击力%": 25.92,
    "防御力%": 32.4,
    "速度": 15.6,
    "暴击率": 19.44,
    "暴击伤害": 38.88,
    "击破特攻": 38.88,
    "效果命中": 25.92,
    "效果抵抗": 25.92,
}

# 评分等级阈值（基于完美遗器=100分，考虑刷遗器成本）。
SCORE_GRADES = [
    (55.0, "ACE", "#FF3030"),  # 红色：顶级，带动态闪烁效果
    (48.0, "SSS", "#FFD700"),  # 金色：极佳
    (40.0, "SS",  "#FF6B9D"),  # 粉色：优秀
    (32.0, "S",   "#65F2A5"),  # 绿色：良好
    (22.0, "A",   "#6BB6FF"),  # 蓝色：及格
    (0.0,  "B",   "#888888"),  # 灰色：待替换
]


def score_to_grade(score: float) -> tuple[str, str]:
    """根据总分返回 (等级名, 颜色)。score<=0 返回空字符串。"""
    if score <= 0:
        return ("", "#888888")
    for threshold, name, color in SCORE_GRADES:
        if score >= threshold:
            return (name, color)
    return ("B", "#888888")


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


def _is_percent_stat(name: str) -> bool:
    """判断该副词条在 OCR 中是否带 "%" 后缀。用于正确构造 StatLine.percent 字段。"""
    return name.endswith("%") or name in {"暴击率", "暴击伤害", "击破特攻", "效果命中", "效果抵抗"}


def _compute_perfect_baseline(base: CharacterStats, relics: dict[str, Relic], buffs: dict[str, float],
                              slot: str, relic: Relic, config: CharacterConfig | None) -> float:
    """计算完美遗器基准：排除主词条同名后，取 min(4, 候选数) 个边际贡献最大的有效副词条（全最大值）
    组合后的 leave-one-out 总边际DPS贡献。
    用于归一化，使该slot的完美遗器（实际可达的最大副词条组合）= 100分，跨slot跨角色可比。
    用 leave-one-out 而非"单独贡献之和"，避免乘法协同效应导致完美遗器得分 >100。
    排除与主词条同名的副词条（如衣服主爆伤→副词条不可能有爆伤，球主生命值%→副词条不再选生命值%）。"""
    valid_subs = config.valid_subs if config and config.valid_subs else _DEFAULT_VALID_SUBS
    main_name = relic.main_name or ""
    excluded = {main_name}
    base_relics = {k: v for k, v in relics.items() if k != slot}
    base_dps = expected_damage(build_totals(base, base_relics, buffs), config)

    candidates = []
    for name in valid_subs:
        if name in excluded:
            continue
        max_val = STAT_MAX_VALUES.get(name, 0.0)
        if max_val <= 0:
            continue
        test_relic = Relic(slot=slot, main_name=relic.main_name, main_value=relic.main_value)
        test_relic.subs = [StatLine(name=name, value=max_val, percent=_is_percent_stat(name))]
        test_relics = {**base_relics, slot: test_relic}
        test_dps = expected_damage(build_totals(base, test_relics, buffs), config)
        candidates.append((name, max_val, test_dps - base_dps))

    if not candidates:
        return expected_damage(build_totals(base, relics, buffs), config)

    # 过滤边际贡献<=0的候选（如姬子速度无效果），避免拉低完美基准
    candidates = [c for c in candidates if c[2] > 0]
    if not candidates:
        return expected_damage(build_totals(base, relics, buffs), config)

    # 跨slot一致：每个slot的"完美遗器"=100分
    # 候选不足4个时（如衣服主爆伤只剩3候选），用全部候选作为完美基准
    candidates.sort(key=lambda x: x[2], reverse=True)
    top_n = min(4, len(candidates))
    top4 = candidates[:top_n]
    if sum(c[2] for c in top4) <= 0:
        return expected_damage(build_totals(base, relics, buffs), config)

    def _build_relic_with_subs(sub_list):
        r = Relic(slot=slot, main_name=relic.main_name, main_value=relic.main_value)
        r.subs = [StatLine(name=n, value=v, percent=_is_percent_stat(n)) for n, v in sub_list]
        return r

    perfect_sub_list = [(n, v) for n, v, _ in top4]
    perfect_relic = _build_relic_with_subs(perfect_sub_list)
    perfect_relics = {**base_relics, slot: perfect_relic}
    perfect_dps = expected_damage(build_totals(base, perfect_relics, buffs), config)

    loo_sum = 0.0
    for i in range(len(top4)):
        reduced_sub_list = [(n, v) for j, (n, v, _) in enumerate(top4) if j != i]
        reduced_relic = _build_relic_with_subs(reduced_sub_list)
        reduced_relics = {**base_relics, slot: reduced_relic}
        reduced_dps = expected_damage(build_totals(base, reduced_relics, buffs), config)
        loo_sum += perfect_dps - reduced_dps

    if loo_sum <= 0:
        return max(perfect_dps - base_dps, 1.0)
    return loo_sum


def score_relic_lines(base: CharacterStats, relics: dict[str, Relic], buffs: dict[str, float], slot: str,
                      config: CharacterConfig | None = None) -> None:
    totals_with = build_totals(base, relics, buffs)
    with_all = expected_damage(totals_with, config)
    if with_all <= 0 or slot not in relics:
        return
    relic = relics[slot]
    crit_rate_raw = totals_with["crit_rate_raw"]
    is_overflow = crit_rate_raw > 1.0
    with_crit_100 = expected_damage_crit_100(totals_with, config)
    perfect_baseline = _compute_perfect_baseline(base, relics, buffs, slot, relic, config)
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
        stat.score = max(0.0, (with_all - without) / perfect_baseline * 100.0)
        # 理论分：暴击率溢出时暴击率词条用未截断暴击率；爆伤词条用暴击率=100%
        if stat.name == "暴击率" and is_overflow:
            without_theoretical = expected_damage_theoretical(reduced_totals, config)
            with_theoretical = expected_damage_theoretical(totals_with, config)
            stat.theoretical_score = max(0.0, (with_theoretical - without_theoretical) / perfect_baseline * 100.0)
        elif stat.name == "暴击伤害":
            without_100 = expected_damage_crit_100(reduced_totals, config)
            stat.theoretical_score = max(0.0, (with_crit_100 - without_100) / perfect_baseline * 100.0)
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
        old_counts: dict[tuple[str, bool], int] = {}
        for stat in old_relic.subs:
            if _stat_weight(stat.name, config) > 0:
                old_counts[(stat.name, stat.percent)] = old_counts.get((stat.name, stat.percent), 0) + 1
        new_counts: dict[tuple[str, bool], int] = {}
        for stat in new_relic.subs:
            if _stat_weight(stat.name, config) > 0:
                new_counts[(stat.name, stat.percent)] = new_counts.get((stat.name, stat.percent), 0) + 1
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
