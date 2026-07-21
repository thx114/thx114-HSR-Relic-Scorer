"""应用主窗口：角色配置、遗器管理、热键监听、OCR 调度。"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk, simpledialog

try:
    from PIL import Image, ImageTk
    from pynput import keyboard, mouse
except ImportError as exc:
    Image = None
    ImageTk = None
    keyboard = None
    mouse = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

# numpy 单独导入：打包后可能因 DLL 加载失败抛出 OSError 等非 ImportError，
# 必须用 except Exception 兜底，否则 np 变量从未被定义会导致运行时 NameError。
try:
    import numpy as np
except Exception:
    np = None

from models import (
    CharacterConfig, CharacterStats, Relic, StatLine,
    character_stats_from_dict, parse_plus_expr, relic_from_dict,
    safe_eval_formula,
)
from ocr_utils import (
    OCR_URL, UmiOcrClient, box_mid_y, detect_enhance_marker_by_color,
    flatten_ocr_text, parse_detail_stats, parse_detail_stats_from_lines, parse_relic,
)
from overlay import CollapsibleFrame, Overlay
from scoring import (
    apply_conversion_formulas, apply_score_deltas, build_totals,
    compute_damage_base, expected_damage, expected_damage_crit_100,
    expected_damage_theoretical, score_relic_lines, score_to_grade,
    _is_percent_stat, _stat_weight,
)
from utils import (
    BASE_DIR, CONFIG_PATH, DEFAULT_DETAIL_REGION,
    DEFAULT_RELIC_REGION, clear_logs_on_startup, format_region, grab_region,
    is_admin, is_star_rail_active, log_stats, log_to_file,
    parse_region, set_window_clickthrough,
)

import requests

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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        if IMPORT_ERROR is not None:
            messagebox.showerror("缺少依赖", f"{IMPORT_ERROR}\n\n请在项目目录运行：pip install -r requirements.txt")
            raise SystemExit(1)
        # 启动时清空日志文件
        clear_logs_on_startup()
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
        # 多遗器记录模式：按 N 切换。开启后，每次空格识别的遗器加入候选池（按 slot 分组），
        # 再按 N 完成记录并计算最优组合。
        self._multi_relic_mode = False
        self._multi_relic_pool: dict[str, list[Relic]] = {}  # {slot: [relic, ...]}
        self._build_ui()
        self.load_config()
        self.bind_config_traces()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._start_keyboard_listener()
        # 确保 overlay 首次显示的是当前期望伤害，而不是默认的"等待记录"
        try:
            self.refresh_damage()
        except Exception as exc:
            self.log(f"初始化 refresh_damage 失败：{exc}")

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
        # 切换角色后重置标志位，让下次识别遗器时记录新角色信息到精简日志
        self._stats_character_logged = False
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
                elif getattr(key, "char", None) in ("n", "N"):
                    if is_star_rail_active():
                        self.after(0, self.toggle_multi_relic_mode)
            except Exception as exc:
                # Python 3 在 except 块结束时删除 as 变量，必须用默认参数捕获，
                # 否则 lambda 延迟执行时会 NameError。
                err = str(exc) or exc.__class__.__name__
                self.after(0, lambda: self.log(f"热键错误：{err}"))

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
            # Python 3 在 except 块结束时删除 as 变量，必须先转字符串再传给 lambda。
            err = str(exc) or exc.__class__.__name__
            log_to_file(f"ERROR: {err}", "ERROR")
            import traceback
            log_to_file(f"TRACEBACK:\n{traceback.format_exc()}", "ERROR")
            self.after(0, lambda: self.overlay.show_error(err))
            self.after(0, lambda: self.log(f"错误：{err}"))

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
        # 截图前隐藏所有 overlay（主窗口 + 遗器分数/调试窗口），避免 overlay 上的数字
        # （如遗器差值 -38）被 OCR 识别进基础面板，污染生命/攻击/防御/暴击等数值。
        self.overlay.withdraw()
        self.overlay._clear_windows(self.overlay.stat_windows)
        self.overlay._clear_windows(self.overlay.debug_windows)
        time.sleep(0.15)  # 等待窗口实际消失（withdraw/destroy 异步生效）
        image, _ = grab_region(self.detail_region.get())
        self.overlay.deiconify()
        self.after(0, lambda: self.show_preview(image, parse_region(self.detail_region.get())))
        lines = self.ocr.image_to_lines(image)
        text = flatten_ocr_text(lines)
        log_to_file(f"capture_base: raw_text=\n{text}", "DEBUG")
        # 优先用基于 box y 坐标的解析（不依赖 OCR 输出顺序，抗错位）
        stats = parse_detail_stats_from_lines(lines)
        log_to_file(f"capture_base: parsed stats - hp_base={stats.hp_base}, atk_base={stats.atk_base}, def_base={stats.def_base}, speed={stats.speed}, crit_rate={stats.crit_rate*100:.1f}%, crit_dmg={stats.crit_dmg*100:.1f}%", "DEBUG")
        if stats.atk_base <= 0:
            raise RuntimeError("未能解析攻击力基础值，请检查脱装备详情区域。")
        self.base_stats = stats
        # 同步到当前角色配置
        if self.current_character in self.character_configs:
            self.character_configs[self.current_character].base_stats = stats
        # 识别角色后记录到精简日志，并标记本次启动已记录（避免 capture_relic 重复记录）
        self._log_character_stats()
        self._stats_character_logged = True
        self.after(0, lambda: self.log(f"基础值已记录：攻击 {stats.atk_base}+{stats.atk_bonus}，暴击 {stats.crit_rate*100:.1f}%，爆伤 {stats.crit_dmg*100:.1f}%\nOCR:\n{text}"))
        if self.debug_mode.get():
            self.after(0, lambda: self.overlay.set_debug_overlay(None, None, stats=stats))
        # 重新评分并恢复遗器分数 overlay（如果有当前遗器）
        self.recalculate_all_relic_scores()
        self.refresh_damage()
        if self.current_relic_region and self.relics:
            latest_slot = None
            for slot in self.relics:
                if self.relics[slot].total_score > 0:
                    latest_slot = slot
                    break
            if latest_slot:
                self.after(0, lambda slot=latest_slot: self.overlay.set_stat_scores(self.current_relic_region, self.relics[slot]))
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
        """对缺失数值的词条名，裁剪右侧区域并重新 OCR（放大 3 倍提升小字识别）。

        性能优化：将所有缺失数值的词条名右侧区域合并成一张大图，
        一次性 OCR，避免对每个缺失词条都调用一次 OCR（原来 N 个缺失=N 次 OCR）。"""
        stat_names = ["生命值", "攻击力", "防御力", "速度", "暴击率", "暴击伤害",
                      "击破特攻", "效果命中", "效果抵抗",
                      "物理属性伤害提高", "火属性伤害提高", "冰属性伤害提高",
                      "雷属性伤害提高", "风属性伤害提高", "量子属性伤害提高", "虚数属性伤害提高"]
        augmented = list(lines)

        # 收集所有需要重新 OCR 的词条及其裁剪区域
        crops_info: list[tuple[str, float, tuple[int, int, int, int]]] = []  # (text, name_y, crop_box)
        for item in lines:
            text = str(item.get("text", "")).strip()
            box = item.get("box")
            if not box:
                continue
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
            crops_info.append((text, name_y, (crop_x1, crop_y1, crop_x2, crop_y2)))

        if not crops_info:
            return augmented

        # 将所有 crop 按垂直顺序拼成一张大图，每块高度固定（放大 3 倍后 = 36*3=108）
        # 块间留 4 像素白边避免文字粘连
        crop_h_px = 36  # crop_y2 - crop_y1 的近似值
        scale = 3
        gap = 4
        block_h = crop_h_px * scale + gap
        block_w = 250 * scale
        combined = Image.new("RGB", (block_w, block_h * len(crops_info)), (0, 0, 0))
        # 记录每个块在大图中的 y 起始位置，用于后续坐标映射
        block_offsets: list[tuple[str, float, tuple[int, int, int, int], int]] = []
        for idx, (text, name_y, crop_box) in enumerate(crops_info):
            crop = image.crop(crop_box)
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
            y_offset = idx * block_h
            combined.paste(crop, (0, y_offset))
            block_offsets.append((text, name_y, crop_box, y_offset))

        try:
            new_lines = self.ocr.image_to_lines(combined)
            log_to_file(f"_ocr_missing_values: batched {len(crops_info)} crops in 1 OCR, results={[nl.get('text') for nl in new_lines]}", "DEBUG")
            for nl in new_lines:
                nl_box = nl.get("box")
                if not nl_box:
                    continue
                # 大图中的 y 中点
                nl_mid_y = box_mid_y(nl_box)
                # 找到所属块
                best_block = None
                for text, name_y, crop_box, y_offset in block_offsets:
                    if y_offset <= nl_mid_y <= y_offset + block_h:
                        best_block = (text, name_y, crop_box, y_offset)
                        break
                if not best_block:
                    continue
                _text, name_y, crop_box, y_offset = best_block
                # 大图坐标 → 原图坐标：先减块偏移，再除以缩放，最后加 crop 偏移
                crop_x1, crop_y1, _cx2, _cy2 = crop_box
                nl["box"] = [[(p[0]) / scale + crop_x1,
                              (p[1] - y_offset) / scale + crop_y1] for p in nl_box]
                augmented.append(nl)
        except Exception as exc:
            log_to_file(f"_ocr_missing_values: batched re-OCR failed: {exc}", "ERROR")

        return augmented

    def capture_relic(self):
        self.after(0, self.overlay.clear_error)
        # 首次识别遗器时，从存档搬出角色信息记录到精简日志（避免用户每次都要重识别角色）
        if not getattr(self, "_stats_character_logged", False):
            self._log_character_stats()
            self._stats_character_logged = True
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
        # 已移除 _ocr_missing_values：该函数对每个缺数值的词条单独 OCR 一次，
        # 导致单次识别总耗时从 ~0.8s 增至 ~3.5s（+2.7s），且 RapidOCR 对小字的二次识别成功率低。
        # parse_relic 内部已用 y 坐标容差做跨行匹配，足以处理词条名和数值分行的情况。
        relic = parse_relic(lines)
        if relic.slot == "未知":
            # 精简日志：记录识别失败的主副词条，便于发给我调试
            main_str = f"{relic.main_name}{relic.main_value:g}{'%' if relic.main_name and _is_percent_stat(relic.main_name) else ''}" if relic.main_name else "无"
            subs_str = " ".join(f"{s.name}{s.value:g}{'%' if s.percent else ''}" for s in relic.subs)
            log_stats(f"[识别失败] 主:{main_str} | 副:{subs_str}")
            raise RuntimeError("未能识别遗器位置，请检查遗器区域。")

        # 多遗器记录模式：识别成功后加入候选池，不替换当前装备、不更新评分
        if self._multi_relic_mode:
            self._add_to_multi_relic_pool(relic)
            # 仍刷新 overlay 显示该遗器评分（不影响当前装备）
            cfg = self.current_config()
            score_relic_lines(self.base_stats, {relic.slot: relic}, self.buffs(), relic.slot, cfg)
            self.after(0, lambda: self.overlay.set_stat_scores(region, relic))
            if self.debug_mode.get():
                self.after(0, lambda: self.overlay.set_debug_overlay(region, relic))
            else:
                self.after(0, self.overlay.clear_debug)
            return

        old_relic = self.relics.get(relic.slot)
        if old_relic and self._is_same_relic(old_relic, relic):
            # OCR 数值在容差内，视为同一遗器，不更新评分，但仍刷新 overlay
            # 同步新 OCR 的 box 到 old_relic（配置加载的 old_relic.box 是 None）
            old_relic.main_box = relic.main_box
            for old_stat, new_stat in zip(old_relic.subs, relic.subs):
                old_stat.box = new_stat.box
            # 精简日志：同一遗器也记录，方便用户发送完整数据
            cfg = self.current_config()
            grade_name, _ = score_to_grade(old_relic.total_score)
            grade_str = f" [{grade_name}]" if grade_name else ""
            main_str = f"{old_relic.main_name}{old_relic.main_value:g}{'%' if old_relic.main_name and _is_percent_stat(old_relic.main_name) else ''}"
            subs_str = " ".join(
                f"{s.name}{s.value:g}{'%' if s.percent else ''}={s.score:.1f}"
                for s in old_relic.subs if _stat_weight(s.name, cfg) > 0
            )
            log_stats(f"[遗器·同] {old_relic.slot} 主:{main_str} | {subs_str} | 总分={old_relic.total_score:.2f}{grade_str}")
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

    def toggle_multi_relic_mode(self):
        """切换多遗器记录模式。
        - 未开启时按 N：开启模式，清空候选池，overlay 顶部显示提示
        - 开启时按 N：完成记录，计算最优组合并展示结果，退出模式"""
        if not self._multi_relic_mode:
            # 开启记录模式
            self._multi_relic_mode = True
            self._multi_relic_pool = {}
            count_text = "多遗器记录模式已开启（候选池已清空）"
            self.overlay.set_panel(line2=count_text)
            # 刷新 line1 显示当前期望伤害，避免仍是初始的"等待记录"
            self.refresh_damage()
            self.log(count_text + "。对每个 slot 的遗器按空格识别，再按 N 计算最优组合。")
            log_stats("[多遗器] 进入记录模式")
        else:
            # 完成记录，计算最优组合
            self._multi_relic_mode = False
            self._compute_best_combination()

    def _add_to_multi_relic_pool(self, relic: Relic):
        """将识别到的遗器加入多遗器候选池（仅多遗器模式下调用）。"""
        if relic.slot == "未知" or not relic.main_name:
            return
        pool = self._multi_relic_pool.setdefault(relic.slot, [])
        # 去重：同一件遗器（signature 相同）不重复加入
        sig = relic.signature()
        for existing in pool:
            if existing.signature() == sig:
                return
        pool.append(relic)
        # 在 overlay 显示当前候选池状态
        slot_counts = {s: len(lst) for s, lst in self._multi_relic_pool.items()}
        total = sum(slot_counts.values())
        status_parts = [f"{s}×{c}" for s, c in slot_counts.items() if c > 0]
        status_text = f"多遗器记录中（共{total}件）：{' '.join(status_parts)}"
        self.overlay.set_panel(line2=status_text)
        self.log(f"已记录 {relic.slot} 遗器（{relic.main_name}），候选池：{status_text}")

    def _compute_best_combination(self):
        """计算最优遗器组合并展示结果。"""
        if not self._multi_relic_pool:
            self.overlay.set_panel(line2="多遗器记录为空，未计算")
            self.log("多遗器记录模式结束：候选池为空")
            return
        cfg = self.current_config()
        try:
            from optimizer import compute_combination_score, score_combination, format_combo_report
            best_combo, best_dps, perfect_baseline = compute_combination_score(
                self.base_stats, self._multi_relic_pool, self.buffs(), cfg
            )
            if not best_combo:
                self.overlay.set_panel(line2="多遗器计算失败：无有效候选")
                self.log("多遗器计算失败：候选池中无有效遗器")
                return
            total_score = score_combination(self.base_stats, best_combo, self.buffs(), cfg, perfect_baseline)
            report = format_combo_report(best_combo, total_score, best_dps, cfg)
            # 展示在 overlay 第 2/3/4 行
            slot_summary = " ".join(f"{s}" for s in best_combo.keys())
            self.overlay.set_panel(
                line2=f"最优组合 总分={total_score:.1f} 期望DPS={best_dps:.0f}",
                line3=slot_summary,
                line4="详情见日志/弹窗"
            )
            self.log(f"多遗器计算完成：\n{report}")
            log_stats(f"[多遗器] 最优组合 总分={total_score:.2f} DPS={best_dps:.0f}\n{report}")
            # 弹窗显示完整报告
            messagebox.showinfo("最优遗器组合", report)
        except Exception as exc:
            err_msg = f"多遗器计算错误：{exc}"
            self.overlay.set_panel(line2=err_msg)
            self.log(err_msg)
            log_stats(f"[多遗器] 计算错误：{exc}")

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

    def _log_character_stats(self):
        """从存档搬出角色信息到精简日志（无需用户重新识别角色）。"""
        stats = self.base_stats
        ally = " ".join(f"{BUFF_LABELS.get(k,k)}={v.get()}" for k, v in self.ally_buff_vars.items() if parse_plus_expr(v.get()) != 0)
        self_buff = " ".join(f"{BUFF_LABELS.get(k,k)}={v.get()}" for k, v in self.self_buff_vars.items() if parse_plus_expr(v.get()) != 0)
        cfg = self.current_config()
        dmg_src = DAMAGE_SOURCES.get(cfg.damage_source, cfg.damage_source) if cfg else "?"
        log_stats(f"[角色] {self.current_character} | HP={stats.hp_base}+{stats.hp_bonus} ATK={stats.atk_base}+{stats.atk_bonus} DEF={stats.def_base}+{stats.def_bonus} SPD={stats.speed} 暴击={stats.crit_rate*100:.1f}% 爆伤={stats.crit_dmg*100:.1f}% 伤害源={dmg_src}")
        valid_subs = " ".join(cfg.valid_subs) if cfg and cfg.valid_subs else "(默认)"
        log_stats(f"  有效副词条: {valid_subs}")
        if ally:
            log_stats(f"  队友拐: {ally}")
        if self_buff:
            log_stats(f"  自拐: {self_buff}")

    def log_relic(self, slot, old_damage, new_damage, delta, text):
        relic = self.relics[slot]
        cfg = self.current_config()
        grade_name, _ = score_to_grade(relic.total_score)
        grade_str = f" [{grade_name}]" if grade_name else ""
        self.log(f"{slot} 已记录，期望伤害 {old_damage:,.0f} -> {new_damage:,.0f}，差值 {delta:+,.0f}，总分 {relic.total_score:.2f} ({relic.total_delta:+.2f}){grade_str}")
        for stat in relic.subs:
            valid = "有效" if _stat_weight(stat.name, cfg) > 0 else "无效"
            unit = "%" if stat.percent else ""
            self.log(f"  {stat.name} {stat.value:g}{unit}：{stat.score:.2f} 分，差值 {stat.delta:+.2f} ({valid})")
        self.log("OCR:\n" + text)
        # 精简日志：一行汇总slot+主词条+副词条+评分
        main_str = f"{relic.main_name}{relic.main_value:g}{'%' if relic.main_name and _is_percent_stat(relic.main_name) else ''}"
        subs_str = " ".join(
            f"{s.name}{s.value:g}{'%' if s.percent else ''}={s.score:.1f}"
            for s in relic.subs if _stat_weight(s.name, cfg) > 0
        )
        log_stats(f"[遗器] {slot} 主:{main_str} | {subs_str} | 总分={relic.total_score:.2f}{grade_str}")

    def log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        self.text.insert("end", f"[{stamp}] {msg}\n")
        self.text.see("end")



if __name__ == "__main__":
    from utils import enable_dpi_awareness, relaunch_as_admin
    enable_dpi_awareness()
    if not is_admin():
        relaunch_as_admin()
        raise SystemExit(0)
    App().mainloop()
