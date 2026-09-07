"""Loading assets/characters/appearance_pools.json + character_manifest.json,
and the pieces of CharacterAssetRegistry.gd's random_appearance() this addon
needs: the render order, the layer option lists the panel shows, and the
weighted-pick/exclusivity/underwear-set logic Randomize uses.

Pure Python, no bpy - same split as manifest.py in the parent package.
"""

import json
import os
import random

NONE_ITEM = ""  # sentinel: this optional layer/slot is unworn/bald/clean-shaven

_cache = {"root": None, "pools": None, "manifest": None}
_locomotion_cache = {"root": None, "masks": None}


def default_characters_root():
    """Best guess at where the extractor wrote its output.

    Probed rather than counted, because this add-on ships in two situations
    and the answer is a different number of levels up in each:

        pz-character/src/addon/pz_character_viewer/character/pools.py
        pz-character/assets/characters/            <- 4 up, split-out repo
        <repo>/assets/characters/                  <- 6 up, still inside
                                                      pz-testing, sharing that
                                                      repo's own export rather
                                                      than duplicating 57 MB

    Installed the normal way - copied or zipped into Blender's own
    ``scripts/addons`` - neither exists and nothing here can find the data, so
    this returns "" and the panel asks for the folder rather than silently
    pointing at a path with no files in it. Set it once in the add-on
    preferences (or with the folder button in the sidebar) and Blender
    remembers it.

    ``characters.pzc`` has to be present for a candidate to count: an
    ``assets/characters`` left behind by an interrupted export would otherwise
    win over a real one further up.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for up in (3, 4, 5, 6, 7):
        cand = os.path.normpath(
            os.path.join(here, *([".."] * up), "assets", "characters")
        )
        if os.path.isfile(os.path.join(cand, "characters.pzc")):
            return cand
    return ""

def clear_cache():
    _cache.update(root=None, pools=None, manifest=None)
    _locomotion_cache.update(root=None, masks=None)
    _index_cache.update(key=None, index=None)


def load(characters_root, force=False):
    if not force and _cache["root"] == characters_root and _cache["pools"] is not None:
        return _cache["pools"]

    with open(os.path.join(characters_root, "appearance_pools.json"), "r", encoding="utf-8") as f:
        pools = json.load(f)
    manifest_path = os.path.join(characters_root, "character_manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    _cache.update(root=characters_root, pools=pools, manifest=manifest)
    return pools


def manifest(characters_root):
    load(characters_root)
    return _cache["manifest"]


def locomotion_masks(characters_root, force=False):
    """bone_masks from assets/characters/locomotion_manifest.json, e.g.
    {"Human": {"upper": [bone_name, ...], "lower": [bone_name, ...]}} -
    already derived offline by tools/pz_animscript.py, not re-derived here."""
    if not force and _locomotion_cache["root"] == characters_root and _locomotion_cache["masks"] is not None:
        return _locomotion_cache["masks"]

    path = os.path.join(characters_root, "locomotion_manifest.json")
    masks = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            masks = json.load(f).get("bone_masks", {})

    _locomotion_cache.update(root=characters_root, masks=masks)
    return masks


def filter_clip_ids(characters_root, substring="", category=""):
    """Sorted clip ids (character_manifest.json's "clips" keys) filtered by a
    case-insensitive substring and/or a clip_sets category - the id's leading
    "Bob"/"Kate"/"Zombie" path segment. Mirrors manifest.filter_ids() in the
    parent package."""
    clips = manifest(characters_root).get("clips", {})
    substring = substring.lower()
    result = []
    for clip_id in sorted(clips.keys()):
        if category and not (clip_id == category or clip_id.startswith(category + "/")):
            continue
        if substring and substring not in clip_id.lower():
            continue
        result.append(clip_id)
    return result


def render_order(pools):
    """Clothing/overlay slots in PZ's own render order, with anything the
    rules didn't name appended - matches _render_order() in
    CharacterAssetRegistry.gd so a slot can never go unreachable."""
    order = list(pools.get("rules", {}).get("render_order", []))
    for slot in pools.get("mesh_slots", []) + pools.get("overlay_slots", []):
        if slot not in order:
            order.append(slot)
    return order


#: id(pools) -> the index below. `pools` is the cached dict `load()` hands
#: back, so its identity is stable for as long as the cache is, and the panel
#: calls item_allows_tint() once per visible row per redraw - rebuilding a
#: ~1,800-entry index at that rate is the one thing here that would actually
#: show up as UI lag.
_index_cache = {"key": None, "index": None}


def _by_item_index(pools):
    if _index_cache["key"] == id(pools) and _index_cache["index"] is not None:
        return _index_cache["index"]
    by_item = {}
    for kind, is_mesh in (("clothing", True), ("overlays", False)):
        for slot, entries in pools.get(kind, {}).items():
            for entry in entries:
                item = entry.get("item", "")
                if item:
                    by_item[item] = {"slot": slot, "entry": entry, "kind": "mesh" if is_mesh else "overlay"}
    _index_cache.update(key=id(pools), index=by_item)
    return by_item


#: Display-only overrides where the pools slot key reads badly title-cased.
#: The slot key itself has to stay whatever _item_body_locations() lowercased
#: PZ's BodyLocation to (here, "eyes" - see tools/pz_characters.py) since
#: that's what appearance_pools.json/randomize_appearance() actually key on;
#: this only changes what the panel prints.
SLOT_LABELS = {"eyes": "Glasses", "mask": "Mask", "maskeyes": "Mask (Eyes)",
               "maskfull": "Mask (Full)", "fannypackfront": "Fanny Pack (Front)",
               "fannypackback": "Fanny Pack (Back)", "ammostrap": "Ammo Strap",
               "shoulderholster": "Shoulder Holster"}


def layers_for_gender(pools, gender):
    """Ordered list of {"slot", "label", "options": [ids...]} for the
    customization panel. `options[0]` is always the "None"/bald/clean-shaven
    sentinel for anything optional; the body itself carries no such entry -
    there is always exactly one body per gender.

    An "id" for a clothing/overlay slot is the pool entry's "item" name (the
    same key random_appearance() resolves through _by_item), so cycling
    stores something build.py can look back up without re-deriving it."""
    layers = []

    tones = pools.get("body", {}).get(gender, {}).get("skin_tones", [])
    layers.append({"slot": "skin_tone", "label": "Skin Tone", "options": list(tones)})

    hair = [h["id"] for h in pools.get("hair", {}).get(gender, [])]
    layers.append({"slot": "hair", "label": "Hair", "options": [NONE_ITEM] + hair})

    if gender == "male":
        beards = [b["id"] for b in pools.get("beard", [])]
        layers.append({"slot": "beard", "label": "Beard", "options": [NONE_ITEM] + beards})

    for slot in render_order(pools):
        if slot == "weapon":
            # Two layers, not one: a held item is picked per hand. Both
            # share the same option list (weapon_options()) - which hand a
            # pick lands in is what the panel's slot key says, not anything
            # about the item itself. The "only one two-handed item" rule
            # this needs is applied afterward, on the resolved appearance
            # dict (see equipment.resolve_hands(), called from
            # ops._rebuild_scene()) - these two layers hold whatever was
            # picked most recently, cycle_layer/reset_layer neither know nor
            # need to know about the collapse.
            options = weapon_options(pools)
            if len(options) > 1:
                layers.append({"slot": "weapon_right", "label": "Right Hand", "options": options})
                layers.append({"slot": "weapon_left", "label": "Left Hand", "options": options})
            continue
        items = [e.get("item", "") for e in pools.get("clothing", {}).get(slot, [])]
        items += [e.get("item", "") for e in pools.get("overlays", {}).get(slot, []) if e.get("item", "") not in items]
        items = [i for i in items if i]
        if not items:
            continue
        label = SLOT_LABELS.get(slot, slot.replace("_", " ").title())
        layers.append({"slot": slot, "label": label, "options": [NONE_ITEM] + items})

    return layers


def weapon_options(pools):
    """[NONE_ITEM] + every held-item id in the `weapon` pool, gender-independent
    (both genders share one weapon mesh list) - the shared options list both
    the "Right Hand" and "Left Hand" layers get in layers_for_gender()."""
    items = [e.get("item", "") for e in pools.get("clothing", {}).get("weapon", [])]
    return [NONE_ITEM] + [i for i in items if i]


def resolve_item(pools, item, gender):
    """(kind, mesh_id_or_None, entry_dict) for a chosen item name, or None if
    unresolvable for this gender (e.g. a mesh garment with no model for it).
    Mirrors _resolve_entry() in CharacterAssetRegistry.gd."""
    entry = _by_item_index(pools).get(item)
    if entry is None:
        return None
    ent = entry["entry"]
    if entry["kind"] == "mesh":
        mesh_id = ent.get(gender)
        if not mesh_id:
            return None
        return "mesh", mesh_id, ent
    textures = ent.get("textures", [])
    if not textures:
        return None
    return "overlay", None, ent


# --------------------------------------------------------------------------
# Clothing tint - PZ's own per-garment colour picker.
#
# `clothingItems/<Item>.xml` carries `<m_AllowRandomTint>`; the exporter mines
# it into a pool entry's "tint" key (present only when true). PZ shows a
# colour button for exactly the items that have it
# (`CharacterCreationMain:updateColorButton()`), stores the choice on the
# ItemVisual, and multiplies it into the garment's base texture --
# `media/shaders/hueChange.frag` is literally `col.r *= R; col.g *= G;
# col.b *= B;` with alpha untouched. It is a multiply, not a hue rotation,
# despite the shader's name.
#
# **Colours here are LINEAR, and that is what makes the multiply match PZ.**
# PZ multiplies the sRGB-encoded texel by an sRGB-encoded tint. Blender hands
# both `image.pixels` and a material's Base Color to us already decoded to
# linear, so the naive thing - multiplying linear by the picked sRGB triple -
# would be visibly wrong. Under the pure-gamma approximation the identity
# `decode(a*t) == decode(a) * decode(t)` holds exactly, so multiplying linear
# texels by the *linear* tint reproduces PZ's sRGB multiply. It is also
# exactly what Blender's own colour picker wants: a FloatVectorProperty with
# subtype "COLOR" is scene-linear, and its Hex field shows the sRGB hex, so
# the number the user reads off the picker is the number PZ would store.
# The identity is exact only for a pure power curve; the real sRGB transfer
# function has a linear toe near black, so the two disagree slightly.
# **Measured**, over a real overlay texture at six tints (test_character_tint.py
# section 7): worst case **1.87/255**, and it is the dark tints that are worst
# (0.05 grey: 1.87; 0.9 grey: 0.20). Under 2/255 is below what an 8-bit
# display can show, so this is a rounding difference, not an approximation
# anyone can see - but it is not zero, and a previous revision of this comment
# claimed it was confined to "the bottom 0.3% of the range", which the
# measurement does not support.
# --------------------------------------------------------------------------

#: Untinted. `ItemVisual.getTint()` returns ImmutableColor.white for any item
#: without the flag, and white is the multiply's identity, so an untintable
#: garment and a garment tinted white are the same picture.
DEFAULT_TINT = (1.0, 1.0, 1.0)


def item_allows_tint(pools, item):
    """Whether this clothing/overlay item carries PZ's `m_AllowRandomTint`.

    False for anything unknown, for the "" (unworn) sentinel, and for pools
    files written before the exporter mined the flag - so an old
    appearance_pools.json degrades to "no item is tintable" rather than
    raising. Re-mine one with `pz_characters.py --pools-only`.
    """
    if not item:
        return False
    found = _by_item_index(pools).get(item)
    return bool(found and found["entry"].get("tint"))


def any_tintable(pools, appearance):
    """True if anything currently worn can be tinted - lets the panel hide
    the whole colour affordance rather than show a dead control."""
    return any(item_allows_tint(pools, item)
               for slot, item in appearance.items()
               if slot not in ("skin_tone", "hair", "beard"))


def srgb_to_linear(c):
    """The exact sRGB EOTF, per channel. Blender applies this on load, so it
    is also how a colour typed into the picker's Hex field becomes the float
    the property stores."""
    return tuple(
        v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        for v in c
    )


def linear_to_srgb(c):
    """Inverse of srgb_to_linear() - for reporting a tint the way PZ would
    write it, e.g. in an operator's status line."""
    return tuple(
        v * 12.92 if v <= 0.0031308 else 1.055 * (v ** (1.0 / 2.4)) - 0.055
        for v in c
    )


def random_tint(rng=None):
    """Port of `OutfitRNG.randomImmutableColor()`: hue anywhere, saturation
    capped at 0.6 and value clamped to [0.1, 0.9]. That is what keeps PZ's
    random clothing muted rather than primary-coloured, and why it never
    rolls pure black or pure white.

    PZ's numbers are HSB in sRGB space; the result is converted to linear on
    the way out, per this section's header.
    """
    import colorsys

    rng = rng or random.Random()
    h = rng.uniform(0.0, 1.0)
    s = rng.uniform(0.0, 0.6)
    # PZ raises the floor to 0.2 under the noBlackClothes sandbox option;
    # 0.1 is the default game's value and the one used here.
    v = rng.uniform(0.1, 0.9)
    return srgb_to_linear(colorsys.hsv_to_rgb(h, s, v))


def randomize_tints(pools, appearance, rng=None):
    """{slot: linear rgb} for every worn item that allows a tint, matching
    `ClothingItemReference.randomize()` - which rolls a colour for exactly
    the tintable items and pins everything else to white."""
    rng = rng or random.Random()
    out = {}
    for slot, item in appearance.items():
        if slot in ("skin_tone", "hair", "beard"):
            continue
        if item_allows_tint(pools, item):
            out[slot] = random_tint(rng)
    return out


def roll_random_damage(pools, appearance, gender, rng=None, hole_count=1, damage=None):
    """Adds `hole_count` new hole(s) to random regions, mirroring PZ's own
    Clothing.addRandomHole(): picks a uniformly random region and skips it
    if already holed, rather than picking only from not-yet-holed regions -
    same rejection-sampling shape as the decompiled Java.

    **v1 simplification, worth re-reading before extending this:** PZ tracks
    a garment's own `getCoveredParts()` and only rolls a hole where that
    specific item has cloth. This add-on has no per-slot region coverage map
    yet (mesh clothing's own `masks` list uses the coarser, differently-named
    BODY_MASK_REGIONS set - see src/extractor/pz_characters.py's comment on
    DAMAGE_REGIONS), so every one of the 18 DAMAGE_REGIONS is treated as
    eligible regardless of what is actually worn there. A hole "under"
    nothing worn just lands on bare skin, which is a meaningful state in PZ
    too, so this reads as a simplification rather than a bug - revisit if
    testing shows it picking oddly.

    `damage` is the current {"holes": [...], "blood": [...], "dirt": [...]}
    dict (from props.damage_dict()); pass None/{} to start from a clean
    character. Returns a NEW dict in the same shape - does not mutate the
    one passed in.
    """
    rng = rng or random.Random()
    regions = list(pools.get("damage", {}).get("hole_masks", {}).keys())
    out = {
        "holes": list((damage or {}).get("holes") or []),
        "blood": list((damage or {}).get("blood") or []),
        "dirt": list((damage or {}).get("dirt") or []),
    }
    if not regions:
        return out

    for _ in range(max(1, hole_count)):
        region = regions[rng.randrange(len(regions))]
        if region in out["holes"]:
            continue
        out["holes"].append(region)
    return out


def default_appearance(pools, gender="male"):
    """Bald, naked - every optional layer at its None sentinel, skin tone at
    pool index 0. This is what the Default button and initial preview use."""
    layers = layers_for_gender(pools, gender)
    return {layer["slot"]: layer["options"][0] if layer["slot"] == "skin_tone" else NONE_ITEM for layer in layers}


# --------------------------------------------------------------------------
# Randomize - a port of random_appearance() in CharacterAssetRegistry.gd.
# Weighted picks, exclusivity-by-replacement, and the underwear matched-set
# rule all come from there; see that file's doc comments for why each one
# exists (PZ's own ClothingSelectionDefinitions/BodyLocations/
# UnderwearDefinition tables).
# --------------------------------------------------------------------------


def randomize_appearance(pools, gender, rng=None):
    rng = rng or random.Random()
    rules = pools.get("rules", {})
    by_item = _by_item_index(pools)
    order = render_order(pools)

    worn = {}  # slot -> item name

    def wear(slot, item):
        for pair in rules.get("exclusive", []):
            if len(pair) != 2:
                continue
            other = pair[1] if pair[0] == slot else (pair[0] if pair[1] == slot else "")
            if other:
                worn.pop(other, None)
        worn[slot] = item

    def pick_wearable(fallback_slot, names):
        candidates = []
        for name in names:
            found = by_item.get(name)
            if found:
                candidates.append((found["slot"], found["entry"]))
        if not candidates:
            for entry in pools.get("clothing", {}).get(fallback_slot, []) + pools.get("overlays", {}).get(
                fallback_slot, []
            ):
                candidates.append((fallback_slot, entry))
        wearable = [(slot, entry) for slot, entry in candidates if resolve_item(pools, entry.get("item", ""), gender)]
        if not wearable:
            return None
        return wearable[rng.randrange(len(wearable))]

    # Underwear: a matched set, not two independent rolls - see
    # _wear_underwear()'s doc comment on why (a set is gendered as a whole).
    underwear = rules.get("underwear", {})
    sets = underwear.get("sets", [])
    if sets and rng.randint(1, 100) <= int(underwear.get("base_chance", 100)):
        candidates = [s for s in sets if not s.get("gender") or s.get("gender") == gender]
        total = sum(int(s.get("weight", 1)) for s in candidates)
        if candidates and total > 0:
            roll = rng.randint(1, total)
            chosen = candidates[-1]
            for s in candidates:
                roll -= int(s.get("weight", 1))
                if roll <= 0:
                    chosen = s
                    break
            tops = chosen.get("top", [])
            if tops:
                top_total = sum(int(t.get("chance", 1)) for t in tops)
                top_roll = rng.randint(1, max(top_total, 1))
                for t in tops:
                    top_roll -= int(t.get("chance", 1))
                    if top_roll <= 0:
                        found = by_item.get(t.get("item", ""))
                        if found:
                            wear(found["slot"], t.get("item", ""))
                        break
            bottom = chosen.get("bottom", "")
            found = by_item.get(bottom)
            if found:
                wear(found["slot"], bottom)

    outfit = rules.get("outfits", {}).get(gender, {})
    for slot in order:
        rule = outfit.get(slot)
        if not rule:
            continue
        if rng.randint(1, 100) > int(rule.get("chance", 100)):
            continue
        pick = pick_wearable(slot, rule.get("items", []))
        if pick:
            wear(pick[0], pick[1].get("item", ""))

    outer = outfit.get("__outerwear__", {})
    if outer and rng.randint(1, 100) <= int(outer.get("chance", 0)):
        slots = outer.get("slots", [])
        if slots:
            slot = slots[rng.randrange(len(slots))]
            pick = pick_wearable(slot, [])
            if pick:
                wear(pick[0], pick[1].get("item", ""))

    appearance = default_appearance(pools, gender)
    tones = pools.get("body", {}).get(gender, {}).get("skin_tones", [])
    if tones:
        appearance["skin_tone"] = tones[rng.randrange(len(tones))]

    hair = pools.get("hair", {}).get(gender, [])
    if hair and rng.random() < 0.9:
        appearance["hair"] = hair[rng.randrange(len(hair))]["id"]

    if gender == "male":
        beards = pools.get("beard", [])
        if beards and rng.random() < 0.4:
            appearance["beard"] = beards[rng.randrange(len(beards))]["id"]

    for slot, item in worn.items():
        appearance[slot] = item

    return appearance
