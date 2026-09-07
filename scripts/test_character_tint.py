"""Headless regression test for the clothing tint - PZ's per-garment colour.

Run it:

    "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" \
        --background --factory-startup \
        --python scripts/test_character_tint.py

Exits non-zero on failure, so it can be wired into CI next to
tools/ci_check_scripts.gd. Runs against the real shipped
assets/characters/, which must have been exported by a pz_characters.py new
enough to mine `m_AllowRandomTint` - re-mine with `extract.bat --pools-only` if section 1
reports zero tintable entries.

Why this exists
---------------
The tint is three separate claims that all look fine on screen while being
wrong, and two of them are arithmetic rather than plumbing:

  * PZ tints a garment by multiplying its base texture's RGB per channel
    (media/shaders/hueChange.frag) and leaving alpha alone. Tinting alpha
    would eat a garment's own cutout, and would not be visible on any of the
    solid-bodied garments you would reach for first.
  * PZ's multiply is in sRGB space; Blender hands us linear. Multiplying
    linear by the picked sRGB triple is the obvious thing and is visibly too
    dark. Section 7 measures the fix against PZ's own arithmetic rather than
    taking the identity on trust - the answer is a worst case of 1.87/255,
    not zero.
  * A colour is stored per slot but tintability is a property of the ITEM, so
    a colour left behind on a slot whose garment has since changed must not
    leak into the render. Section 5 pins that.

What it does not cover: the panel (no bpy.ops here beyond register), and
whether the swatch appears - tintable_slots() is exercised only indirectly
through item_allows_tint().
"""
import os
import sys

# This file lives in pz-character/scripts/; HERE/REPO is pz-character
# itself, one level up - with the add-on under src/addon/ and the
# extractor's output under assets/characters/ beside it.
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
    """Fail loudly if `mod` came from anywhere but this repo.

    Blender's scripts/addons may hold an installed copy of this add-on, and
    --factory-startup disables add-ons without taking that folder off the
    import path - so a plain `import` can resolve to the installed copy and
    the test then passes for code nobody is editing. Measured: an installed
    pz_character_viewer under %APPDATA%/Blender Foundation/Blender/5.2 was
    what one of these tests was actually exercising.
    """
    import os as _os

    actual = _os.path.normcase(_os.path.dirname(_os.path.abspath(mod.__file__)))
    wanted = _os.path.normcase(_os.path.abspath(expected_dir))
    if not actual.startswith(wanted):
        raise SystemExit(
            f"{label} was imported from {actual}, not {wanted}.\n"
            "An installed copy in Blender's scripts/addons is shadowing the repo. "
            "Uninstall it, or run with --factory-startup and a sys.path that puts "
            "the repo first (which this test does - so if you see this, the "
            "module was already in sys.modules before the insert)."
        )


_assert_repo_module(pz_character_viewer, ADDON_DIR, "pz_character_viewer")

failures = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        failures.append(label)


pz_character_viewer.register()
# register() alone does not put the add-on into preferences.addons - that is
# addon_enable's doing - so there may be nothing to write the root into.
# Harmless either way: every call below passes ROOT explicitly.
addon = bpy.context.preferences.addons.get(ADDON_ID)
if addon is not None:
    addon.preferences.characters_root = ROOT
print("characters root:", ROOT)
if not os.path.isfile(os.path.join(ROOT, "characters.pzc")):
    sys.exit("no characters.pzc under " + ROOT + " - run setup.bat first")

pools_data = pools.load(ROOT)

# ---------------------------------------------------------------- flags ---
print("\n[1] exporter flag reaches the add-on")
mesh_tintable = [(s, e["item"]) for s, v in pools_data["clothing"].items()
                 for e in v if e.get("tint")]
ovl_tintable = [(s, e["item"]) for s, v in pools_data["overlays"].items()
                for e in v if e.get("tint")]
check("mesh entries carry tint", len(mesh_tintable) > 0, f"{len(mesh_tintable)}")
check("overlay entries carry tint", len(ovl_tintable) > 0, f"{len(ovl_tintable)}")
check("item_allows_tint agrees", pools.item_allows_tint(pools_data, mesh_tintable[0][1]))
untintable = next(e["item"] for s, v in pools_data["clothing"].items()
                  for e in v if not e.get("tint"))
check("untintable item says no", not pools.item_allows_tint(pools_data, untintable))
check("empty slot says no", not pools.item_allows_tint(pools_data, ""))
check("unknown item says no", not pools.item_allows_tint(pools_data, "NoSuchThing"))

# ------------------------------------------------------------ random ------
print("\n[2] random_tint stays inside PZ's HSB box")
import colorsys
import random

rng = random.Random(1234)
bad = 0
for _ in range(400):
    lin = pools.random_tint(rng)
    h, s, v = colorsys.rgb_to_hsv(*pools.linear_to_srgb(lin))
    if s > 0.6001 or not (0.0999 <= v <= 0.9001):
        bad += 1
check("400 rolls all within s<=0.6, 0.1<=v<=0.9", bad == 0, f"{bad} outside")

# ------------------------------------------------------ mesh garment ------
print("\n[3] a tinted mesh garment gets a multiply node")
reader = PZCReader(os.path.join(ROOT, "characters.pzc"))

# Find a tintable mesh garment that actually resolves for one of the genders.
target = None
for gender in ("male", "female"):
    for slot, item in mesh_tintable:
        resolved = pools.resolve_item(pools_data, item, gender)
        if resolved and resolved[0] == "mesh" and reader.get_mesh(resolved[1]) is not None:
            target = (gender, slot, item, resolved[1])
            break
    if target:
        break
check("found a buildable tintable mesh garment", target is not None, str(target))

gender, slot, item, mesh_id = target
appearance = pools.default_appearance(pools_data, gender)
appearance[slot] = item
TINT = (0.25, 0.5, 0.75)
build.build_character(bpy.context, ROOT, reader, pools_data, appearance, gender, {slot: TINT})

objs = build.slot_objects(bpy.context, slot)
check("garment object tagged with its slot", len(objs) == 1, f"{[o.name for o in objs]}")
mats = [m for o in objs for m in o.data.materials]
node = mats[0].node_tree.nodes.get(build.TINT_NODE_NAME)
check("material has the tint node", node is not None)
got = tuple(round(v, 6) for v in node.inputs[1].default_value)[:3]
check("node carries the linear tint", got == TINT, f"{got}")
bsdf = mats[0].node_tree.nodes.get("Principled BSDF")
# By name, not identity - nodes.get() hands back a fresh RNA wrapper each
# call, so `is` is never true even for the same node.
src_node = bsdf.inputs["Base Color"].links[0].from_node
check("tint node feeds Base Color",
      src_node.name == node.name and src_node.bl_idname == "ShaderNodeVectorMath",
      f"{src_node.name}/{src_node.bl_idname}")
alpha_src = bsdf.inputs["Alpha"].links[0].from_node.bl_idname
check("alpha still comes straight from the image", alpha_src == "ShaderNodeTexImage", alpha_src)

print("\n[4] set_slot_tint repaints in place")
NEW = (1.0, 0.0, 0.5)
written = build.set_slot_tint(bpy.context, slot, NEW)
got = tuple(round(v, 6) for v in node.inputs[1].default_value)[:3]
check("one material written", written == 1, str(written))
check("node updated without a rebuild", got == NEW, f"{got}")

print("\n[5] an untintable garment is forced to white")
appearance2 = pools.default_appearance(pools_data, gender)
un_slot = un_item = None
for s, v in pools_data["clothing"].items():
    for e in v:
        if e.get("tint"):
            continue
        r = pools.resolve_item(pools_data, e["item"], gender)
        if r and r[0] == "mesh" and reader.get_mesh(r[1]) is not None:
            un_slot, un_item = s, e["item"]
            break
    if un_slot:
        break
appearance2[un_slot] = un_item
build.build_character(bpy.context, ROOT, reader, pools_data, appearance2, gender,
                      {un_slot: (0.0, 0.0, 0.0)})
objs2 = build.slot_objects(bpy.context, un_slot)
n2 = objs2[0].data.materials[0].node_tree.nodes.get(build.TINT_NODE_NAME)
got2 = tuple(round(v, 6) for v in n2.inputs[1].default_value)[:3]
check("stale colour on an untintable slot is ignored", got2 == (1.0, 1.0, 1.0),
      f"{got2} ({un_slot}/{un_item})")

# ---------------------------------------------------- overlay garment -----
print("\n[6] a tinted overlay changes the body composite")
import numpy as np

ov = None
for g in ("male", "female"):
    for slot_o, item_o in ovl_tintable:
        r = pools.resolve_item(pools_data, item_o, g)
        if r and r[0] == "overlay":
            ov = (g, slot_o, item_o)
            break
    if ov:
        break
check("found a tintable overlay", ov is not None, str(ov))

g, slot_o, item_o = ov
app = pools.default_appearance(pools_data, g)
app[slot_o] = item_o
skin = app["skin_tone"]


def composite_pixels(tints):
    img = build.composite_body_texture(ROOT, pools_data, skin, app, g, tints)
    return np.array(img.pixels[:], dtype=np.float32)


white = composite_pixels({})
red = composite_pixels({slot_o: (1.0, 0.0, 0.0)})
check("white and red composites differ", not np.allclose(white, red),
      f"maxdiff={float(np.abs(white - red).max()):.4f}")

w4 = white.reshape(-1, 4)
r4 = red.reshape(-1, 4)
changed = np.abs(w4 - r4).max(axis=1) > 1e-6
check("something actually changed", changed.sum() > 0, f"{int(changed.sum())} texels")
check("alpha is untouched", np.allclose(w4[:, 3], r4[:, 3]),
      f"maxdiff={float(np.abs(w4[:, 3] - r4[:, 3]).max()):.6f}")
# A pure-red tint zeroes G and B wherever the overlay is opaque.
opaque = changed
check("green/blue driven down by a red tint",
      float(r4[opaque, 1].max()) <= float(w4[opaque, 1].max()) + 1e-6)

check("untinted composite is stable", np.allclose(white, composite_pixels({})))

# The fidelity claim: multiplying LINEAR texels by the LINEAR tint should
# reproduce PZ's sRGB-space multiply. Recompute the overlay's contribution
# the way hueChange.frag would - encode to sRGB, multiply by the sRGB tint,
# decode - and compare against what the add-on produced.
print(chr(10) + "[7] the linear multiply matches PZ's sRGB multiply")

# Measured on the overlay's own texture, NOT on the composite. The add-on
# tints the garment layer and then alpha-blends it over the skin, which is
# also PZ's order; comparing composites would fold the blend's own
# non-commutativity into the number and measure the wrong thing.


def to_srgb(a):
    return np.where(a <= 0.0031308, a * 12.92,
                    1.055 * np.power(np.clip(a, 1e-8, None), 1 / 2.4) - 0.055)


def to_linear(a):
    return np.where(a <= 0.04045, a / 12.92, np.power((a + 0.055) / 1.055, 2.4))


ovl_rel = pools.resolve_item(pools_data, item_o, g)[2]["textures"][0]
ovl_img = build._load_image(ROOT, ovl_rel)
L = np.array(ovl_img.pixels[:], dtype=np.float32).reshape(-1, 4)[:, :3]

worst = 0.0
for srgb_tint in ((0.75, 0.5, 0.25), (0.5, 0.5, 0.5), (0.1, 0.9, 0.35),
                  (0.05, 0.05, 0.05), (0.9, 0.9, 0.9), (0.3, 0.3, 0.3)):
    lin_tint = np.asarray(pools.srgb_to_linear(srgb_tint), dtype=np.float32)
    addon = L * lin_tint                                   # what the add-on does
    pz = to_linear(to_srgb(L) * np.asarray(srgb_tint))     # what hueChange.frag does
    err = float(np.abs(to_srgb(addon) - to_srgb(pz)).max())
    print(f"        tint {srgb_tint}: {err * 255:.2f}/255")
    worst = max(worst, err)

check("worst-case sRGB error under 2/255", worst < 2.0 / 255.0,
      f"max {worst:.6f} = {worst * 255:.2f}/255")

check("a tint on an untintable overlay is ignored",
      np.allclose(white, composite_pixels({"skin_tone": (1.0, 0.0, 0.0)})))


# ------------------------------------------ panel + the live update path --
print(chr(10) + "[8] the panel's swatch condition and the live update path")
from pz_character_viewer.character import panel as panel_mod  # noqa: E402
from pz_character_viewer.character import props as props_mod  # noqa: E402

props = bpy.context.scene.pz_character
props.gender = gender
props_mod.rebuild_layers(props, pools_data, gender)
props_mod.apply_appearance(props, appearance, {})

# The add-on may not be in preferences.addons (see the register() note above),
# in which case _characters_root() falls back to default_characters_root() -
# which is this repo's assets/characters, i.e. the same folder. Skip the panel
# checks rather than assert a false negative if that fallback ever moves.
from pz_character_viewer.character.ops import _characters_root  # noqa: E402

if os.path.normcase(os.path.normpath(_characters_root(bpy.context))) != \
        os.path.normcase(os.path.normpath(ROOT)):
    print("  SKIP  panel checks - _characters_root() does not point at ROOT")
else:
    tintable = panel_mod.tintable_slots(bpy.context)
    check("the worn tintable garment's slot is offered a swatch", slot in tintable,
          f"{slot} in {sorted(tintable)}")
    check("skin_tone is never offered one", "skin_tone" not in tintable)
    # hair/beard get a swatch unconditionally (PZ tints their greyscale art
    # regardless of m_AllowRandomTint) whenever something is actually worn
    # there - unlike skin_tone, which has no such control at all.
    for hb_slot, pool in (("hair", pools_data.get("hair", {}).get(gender, [])),
                          ("beard", pools_data.get("beard", []) if gender == "male" else [])):
        if pool:
            check(f"{hb_slot} is offered a swatch when worn",
                  hb_slot in tintable or not appearance.get(hb_slot),
                  f"{hb_slot} in {sorted(tintable)}, appearance={appearance.get(hb_slot)!r}")
    # Swap that slot to an untintable garment and the swatch must go away.
    layer = next(l for l in props.layers if l.slot == slot)
    import json as _json

    opts = _json.loads(layer.options)
    other = next((o for o in opts
                  if o and not pools.item_allows_tint(pools_data, o)), None)
    if other is None:
        print("  SKIP  no untintable option in this slot to swap to")
    else:
        with props_mod.suspend_updates():
            layer.index = opts.index(other)
        check("swapping to an untintable garment withdraws the swatch",
              slot not in panel_mod.tintable_slots(bpy.context), other)
        with props_mod.suspend_updates():
            layer.index = opts.index(item)

    print("\n[9] assigning the property repaints without a rebuild")
    build.build_character(bpy.context, ROOT, reader, pools_data, appearance, gender,
                          props_mod.tint_dict(props))
    # apply_tint() deliberately does nothing unless a preview exists - the
    # swatch is only reachable after Preview Character, which sets this. The
    # test builds the character directly, so it has to set the flag itself.
    props.preview_enabled = True
    obj_before = build.slot_objects(bpy.context, slot)[0]
    node_before = obj_before.data.materials[0].node_tree.nodes.get(build.TINT_NODE_NAME)
    check("starts white", tuple(round(v, 4) for v in node_before.inputs[1].default_value)[:3]
          == (1.0, 1.0, 1.0))

    LIVE = (0.2, 0.7, 0.4)
    layer = next(l for l in props.layers if l.slot == slot)
    layer.tint = LIVE  # NOT suspended - this must fire _on_tint_changed
    objs_after = build.slot_objects(bpy.context, slot)
    check("no rebuild happened (same object)", objs_after and objs_after[0] is obj_before,
          f"{[o.name for o in objs_after]}")
    node_after = objs_after[0].data.materials[0].node_tree.nodes.get(build.TINT_NODE_NAME)
    got_live = tuple(round(v, 6) for v in node_after.inputs[1].default_value)[:3]
    check("the update callback wrote the colour through", got_live == LIVE, f"{got_live}")

    # And the guard: a suspended write must NOT reach the material.
    with props_mod.suspend_updates():
        layer.tint = (0.9, 0.1, 0.1)
    still = tuple(round(v, 6) for v in
                  build.slot_objects(bpy.context, slot)[0].data.materials[0]
                  .node_tree.nodes.get(build.TINT_NODE_NAME).inputs[1].default_value)[:3]
    check("suspend_updates() really suppresses the callback", still == LIVE, f"{still}")

reader.close()
pz_character_viewer.unregister()

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
