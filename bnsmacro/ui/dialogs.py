"""Modal editors for conditions, actions and hotkeys."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Sequence, Tuple

from ..hotkey import HotkeyManager
from ..profile import (METRIC_CHANNEL, METRIC_DISTANCE, Action, Condition,
                       Hotkey, Probe, format_color, parse_color)
from .widgets import ColorSwatch

ACTION_TYPES = [
    ("key", "点按"),
    ("down", "按下不放"),
    ("up", "松开"),
    ("delay", "等待"),
    ("move", "移动鼠标(转视角)"),
]
ACTION_LABEL = dict(ACTION_TYPES)
ACTION_FROM_LABEL = {v: k for k, v in ACTION_TYPES}

METRIC_LABELS = [(METRIC_CHANNEL, "单通道容差"), (METRIC_DISTANCE, "RGB 距离")]
METRIC_LABEL = dict(METRIC_LABELS)
METRIC_FROM_LABEL = {v: k for k, v in METRIC_LABELS}


class _Modal(tk.Toplevel):
    def __init__(self, master, title: str):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.result = None
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self._buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        self._buttons.pack(fill="x")
        ttk.Button(self._buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(self._buttons, text="确定", command=self._accept).pack(
            side="right", padx=(0, 6))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._accept())

    def _accept(self) -> None:
        raise NotImplementedError

    def show(self):
        self.update_idletasks()
        parent = self.master.winfo_toplevel()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.wait_window(self)
        return self.result


class KeyCaptureButton(ttk.Button):
    """Press it, then press any key or mouse button to record that key."""

    def __init__(self, master, hotkeys: HotkeyManager,
                 on_key: Callable[[str], None], text: str = "按键捕获", **kw):
        super().__init__(master, text=text, command=self._arm, **kw)
        self._hotkeys = hotkeys
        self._on_key = on_key
        self._default_text = text
        self._armed = False

    def _arm(self) -> None:
        if self._armed:
            return self._disarm()
        self._armed = True
        self.configure(text="按任意键…")
        self._hotkeys.set_catch_all(self._captured)

    def _disarm(self) -> None:
        self._armed = False
        self.configure(text=self._default_text)
        self._hotkeys.clear_catch_all()

    def _captured(self, key_name: str) -> None:
        if not self._armed:
            return
        self._disarm()
        try:
            self.after(0, lambda: self._on_key(key_name))
        except tk.TclError:
            pass

    def destroy(self) -> None:  # noqa: D401 - tk override
        if self._armed:
            self._disarm()
        super().destroy()


class ConditionDialog(_Modal):
    def __init__(self, master, probes: Sequence[Probe],
                 condition: Optional[Condition] = None,
                 sample_color: Optional[Callable[[], Optional[Tuple[int, int, int]]]] = None):
        super().__init__(master, "取色条件")
        cond = condition or Condition(probe=probes[0].id if probes else "")
        self._probes = list(probes)
        self._sample_color = sample_color

        labels = [f"{p.id}  ({p.x},{p.y})  {p.note}".rstrip() for p in self._probes]
        self.var_probe = tk.StringVar()
        current = next((l for p, l in zip(self._probes, labels) if p.id == cond.probe),
                       labels[0] if labels else "")
        self.var_probe.set(current)
        self.var_color = tk.StringVar(value=cond.color)
        self.var_tol = tk.IntVar(value=cond.tolerance)
        self.var_metric = tk.StringVar(value=METRIC_LABEL.get(cond.metric, "单通道容差"))
        self.var_negate = tk.BooleanVar(value=cond.negate)

        grid = self.body
        ttk.Label(grid, text="取色点").grid(row=0, column=0, sticky="w", pady=3)
        combo = ttk.Combobox(grid, textvariable=self.var_probe, values=labels,
                             state="readonly", width=32)
        combo.grid(row=0, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(grid, text="目标颜色").grid(row=1, column=0, sticky="w", pady=3)
        entry = ttk.Entry(grid, textvariable=self.var_color, width=12)
        entry.grid(row=1, column=1, sticky="w", pady=3)
        self.swatch = ColorSwatch(grid, width=70, height=22)
        self.swatch.grid(row=1, column=2, sticky="w", padx=6)
        self.var_color.trace_add("write", lambda *_a: self._sync_swatch())
        self._sync_swatch()

        if sample_color is not None:
            ttk.Button(grid, text="取当前取色器的颜色", command=self._grab_current
                       ).grid(row=2, column=1, columnspan=2, sticky="we", pady=(0, 6))

        ttk.Label(grid, text="容差").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Spinbox(grid, from_=0, to=255, textvariable=self.var_tol, width=6
                    ).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Combobox(grid, textvariable=self.var_metric,
                     values=[l for _k, l in METRIC_LABELS], state="readonly", width=14
                     ).grid(row=3, column=2, sticky="w", padx=6)

        ttk.Checkbutton(grid, text="取反（颜色 不 匹配时才算成立）",
                        variable=self.var_negate
                        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _sync_swatch(self) -> None:
        try:
            self.swatch.set(parse_color(self.var_color.get()))
        except ValueError:
            pass

    def _grab_current(self) -> None:
        if not self._sample_color:
            return
        rgb = self._sample_color()
        if rgb:
            self.var_color.set(format_color(rgb))

    def _accept(self) -> None:
        label = self.var_probe.get()
        probe_id = label.split()[0] if label else ""
        try:
            color = format_color(parse_color(self.var_color.get()))
        except ValueError:
            from tkinter import messagebox
            messagebox.showerror("颜色格式不对", "请填 #RRGGBB，例如 #C83C28",
                                 parent=self)
            return
        self.result = Condition(
            probe=probe_id, color=color, tolerance=int(self.var_tol.get()),
            metric=METRIC_FROM_LABEL.get(self.var_metric.get(), METRIC_CHANNEL),
            negate=bool(self.var_negate.get()),
        )
        self.destroy()


class ActionDialog(_Modal):
    def __init__(self, master, hotkeys: HotkeyManager,
                 action: Optional[Action] = None):
        super().__init__(master, "按键动作")
        act = action or Action()
        self.var_type = tk.StringVar(value=ACTION_LABEL.get(act.type, "点按"))
        self.var_key = tk.StringVar(value=act.key)
        self.var_hold = tk.IntVar(value=act.hold_ms)
        self.var_ms = tk.IntVar(value=act.ms)
        self.var_dx = tk.IntVar(value=act.dx)
        self.var_dy = tk.IntVar(value=act.dy)
        self.var_ctrl = tk.BooleanVar(value="ctrl" in act.modifiers)
        self.var_shift = tk.BooleanVar(value="shift" in act.modifiers)
        self.var_alt = tk.BooleanVar(value="alt" in act.modifiers)

        grid = self.body
        ttk.Label(grid, text="类型").grid(row=0, column=0, sticky="w", pady=3)
        combo = ttk.Combobox(grid, textvariable=self.var_type,
                             values=[l for _k, l in ACTION_TYPES],
                             state="readonly", width=18)
        combo.grid(row=0, column=1, columnspan=2, sticky="w", pady=3)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_rows())

        self.row_key = ttk.Frame(grid)
        self.row_key.grid(row=1, column=0, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.row_key, text="按键").pack(side="left")
        ttk.Entry(self.row_key, textvariable=self.var_key, width=14).pack(
            side="left", padx=(8, 6))
        KeyCaptureButton(self.row_key, hotkeys,
                         lambda k: self.var_key.set(k)).pack(side="left")

        self.row_mods = ttk.Frame(grid)
        self.row_mods.grid(row=2, column=0, columnspan=3, sticky="we")
        ttk.Label(self.row_mods, text="组合键").pack(side="left")
        for text, var in (("Ctrl", self.var_ctrl), ("Shift", self.var_shift),
                          ("Alt", self.var_alt)):
            ttk.Checkbutton(self.row_mods, text=text, variable=var).pack(
                side="left", padx=(8, 0))

        self.row_hold = ttk.Frame(grid)
        self.row_hold.grid(row=3, column=0, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.row_hold, text="按住时长").pack(side="left")
        ttk.Spinbox(self.row_hold, from_=0, to=2000, textvariable=self.var_hold,
                    width=7).pack(side="left", padx=(8, 4))
        ttk.Label(self.row_hold, text="ms（0 = 用全局值）").pack(side="left")

        self.row_delay = ttk.Frame(grid)
        self.row_delay.grid(row=4, column=0, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.row_delay, text="等待").pack(side="left")
        ttk.Spinbox(self.row_delay, from_=0, to=60000, textvariable=self.var_ms,
                    width=7).pack(side="left", padx=(8, 4))
        ttk.Label(self.row_delay, text="ms").pack(side="left")

        self.row_move = ttk.Frame(grid)
        self.row_move.grid(row=5, column=0, columnspan=3, sticky="we", pady=3)
        ttk.Label(self.row_move, text="相对位移 X").pack(side="left")
        ttk.Spinbox(self.row_move, from_=-4000, to=4000, textvariable=self.var_dx,
                    width=7).pack(side="left", padx=(6, 10))
        ttk.Label(self.row_move, text="Y").pack(side="left")
        ttk.Spinbox(self.row_move, from_=-4000, to=4000, textvariable=self.var_dy,
                    width=7).pack(side="left", padx=(6, 0))

        self._sync_rows()

    def _sync_rows(self) -> None:
        kind = ACTION_FROM_LABEL.get(self.var_type.get(), "key")
        show = {
            "key": (True, True, True, False, False),
            "down": (True, True, False, False, False),
            "up": (True, True, False, False, False),
            "delay": (False, False, False, True, False),
            "move": (False, False, False, False, True),
        }[kind]
        for frame, visible in zip(
            (self.row_key, self.row_mods, self.row_hold, self.row_delay, self.row_move),
            show,
        ):
            frame.grid() if visible else frame.grid_remove()

    def _accept(self) -> None:
        kind = ACTION_FROM_LABEL.get(self.var_type.get(), "key")
        mods = [n for n, v in (("ctrl", self.var_ctrl), ("shift", self.var_shift),
                               ("alt", self.var_alt)) if v.get()]
        self.result = Action(
            type=kind, key=self.var_key.get().strip(),
            hold_ms=int(self.var_hold.get()), ms=int(self.var_ms.get()),
            dx=int(self.var_dx.get()), dy=int(self.var_dy.get()),
            modifiers=mods if kind in ("key", "down", "up") else [],
        )
        self.destroy()


class HotkeyDialog(_Modal):
    MODES = [("toggle", "自动（按一下开，再按一下关）"),
             ("hold", "长按（按住才生效）"),
             ("once", "单击（按一次跑一轮）")]
    MODE_LABEL = dict(MODES)
    MODE_FROM_LABEL = {v: k for k, v in MODES}

    def __init__(self, master, hotkeys: HotkeyManager, title: str,
                 value: Optional[Hotkey] = None, allow_modes: bool = True):
        super().__init__(master, title)
        hk = value or Hotkey()
        self.var_key = tk.StringVar(value=hk.key)
        self.var_mode = tk.StringVar(value=self.MODE_LABEL.get(hk.mode, self.MODES[0][1]))
        self.var_swallow = tk.BooleanVar(value=hk.swallow)
        self.var_enabled = tk.BooleanVar(value=hk.enabled)
        self.var_ctrl = tk.BooleanVar(value="ctrl" in hk.modifiers)
        self.var_shift = tk.BooleanVar(value="shift" in hk.modifiers)
        self.var_alt = tk.BooleanVar(value="alt" in hk.modifiers)

        grid = self.body
        row = ttk.Frame(grid); row.grid(row=0, column=0, sticky="we", pady=3)
        ttk.Label(row, text="按键").pack(side="left")
        ttk.Entry(row, textvariable=self.var_key, width=14).pack(side="left", padx=(8, 6))
        KeyCaptureButton(row, hotkeys, lambda k: self.var_key.set(k)).pack(side="left")

        row = ttk.Frame(grid); row.grid(row=1, column=0, sticky="we", pady=3)
        ttk.Label(row, text="组合键").pack(side="left")
        for text, var in (("Ctrl", self.var_ctrl), ("Shift", self.var_shift),
                          ("Alt", self.var_alt)):
            ttk.Checkbutton(row, text=text, variable=var).pack(side="left", padx=(8, 0))

        if allow_modes:
            row = ttk.Frame(grid); row.grid(row=2, column=0, sticky="we", pady=3)
            ttk.Label(row, text="方式").pack(side="left")
            ttk.Combobox(row, textvariable=self.var_mode,
                         values=[l for _k, l in self.MODES], state="readonly",
                         width=26).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(grid, text="启用", variable=self.var_enabled
                        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(grid, text="拦截该按键（不再传给游戏）",
                        variable=self.var_swallow
                        ).grid(row=4, column=0, sticky="w")

    def _accept(self) -> None:
        mods = [n for n, v in (("ctrl", self.var_ctrl), ("shift", self.var_shift),
                               ("alt", self.var_alt)) if v.get()]
        self.result = Hotkey(
            key=self.var_key.get().strip().lower(), modifiers=mods,
            mode=self.MODE_FROM_LABEL.get(self.var_mode.get(), "toggle"),
            swallow=bool(self.var_swallow.get()),
            enabled=bool(self.var_enabled.get()),
        )
        self.destroy()


class ProbeDialog(_Modal):
    def __init__(self, master, probe: Optional[Probe] = None,
                 taken_ids: Sequence[str] = ()):
        super().__init__(master, "取色点")
        p = probe or Probe(id="", x=0, y=0)
        self._original_id = p.id
        self._taken = set(taken_ids)
        self.var_id = tk.StringVar(value=p.id)
        self.var_x = tk.IntVar(value=p.x)
        self.var_y = tk.IntVar(value=p.y)
        self.var_note = tk.StringVar(value=p.note)

        grid = self.body
        for i, (label, var, width) in enumerate((
            ("ID", self.var_id, 12), ("X", self.var_x, 8),
            ("Y", self.var_y, 8), ("备注", self.var_note, 28),
        )):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(grid, textvariable=var, width=width).grid(
                row=i, column=1, sticky="w", pady=3)

    def _accept(self) -> None:
        from tkinter import messagebox
        probe_id = self.var_id.get().strip()
        if not probe_id:
            messagebox.showerror("缺少 ID", "取色点必须有一个 ID", parent=self)
            return
        if probe_id != self._original_id and probe_id in self._taken:
            messagebox.showerror("ID 重复", f"已经有一个叫 {probe_id} 的取色点了",
                                 parent=self)
            return
        self.result = Probe(id=probe_id, x=int(self.var_x.get()),
                            y=int(self.var_y.get()),
                            note=self.var_note.get().strip())
        self.destroy()


class ProbeLayoutDialog(_Modal):
    """Settings for 一键铺点.

    A hotbar is a row of evenly spaced icons, so picking every one of them by
    hand is pure busywork: take the first and the last, and the rest follow.
    """

    def __init__(self, master, next_id: str = "p1"):
        super().__init__(master, "一键铺点")
        self.var_count = tk.IntVar(value=8)
        self.var_prefix = tk.StringVar(value=next_id.rstrip("0123456789") or "p")
        self.var_note = tk.StringVar(value="技能格")

        grid = self.body
        ttk.Label(grid, text="数量").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Spinbox(grid, from_=2, to=40, textvariable=self.var_count, width=6
                    ).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(grid, text="个（含首尾）", foreground="#666666").grid(
            row=0, column=2, sticky="w", padx=6)

        ttk.Label(grid, text="ID 前缀").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.var_prefix, width=8).grid(
            row=1, column=1, sticky="w", pady=3)

        ttk.Label(grid, text="备注前缀").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.var_note, width=16).grid(
            row=2, column=1, columnspan=2, sticky="w", pady=3)

        ttk.Label(grid, justify="left", foreground="#666666", wraplength=330,
                  text="确定之后：把鼠标放到第一个图标中心按取色键，"
                       "再放到最后一个图标中心按一次，中间的点自动等距铺开。"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _accept(self) -> None:
        try:
            count = int(self.var_count.get())
        except (tk.TclError, ValueError):
            return
        if count < 2:
            from tkinter import messagebox
            messagebox.showerror("数量太少", "至少 2 个点才谈得上铺", parent=self)
            return
        self.result = {
            "count": count,
            "prefix": self.var_prefix.get().strip() or "p",
            "note": self.var_note.get().strip(),
        }
        self.destroy()
