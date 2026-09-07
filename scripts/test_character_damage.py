"""Headless regression test for the damage system - holes, blood, dirt, and
zombie skins.

Run it:

    "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" \
        --background --factory-startup \
        --python scripts/test_character_damage.py

Exits non-zero on failure. Runs against the real shipped assets/characters/,
which must have been exported by a pz_characters.py new enough to mine the
"damage" pool and the zombie skin_tones entries - re-mine with
`extract.bat --pools-only` if section 1 reports zero hole masks.

Why this exists
----------------
Two things here are easy to get subtly wrong and have a bug that looks fine
on screen:

  * composite_garment_texture() MUST return a fresh Image, never the one
    _load_image() cached - that cache is shared across every build, so
    writing a hole into it in place would corrupt every other character (and
    every other build of this one) wearing the same garment texture. Section
    4 asserts the returned image is a different datablock, and section 5
    builds two different damage states on the same garment mesh_id in one
    session and checks neither corrupted the other.
  * Holes have to cut through everything already composited - skin AND
    overlay clothing - not just the skin base. Section 3 checks alpha is
    actually zero in a masked region after compositing, not just "some
    pixels changed".

What it does not cover: the panel (no bpy.ops here beyond register), and
blood/dirt colour correctness (only that the masked region's alpha rises,
mirroring how test_character_tint.py checks plumbing over exact pixel
colour).
"""
import os
import sys

# This file lives in pz-character/scripts/; HERE/REPO is pz-character
# itself, one level up.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = HERE
ADDON_DIR = os.path.join(HERE, "src", "addon")
sys.path.insert(0, ADDON_DIR)

import bpy  # noqa: E402

import pz_character_viewer  # noqa: E402
from pz_character_viewer.character import build, pools  # noqa: E402
from pz_character_viewer.character.pzc_reader import PZCReader  # noqa: E402

# See scripts/_charroot.py for the resolution order and for what
# happens when there is no export yet (a message, not a stack trace).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _charroot  # noqa: E402

ROOT = _charroot.require()
ADDON_ID = "pz_character_viewer"


def _assert_repo_module(mod, expected_dir, label):
    """See test_character_tint.py's own copy of this - same trap, an
    installed copy under Blender's scripts/addons can shadow the repo even
    under --factory-startup."""
    import os as _os

    actual = _os.path.normcase(_os.path.dirname(_os.path.abspath(mod.__file__)))
    wanted = _os.path.normcase(_os.path.abspath(expected_dir))
    if not actual.startswith(wanted):
        raise SystemExit(
            f"{label} was imported from {actual}, not {wanted}.\n"
            "An installed copy in Blender's scripts/addons is shadowing the repo."
        )


_assert_repo_module(pz_character_viewer, ADDON_DIR, "pz_character_viewer")

failures = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        failures.append(label)


pz_character_viewer.register()
addon = bpy.context.preferences.addons.get(ADDON_ID)
if addon is not None:
    addon.preferences.characters_root = ROOT
print("characters root:", ROOT)
if not os.path.isfile(os.path.join(ROOT, "characters.pzc")):
    sys.exit("no characters.pzc under " + ROOT + " - run setup.bat first")

pools_data = pools.load(ROOT)

# ------------------------------------------------------- extractor pool ---
print("\n[1] damage pool reached the add-on")
damage_pool = pools_data.get("damage", {})
hole_masks = damage_pool.get("hole_masks", {})
missing_regions = [r for r in (
    "Back", "Chest", "FootL", "FootR", "Groin", "HandL", "HandR", "Head",
    "LArmL", "LArmR", "LLegL", "LLegR", "Neck", "Stomach",
    "UArmL", "UArmR", "ULegL", "ULegR",
) if r not in hole_masks]
check("all 18 DAMAGE_REGIONS present", not missing_regions, f"missing: {missing_regions}")
check("blood_overlay present", bool(damage_pool.get("blood_overlay")))
check("dirt_overlay present", bool(damage_pool.get("dirt_overlay")))

print("\n[1b] zombie skins merged into skin_tones")
for gender in ("male", "female"):
    tones = pools_data.get("body", {}).get(gender, {}).get("skin_tones", [])
    zombie_tones = [t for t in tones if "Zombie_Body" in t]
    check(f"{gender} skin_tones has 5 zombie entries", len(zombie_tones) == 5,
          f"{zombie_tones}")
    check(f"{gender} index 0 is still the canonical player tone",
          bool(tones) and "Zombie" not in tones[0], tones[0] if tones else "(empty)")

# --------------------------------------------------------- random roll ----
print("\n[2] roll_random_damage is deterministic and skips already-holed regions")
import random

rng1 = random.Random(1234)
rng2 = random.Random(1234)
appearance0 = pools.default_appearance(pools_data, "male")
d1 = pools.roll_random_damage(pools_data, appearance0, "male", rng=rng1, hole_count=5)
d2 = pools.roll_random_damage(pools_data, appearance0, "male", rng=rng2, hole_count=5)
check("same seed -> same holes", d1["holes"] == d2["holes"], f"{d1['holes']} vs {d2['holes']}")
check("no duplicate regions", len(d1["holes"]) == len(set(d1["holes"])), f"{d1['holes']}")

d3 = pools.roll_random_damage(pools_data, appearance0, "male",
                              rng=random.Random(1), hole_count=1, damage=d1)
check("existing holes are preserved, not replaced",
      set(d1["holes"]) <= set(d3["holes"]), f"{d1['holes']} not subset of {d3['holes']}")

# ---------------------------------------------------- _apply_damage_passes
print("\n[3] a hole reveals skin (restore=), never punches a see-through gap")
import numpy as np

region = next(iter(hole_masks))
mask_img = build._load_image(ROOT, hole_masks[region])
mw, mh = mask_img.size
mask_arr = np.array(mask_img.pixels[:], dtype=np.float32).reshape(mh, mw, 4)
gate = mask_arr[:, :, 3] > 0.0
check(f"{region} region has some masked texels", bool(gate.any()), f"{gate.sum()} texels")

# Synthetic "clothing painted over skin": opaque grey skin, opaque blue
# "shirt" painted over the whole image - stands in for a worn garment
# covering this region, without needing a real garment's UV footprint to
# happen to line up with this particular damage region.
skin = np.zeros((mh, mw, 4), dtype=np.float32)
skin[:, :, :3] = 0.6
skin[:, :, 3] = 1.0
clothed = skin.copy()
clothed[:, :, :3] = np.array([0.1, 0.1, 0.8])

# No `restore` (composite_garment_texture()'s path - a mesh garment is its
# own separate object over the body mesh in 3D, so alpha 0 correctly reveals
# what is drawn underneath in the normal way).
erased = clothed.copy()
build._apply_damage_passes(erased, ROOT, pools_data,
                           {"holes": [region], "blood": [], "dirt": []})
check("no restore -> hole erases to alpha 0 (mesh-garment path)",
      bool(np.all(erased[gate, 3] == 0.0)), f"max={erased[gate, 3].max()}")

# `restore=skin` (composite_body_texture()'s path - skin and overlay
# clothing share one flat image, so a hole has to fall back to the
# pre-clothing skin snapshot instead of alpha 0, or it punches a
# see-through gap straight through the character - the bug reported from
# an actual build).
restored = clothed.copy()
build._apply_damage_passes(restored, ROOT, pools_data,
                           {"holes": [region], "blood": [], "dirt": []}, restore=skin)
check("restore=skin -> hole stays fully opaque, no see-through gap",
      bool(np.all(restored[gate, 3] == 1.0)), f"min={restored[gate, 3].min()}")
check("restore=skin -> hole shows the skin colour, not the clothing colour",
      bool(np.allclose(restored[gate, :3], skin[gate, :3])))
check("restore=skin -> untouched outside the mask (still clothing colour)",
      bool(np.allclose(restored[~gate, :3], clothed[~gate, :3])))

print("\n[3b] a real build stays fully opaque on bare skin - no worn garment there")
appearance = pools.default_appearance(pools_data, "male")  # naked/bald
damage = {"holes": [region], "blood": [], "dirt": []}
tex_holed = build.composite_body_texture(ROOT, pools_data, appearance["skin_tone"],
                                         appearance, "male", {}, damage)
w, h = tex_holed.size
check(f"{region} mask matches body texture size", (mw, mh) == (w, h), f"{(mw, mh)} vs {(w, h)}")
if (mw, mh) == (w, h):
    arr_holed = np.array(tex_holed.pixels[:], dtype=np.float32).reshape(h, w, 4)
    check("bare-skin hole leaves alpha at 1.0 (this is the exact reported bug)",
          bool(np.all(arr_holed[gate, 3] == 1.0)), f"min={arr_holed[gate, 3].min()}")

print("\n[3c] blood raises alpha in the masked region without a hole")
tex_plain = build.composite_body_texture(ROOT, pools_data, appearance["skin_tone"],
                                         appearance, "male", {}, None)
arr_plain = np.array(tex_plain.pixels[:], dtype=np.float32).reshape(h, w, 4)
damage_blood = {"holes": [], "blood": [region], "dirt": []}
tex_blood = build.composite_body_texture(ROOT, pools_data, appearance["skin_tone"],
                                         appearance, "male", {}, damage_blood)
arr_blood = np.array(tex_blood.pixels[:], dtype=np.float32).reshape(h, w, 4)
if (mw, mh) == (w, h):
    check("blood pass changed the masked region",
          not np.array_equal(arr_plain[gate], arr_blood[gate]))

# -------------------------------------------------- mesh garment holes ----
print("\n[4] composite_garment_texture() never mutates the cached _load_image()")
reader = PZCReader(os.path.join(ROOT, "characters.pzc"))

target = None
for gender in ("male", "female"):
    for slot, entries in pools_data.get("clothing", {}).items():
        for entry in entries:
            textures = entry.get("textures", [])
            mesh_id = entry.get(gender)
            if textures and mesh_id and reader.get_mesh(mesh_id) is not None:
                target = (gender, slot, entry, textures[0])
                break
        if target:
            break
    if target:
        break
check("found a buildable mesh garment with a texture", target is not None, str(target))

if target:
    gender, slot, entry, tex_rel = target
    cached = build._load_image(ROOT, tex_rel)
    damaged = build.composite_garment_texture(ROOT, pools_data, tex_rel, {"holes": [region], "blood": [], "dirt": []})
    check("composite_garment_texture returned an image", damaged is not None)
    if damaged is not None:
        check("returned image is NOT the cached _load_image() datablock",
              damaged.name != cached.name, f"{damaged.name} vs {cached.name}")
        cached_pixels_untouched = np.array(cached.pixels[:], dtype=np.float32)
        check("cached source image still has non-zero alpha somewhere",
              bool((cached_pixels_untouched[3::4] > 0.0).any()))

    print("\n[5] two different damage states on the same garment don't collide")
    appearance_g = pools.default_appearance(pools_data, gender)
    appearance_g[slot] = entry["item"]

    build.build_character(bpy.context, ROOT, reader, pools_data, appearance_g, gender,
                          {}, {"holes": [region], "blood": [], "dirt": []})
    objs_a = build.slot_objects(bpy.context, slot)
    img_a = objs_a[0].data.materials[0].node_tree.nodes.get("Image Texture")
    name_a = objs_a[0].data.materials[0].node_tree.nodes.get("Image Texture").image.name if img_a else None

    other_region = next(r for r in hole_masks if r != region)
    build.build_character(bpy.context, ROOT, reader, pools_data, appearance_g, gender,
                          {}, {"holes": [other_region], "blood": [], "dirt": []})
    objs_b = build.slot_objects(bpy.context, slot)
    img_b = objs_b[0].data.materials[0].node_tree.nodes.get("Image Texture")
    name_b = objs_b[0].data.materials[0].node_tree.nodes.get("Image Texture").image.name if img_b else None

    check("second build produced a distinct image from the first",
          name_a is not None and name_b is not None and name_a != name_b,
          f"{name_a} vs {name_b}")

reader.close()
pz_character_viewer.unregister()

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
