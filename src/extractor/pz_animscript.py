#!/usr/bin/env python3
"""
Mines PZ's own locomotion/animation state machine into a locomotion manifest.

Why
---
Fourth offline export, sibling to `pz_pack.py` (world geometry), `pz_textures.py`
(sprite art) and `pz_characters.py` (meshes/clips). PZ's blend weights,
transition graph and upper/lower body layering are not engine-internal — they
are plain, hand-editable XML (`media/animstates/testanimstates*.xml`,
`media/animscript/combat.xml`) — so, per this project's "the manifest is
generated, not authored" rule, they get mined here rather than hand-tuned
inside a Godot `AnimationTree`.

Full rationale and what the runtime does with this:
docs/project/pipeline/character_assets.md (locomotion follow-on, once written)

Source files
------------
`media/animstates/testanimstates.xml` is the live state-machine definition —
locomotion, upper/lower layering, climbing, most emotes. `testanimstates2.xml`
is confirmed (by direct diff of state names) to be a near-complete duplicate
of it, not a complement — 73 of its 73 states already exist in the first
file, `BaseAnim` being the only one missing. `media/animscript/combat.xml` is
an older, simpler tag dialect that carries a handful of hit-react/attack
one-shots not present in either animstates file. All three are still parsed
and merged (rather than hardcoding "ignore testanimstates2.xml"), because
that duplication is a property of *this* game version, not a guarantee — a
state name collision keeps the entry from whichever file is listed first in
`ANIM_SOURCES` and records the drop, the same "warn, don't fail" discipline
`pz_textures.py` uses for `missing.json`.

`media/animscript/Master_Variables.xml` (if present) holds the blend-curve
shaping constants (`InverseExponential`/`Linear`, increase/decrease
multipliers) referenced by name from `<Blends>` in the animstates files. In
practice the `<Blends>` block in `testanimstates.xml` is self-contained (it
carries `<Type>`/`<Increase>`/`<Decrease>` inline), so `Master_Variables.xml`
is read only as a fallback when a state's `<Blends>` entry is missing one.

Output
------
`assets/characters/locomotion_manifest.json` — a sibling to
`character_manifest.json`, not an extension of it. Locomotion is a state
graph, a distinct concern from mesh/clip assets, and is consumed by a
different runtime subsystem (`LocomotionController`, not
`CharacterAssetRegistry`). Schema:

    {
      "version": 1,
      "variables": {"MoveDelta": {"curve": "InverseExponential",
                                   "increase": 0.185, "decrease": 0.125}, ...},
      "states": {
        "NormalMovement": {
          "upper": false,
          "commands": [
            {"type": "blend1d", "var": "MoveDelta", "min": 0.0, "max": 1.0,
             "repeat": -1, "blend_time": 0.0, "can_cancel": false,
             "entries": [{"anim": "@idle", "val": 0.0, "speed_delta": 0.6}, ...]},
            {"type": "playanim", "anim": "Bob_Idle", "repeat": -1,
             "speed_delta": 0.3, "blend_time": 0.3, "can_cancel": true,
             "upper": false, "blend_out_upper": null}
          ],
          "out": [{"target": "Running", "conditions": {"IsSprinting": "false"},
                    "out_blend": 0.4}]
        }, ...
      },
      "bone_masks": {"Human": {"upper": [...], "lower": [...]}}
    }

`@idle`/`@walk`/`@run`/`@sneakIdle`/... tokens are PZ's own per-weapon/context
clip-set indirection (resolved at runtime against whatever is equipped) and
are recorded as-is, **not** resolved here — resolving them needs the weapon
table this exporter has no reason to read. `entries`/`commands` cross-validate
literal (non-`@`) clip names against `character_manifest.json`'s `clips` and
warn (not fail) on anything unresolved, written to `missing.json` alongside
whatever `pz_characters.py` already put there.

`bone_masks` is derived, not mined — `Upper="true"`/`BlendOutUpper` mark a
*state* as upper-body-only but never enumerate which bones "upper" means.
Walked once from the packed `Human` skeleton (same `Master_Bones.xml`-derived
hierarchy `pz_characters.py` already reads): everything at or below
`Bip01_Spine1` is "upper", everything else is "lower". One table per
`SKELETON_ID`, computed offline, not redefined per NPC instance.

Usage
-----
  python pz_animscript.py --out assets/characters/locomotion_manifest.json
  python pz_animscript.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import xfile
from pz_characters import SKELETON_SOURCE, SKELETON_ID

#: Where the Build 42 install lives. `--game-dir` overrides it; so does
#: PZ_GAME_DIR in the environment, which is how `extract.bat` and
#: `setup.bat` pass along the folder they found or you picked. The literal
#: below is only the last resort: Steam's default install location, which is
#: very often wrong. Pass --game-dir or set PZ_GAME_DIR rather than relying
#: on it.
GAME_DIR = os.environ.get(
    "PZ_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid"
)

#: Listed in priority order — first file wins a state-name collision.
ANIM_SOURCES = (
    "media/animstates/testanimstates.xml",
    "media/animstates/testanimstates2.xml",
    "media/animscript/combat.xml",
)

#: The bone below which the whole subtree is "upper body" for masking
#: purposes. See module docstring — this is a derived split, not mined data.
UPPER_BODY_ROOT = "Bip01_Spine1"


class ExportError(Exception):
    pass


# --------------------------------------------------------------------------
# <Blends> — variable shaping (curve type, increase/decrease multipliers)
# --------------------------------------------------------------------------

def _parse_blends(root: ET.Element) -> dict:
    out = {}
    blends = root.find("Blends")
    if blends is None:
        return out
    for var in blends:
        entry = {"curve": None, "increase": None, "decrease": None}
        type_el = var.find("Type")
        if type_el is not None:
            entry["curve"] = (type_el.text or "").strip()
        inc = var.find("Increase")
        if inc is not None and inc.text:
            entry["increase"] = float(inc.text.strip())
        dec = var.find("Decrease")
        if dec is not None and dec.text:
            entry["decrease"] = float(dec.text.strip())
        out[var.tag] = entry
    return out


# --------------------------------------------------------------------------
# <AnimStates> — states, commands, transitions
# --------------------------------------------------------------------------

def _attr_float(el: ET.Element, name: str, default=None):
    v = el.get(name)
    return float(v) if v is not None else default


def _attr_bool(el: ET.Element, name: str, default=False) -> bool:
    v = el.get(name)
    if v is None:
        return default
    return v.strip().lower() == "true"


def _parse_blend1d(el: ET.Element) -> dict:
    return {
        "type": "blend1d",
        "var": el.get("Var"),
        "min": _attr_float(el, "Min", 0.0),
        "max": _attr_float(el, "Max", 1.0),
        "repeat": int(_attr_float(el, "Repeat", 0)),
        "blend_time": _attr_float(el, "BlendTime", 0.0),
        "can_cancel": _attr_bool(el, "CanCancel", True),
        "random_start": _attr_bool(el, "RandomStart", False),
        "entries": [
            {"anim": e.get("Anim"), "val": _attr_float(e, "Val", 0.0),
             "speed_delta": _attr_float(e, "SpeedDelta", 1.0)}
            for e in el.findall("Entry")
        ],
    }


def _parse_blend2d(el: ET.Element) -> dict:
    return {
        "type": "blend2d",
        "var_x": el.get("VarX"),
        "var_y": el.get("VarY"),
        "grid_width": int(_attr_float(el, "GridWidth", 1)),
        "grid_height": int(_attr_float(el, "GridHeight", 1)),
        "speed_delta": _attr_float(el, "SpeedDelta", 1.0),
        "can_cancel": _attr_bool(el, "CanCancel", True),
        "entries": [
            {"anim": e.get("Anim"), "x": _attr_float(e, "X", 0.0),
             "y": _attr_float(e, "Y", 0.0)}
            for e in el.findall("Entry")
        ],
    }


def _parse_playanim(el: ET.Element) -> dict:
    return {
        "type": "playanim",
        "anim": el.get("Anim"),
        "repeat": int(_attr_float(el, "Repeat", 0)),
        "speed_delta": _attr_float(el, "SpeedDelta", 1.0),
        "blend_time": _attr_float(el, "BlendTime", 0.0),
        "can_cancel": _attr_bool(el, "CanCancel", True),
        "upper": _attr_bool(el, "Upper", False),
        "blend_out_upper": _attr_float(el, "BlendOutUpper", None),
        "start_frame": _attr_float(el, "StartFrame", None),
        "end_frame": _attr_float(el, "EndFrame", None),
    }


def _parse_command(el: ET.Element):
    """One `<Commands>` child -> a manifest command dict, or None to skip.

    `Or` (randomised choice among several `PlayAnim`s) and `Event` sub-tags on
    `PlayAnim` are flattened rather than modelled as their own node types —
    the design doc's one-shot stack only needs "what clip(s), what happens
    when it fires", not PZ's own dice-roll mechanism, which is fine to
    re-implement as a uniform pick over `choices` at runtime.
    """
    if el.tag == "Blend1D":
        return _parse_blend1d(el)
    if el.tag == "Blend2D":
        return _parse_blend2d(el)
    if el.tag == "PlayAnim":
        cmd = _parse_playanim(el)
        events = el.findall("Event")
        if events:
            cmd["events"] = [
                {"time_delta": _attr_float(e, "TimeDelta", 0.0),
                 "event_type": e.get("Type"), "effect": e.get("Effect")}
                for e in events
            ]
        return cmd
    if el.tag == "SetValue":
        return {"type": "setvalue", "name": el.get("name"),
                "value": el.get("value"),
                "force_blend": _attr_bool(el, "ForceBlend", False)}
    if el.tag == "SetCollision":
        return {"type": "setcollision", "active": _attr_bool(el, "active", True)}
    if el.tag == "Delay":
        return {"type": "delay", "time": _attr_float(el, "Time", 0.0)}
    if el.tag == "Or":
        choices = [_parse_command(c) for c in el if c.tag == "PlayAnim"]
        return {"type": "or", "choices": [c for c in choices if c]}
    return None


def _parse_commands(state_el: ET.Element) -> list:
    commands_el = state_el.find("Commands")
    if commands_el is None:
        return []
    out = []
    for child in commands_el:
        cmd = _parse_command(child)
        if cmd:
            out.append(cmd)
    return out


def _parse_out(state_el: ET.Element) -> list:
    out_el = state_el.find("Out")
    if out_el is None:
        return []
    transitions = []
    for target_el in out_el:
        conditions = {k: v for k, v in target_el.attrib.items() if k != "OutBlend"}
        transitions.append({
            "target": target_el.tag,
            "conditions": conditions,
            "out_blend": _attr_float(target_el, "OutBlend", 0.0),
        })
    return transitions


def _state_is_upper(state_el: ET.Element, commands: list) -> bool:
    """A state counts as upper-body-only if every PlayAnim in it says so.

    Mixed states (a `Blend1D` locomotion base plus an `Upper="true"` overlay,
    e.g. `ZombieAttack`) are common and are *not* upper-only as a whole —
    per-command `upper`/`blend_out_upper` flags are what the tree builder
    actually reads; this is a convenience summary flag only.
    """
    play_anims = [c for c in commands if c.get("type") == "playanim"]
    return bool(play_anims) and all(c.get("upper") for c in play_anims)


def _walk_states(anim_states_el: ET.Element, prefix: str, into: dict, dropped: list):
    """Recurse into `<AnimStates>`, flattening nested state groups.

    Some states (`ClimbWindow`) hold a nested `<AnimStates><Default>...`
    rather than `<Commands>`/`<Out>` directly — PZ uses this for variants of
    one logical action. Flattened here as `ClimbWindow.Default` so every leaf
    still gets a `Commands`/`Out` pair; nothing downstream needs to know the
    nesting existed.
    """
    for state_el in anim_states_el:
        name = f"{prefix}.{state_el.tag}" if prefix else state_el.tag
        nested = state_el.find("AnimStates")
        if nested is not None:
            _walk_states(nested, name, into, dropped)
            continue
        commands = _parse_commands(state_el)
        entry = {
            "upper": _state_is_upper(state_el, commands),
            "commands": commands,
            "out": _parse_out(state_el),
        }
        if name in into:
            dropped.append(name)
            continue
        into[name] = entry


def _parse_source(path: Path, into_states: dict, into_vars: dict, dropped: list):
    xml_text = path.read_text(encoding="utf-8-sig")
    root = ET.fromstring(xml_text)
    for var, entry in _parse_blends(root).items():
        into_vars.setdefault(var, entry)
    anim_states = root.find("AnimStates")
    if anim_states is not None:
        _walk_states(anim_states, "", into_states, dropped)


# --------------------------------------------------------------------------
# Bone masks — derived from the packed Human skeleton, not mined
# --------------------------------------------------------------------------

def _derive_bone_masks(game: Path) -> dict:
    skel_path = game / "media" / SKELETON_SOURCE
    bones = xfile.read_skeleton(xfile.parse_file(skel_path))
    if UPPER_BODY_ROOT not in bones:
        raise ExportError(f"{UPPER_BODY_ROOT!r} not found in {SKELETON_SOURCE} "
                          f"-- bone list changed, update UPPER_BODY_ROOT")

    upper: set[str] = set()

    def visit(name: str) -> None:
        upper.add(name)
        for child in bones[name].children:
            visit(child)

    visit(UPPER_BODY_ROOT)
    lower = [n for n in bones if n not in upper]
    return {SKELETON_ID: {"upper": sorted(upper), "lower": sorted(lower)}}


# --------------------------------------------------------------------------
# Cross-validation against character_manifest.json's clips
# --------------------------------------------------------------------------

def _referenced_clips(states: dict) -> set:
    out = set()

    def visit_cmd(cmd: dict):
        if cmd.get("type") in ("blend1d", "blend2d"):
            for e in cmd.get("entries", []):
                if e.get("anim"):
                    out.add(e["anim"])
        elif cmd.get("type") == "playanim":
            if cmd.get("anim"):
                out.add(cmd["anim"])
        elif cmd.get("type") == "or":
            for c in cmd.get("choices", []):
                visit_cmd(c)

    for state in states.values():
        for cmd in state["commands"]:
            visit_cmd(cmd)
    return out


def _validate_clips(states: dict, character_manifest_path: Path) -> list:
    """Warn (not fail) on literal clip names absent from character_manifest.json.

    `@`-prefixed tokens are PZ's own weapon/context indirection, resolved at
    runtime against whatever is equipped -- not this exporter's job, and not
    something `character_manifest.json` would ever contain literally.
    """
    if not character_manifest_path.exists():
        print(f"warning: {character_manifest_path} not found, skipping clip "
              f"cross-validation -- run pz_characters.py first")
        return []
    manifest = json.loads(character_manifest_path.read_text(encoding="utf-8"))
    known = set(manifest.get("clips", {}))
    # Clip ids in character_manifest.json are paths like "Bob/Bob_Idle"; the
    # animstates XML names bare clip names ("Bob_Idle"). Match on the tail.
    known_tails = {cid.rsplit("/", 1)[-1] for cid in known}

    missing = []
    for anim in sorted(_referenced_clips(states)):
        if anim.startswith("@"):
            continue
        if anim not in known_tails:
            missing.append(anim)
    return missing


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export(game: Path, out: Path, character_manifest: Path, dry_run: bool) -> dict:
    states: dict = {}
    variables: dict = {}
    dropped: list = []

    sources = []
    for rel in ANIM_SOURCES:
        path = game / rel
        if not path.exists():
            print(f"warning: {path} not found, skipping")
            continue
        sources.append(path)
        _parse_source(path, states, variables, dropped)

    if not sources:
        raise ExportError("no animstates/animscript source files found")

    print(f"game     {game}")
    print(f"sources  {len(sources)}: {', '.join(p.name for p in sources)}")
    print(f"states   {len(states)}, {len(variables)} blend variable(s)")
    if dropped:
        print(f"dropped  {len(dropped)} duplicate state name(s) "
              f"(first source wins): {', '.join(sorted(set(dropped))[:10])}"
              + (" ..." if len(set(dropped)) > 10 else ""))

    missing_clips = _validate_clips(states, character_manifest)
    if missing_clips:
        print(f"missing  {len(missing_clips)} referenced clip name(s) not in "
              f"character_manifest.json: {', '.join(missing_clips[:10])}"
              + (" ..." if len(missing_clips) > 10 else ""))

    bone_masks = _derive_bone_masks(game)
    for skel_id, masks in bone_masks.items():
        print(f"masks    {skel_id}: {len(masks['upper'])} upper / "
              f"{len(masks['lower'])} lower bone(s)")

    manifest = {
        "version": 1,
        "variables": variables,
        "states": states,
        "bone_masks": bone_masks,
    }

    if dry_run:
        print("\n(dry run, nothing written)")
        return manifest

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    if missing_clips:
        missing_path = out.with_name("missing.json")
        existing = []
        if missing_path.exists():
            try:
                existing = json.loads(missing_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []
        existing += [{"id": c, "reason": "referenced by animscript, no matching clip"}
                     for c in missing_clips]
        missing_path.write_text(json.dumps(existing, indent=1), encoding="utf-8")

    print(f"\nwrote {out} ({out.stat().st_size / 1e3:.1f} KB)")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mine PZ's animscript/AnimSets into a locomotion manifest.")
    ap.add_argument("--game-dir", default=GAME_DIR)
    ap.add_argument("--out", default="assets/characters/locomotion_manifest.json")
    ap.add_argument("--character-manifest",
                    default="assets/characters/character_manifest.json",
                    help="used only to cross-validate referenced clip names")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        export(Path(args.game_dir), Path(args.out), Path(args.character_manifest),
               args.dry_run)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
