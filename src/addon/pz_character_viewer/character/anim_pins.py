"""Persists which animation clips are pinned in the Animation Library panel.

Pure Python, no bpy - same split as pools.py/pzc_reader.py. Read-modify-write
+ .bak idiom copied from overrides.py in the parent package (that file writes
data/sprite_overrides.json the same way).

Lives at assets/characters/blender_animation_pins.json, sibling of
characters.pzc - per-repo character data, same tree as appearance_pools.json/
character_manifest.json, not addon code.

Not guarded against two writers racing (two Blender instances, or a human +
the export pipeline) - acceptable for a single-user local tool, same as
overrides.py doesn't guard against it either.
"""

import json
import os
import shutil

PINS_FILENAME = "blender_animation_pins.json"


def _pins_path(characters_root):
    return os.path.join(characters_root, PINS_FILENAME)


def load(characters_root):
    """The current pinned clip ids, or an empty set if the file doesn't
    exist yet or fails to parse."""
    path = _pins_path(characters_root)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    return set(data.get("pinned", []))


def _save(characters_root, pinned):
    path = _pins_path(characters_root)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    else:
        os.makedirs(characters_root, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pinned": sorted(pinned)}, f, indent="\t", ensure_ascii=False)
        f.write("\n")
    return path


def toggle(characters_root, clip_id):
    """Flip clip_id's pinned state and persist it. Returns the new state.

    Raises OSError if characters_root isn't writable - callers should catch
    that and report a warning rather than crash, the same way
    browse_characters_root/reload_pools degrade on other I/O failures."""
    pinned = load(characters_root)
    if clip_id in pinned:
        pinned.discard(clip_id)
        new_state = False
    else:
        pinned.add(clip_id)
        new_state = True
    _save(characters_root, pinned)
    return new_state


def is_pinned(characters_root, clip_id):
    return clip_id in load(characters_root)
