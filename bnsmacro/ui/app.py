"""Main window."""
from __future__ import annotations

import json
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

from .. import __version__
from .. import window as win
from ..engine import LOG_ERROR, LOG_INFO, LOG_WARN, MacroEngine
from ..hotkey import Binding, HotkeyManager
from ..importer import import_legacy_folder
from ..profile import (Probe, Profile, Rule, default_profile, format_color)
from ..winapi import (begin_high_resolution_timer, enable_dpi_awareness,
                      end_high_resolution_timer)
from .dialogs import (ActionDialog, ConditionDialog, HotkeyDialog,
                      ProbeDialog, ProbeLayoutDialog)
from .picker import PickerPanel
from .widgets import ColorSwatch, ScrolledTree, hint, rgb_to_hex

PROFILE_DIR = Path.cwd() / "profiles"
REFRESH_MS = 120

HOTKEY_SLOTS = [
    ("toggle", "主开关", "开/关整个宏。"),
    ("hold", "长按触发", "设了它就得按住才动；留空则开了就一直跑。"),
    ("panic", "急停", "立刻停宏并松开所有按键。"),
]


class App(tk.Tk):
    def __init__(self, profile_dir: Path = PROFILE_DIR):
        super().__init__()
        self.title(f"剑灵取色宏 v{__version__}")
        self.geometry("1060x800")
        self.minsize(980, 700)

        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.profile_dir / "_state.json"
        self._dirty = False
        self._log_lines: List[Tuple[str, str]] = []
        self._log_lock = threading.Lock()
        self._layout: Optional[Dict] = None   # 一键铺点 in progress
        # Booting into an empty profile made the whole window look broken on
        # first run, with the template sitting unloaded in the dropdown.
        self.profile: Profile = self._startup_profile()

        self.hotkeys = HotkeyManager()
        self.hotkeys.on_error = lambda exc: self.log(f"热键回调出错: {exc}", LOG_ERROR)
        self.engine = MacroEngine(self.profile, log=self.log)

        self._build()
        self._load_profile_list()
        self._apply_profile_to_ui()

        try:
            self.hotkeys.start()
        except RuntimeError as exc:
            self.log(f"全局热键启动失败: {exc}", LOG_ERROR)
        self.engine.start()
        self._rebind_hotkeys()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(REFRESH_MS, self._refresh)
        self.log(f"就绪。配置目录: {self.profile_dir}", LOG_INFO)

    # ==================================================================
    # layout
    # ==================================================================
    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Run.TButton", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Head.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

        self._build_toolbar()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._build_tab_run()
        self._build_tab_probes()
        self._build_tab_rules()
        self._build_tab_settings()
        self._build_tab_help()
        self._build_statusbar()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8, 8, 6))
        bar.pack(fill="x")

        self.btn_run = ttk.Button(bar, text="启动", style="Run.TButton",
                                  command=self._toggle_macro, width=16)
        self.btn_run.pack(side="right")

        ttk.Label(bar, text="配置").pack(side="left")
        self.var_profile = tk.StringVar()
        self.cbo_profile = ttk.Combobox(bar, textvariable=self.var_profile,
                                        state="readonly", width=22)
        self.cbo_profile.pack(side="left", padx=(6, 8))
        self.cbo_profile.bind("<<ComboboxSelected>>",
                              lambda _e: self._open_selected_profile())

        for text, cmd in (("新建", self._new_profile), ("保存", self._save_profile),
                          ("另存为", self._save_profile_as),
                          ("导入旧宏", self._import_legacy),
                          ("打开目录", self._open_folder)):
            ttk.Button(bar, text=text, command=cmd, width=7).pack(side="left", padx=2)


    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 2, 10, 6))
        bar.pack(fill="x")
        self.var_status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_status, foreground="#555555").pack(side="left")

    # -- run tab -------------------------------------------------------
    def _build_tab_run(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  运行  ")

        info = ttk.LabelFrame(tab, text="状态", padding=10)
        info.pack(fill="x")
        self.var_state = tk.StringVar(value="已停止")
        self.var_window = tk.StringVar(value="窗口: -")
        self.var_perf = tk.StringVar(value="取色频率: -")
        self.var_fired = tk.StringVar(value="触发: 0 次")
        self.lbl_state = ttk.Label(info, textvariable=self.var_state,
                                   font=("Microsoft YaHei UI", 14, "bold"))
        self.lbl_state.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 24))
        ttk.Label(info, textvariable=self.var_window).grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.var_perf).grid(row=1, column=1, sticky="w")
        ttk.Label(info, textvariable=self.var_fired).grid(row=0, column=2, sticky="w",
                                                          padx=(24, 0))
        self.var_lastrule = tk.StringVar(value="最近规则: -")
        ttk.Label(info, textvariable=self.var_lastrule).grid(row=1, column=2, sticky="w",
                                                             padx=(24, 0))

        live = ttk.LabelFrame(tab, text="取色点实时颜色", padding=(10, 6))
        live.pack(fill="x", pady=(10, 0))
        self.live_host = ttk.Frame(live)
        self.live_host.pack(fill="x")
        self._live_rows: Dict[str, Tuple[ttk.Label, ColorSwatch, ttk.Label]] = {}

        logf = ttk.LabelFrame(tab, text="日志", padding=(6, 4))
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.txt_log = tk.Text(logf, height=10, wrap="none", state="disabled",
                               font=("Consolas", 9), background="#fbfbfb",
                               relief="flat")
        scroll = ttk.Scrollbar(logf, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.txt_log.tag_configure(LOG_ERROR, foreground="#c0392b")
        self.txt_log.tag_configure(LOG_WARN, foreground="#b9770e")
        self.txt_log.tag_configure(LOG_INFO, foreground="#2c3e50")

    # -- probes tab ----------------------------------------------------
    def _build_tab_probes(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  取色点  ")

        self.picker = PickerPanel(tab, self.hotkeys, self._on_picked, padding=(4, 2))
        self.picker.pack(fill="x")

        hint(tab, "取色点保存的是「相对游戏窗口客户区」的坐标，所以挪动窗口不会失效；"
                  "换分辨率时会按比例缩放。想用绝对屏幕坐标就去「设置」里改锚点方式。"
             ).pack(fill="x", pady=(8, 4))

        self.tree_probes = ScrolledTree(tab, columns=[
            ("id", "ID", 70, "w"), ("x", "X", 70, "e"), ("y", "Y", 70, "e"),
            ("color", "当前颜色", 110, "w"), ("note", "备注", 300, "w"),
        ], height=9)
        self.tree_probes.pack(fill="both", expand=True)
        self.tree_probes.tree.bind("<Double-1>", lambda _e: self._edit_probe())

        row = ttk.Frame(tab); row.pack(fill="x", pady=(6, 0))
        for text, cmd in (("新增", self._add_probe), ("编辑", self._edit_probe),
                          ("删除", self._delete_probe)):
            ttk.Button(row, text=text, command=cmd, width=9).pack(side="left", padx=(0, 5))
        ttk.Button(row, text="一键铺点", command=self._start_layout, width=10
                   ).pack(side="left", padx=(8, 5))
        ttk.Button(row, text="清空", command=self._clear_probes, width=7
                   ).pack(side="left")
        self.var_probe_hint = tk.StringVar(
            value="取色器开启时按取色键直接新增一个点")
        ttk.Label(row, textvariable=self.var_probe_hint,
                  foreground="#666666").pack(side="left", padx=10)

    # -- rules tab -----------------------------------------------------
    def _build_tab_rules(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  规则  ")

        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        paned.add(left, weight=1)
        ttk.Label(left, text="规则按优先级从高到低判断，命中一条就执行并结束本轮。",
                  foreground="#666666", wraplength=300, justify="left").pack(
            fill="x", pady=(0, 6))
        self.tree_rules = ScrolledTree(left, columns=[
            ("on", "启用", 46, "center"), ("pri", "优先", 48, "e"),
            ("name", "名称", 150, "w"),
        ], height=16)
        self.tree_rules.pack(fill="both", expand=True)
        self.tree_rules.tree.bind("<<TreeviewSelect>>", lambda _e: self._load_rule_editor())
        self.tree_rules.tree.bind("<Double-1>", lambda _e: self._toggle_rule_enabled())

        row = ttk.Frame(left); row.pack(fill="x", pady=(6, 0))
        for text, cmd in (("新增", self._add_rule), ("复制", self._copy_rule),
                          ("删除", self._delete_rule)):
            ttk.Button(row, text=text, command=cmd, width=6).pack(side="left", padx=(0, 3))
        ttk.Button(row, text="↑", width=2,
                   command=lambda: self._move_rule(-1)).pack(side="left")
        ttk.Button(row, text="↓", width=2,
                   command=lambda: self._move_rule(1)).pack(side="left", padx=2)

        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self._build_rule_editor(right)

    def _build_rule_editor(self, parent) -> None:
        head = ttk.LabelFrame(parent, text="规则", padding=10)
        head.pack(fill="x")
        self.var_rule_name = tk.StringVar()
        self.var_rule_enabled = tk.BooleanVar(value=True)
        self.var_rule_pri = tk.IntVar(value=0)
        self.var_rule_cd = tk.IntVar(value=0)
        self.var_rule_match = tk.StringVar(value="全部满足")

        ttk.Label(head, text="名称").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(head, textvariable=self.var_rule_name, width=26).grid(
            row=0, column=1, sticky="w", pady=3)
        ttk.Checkbutton(head, text="启用", variable=self.var_rule_enabled,
                        command=self._commit_rule_head).grid(row=0, column=2,
                                                             sticky="w", padx=(12, 0))

        ttk.Label(head, text="优先级").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(head, from_=-999, to=999, textvariable=self.var_rule_pri, width=8,
                    command=self._commit_rule_head).grid(row=1, column=1, sticky="w")
        ttk.Label(head, text="越大越先判断", foreground="#666666").grid(
            row=1, column=2, sticky="w", padx=(12, 0))

        ttk.Label(head, text="冷却").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Spinbox(head, from_=0, to=60000, textvariable=self.var_rule_cd, width=8,
                    command=self._commit_rule_head).grid(row=2, column=1, sticky="w")
        ttk.Label(head, text="ms，触发后至少歇这么久", foreground="#666666",
                  wraplength=150, justify="left").grid(row=2, column=2, sticky="w",
                                                       padx=(12, 0))

        ttk.Label(head, text="条件关系").grid(row=3, column=0, sticky="w", pady=3)
        cbo = ttk.Combobox(head, textvariable=self.var_rule_match,
                           values=["全部满足", "任一满足"], state="readonly", width=10)
        cbo.grid(row=3, column=1, sticky="w")
        cbo.bind("<<ComboboxSelected>>", lambda _e: self._commit_rule_head())
        for var in (self.var_rule_name,):
            var.trace_add("write", lambda *_a: self._commit_rule_head())

        conds = ttk.LabelFrame(parent, text="取色条件", padding=(8, 6))
        conds.pack(fill="both", expand=True, pady=(10, 0))
        self.tree_conds = ScrolledTree(conds, columns=[
            ("probe", "取色点", 80, "w"), ("color", "颜色", 90, "w"),
            ("tol", "容差", 55, "e"), ("neg", "取反", 50, "center"),
        ], height=5)
        self.tree_conds.pack(fill="both", expand=True)
        self.tree_conds.tree.bind("<Double-1>", lambda _e: self._edit_condition())
        row = ttk.Frame(conds); row.pack(fill="x", pady=(6, 0))
        for text, cmd in (("新增", self._add_condition), ("编辑", self._edit_condition),
                          ("删除", self._delete_condition)):
            ttk.Button(row, text=text, command=cmd, width=6).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="学色(选中)", command=self._learn_selected_color,
                   width=11).pack(side="left", padx=(8, 4))
        ttk.Button(row, text="学色(整条)", command=self._learn_rule_colors,
                   width=11).pack(side="left")

        acts = ttk.LabelFrame(parent, text="按键动作（自上而下依次执行）", padding=(8, 6))
        acts.pack(fill="both", expand=True, pady=(10, 0))
        self.tree_acts = ScrolledTree(acts, columns=[
            ("step", "#", 34, "e"), ("desc", "动作", 300, "w"),
        ], height=5)
        self.tree_acts.pack(fill="both", expand=True)
        self.tree_acts.tree.bind("<Double-1>", lambda _e: self._edit_action())
        row = ttk.Frame(acts); row.pack(fill="x", pady=(6, 0))
        for text, cmd in (("新增", self._add_action), ("编辑", self._edit_action),
                          ("删除", self._delete_action)):
            ttk.Button(row, text=text, command=cmd, width=8).pack(side="left", padx=(0, 5))
        ttk.Button(row, text="↑", width=3,
                   command=lambda: self._move_action(-1)).pack(side="left")
        ttk.Button(row, text="↓", width=3,
                   command=lambda: self._move_action(1)).pack(side="left", padx=2)

    # -- settings tab --------------------------------------------------
    def _build_tab_settings(self) -> None:
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="  设置  ")

        hk = ttk.LabelFrame(tab, text="全局热键", padding=10)
        hk.pack(fill="x")
        self._hotkey_labels: Dict[str, tk.StringVar] = {}
        for i, (slot, title, desc) in enumerate(HOTKEY_SLOTS):
            ttk.Label(hk, text=title, style="Head.TLabel").grid(row=i, column=0,
                                                                sticky="w", pady=4)
            var = tk.StringVar(value="未设置")
            self._hotkey_labels[slot] = var
            ttk.Label(hk, textvariable=var, width=22,
                      font=("Consolas", 10)).grid(row=i, column=1, sticky="w", padx=10)
            ttk.Button(hk, text="修改", width=7,
                       command=lambda s=slot, t=title: self._edit_hotkey(s, t)
                       ).grid(row=i, column=2)
            ttk.Button(hk, text="清除", width=7,
                       command=lambda s=slot: self._clear_hotkey(s)
                       ).grid(row=i, column=3, padx=4)
            ttk.Label(hk, text=desc, foreground="#666666").grid(row=i, column=4,
                                                                sticky="w", padx=10)

        tm = ttk.LabelFrame(tab, text="节奏", padding=10)
        tm.pack(fill="x", pady=(10, 0))
        self.var_tick = tk.IntVar(value=10)
        self.var_hold = tk.IntVar(value=30)
        self.var_interval = tk.IntVar(value=40)
        self.var_jitter = tk.IntVar(value=0)
        rows = [
            ("取色间隔", self.var_tick, "ms，实际不会快过显示器刷新率"),
            ("默认按住时长", self.var_hold, "ms，太短游戏可能吃不到按键"),
            ("按键最小间隔", self.var_interval, "ms，两次按键之间至少隔这么久"),
            ("随机抖动", self.var_jitter, "ms，让节奏不那么机械（0 = 关闭）"),
        ]
        for i, (label, var, desc) in enumerate(rows):
            ttk.Label(tm, text=label).grid(row=i, column=0, sticky="w", pady=3)
            sp = ttk.Spinbox(tm, from_=0, to=5000, textvariable=var, width=8,
                             command=self._commit_settings)
            sp.grid(row=i, column=1, sticky="w", padx=8)
            sp.bind("<FocusOut>", lambda _e: self._commit_settings())
            ttk.Label(tm, text=desc, foreground="#666666").grid(row=i, column=2,
                                                                sticky="w")

        wd = ttk.LabelFrame(tab, text="游戏窗口", padding=10)
        wd.pack(fill="x", pady=(10, 0))
        self.var_wtitle = tk.StringVar()
        self.var_anchor = tk.StringVar(value="跟随窗口")
        self.var_capture = tk.StringVar(value="桌面截屏(快)")
        self.var_fg = tk.BooleanVar(value=True)
        self.var_scale = tk.BooleanVar(value=True)

        ttk.Label(wd, text="窗口标题包含").grid(row=0, column=0, sticky="w", pady=3)
        self.cbo_window = ttk.Combobox(wd, textvariable=self.var_wtitle, width=38)
        self.cbo_window.grid(row=0, column=1, sticky="w", padx=8)
        self.cbo_window.bind("<<ComboboxSelected>>", lambda _e: self._commit_settings())
        self.cbo_window.bind("<FocusOut>", lambda _e: self._commit_settings())
        ttk.Button(wd, text="刷新列表", width=10,
                   command=self._refresh_windows).grid(row=0, column=2)

        ttk.Label(wd, text="坐标锚点").grid(row=1, column=0, sticky="w", pady=3)
        cbo = ttk.Combobox(wd, textvariable=self.var_anchor,
                           values=["跟随窗口", "绝对屏幕坐标"], state="readonly", width=16)
        cbo.grid(row=1, column=1, sticky="w", padx=8)
        cbo.bind("<<ComboboxSelected>>", lambda _e: self._commit_settings())

        ttk.Label(wd, text="截屏方式").grid(row=2, column=0, sticky="w", pady=3)
        cbo = ttk.Combobox(wd, textvariable=self.var_capture,
                           values=["桌面截屏(快)", "窗口截屏(可被遮挡)"],
                           state="readonly", width=20)
        cbo.grid(row=2, column=1, sticky="w", padx=8)
        cbo.bind("<<ComboboxSelected>>", lambda _e: self._commit_settings())

        ttk.Checkbutton(wd, text="只在游戏是前台窗口时运行", variable=self.var_fg,
                        command=self._commit_settings).grid(row=3, column=0,
                                                            columnspan=2, sticky="w",
                                                            pady=(8, 0))
        ttk.Checkbutton(wd, text="分辨率变化时按比例缩放坐标", variable=self.var_scale,
                        command=self._commit_settings).grid(row=4, column=0,
                                                            columnspan=2, sticky="w")
        self.var_refsize = tk.StringVar(value="")
        ttk.Label(wd, textvariable=self.var_refsize, foreground="#666666").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(wd, text="把当前窗口尺寸设为基准", width=24,
                   command=self._set_reference_size).grid(row=6, column=0,
                                                          columnspan=2, sticky="w",
                                                          pady=(4, 0))

    def _build_tab_help(self) -> None:
        tab = ttk.Frame(self.nb, padding=14)
        self.nb.add(tab, text="  说明  ")
        text = tk.Text(tab, wrap="word", relief="flat", background="#fbfbfb",
                       font=("Microsoft YaHei UI", 10), padx=10, pady=10)
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

    # ==================================================================
    # profile plumbing
    # ==================================================================
    def _read_state(self) -> Dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, **kw) -> None:
        state = self._read_state()
        state.update(kw)
        try:
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _profile_files(self) -> List[Path]:
        """Every profile in the folder, real ones before templates."""
        files = [f for f in sorted(self.profile_dir.glob("*.json"))
                 if not f.name.startswith(("_", "."))]
        return (sorted(f for f in files if ".template." not in f.name)
                + sorted(f for f in files if ".template." in f.name))

    def _startup_profile(self) -> Profile:
        """Last thing you had open, else any profile, else the skeleton."""
        candidates: List[Path] = []
        last = self._read_state().get("last_profile")
        if last:
            candidates.append(self.profile_dir / str(last))
        candidates += self._profile_files()
        for path in candidates:
            if not path.is_file():
                continue
            try:
                profile = Profile.load(path)
            except Exception as exc:
                self.log(f"配置 {path.name} 读不了: {exc}", LOG_WARN)
                continue
            self.log(f"已加载配置「{profile.name}」（{path.name}）", LOG_INFO)
            return profile
        self.log("配置目录是空的，开了一份新配置", LOG_INFO)
        return default_profile()

    def _load_profile_list(self) -> None:
        files = [f.name for f in self._profile_files()]
        self.cbo_profile["values"] = files
        if self.profile.path and self.profile.path.name in files:
            self.var_profile.set(self.profile.path.name)

    def _open_selected_profile(self) -> None:
        name = self.var_profile.get()
        if not name:
            return
        if not self._confirm_discard():
            self._load_profile_list()
            return
        try:
            self.profile = Profile.load(self.profile_dir / name)
        except Exception as exc:
            messagebox.showerror("打不开", f"{name}\n\n{exc}", parent=self)
            return
        self._dirty = False
        self._write_state(last_profile=name)
        self.engine.set_profile(self.profile)
        self._apply_profile_to_ui()
        self._rebind_hotkeys()
        self.log(f"已加载配置「{self.profile.name}」", LOG_INFO)

    def _new_profile(self) -> None:
        if not self._confirm_discard():
            return
        self.profile = default_profile()
        self._dirty = True
        self.engine.set_profile(self.profile)
        self._apply_profile_to_ui()
        self._rebind_hotkeys()

    def _save_profile(self) -> None:
        if self.profile.path is None:
            return self._save_profile_as()
        self.profile.save()
        self._dirty = False
        self._write_state(last_profile=self.profile.path.name)
        self._load_profile_list()
        self.log(f"已保存 {self.profile.path.name}", LOG_INFO)

    def _save_profile_as(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="另存为", defaultextension=".json",
            initialdir=str(self.profile_dir),
            initialfile=f"{self.profile.name}.json",
            filetypes=[("配置文件", "*.json")])
        if not path:
            return
        self.profile.save(Path(path))
        self._dirty = False
        self._write_state(last_profile=Path(path).name)
        self._load_profile_list()
        self.var_profile.set(Path(path).name)
        self.log(f"已保存 {Path(path).name}", LOG_INFO)

    def _import_legacy(self) -> None:
        folder = filedialog.askdirectory(
            parent=self, title="选择旧宏的 data 文件夹（里面有 settings.json）")
        if not folder:
            return
        try:
            profile, notes = import_legacy_folder(Path(folder))
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self.profile = profile
        self._dirty = True
        self.engine.set_profile(self.profile)
        self._apply_profile_to_ui()
        self._rebind_hotkeys()
        for note in notes:
            self.log(f"导入: {note}", LOG_INFO)
        messagebox.showinfo(
            "导入完成",
            "已把能对应上的设置搬过来了：\n\n" + "\n".join(f"· {n}" for n in notes) +
            "\n\n取色点和规则需要你自己重新配 —— 旧宏的颜色数据不在配置文件里。",
            parent=self)

    def _open_folder(self) -> None:
        import os
        os.startfile(self.profile_dir)  # noqa: S606 - explicit user action

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = messagebox.askyesnocancel(
            "还没保存", "当前配置有改动，要先保存吗？", parent=self)
        if answer is None:
            return False
        if answer:
            self._save_profile()
        return True

    def _mark_dirty(self) -> None:
        self._dirty = True

    # ==================================================================
    # profile -> ui
    # ==================================================================
    def _apply_profile_to_ui(self) -> None:
        s = self.profile.settings
        self.var_tick.set(s.tick_ms)
        self.var_hold.set(s.key_hold_ms)
        self.var_interval.set(s.key_interval_ms)
        self.var_jitter.set(s.jitter_ms)
        self.var_wtitle.set(s.window_title)
        self.var_anchor.set("绝对屏幕坐标" if s.anchor_mode == "screen" else "跟随窗口")
        self.var_capture.set("窗口截屏(可被遮挡)" if s.capture_mode == "window"
                             else "桌面截屏(快)")
        self.var_fg.set(s.require_foreground)
        self.var_scale.set(s.scale_with_resolution)
        self._sync_refsize_label()
        self._refresh_hotkey_labels()
        self._refresh_probe_tree()
        self._refresh_rule_tree()
        self._rebuild_live_rows()

    def _sync_refsize_label(self) -> None:
        rw, rh = self.profile.settings.reference_size
        self.var_refsize.set(
            f"基准分辨率: {rw}×{rh}" if rw and rh
            else "基准分辨率: 未设置（不缩放，坐标按原样使用）")

    def _refresh_hotkey_labels(self) -> None:
        for slot, var in self._hotkey_labels.items():
            hk = self.profile.hotkeys.get(slot)
            var.set(hk.describe() if hk and hk.key else "未设置")
        toggle = self.profile.hotkeys.get("toggle")
        label = toggle.describe() if toggle and toggle.key else "未设置"
        base = "停止" if self.engine.armed else "启动"
        self.btn_run.configure(text=f"{base}  ({label})")

    def _refresh_probe_tree(self) -> None:
        tree = self.tree_probes.tree
        selected = self.tree_probes.selected()
        self.tree_probes.clear()
        for probe in self.profile.probes:
            tree.insert("", "end", iid=probe.id,
                        values=(probe.id, probe.x, probe.y, "-", probe.note))
        if selected and tree.exists(selected):
            tree.selection_set(selected)

    def _refresh_rule_tree(self) -> None:
        tree = self.tree_rules.tree
        selected = self.tree_rules.selected()
        self.tree_rules.clear()
        for rule in self.profile.sorted_rules():
            tree.insert("", "end", iid=rule.id,
                        values=("✓" if rule.enabled else "", rule.priority,
                                rule.name or rule.id))
        if selected and tree.exists(selected):
            tree.selection_set(selected)
        elif self.profile.rules:
            tree.selection_set(self.profile.sorted_rules()[0].id)
        self._load_rule_editor()

    def _rebuild_live_rows(self) -> None:
        for child in self.live_host.winfo_children():
            child.destroy()
        self._live_rows.clear()
        if not self.profile.probes:
            ttk.Label(self.live_host, text="还没有取色点 —— 去「取色点」标签页加几个。",
                      foreground="#888888").pack(anchor="w")
            return
        for i, probe in enumerate(self.profile.probes):
            row = ttk.Frame(self.live_host)
            row.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 24), pady=2)
            name = ttk.Label(row, text=f"{probe.id}", width=5,
                             font=("Consolas", 9, "bold"))
            name.pack(side="left")
            swatch = ColorSwatch(row, width=80, height=18)
            swatch.pack(side="left", padx=4)
            note = ttk.Label(row, text=probe.note or "", foreground="#666666", width=14)
            note.pack(side="left")
            self._live_rows[probe.id] = (name, swatch, note)

    # ==================================================================
    # ui -> profile
    # ==================================================================
    def _commit_settings(self) -> None:
        s = self.profile.settings
        try:
            s.tick_ms = max(0, int(self.var_tick.get()))
            s.key_hold_ms = max(0, int(self.var_hold.get()))
            s.key_interval_ms = max(0, int(self.var_interval.get()))
            s.jitter_ms = max(0, int(self.var_jitter.get()))
        except (tk.TclError, ValueError):
            return
        s.window_title = self.var_wtitle.get().strip()
        s.anchor_mode = "screen" if self.var_anchor.get() == "绝对屏幕坐标" else "client"
        s.capture_mode = "window" if self.var_capture.get().startswith("窗口") else "desktop"
        s.require_foreground = bool(self.var_fg.get())
        s.scale_with_resolution = bool(self.var_scale.get())
        self.engine.set_profile(self.profile)
        self._mark_dirty()

    def _refresh_windows(self) -> None:
        titles = [w.title for w in win.list_windows()]
        self.cbo_window["values"] = titles
        guess = win.find_game_window(self.var_wtitle.get())
        if guess and not self.var_wtitle.get():
            self.var_wtitle.set(guess.title)
            self._commit_settings()
        self.log(f"找到 {len(titles)} 个窗口", LOG_INFO)

    def _set_reference_size(self) -> None:
        target = win.find_game_window(self.profile.settings.window_title,
                                      self.profile.settings.window_class)
        if not target:
            messagebox.showwarning("没找到窗口",
                                   "先在上面填好窗口标题，并确保游戏已经开着。",
                                   parent=self)
            return
        self.profile.settings.reference_size = [target.client.width, target.client.height]
        self.profile.settings.window_class = target.class_name
        self._sync_refsize_label()
        self.engine.set_profile(self.profile)
        self._mark_dirty()
        self.log(f"基准分辨率设为 {target.client.width}×{target.client.height}", LOG_INFO)

    # -- hotkeys -------------------------------------------------------
    def _edit_hotkey(self, slot: str, title: str) -> None:
        current = self.profile.hotkeys.get(slot)
        allow_modes = slot == "toggle"
        result = HotkeyDialog(self, self.hotkeys, f"设置热键 — {title}",
                              current, allow_modes=allow_modes).show()
        if result is None:
            return
        if slot == "hold":
            result.mode = "hold"
        elif slot == "panic":
            result.mode = "press"
        self.profile.hotkeys[slot] = result
        self._mark_dirty()
        self._refresh_hotkey_labels()
        self._rebind_hotkeys()

    def _clear_hotkey(self, slot: str) -> None:
        self.profile.hotkeys.pop(slot, None)
        self._mark_dirty()
        self._refresh_hotkey_labels()
        self._rebind_hotkeys()

    def _rebind_hotkeys(self) -> None:
        for name in ("toggle", "hold", "panic"):
            self.hotkeys.remove(name)
        handlers = {
            # the toggle key owns the master switch; the hold key owns only
            # the trigger.  They must never write the same flag.
            "toggle": (self.engine.toggle, None),
            "hold": (lambda: self.engine.set_trigger(True),
                     lambda: self.engine.set_trigger(False)),
            "panic": (self._panic, None),
        }
        hold = self.profile.hotkeys.get("hold")
        self.engine.set_requires_trigger(bool(hold and hold.key and hold.enabled))
        for slot, (on_press, on_release) in handlers.items():
            hk = self.profile.hotkeys.get(slot)
            if not hk or not hk.key or not hk.enabled:
                continue
            mode = "hold" if slot == "hold" else hk.mode
            self.hotkeys.set(Binding(
                name=slot, key=hk.key, modifiers=hk.modifiers,
                mode=mode, swallow=hk.swallow,
                on_press=on_press, on_release=on_release,
            ))

    def _panic(self) -> None:
        self.engine.set_active(False)
        self.log("急停", LOG_WARN)

    def _toggle_macro(self) -> None:
        problems = self.profile.problems()
        if not self.engine.armed and problems:
            if not messagebox.askokcancel(
                    "配置有问题", "\n".join(problems) + "\n\n还是要启动吗？", parent=self):
                return
        self.engine.toggle()

    # -- probes --------------------------------------------------------
    def _on_picked(self, x: int, y: int, rgb: Tuple[int, int, int]) -> None:
        """Picker hotkey fired: store a new probe, or collect a 铺点 corner."""
        px, py = self._to_profile_coords(x, y)
        if self._layout is not None:
            return self._layout_pick(px, py)

        probe = Probe(id=self.profile.next_probe_id(), x=px, y=py,
                      note=format_color(rgb))
        self.profile.probes.append(probe)
        self._after_probe_change()
        self.log(f"新增取色点 {probe.id} @ ({px},{py}) {format_color(rgb)}", LOG_INFO)

    def _to_profile_coords(self, x: int, y: int) -> Tuple[int, int]:
        """Screen point -> the coordinate space the profile stores."""
        target = win.find_game_window(self.profile.settings.window_title,
                                      self.profile.settings.window_class)
        anchor = win.Anchor(self.profile.settings.anchor_mode,
                            self.profile.reference_size(),
                            self.profile.settings.scale_with_resolution)
        anchor.bind(target)
        if (self.profile.settings.anchor_mode == "client" and target
                and not target.client.contains(x, y)):
            self.log("取的点不在游戏窗口里，已按绝对坐标记下", LOG_WARN)
            px, py = x, y
        else:
            px, py = anchor.to_profile(x, y)
        if (target and self.profile.settings.scale_with_resolution
                and not self.profile.reference_size()):
            self.profile.settings.reference_size = [target.client.width,
                                                    target.client.height]
            self.profile.settings.window_class = target.class_name
            if not self.profile.settings.window_title:
                self.profile.settings.window_title = target.title
            self._sync_refsize_label()
            self.var_wtitle.set(self.profile.settings.window_title)
        return px, py

    # -- 一键铺点 ------------------------------------------------------
    def _start_layout(self) -> None:
        options = ProbeLayoutDialog(self, self.profile.next_probe_id()).show()
        if not options:
            return
        options["points"] = []
        self._layout = options
        if not self.picker.running:
            self.picker.start()
        self.var_probe_hint.set("铺点中：对准【第一个】图标按取色键")
        self.log(f"一键铺点：准备生成 {options['count']} 个点", LOG_INFO)

    def _layout_pick(self, x: int, y: int) -> None:
        assert self._layout is not None
        points = self._layout["points"]
        points.append((x, y))
        if len(points) == 1:
            self.var_probe_hint.set("铺点中：对准【最后一个】图标按取色键")
            self.log(f"铺点起点 ({x},{y})", LOG_INFO)
            return

        (x0, y0), (x1, y1) = points[0], points[1]
        count = self._layout["count"]
        prefix, note = self._layout["prefix"], self._layout["note"]
        self._layout = None
        self.var_probe_hint.set("取色器开启时按取色键直接新增一个点")

        used = {p.id for p in self.profile.probes}
        steps = count - 1
        created = []
        for i in range(count):
            # linear interpolation, first and last land exactly on the picks
            px = int(round(x0 + (x1 - x0) * i / steps))
            py = int(round(y0 + (y1 - y0) * i / steps))
            pid, n = f"{prefix}{i + 1}", i + 1
            while pid in used:            # never clobber an existing probe
                n += 1
                pid = f"{prefix}{n}"
            used.add(pid)
            created.append(Probe(id=pid, x=px, y=py,
                                 note=f"{note}{i + 1}" if note else ""))
        self.profile.probes.extend(created)
        self._after_probe_change()
        self.log(f"铺点完成：{created[0].id}…{created[-1].id}，"
                 f"共 {len(created)} 个，从 ({x0},{y0}) 到 ({x1},{y1})", LOG_INFO)

    def _clear_probes(self) -> None:
        if not self.profile.probes:
            return
        if not messagebox.askokcancel(
                "清空取色点",
                f"删掉全部 {len(self.profile.probes)} 个取色点？"
                f"引用它们的规则条件会失效。", parent=self):
            return
        self.profile.probes.clear()
        self._after_probe_change()
        self.log("已清空取色点", LOG_INFO)

    def _add_probe(self) -> None:
        result = ProbeDialog(self, Probe(id=self.profile.next_probe_id(), x=0, y=0),
                             [p.id for p in self.profile.probes]).show()
        if result:
            self.profile.probes.append(result)
            self._after_probe_change()

    def _edit_probe(self) -> None:
        probe_id = self.tree_probes.selected()
        probe = self.profile.probe(probe_id) if probe_id else None
        if not probe:
            return
        result = ProbeDialog(self, probe, [p.id for p in self.profile.probes]).show()
        if not result:
            return
        old_id = probe.id
        probe.id, probe.x, probe.y, probe.note = (result.id, result.x, result.y,
                                                  result.note)
        if old_id != probe.id:
            for rule in self.profile.rules:
                for cond in rule.conditions:
                    if cond.probe == old_id:
                        cond.probe = probe.id
        self._after_probe_change()

    def _delete_probe(self) -> None:
        probe_id = self.tree_probes.selected()
        if not probe_id:
            return
        used = [r.name or r.id for r in self.profile.rules
                if any(c.probe == probe_id for c in r.conditions)]
        if used and not messagebox.askokcancel(
                "还有人用", f"规则 {', '.join(used)} 还在用 {probe_id}，删了它们会失效。\n"
                            f"确定删除？", parent=self):
            return
        self.profile.probes = [p for p in self.profile.probes if p.id != probe_id]
        self._after_probe_change()

    def _after_probe_change(self) -> None:
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        self._refresh_probe_tree()
        self._rebuild_live_rows()
        self._load_rule_editor()

    # -- rules ---------------------------------------------------------
    def _current_rule(self) -> Optional[Rule]:
        rule_id = self.tree_rules.selected()
        if not rule_id:
            return None
        return next((r for r in self.profile.rules if r.id == rule_id), None)

    def _add_rule(self) -> None:
        rule = Rule(id=self.profile.next_rule_id(), name="新规则", priority=0)
        self.profile.rules.append(rule)
        self._mark_dirty()
        self._refresh_rule_tree()
        self.tree_rules.tree.selection_set(rule.id)

    def _copy_rule(self) -> None:
        rule = self._current_rule()
        if not rule:
            return
        import copy
        clone = copy.deepcopy(rule)
        clone.id = self.profile.next_rule_id()
        clone.name = f"{rule.name or rule.id} 副本"
        self.profile.rules.append(clone)
        self._mark_dirty()
        self._refresh_rule_tree()
        self.tree_rules.tree.selection_set(clone.id)

    def _delete_rule(self) -> None:
        rule = self._current_rule()
        if not rule:
            return
        if not messagebox.askokcancel("删除规则", f"删掉「{rule.name or rule.id}」？",
                                      parent=self):
            return
        self.profile.rules = [r for r in self.profile.rules if r.id != rule.id]
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        self._refresh_rule_tree()

    def _move_rule(self, delta: int) -> None:
        rule = self._current_rule()
        if not rule:
            return
        rule.priority += -delta  # up in the list == higher priority
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        self._refresh_rule_tree()
        self.tree_rules.tree.selection_set(rule.id)

    def _toggle_rule_enabled(self) -> None:
        rule = self._current_rule()
        if not rule:
            return
        rule.enabled = not rule.enabled
        self.var_rule_enabled.set(rule.enabled)
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        self._refresh_rule_tree()

    def _load_rule_editor(self) -> None:
        rule = self._current_rule()
        self._loading_rule = True
        try:
            if rule is None:
                self.var_rule_name.set("")
                self.var_rule_pri.set(0)
                self.var_rule_cd.set(0)
                self.var_rule_enabled.set(False)
                self.var_rule_match.set("全部满足")
            else:
                self.var_rule_name.set(rule.name)
                self.var_rule_pri.set(rule.priority)
                self.var_rule_cd.set(rule.cooldown_ms)
                self.var_rule_enabled.set(rule.enabled)
                self.var_rule_match.set("任一满足" if rule.match == "any" else "全部满足")
        finally:
            self._loading_rule = False
        self._refresh_cond_tree()
        self._refresh_act_tree()

    def _commit_rule_head(self) -> None:
        if getattr(self, "_loading_rule", False):
            return
        rule = self._current_rule()
        if rule is None:
            return
        try:
            rule.name = self.var_rule_name.get()
            rule.priority = int(self.var_rule_pri.get())
            rule.cooldown_ms = int(self.var_rule_cd.get())
        except (tk.TclError, ValueError):
            return
        rule.enabled = bool(self.var_rule_enabled.get())
        rule.match = "any" if self.var_rule_match.get() == "任一满足" else "all"
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        tree = self.tree_rules.tree
        if tree.exists(rule.id):
            tree.item(rule.id, values=("✓" if rule.enabled else "", rule.priority,
                                       rule.name or rule.id))

    def _refresh_cond_tree(self) -> None:
        self.tree_conds.clear()
        rule = self._current_rule()
        if not rule:
            return
        for i, cond in enumerate(rule.conditions):
            self.tree_conds.tree.insert(
                "", "end", iid=str(i),
                values=(cond.probe, cond.color, cond.tolerance,
                        "是" if cond.negate else ""))

    def _refresh_act_tree(self) -> None:
        self.tree_acts.clear()
        rule = self._current_rule()
        if not rule:
            return
        for i, action in enumerate(rule.actions):
            self.tree_acts.tree.insert("", "end", iid=str(i),
                                       values=(i + 1, action.describe()))

    def _picker_color(self) -> Optional[Tuple[int, int, int]]:
        last = getattr(self.picker, "_last", None)
        return last[1] if last else None

    def _add_condition(self) -> None:
        rule = self._current_rule()
        if not rule:
            return
        if not self.profile.probes:
            messagebox.showinfo("先加取色点", "条件是针对取色点的，先去「取色点」加一个。",
                                parent=self)
            return
        result = ConditionDialog(self, self.profile.probes, None,
                                 self._picker_color).show()
        if result:
            rule.conditions.append(result)
            self._after_rule_change()

    def _edit_condition(self) -> None:
        rule = self._current_rule()
        index = self.tree_conds.selected()
        if not rule or index is None:
            return
        result = ConditionDialog(self, self.profile.probes,
                                 rule.conditions[int(index)], self._picker_color).show()
        if result:
            rule.conditions[int(index)] = result
            self._after_rule_change()

    def _delete_condition(self) -> None:
        rule = self._current_rule()
        index = self.tree_conds.selected()
        if not rule or index is None:
            return
        rule.conditions.pop(int(index))
        self._after_rule_change()

    # -- 一键学色：把画面上现在的颜色写进条件 --------------------------
    def _live_colors(self) -> Optional[Dict[str, Tuple[int, int, int]]]:
        """One-shot read of every probe, whether or not the macro is running."""
        try:
            readings = self.engine.snapshot_probes()
        except Exception as exc:
            messagebox.showerror(
                "读不到颜色",
                f"{exc}\n\n游戏在独占全屏的话换成窗口模式再试。", parent=self)
            return None
        return {r.probe_id: r.rgb for r in readings if r.ok}

    def _learn_selected_color(self) -> None:
        rule = self._current_rule()
        index = self.tree_conds.selected()
        if not rule or index is None:
            messagebox.showinfo("先选一条", "在上面的列表里选中要学色的条件。",
                                parent=self)
            return
        colors = self._live_colors()
        if colors is None:
            return
        cond = rule.conditions[int(index)]
        rgb = colors.get(cond.probe)
        if rgb is None:
            messagebox.showwarning("没读到", f"取色点 {cond.probe} 这一轮没读到颜色。",
                                   parent=self)
            return
        cond.color = format_color(rgb)
        if cond.tolerance == 0:
            cond.tolerance = 25      # 0 would only ever match this exact pixel
        self._after_rule_change()
        self.tree_conds.tree.selection_set(index)
        self.log(f"学色: {cond.probe} = {cond.color}（容差 {cond.tolerance}）", LOG_INFO)

    def _learn_rule_colors(self) -> None:
        """Snapshot every condition of the current rule from one frame.

        The point is to put the game in the state you want the rule to fire on
        -- skill lit up, buff present -- and capture all of it at once.
        """
        rule = self._current_rule()
        if not rule or not rule.conditions:
            messagebox.showinfo("没有条件", "这条规则还没有任何取色条件。", parent=self)
            return
        colors = self._live_colors()
        if colors is None:
            return
        learned, missed = 0, []
        for cond in rule.conditions:
            rgb = colors.get(cond.probe)
            if rgb is None:
                missed.append(cond.probe)
                continue
            cond.color = format_color(rgb)
            if cond.tolerance == 0:
                cond.tolerance = 25
            learned += 1
        self._after_rule_change()
        note = f"整条学色: {rule.name or rule.id} 更新了 {learned} 个条件"
        if missed:
            note += f"，{len(missed)} 个没读到（{', '.join(missed)}）"
        self.log(note, LOG_WARN if missed else LOG_INFO)

    def _add_action(self) -> None:
        rule = self._current_rule()
        if not rule:
            return
        result = ActionDialog(self, self.hotkeys).show()
        if result:
            rule.actions.append(result)
            self._after_rule_change()

    def _edit_action(self) -> None:
        rule = self._current_rule()
        index = self.tree_acts.selected()
        if not rule or index is None:
            return
        result = ActionDialog(self, self.hotkeys, rule.actions[int(index)]).show()
        if result:
            rule.actions[int(index)] = result
            self._after_rule_change()

    def _delete_action(self) -> None:
        rule = self._current_rule()
        index = self.tree_acts.selected()
        if not rule or index is None:
            return
        rule.actions.pop(int(index))
        self._after_rule_change()

    def _move_action(self, delta: int) -> None:
        rule = self._current_rule()
        index = self.tree_acts.selected()
        if not rule or index is None:
            return
        i = int(index)
        j = i + delta
        if not (0 <= j < len(rule.actions)):
            return
        rule.actions[i], rule.actions[j] = rule.actions[j], rule.actions[i]
        self._after_rule_change()
        self.tree_acts.tree.selection_set(str(j))

    def _after_rule_change(self) -> None:
        self._mark_dirty()
        self.engine.set_profile(self.profile)
        self._refresh_cond_tree()
        self._refresh_act_tree()

    # ==================================================================
    # logging + periodic refresh
    # ==================================================================
    def log(self, message: str, level: str = LOG_INFO) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._log_lock:
            self._log_lines.append((level, f"[{stamp}] {message}"))

    def _drain_log(self) -> None:
        with self._log_lock:
            pending, self._log_lines = self._log_lines, []
        if not pending:
            return
        self.txt_log.configure(state="normal")
        for level, line in pending:
            self.txt_log.insert("end", line + "\n", level)
        # keep the buffer bounded so a long session cannot eat memory
        if int(self.txt_log.index("end-1c").split(".")[0]) > 500:
            self.txt_log.delete("1.0", "200.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _refresh(self) -> None:
        try:
            self._drain_log()
            status = self.engine.status()

            if status.active:
                self.var_state.set("运行中")
                self.lbl_state.configure(foreground="#1e8449")
            elif self.engine.waiting_for_trigger:
                hold = self.profile.hotkeys.get("hold")
                self.var_state.set(f"等待 {hold.describe()}")
                self.lbl_state.configure(foreground="#b9770e")
            else:
                self.var_state.set("已停止")
                self.lbl_state.configure(foreground="#7f8c8d")
            base = "停止" if self.engine.armed else "启动"
            toggle = self.profile.hotkeys.get("toggle")
            label = toggle.describe() if toggle and toggle.key else "未设置"
            self.btn_run.configure(text=f"{base}  ({label})")

            size = f"{status.window_size[0]}×{status.window_size[1]}" \
                if status.window_size[0] else "-"
            self.var_window.set(f"窗口: {status.window_title}  {size}")
            self.var_perf.set(f"取色频率: {status.fps:5.1f} 次/秒"
                              f"   单轮 {status.tick_ms:4.1f} ms")
            self.var_fired.set(f"触发: {status.fired_total} 次")
            self.var_lastrule.set(f"最近规则: {status.last_rule or '-'}")

            for reading in status.readings:
                row = self._live_rows.get(reading.probe_id)
                if row:
                    row[1].set(reading.rgb)
                item = reading.probe_id
                if self.tree_probes.tree.exists(item):
                    values = list(self.tree_probes.tree.item(item, "values"))
                    values[3] = rgb_to_hex(reading.rgb)
                    self.tree_probes.tree.item(item, values=values)

            bits = []
            if status.error:
                bits.append(f"⚠ {status.error}")
            elif status.active and not status.foreground_ok:
                bits.append("游戏不在前台，已暂停")
            if self._dirty:
                bits.append("● 未保存")
            self.var_status.set("    ".join(bits))
        except Exception:
            self.log("界面刷新出错:\n" + traceback.format_exc(), LOG_ERROR)
        finally:
            self.after(REFRESH_MS, self._refresh)

    # ==================================================================
    def _on_close(self) -> None:
        if not self._confirm_discard():
            return
        try:
            self.picker.stop()
            self.engine.stop()
            self.hotkeys.stop()
        finally:
            end_high_resolution_timer()
            self.destroy()


HELP_TEXT = """\
怎么用
────────────────────────────────────────────────────────
1. 把游戏切成「窗口模式」或「无边框窗口」。独占全屏读不到像素 —— 这是
   Windows 的限制，不是本程序的 bug。

2.「设置」→ 填好窗口标题（点「刷新列表」能列出所有窗口），再点
   「把当前窗口尺寸设为基准」。

3.「取色点」→ 点「一键铺点」，填个数量（技能栏几格就填几），然后对准
   【第一个】图标按 F8、对准【最后一个】图标按 F8 —— 中间的点自动等距
   铺开。技能栏是等距的，两次就够，不用一格一格点。
   零散的点还是用「开始取色」+ F8 一个个加。

4.「规则」→ 选中规则 → 把游戏摆成你想让它触发的样子（技能亮着、buff
   挂着）→ 点「学色(整条)」，当前画面的颜色就写进条件了，不用手敲
   #RRGGBB。只改一个条件就用「学色(选中)」。
   · 条件 = 哪个取色点、要什么颜色、容差多少
   · 动作 = 命中之后按什么键、按多久、中间等多久
   规则按优先级从高到低判断，命中一条就执行完并结束本轮。
   学完色记得把规则的「启用」勾上 —— 骨架里默认全是关的。

5. 回「运行」，按主开关热键（默认 Ctrl+Q）开始。

两个开关的关系
────────────────────────────────────────────────────────
「主开关」管整个宏的死活，「长按触发」只管现在打不打。松开长按键不会
把宏关掉，再按住就接着跑 —— 所以中途要走位、要读条，松手就行，不用
重新开宏。没设长按键的话，主开关一开就一直跑。「急停」两个都关掉。

调参小抄
────────────────────────────────────────────────────────
· 容差：技能图标亮/暗的差别通常很大，20~40 足够；如果误触发就调小，
  漏触发就调大。「运行」页能看到每个点的实时颜色，对着调最快。
· 默认按住时长：游戏一帧才采样一次键盘，太短会被吃掉。30ms 起步。
· 按键最小间隔：卡刀节奏的关键。先从 40~60ms 试。
· 冷却：防止同一条规则疯狂重复触发。
· 取色频率上限 = 显示器刷新率。GDI 从屏幕读一次就要等一帧，这是硬限制，
  把「取色间隔」调到 0 也不会更快。

坐标为什么不会跑偏
────────────────────────────────────────────────────────
取色点存的是相对游戏窗口客户区的坐标，窗口挪了照样准。换分辨率时按
基准分辨率等比缩放 —— 前提是游戏 UI 本身是等比缩放的。改过 UI 缩放
或换了 UI 插件，重新取一次色最稳。

用不了的时候
────────────────────────────────────────────────────────
· 取到的颜色全黑 → 游戏在独占全屏，换窗口模式。
· 按键没反应 → 游戏可能以管理员身份运行，本程序也要用管理员身份运行。
· 热键没反应 → 同上；另外检查热键有没有和游戏内按键撞车。

为什么没有内置的职业宏
────────────────────────────────────────────────────────
取色点的坐标取决于你的分辨率和技能栏摆法，颜色取决于画质设置和 UI
插件，按哪个键取决于你自己怎么绑 —— 这三样没有一样是通用的。
所以内置的只有一份「通用骨架」：8 个技能格对应按键 1-8，坐标是占位
值、颜色待取、规则默认全关。照上面的流程铺点 + 学色 + 启用就能用。

一句话提醒
────────────────────────────────────────────────────────
自动化按键可能违反游戏的用户协议，用不用、怎么用，自己拿主意。
"""


def run(profile_dir: Optional[Path] = None) -> None:
    enable_dpi_awareness()
    begin_high_resolution_timer()
    app = App(profile_dir or PROFILE_DIR)
    try:
        app.mainloop()
    finally:
        end_high_resolution_timer()
