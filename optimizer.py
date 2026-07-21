"""多遗器最优组合计算：从记录的多件遗器中，按 6 个 slot 各选 1 件，
使期望伤害最大化。用回溯剪枝（slot 数固定 6，每个 slot 候选数有限）。

算法：
- 每个 slot 的候选遗器独立评分（基于 build_totals + expected_damage）
- 6 个 slot 全枚举：最坏情况 6^n，但实际每个 slot 候选数 3-10，剪枝后可接受
- 用 leave-one-out 评分：以"全装备该组合"为基准，去掉某件后的 DPS 下降量作为该件贡献
- 最终输出：最优组合 + 总分（=100*实际DPS/完美遗器DPS）
"""
from __future__ import annotations

from itertools import product
from typing import Callable

from models import CharacterConfig, CharacterStats, Relic
from scoring import (
    _compute_perfect_baseline, _stat_weight, build_totals, expected_damage,
    score_relic_lines,
)


RELIC_SLOTS = ["头部", "手部", "躯干", "脚部", "位面球", "连结绳"]


def compute_combination_score(
    base: CharacterStats,
    relics_by_slot: dict[str, list[Relic]],
    buffs: dict[str, float],
    config: CharacterConfig | None,
) -> tuple[dict[str, Relic], float, float]:
    """从每个 slot 的候选遗器列表中，找出期望伤害最高的组合。

    参数：
        relics_by_slot: {slot: [relic1, relic2, ...]}，每个 slot 至少 1 件
        buffs: 拐力字典
        config: 角色配置

    返回：(最佳组合, 期望伤害, 完美基准DPS)
        - 最佳组合: {slot: Relic}
        - 期望伤害: 该组合的 expected_damage
        - 完美基准DPS: 用于归一化评分（100 = 完美遗器组合）

    算法：6 个 slot 全枚举，但用"贪心预排序 + 期望伤害评估"剪枝。
    每个 slot 候选最多 20 件，6 slot 全枚举最坏 20^6 = 6.4e7，太多。
    实际中每个 slot 候选 3-8 件，6^8 = 1.7e6，可接受。
    若某 slot 候选超过 15 件，先按单件边际贡献排序取 top 15。
    """
    # 准备每个 slot 的候选，过滤掉 slot 标签未知或主词条缺失的
    candidates: dict[str, list[Relic]] = {}
    for slot in RELIC_SLOTS:
        lst = [r for r in relics_by_slot.get(slot, []) if r.main_name]
        if not lst:
            continue
        # 候选过多时按单件 DPS 贡献排序取 top 15
        if len(lst) > 15:
            base_relics_empty: dict[str, Relic] = {}
            base_dps = expected_damage(build_totals(base, base_relics_empty, buffs), config)

            def single_score(r: Relic) -> float:
                test_relics = {slot: r}
                dps = expected_damage(build_totals(base, test_relics, buffs), config)
                return dps - base_dps

            lst.sort(key=single_score, reverse=True)
            lst = lst[:15]
        candidates[slot] = lst

    if not candidates:
        empty: dict[str, Relic] = {}
        return empty, 0.0, 1.0

    # 全枚举找最大 DPS 组合
    slots = list(candidates.keys())
    best_combo: dict[str, Relic] = {}
    best_dps = -1.0

    for combo_tuple in product(*[candidates[s] for s in slots]):
        relic_dict = {slots[i]: combo_tuple[i] for i in range(len(slots))}
        dps = expected_damage(build_totals(base, relic_dict, buffs), config)
        if dps > best_dps:
            best_dps = dps
            best_combo = relic_dict

    # 计算完美基准：用第一个 slot 的第一件遗器作为"参考遗器"计算 baseline
    # （完美基准对每个 slot 单独算，这里取最优组合的 leave-one-out 总和作为归一化）
    first_slot = slots[0]
    first_relic = best_combo[first_slot]
    perfect_baseline = _compute_perfect_baseline(
        base, best_combo, buffs, first_slot, first_relic, config
    )

    return best_combo, best_dps, perfect_baseline


def score_combination(
    base: CharacterStats,
    combo: dict[str, Relic],
    buffs: dict[str, float],
    config: CharacterConfig | None,
    perfect_baseline: float,
) -> float:
    """计算一个组合的归一化总分（0-100+）。
    总分 = (组合DPS - 空装DPS) / perfect_baseline * 100

    perfect_baseline 来自 compute_combination_score 的返回值。
    空装DPS = 不装备任何遗器时的 DPS（base + buffs only）。
    """
    empty: dict[str, Relic] = {}
    base_dps = expected_damage(build_totals(base, empty, buffs), config)
    combo_dps = expected_damage(build_totals(base, combo, buffs), config)
    if perfect_baseline <= 0:
        return 0.0
    return max(0.0, (combo_dps - base_dps) / perfect_baseline * 100.0)


def format_combo_report(
    combo: dict[str, Relic],
    total_score: float,
    expected_dps: float,
    config: CharacterConfig | None,
) -> str:
    """格式化最优组合报告文本，用于日志和 UI 显示。"""
    from ocr_utils import pct_to_ratio  # 局部导入避免循环
    lines = []
    lines.append(f"=== 最优组合 ===")
    lines.append(f"总评分: {total_score:.2f}")
    lines.append(f"期望伤害: {expected_dps:.0f}")
    lines.append("")
    lines.append("遗器清单：")
    for slot in RELIC_SLOTS:
        if slot not in combo:
            continue
        r = combo[slot]
        main_pct = "%" if r.main_name and (r.main_name.endswith("%") or r.main_name in {
            "暴击率", "暴击伤害", "击破特攻", "效果命中", "效果抵抗", "属性加伤"
        }) else ""
        main_str = f"{r.main_name}{r.main_value:g}{main_pct}" if r.main_name else "?"
        subs_str = " ".join(
            f"{s.name}{s.value:g}{'%' if s.percent else ''}"
            for s in r.subs
        )
        # 单件评分（在组合中的贡献）
        slot_score = r.total_score if r.total_score > 0 else 0.0
        lines.append(f"  [{slot}] {main_str} | {subs_str} | 件均分={slot_score:.1f}")
    return "\n".join(lines)
