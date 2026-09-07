"""Two hand slots, and PZ's own rule for when a weapon occupies both of them.

Pure Python, no bpy - same split as pools.py. This is a direct port of the
*rules* in the real game's `scripts/items/Equipment.gd` / `ItemRegistry.gd`
(`weapon_type_for_hands`, `TWO_HAND_FALLBACK`, `requires_both_hands`), not a
re-derivation - see those two files if this ever needs to be re-checked
against the game. The one deliberate simplification: the Godot side compares
`InventoryItem` *object identity* to decide "the same weapon is in both
hands" (PZ's own bytecode does an `if_acmpne`, not a value compare); this
addon has no per-instance inventory, so an item's pool id string stands in
for identity. That is exact for what this addon needs it for - one preview
character can only ever be holding one instance of a given item at a time,
so "same id" and "same instance" coincide here.

Item classification (`weapon.type` / `two_hand` / `both_hands`) is NOT mined
by this addon's own extractor - it lives in `assets/items/item_manifest.json`,
built separately by `tools/pz_items.py` (the item catalogue export, a sibling
to the character export, not a dependency of it). `load_item_manifest()`
finds it next to `characters_root` by the repo layout both exports share.
Missing or unreadable degrades to "nothing known about any item" rather than
raising - same convention as pools.locomotion_masks() - so a `.pzc`-only
checkout (no item catalogue exported yet) still lets hands be filled, just
without the both-hands rule.
"""

import json
import os

UNARMED = ""

_manifest_cache = {"path": None, "data": None}


def _item_manifest_path(characters_root):
    """`<repo>/assets/characters` -> `<repo>/assets/items/item_manifest.json` -
    the two exports are siblings under `assets/`, per tools/pz_items.py's own
    DEFAULT_OUT and pools.default_characters_root()'s directory layout."""
    assets_root = os.path.dirname(os.path.normpath(characters_root))
    return os.path.join(assets_root, "items", "item_manifest.json")


def load_item_manifest(characters_root, force=False):
    """{"Base.Shovel": {"weapon": {...}}, ...} - the raw `items` map out of
    item_manifest.json, keyed exactly as tools/pz_items.py wrote it. Empty
    dict if the file is absent or unreadable."""
    path = _item_manifest_path(characters_root)
    if not force and _manifest_cache["path"] == path and _manifest_cache["data"] is not None:
        return _manifest_cache["data"]
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            data = raw.get("items", raw) if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            data = {}
    _manifest_cache.update(path=path, data=data)
    return data


def weapon_info(item_manifest, item_id):
    """The `"weapon"` dict for a pool item name (e.g. `"Shovel"`), or None.

    Pool entries carry PZ's bare item name (`weapon.txt`'s `item Shovel`
    block); item_manifest.json keys are `Module.Item` (`"Base.Shovel"`).
    Tried as `Base.<item_id>` first - true for every vanilla weapon checked
    (all mining in this addon and in tools/pz_items.py runs against the
    un-modded base game) - then falls back to a suffix search so a modded
    item under a different module still resolves rather than reading as
    unarmed.
    """
    if not item_id:
        return None
    direct = item_manifest.get(f"Base.{item_id}")
    if direct is not None:
        return direct.get("weapon")
    suffix = f".{item_id}"
    for key, entry in item_manifest.items():
        if key.endswith(suffix):
            return entry.get("weapon")
    return None


#: PZ's `WeaponType.getWeaponType(IsoGameCharacter, primary, secondary)` demotes
#: exactly these four when the primary is not identically also the secondary -
#: see ItemRegistry.gd's TWO_HAND_FALLBACK for the bytecode-level derivation
#: this mirrors verbatim. `knife`/`heavy`/`throwing` are absent on purpose:
#: PZ returns those before the both-hands check runs, so they never demote.
TWO_HAND_FALLBACK = {
    "2handed": "1handed",
    "spear": "1handed",
    "chainsaw": "1handed",
    "firearm": "handgun",
}


def requires_both_hands(item_manifest, item_id):
    """True for a weapon PZ physically forces into both hand slots
    (`RequiresEquippedBothHands`) - rare (rifles, some improvised two-handers),
    and distinct from a weapon that merely *can* be swung two-handed."""
    info = weapon_info(item_manifest, item_id)
    return bool(info and info.get("both_hands"))


def weapon_type_of(item_manifest, item_id):
    info = weapon_info(item_manifest, item_id)
    return (info or {}).get("type", UNARMED)


def weapon_type_for_hands(item_manifest, primary_id, secondary_id):
    """PZ's real, two-argument `getWeaponType` - the one that actually drives
    the animation variable. `weapon_type_of()` alone is not enough: a
    `2handed`/`spear`/`chainsaw`/`firearm` item only keeps that classification
    while the *same* item also fills the other hand."""
    if not primary_id:
        return UNARMED
    wtype = weapon_type_of(item_manifest, primary_id)
    if wtype == UNARMED:
        return UNARMED
    if wtype in TWO_HAND_FALLBACK and primary_id != secondary_id:
        return TWO_HAND_FALLBACK[wtype]
    return wtype


class Equipment:
    """The two hand slots for one preview character.

    Mirrors `Equipment.gd` field-for-field: `equip_primary()`/
    `equip_secondary()` carry the same both-hands-forces-both-slots and
    both-hands-item-gets-bumped-by-anything-else rules, so a caller cannot
    reach a state PZ itself could not (a two-handed item in one hand with
    something else in the other never happens - see those two methods).
    """

    def __init__(self, item_manifest, primary="", secondary=""):
        self._item_manifest = item_manifest
        self._primary = primary
        self._secondary = secondary

    def primary(self):
        return self._primary

    def secondary(self):
        return self._secondary

    def weapon_type(self):
        return weapon_type_for_hands(self._item_manifest, self._primary, self._secondary)

    def requires_both_hands(self, item_id):
        return requires_both_hands(self._item_manifest, item_id)

    def is_two_hand(self, item_id):
        info = weapon_info(self._item_manifest, item_id)
        return bool(info and info.get("two_hand"))

    def equip_primary(self, item_id):
        """Puts `item_id` in the primary (right) hand. "" empties it.

        A `RequiresEquippedBothHands` item also takes the secondary slot -
        the same id, so `primary() == secondary()` is exactly "this is one
        two-handed item, not two items". Anything already in the secondary
        that was only there because a *different* both-hands item put it
        there is displaced, matching Equipment.gd's `equip_primary()`.
        """
        if not item_id:
            self._set_slots("", self._secondary if self._secondary != self._primary else "")
            return
        if self.requires_both_hands(item_id):
            self._set_slots(item_id, item_id)
        else:
            keep = self._secondary if self._secondary != self._primary else ""
            self._set_slots(item_id, keep)

    def equip_secondary(self, item_id):
        """Puts `item_id` in the secondary (left) hand. "" empties it.

        Refuses to leave a both-hands weapon half-held: if the primary is
        one, it is dropped from both slots first - unless `item_id` is that
        same weapon, which is how a two-handed grip is expressed.
        """
        if not item_id:
            self._set_slots(self._primary if self._primary != self._secondary else "", "")
            return
        keep = self._primary
        if keep and keep != item_id and self.requires_both_hands(keep):
            keep = ""
        self._set_slots(keep, item_id)

    def unequip_all(self):
        self._set_slots("", "")

    def _set_slots(self, new_primary, new_secondary):
        self._primary = new_primary
        self._secondary = new_secondary

    def __repr__(self):
        p = self._primary or "-"
        s = self._secondary or "-"
        return f"Equipment({p} | {s} -> '{self.weapon_type()}')"


def resolve_hands(item_manifest, right_id, left_id):
    """(resolved_right, resolved_left, weapon_type) for a raw
    (right-hand pick, left-hand pick) pair - the normalization
    ops._rebuild_scene() runs the "weapon_right"/"weapon_left" layers'
    current values through on every rebuild, before anything gets built or
    shown.

    A fixed right-then-left replay is enough to collapse *any* combination
    of two independent picks into a state PZ itself could reach, with no
    need to know which of the two the user actually just changed:
    `equip_primary(right_id)` establishes the right hand (and, for a
    both-hands weapon, force-fills the left with it); `equip_secondary
    (left_id)` then either keeps that forced fill (identical id - two-handed
    grip intact) or overrides it, which is exactly `equip_secondary()`'s own
    both-hands-gets-evicted rule. Feeding the *previous* resolved result
    back in as next call's `right_id`/`left_id` (which is what happens,
    since the corrected values are written back into the layers) keeps this
    idempotent - re-resolving an already-legal pair returns it unchanged.
    """
    eq = Equipment(item_manifest)
    eq.equip_primary(right_id)
    eq.equip_secondary(left_id)
    return eq.primary(), eq.secondary(), eq.weapon_type()
