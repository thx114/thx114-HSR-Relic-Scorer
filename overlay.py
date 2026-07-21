"""Overlay 叠加层窗口：透明置顶 + 鼠标穿透 + 评分显示 + ACE 扫光动画。"""
from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

from models import CharacterStats, Relic
from ocr_utils import box_mid_y
from scoring import score_to_grade
from utils import format_score_delta, scaled_font, set_window_clickthrough


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
        # 总分中线在第一条副词条中线上方，间距按缩放系数动态计算（适配字体缩放）
        # 基础 160px（总分行 + 评级行 + 间隔），scale=1.5 时为 240px
        s = self._scale()
        total_to_sub_gap = int(160 * s)
        total_center = max(0, first_sub_center - total_to_sub_gap)
        # 评级中线相对总分的偏移也按缩放计算
        grade_offset = int(48 * s)
        if relic.total_score > 0 or relic.total_delta != 0:
            # "遗器总分：" 标签（白字，无视调试模式），左边与调试词条名同列对齐，与总分同中线
            self._static_label_window(nx, total_center, "遗器总分：")
            # 总分字体放大
            score_text = format_score_delta(relic.total_score, relic.total_delta)
            self._score_window(sx, total_center, score_text,
                               relic.total_delta, font_size=18)
            # 评级窗口：放在总分下一行，左对齐 nx，使用等级颜色
            grade_name, grade_color = score_to_grade(relic.total_score)
            if grade_name:
                grade_center = total_center + grade_offset
                if grade_name == "ACE":
                    # ACE 专用：3 字符独立 Label + 从左到右扫光渐变
                    self._ace_grade_window(nx, grade_center,
                                           scaled_font(22, self._scale(), bold=True))
                else:
                    self._text_window(nx, grade_center, grade_name, "#101820", grade_color,
                                      scaled_font(22, self._scale(), bold=True), 0.9, self.stat_windows)
            # 理论总分与评级同行，右对齐 sx（格式化后与实际分相同时隐藏）
            if relic.theoretical_total > 0 and round(relic.theoretical_total, 1) != round(relic.total_score, 1):
                self._score_window(sx, total_center + grade_offset, f"({relic.theoretical_total:.1f})", 0.0, font_size=14)
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

    def _static_label_window(self, x: int, center_y: int, text: str, font_size: int = 18) -> tk.Toplevel | None:
        """固定文本标签（白字），无视调试模式，与总分同字号对齐。"""
        return self._text_window(x, center_y, text, "#101820", "#ffffff",
                                 scaled_font(font_size, self._scale(), bold=True), 0.9, self.stat_windows)

    def _ace_grade_window(self, x: int, center_y: int, font_spec: tuple) -> tk.Toplevel:
        """ACE 评级专用窗口：将 A/C/E 拆成三个独立 Label 横向排列，
        便于实现从左到右的扫光渐变动画。"""
        bg = "#101820"
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.9)
        win.configure(bg=bg)
        win.withdraw()
        frame = tk.Frame(win, bg=bg)
        frame.pack()
        labels: list[tk.Label] = []
        for ch in ("A", "C", "E"):
            lbl = tk.Label(frame, text=ch, bg=bg, fg="#FF3030",
                           font=font_spec, padx=0, pady=2,
                           borderwidth=0, highlightthickness=0)
            lbl.pack(side=tk.LEFT)
            labels.append(lbl)
        win.update_idletasks()
        fw = frame.winfo_reqwidth()
        fh = frame.winfo_reqheight()
        win.geometry(f"{fw}x{fh}+{x}+{max(0, int(center_y - fh / 2))}")
        win.deiconify()
        set_window_clickthrough(win)
        self.stat_windows.append(win)
        self._start_ace_sweep(win, labels)
        return win

    def _start_ace_sweep(self, win: tk.Toplevel, labels: list[tk.Label]):
        """从左到右的扫光渐变：亮峰按 A→C→E 顺序移动。
        - amp = (1 + cos(2π(t - peak))) / 2，峰在 t=peak 处（amp=1）
        - A peak=0，C peak=1/3，E peak=2/3，周期内亮带从左流到右
        - 颜色在深红 #FF3030 ↔ 亮红 #FF9090 之间过渡（幅度小，不刺眼）
        - 周期 2.4 秒（慢），每帧 60ms
        - 不修改窗口 alpha，避免与 clickthrough 的 layered 样式冲突
        - 窗口销毁时自动停止"""
        base_rgb = (0xFF, 0x30, 0x30)   # 深红 #FF3030
        bright_rgb = (0xFF, 0x90, 0x90) # 亮红 #FF9090
        period_ms = 2400
        frame_ms = 60
        n = len(labels)
        start_ms: list[float | None] = [None]

        def tick():
            if not win.winfo_exists():
                return
            now = time.monotonic() * 1000.0
            if start_ms[0] is None:
                start_ms[0] = now
            t = ((now - start_ms[0]) % period_ms) / period_ms
            for i, lbl in enumerate(labels):
                if not lbl.winfo_exists():
                    return
                peak = i / n  # A=0, C=1/3, E=2/3
                amp = (1.0 + math.cos(2.0 * math.pi * (t - peak))) / 2.0  # 峰在 t=peak
                r = int(base_rgb[0] + (bright_rgb[0] - base_rgb[0]) * amp)
                g = int(base_rgb[1] + (bright_rgb[1] - base_rgb[1]) * amp)
                b = int(base_rgb[2] + (bright_rgb[2] - base_rgb[2]) * amp)
                try:
                    lbl.configure(fg=f"#{r:02X}{g:02X}{b:02X}")
                except Exception:
                    return
            win.after(frame_ms, tick)

        win.after(frame_ms, tick)

    def clear_debug(self):
        self._clear_windows(self.debug_windows)

    def _score_window(self, x: int, center_y: int, text: str, delta: float,
                      width: int = 8, font_size: int = 14) -> tk.Toplevel | None:
        color = "#65f2a5" if delta >= 0 else "#ff5b5b"
        return self._text_window(x, center_y, text, "#101820", color,
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
