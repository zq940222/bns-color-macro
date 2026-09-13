"""Profile data model: probes, rules, actions, settings.

A profile is one JSON file.  It is meant to be hand-editable -- that is the
whole point of owning your own macro -- so the loader is forgiving about
missing fields and never throws away keys it does not understand.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

MATCH_ALL = "all"
MATCH_ANY = "any"

METRIC_CHANNEL = "channel"   # max per-channel difference (like AHK's variation)
METRIC_DISTANCE = "distance"  # euclidean distance in RGB space


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value: Any) -> Tuple[int, int, int]:
    """Accept ``"#RRGGBB"``, ``"RRGGBB"``, ``[r,g,b]`` or an int."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(int(c) & 0xFF for c in value[:3])  # type: ignore[return-value]
    if isinstance(value, int):
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    m = _HEX_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"bad colour: {value!r}")
    raw = int(m.group(1), 16)
    return ((raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF)


def format_color(rgb: Tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
@dataclass
class Probe:
    """A single pixel the engine watches."""
    id: str
    x: int
    y: int
    note: str = ""

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Probe":
        return Probe(id=str(d.get("id", "")), x=int(d.get("x", 0)),
                     y=int(d.get("y", 0)), note=str(d.get("note", "")))


@dataclass
class Condition:
    probe: str
    color: str = "#000000"
    tolerance: int = 20
    metric: str = METRIC_CHANNEL
    negate: bool = False

    @property
    def rgb(self) -> Tuple[int, int, int]:
        return parse_color(self.color)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Condition":
        return Condition(
            probe=str(d.get("probe", "")),
            color=format_color(parse_color(d.get("color", "#000000"))),
            tolerance=int(d.get("tolerance", 20)),
            metric=str(d.get("metric", METRIC_CHANNEL)),
            negate=bool(d.get("negate", False)),
        )


@dataclass
class Action:
    """One step of a rule's sequence.

    ``type``:
      ``"key"``    tap a key or mouse button (``key``, ``hold_ms``)
      ``"down"`` / ``"up"``  hold or release one (``key``)
      ``"delay"``  wait (``ms``)
      ``"move"``   relative mouse move (``dx``, ``dy``) -- rotates the camera
    """
    type: str = "key"
    key: str = ""
    hold_ms: int = 30
    ms: int = 0
    dx: int = 0
    dy: int = 0
    modifiers: List[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Action":
        return Action(
            type=str(d.get("type", "key")),
            key=str(d.get("key", "")),
            hold_ms=int(d.get("hold_ms", 30)),
            ms=int(d.get("ms", 0)),
            dx=int(d.get("dx", 0)),
            dy=int(d.get("dy", 0)),
            modifiers=[str(m) for m in d.get("modifiers", [])],
        )

    def describe(self) -> str:
        if self.type == "delay":
            return f"等待 {self.ms}ms"
        if self.type == "move":
            return f"移动鼠标 ({self.dx},{self.dy})"
        combo = "+".join(self.modifiers + [self.key]) if self.modifiers else self.key
        if self.type == "down":
            return f"按下 {combo}"
        if self.type == "up":
            return f"松开 {combo}"
        return f"点按 {combo} ({self.hold_ms}ms)"


@dataclass
class Rule:
    id: str
    name: str = ""
    enabled: bool = True
    priority: int = 0
    match: str = MATCH_ALL
    conditions: List[Condition] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    cooldown_ms: int = 0

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Rule":
        return Rule(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            enabled=bool(d.get("enabled", True)),
            priority=int(d.get("priority", 0)),
            match=str(d.get("match", MATCH_ALL)),
            conditions=[Condition.from_dict(c) for c in d.get("conditions", [])],
            actions=[Action.from_dict(a) for a in d.get("actions", [])],
            cooldown_ms=int(d.get("cooldown_ms", 0)),
        )


@dataclass
class Hotkey:
    key: str = ""
    modifiers: List[str] = field(default_factory=list)
    mode: str = "toggle"     # toggle | hold | press
    swallow: bool = False
    enabled: bool = True

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Hotkey":
        return Hotkey(
            key=str(d.get("key", "")),
            modifiers=[str(m) for m in d.get("modifiers", [])],
            mode=str(d.get("mode", "toggle")),
            swallow=bool(d.get("swallow", False)),
            enabled=bool(d.get("enabled", True)),
        )

    def describe(self) -> str:
        if not self.key:
            return "未设置"
        return "+".join([m.capitalize() for m in self.modifiers] + [self.key])


@dataclass
class Settings:
    tick_ms: int = 10
    key_hold_ms: int = 30
    key_interval_ms: int = 40
    jitter_ms: int = 0
    require_foreground: bool = True
    capture_mode: str = "desktop"     # desktop | window
    scale_with_resolution: bool = True
    anchor_mode: str = "client"       # client | screen
    window_title: str = ""
    window_class: str = ""
    reference_size: List[int] = field(default_factory=lambda: [0, 0])

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Settings":
        base = Settings()
        for k, v in (d or {}).items():
            if hasattr(base, k):
                setattr(base, k, v)
        if not isinstance(base.reference_size, list) or len(base.reference_size) != 2:
            base.reference_size = [0, 0]
        base.reference_size = [int(base.reference_size[0]), int(base.reference_size[1])]
        return base


@dataclass
class Profile:
    name: str = "新配置"
    game_class: str = ""
    description: str = ""
    settings: Settings = field(default_factory=Settings)
    hotkeys: Dict[str, Hotkey] = field(default_factory=dict)
    probes: List[Probe] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    # -- lookups -----------------------------------------------------------
    def probe(self, probe_id: str) -> Optional[Probe]:
        for p in self.probes:
            if p.id == probe_id:
                return p
        return None

    def next_probe_id(self) -> str:
        n = 1
        used = {p.id for p in self.probes}
        while f"p{n}" in used:
            n += 1
        return f"p{n}"

    def next_rule_id(self) -> str:
        n = 1
        used = {r.id for r in self.rules}
        while f"r{n}" in used:
            n += 1
        return f"r{n}"

    def sorted_rules(self) -> List[Rule]:
        return sorted(self.rules, key=lambda r: (-r.priority, r.id))

    def reference_size(self) -> Optional[Tuple[int, int]]:
        rw, rh = self.settings.reference_size
        return (rw, rh) if rw > 0 and rh > 0 else None

    # -- validation --------------------------------------------------------
    def problems(self) -> List[str]:
        """Human-readable list of things that would stop the macro working."""
        issues: List[str] = []
        ids = [p.id for p in self.probes]
        if len(ids) != len(set(ids)):
            issues.append("取色点 ID 重复")
        for rule in self.rules:
            if not rule.conditions:
                issues.append(f"规则「{rule.name or rule.id}」没有任何取色条件")
            if not rule.actions:
                issues.append(f"规则「{rule.name or rule.id}」没有任何按键动作")
            for cond in rule.conditions:
                if self.probe(cond.probe) is None:
                    issues.append(
                        f"规则「{rule.name or rule.id}」引用了不存在的取色点 {cond.probe}")
        return issues

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.extras)
        data.update({
            "schema": SCHEMA_VERSION,
            "name": self.name,
            "game_class": self.game_class,
            "description": self.description,
            "settings": asdict(self.settings),
            "hotkeys": {k: asdict(v) for k, v in self.hotkeys.items()},
            "probes": [asdict(p) for p in self.probes],
            "rules": [
                {
                    "id": r.id, "name": r.name, "enabled": r.enabled,
                    "priority": r.priority, "match": r.match,
                    "cooldown_ms": r.cooldown_ms,
                    "conditions": [asdict(c) for c in r.conditions],
                    "actions": [asdict(a) for a in r.actions],
                }
                for r in self.rules
            ],
        })
        return data

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Profile":
        known = {"schema", "name", "game_class", "description", "settings",
                 "hotkeys", "probes", "rules"}
        return Profile(
            name=str(d.get("name", "新配置")),
            game_class=str(d.get("game_class", "")),
            description=str(d.get("description", "")),
            settings=Settings.from_dict(d.get("settings", {})),
            hotkeys={k: Hotkey.from_dict(v) for k, v in (d.get("hotkeys") or {}).items()},
            probes=[Probe.from_dict(p) for p in d.get("probes", [])],
            rules=[Rule.from_dict(r) for r in d.get("rules", [])],
            extras={k: v for k, v in d.items() if k not in known},
        )

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path or f"{self.name}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        # write to a sibling temp file first so a crash cannot truncate a
        # profile the user spent an hour picking colours for
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(target)
        self.path = target
        return target

    @staticmethod
    def load(path: Path) -> "Profile":
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = Profile.from_dict(data)
        profile.path = path
        return profile


def default_profile() -> Profile:
    """A minimal but runnable profile, used for 新建配置."""
    return Profile(
        name="新配置",
        description="在「取色点」里加几个点，再在「规则」里把颜色和按键连起来。",
        hotkeys={
            "toggle": Hotkey(key="q", modifiers=["ctrl"], mode="toggle"),
            "panic": Hotkey(key="f12", mode="press"),
        },
    )
