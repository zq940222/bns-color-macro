"""Import settings from the closed-source macro this project was modelled on.

Only the *settings* carry over -- hotkeys, timing, which class was selected.
The colour data does not: in the builds we looked at, ``colors`` in the class
file is an empty placeholder (``[{}, {}, {}]``) and the actual pixels live
inside the packed executable.  So the importer's job is to save you the
fiddly part (re-binding keys) and be honest that the probes and rules have to
be picked again.

Nothing here unpacks or decompiles anything; it reads JSON files that the
other program wrote in plain text next to itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import keys as K
from .profile import Hotkey, Profile, Settings

# 门派 slug -> readable name, as used by the tool's data/ folder
FACTIONS = {
    "jian_shi": "剑士", "quan_shi": "拳师", "li_shi": "力士",
    "qi_gong": "气功士", "ci_ke": "刺客", "zhao_huan": "召唤师",
    "zhou_shu": "咒术师", "ling_jian": "灵剑士", "qiang_shu": "枪术士",
    "qi_shi": "气宗", "ma_feng": "魔枫", "shang_guang": "上罡",
}


def _as_hotkey(raw: Any, mode: str = "toggle") -> Optional[Hotkey]:
    """Map the legacy hotkey shapes onto our own."""
    if not raw:
        return None
    if isinstance(raw, str):
        key = K.normalize(raw)
        return Hotkey(key=key, mode=mode) if key else None
    if isinstance(raw, dict):
        code = K.normalize(str(raw.get("code", "")))
        modifier = str(raw.get("modifier", "")).strip().lower()
        if not code:
            return None
        mods = [modifier] if modifier in ("ctrl", "shift", "alt") else []
        return Hotkey(key=code, modifiers=mods, mode=mode)
    return None


def _read_json(path: Path) -> Dict[str, Any]:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"读不懂这个文件: {path.name}")


def import_legacy_folder(folder: Path) -> Tuple[Profile, List[str]]:
    """Build a fresh profile from a legacy ``data/`` folder.

    Returns the profile plus human-readable notes about what was carried
    over, which the UI shows to the user.
    """
    folder = Path(folder)
    settings_path = folder / "settings.json"
    if not settings_path.is_file():
        raise FileNotFoundError(
            f"{folder} 里没有 settings.json —— 选错文件夹了？")

    raw = _read_json(settings_path)
    notes: List[str] = []
    profile = Profile(name="从旧宏导入", settings=Settings())

    faction = str(raw.get("faction_name", "")).strip()
    if faction:
        profile.game_class = FACTIONS.get(faction, faction)
        profile.name = f"{profile.game_class} (导入)"
        notes.append(f"门派：{profile.game_class}")

    toggle = _as_hotkey(raw.get("global_key"), mode="toggle")
    if toggle:
        profile.hotkeys["toggle"] = toggle
        notes.append(f"主开关热键：{toggle.describe()}")

    main = raw.get("main_macro") or {}
    main_key = _as_hotkey(main.get("hotkey"), mode="hold")
    if main_key:
        # "Auto" in the old tool meant "keep firing while held"
        main_key.mode = "hold" if str(main.get("trigger", "")).lower() == "auto" \
            else "toggle"
        main_key.enabled = bool(main.get("checked", True))
        profile.hotkeys["hold"] = main_key
        notes.append(f"主宏触发键：{main_key.describe()}"
                     f"（{'长按' if main_key.mode == 'hold' else '开关'}）")

    backend = str(raw.get("input_backend", "")).strip()
    if backend and backend.lower() != "sendinput":
        notes.append(f"旧宏用的是 {backend} 发送按键；本程序只有 SendInput（扫描码）")

    # per-class file, e.g. data/ma_feng.json.  The selected faction and the
    # file that is actually present do not always agree, so fall back to
    # whatever other json the folder holds.
    class_path = folder / f"{faction}.json" if faction else None
    if class_path is None or not class_path.is_file():
        others = [p for p in sorted(folder.glob("*.json")) if p.name != "settings.json"]
        class_path = others[0] if others else None
        if class_path is not None:
            notes.append(f"用 {class_path.name} 作为职业配置")
    if class_path and class_path.is_file():
        try:
            class_raw = _read_json(class_path)
        except ValueError:
            class_raw = {}
        interval = class_raw.get("按键间隔时间")
        if isinstance(interval, (int, float)) and interval > 0:
            profile.settings.key_interval_ms = int(interval)
            notes.append(f"按键间隔：{int(interval)} ms")
        colours = [c for c in class_raw.get("colors", []) if c]
        if colours:
            notes.append(f"发现 {len(colours)} 条颜色数据，但格式不公开，没法自动转")
        else:
            notes.append("旧配置里没有保存颜色数据，取色点需要重新取")
        # keep everything else verbatim so nothing is silently lost
        profile.extras["legacy_class_settings"] = class_raw

    profile.extras["legacy_settings"] = raw
    if not notes:
        notes.append("这个 settings.json 里没有能对应上的项目")
    return profile, notes
