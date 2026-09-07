"""Headless regression test for Release & New - banking a built character.

Run it:

    "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" \
        --background --factory-startup \
        --python scripts/test_character_release.py

Exits non-zero on failure. Runs against the real shipped assets/characters.

Why this exists
---------------
"Release" looks like a reparent and is mostly not. Three of the things it has
to do are invisible until much later, and all three fail silently:

  * **The body composite is one shared image**, written in place by every
    build. A released character wearing any texture-overlay garment would have
    its skin repainted by the NEXT character generated - and only for overlay
    garments, so a released character in a mesh jacket looks fine and the same
    character in overlay underwear does not.
  * **purge_baked_actions() deletes every action this add-on baked**, and it
    runs on the next build. A released character that was mid-animation snaps
    back to rest, with nothing anywhere to say why.
  * **The live empty must keep its exact name.** Renaming it and making a new
    one lets Blender hand the new empty "PZ Character.001", after which every
    lookup by ROOT_NAME - remove_existing(), apply_shading(), slot_objects() -
    finds the RELEASED character and starts editing or deleting it.

Sections 5 and 6 are the ones that would catch a regression late otherwise.
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
from pz_character_viewer.character import build, pools, props as props_mod  # noqa: E402
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
addon = bpy.context.preferences.addons.get(ADDON_ID)
if addon is not None:
    addon.preferences.characters_root = ROOT
print("characters root:", ROOT)
if not os.path.isfile(os.path.join(ROOT, "characters.pzc")):
    sys.exit("no characters.pzc under " + ROOT + " - run setup.bat first")

pools_data = pools.load(ROOT)
reader = PZCReader(os.path.join(ROOT, "characters.pzc"))
props = bpy.context.scene.pz_character
GENDER = "male"


def build_one(appearance, tints=None):
    build.build_character(bpy.context, ROOT, reader, pools_data, appearance, GENDER,
                          tints or {})
    props.preview_enabled = True


def live_root():
    return bpy.data.objects.get(build.ROOT_NAME)


def released_empties():
    return [o for o in bpy.data.objects
            if o.get(build.RELEASE_INDEX_PROP) is not None]


# An outfit with BOTH a mesh garment and a texture overlay - the two halves
# behave completely differently on release and only one of them shares an
# image with the next character.
mesh_slot = mesh_item = None
for slot, entries in pools_data["clothing"].items():
    for entry in entries:
        resolved = pools.resolve_item(pools_data, entry["item"], GENDER)
        if resolved and resolved[0] == "mesh" and reader.get_mesh(resolved[1]) is not None:
            mesh_slot, mesh_item = slot, entry["item"]
            break
    if mesh_slot:
        break

ovl_slot = ovl_item = None
for slot, entries in pools_data["overlays"].items():
    for entry in entries:
        resolved = pools.resolve_item(pools_data, entry["item"], GENDER)
        if resolved and resolved[0] == "overlay":
            ovl_slot, ovl_item = slot, entry["item"]
            break
    if ovl_slot:
        break

print("\n[0] fixture")
check("found a mesh garment", mesh_item is not None, f"{mesh_slot}/{mesh_item}")
check("found a texture overlay", ovl_item is not None, f"{ovl_slot}/{ovl_item}")

first = pools.default_appearance(pools_data, GENDER)
first[mesh_slot] = mesh_item
first[ovl_slot] = ovl_item
build_one(first)

root_before = live_root()
children_before = list(root_before.children)
kids_world_before = {c.name: c.matrix_world.copy() for c in children_before}
descendants_before = [o for c in children_before for o in [c] + list(build._descendants(c))]
names_before = {o.name for o in descendants_before}
check("built something to release", len(children_before) > 0, f"{len(children_before)} children")

# ------------------------------------------------------------- release ---
print("\n[1] the reparent itself")
released, moved = build.release_character(bpy.context)
check("returned the new empty", released is not None, getattr(released, "name", ""))
check("moved every descendant", moved == len(descendants_before),
      f"{moved} vs {len(descendants_before)}")
check("released empty is an Empty", released.type == "EMPTY", released.type)
check("released empty is in the scene", released.name in bpy.context.scene.objects)
check("every former child now hangs off it",
      {c.name for c in released.children} == {c.name for c in children_before},
      f"{sorted(c.name for c in released.children)}")
check("nothing was left in the live empty", not list(live_root().children),
      f"{[c.name for c in live_root().children]}")
check("every object survived",
      names_before <= {o.name for o in bpy.data.objects})

print("\n[2] the live empty keeps its exact name")
check("live root is still the same object", live_root() is not None
      and live_root().name == build.ROOT_NAME, live_root().name if live_root() else "gone")
check("released empty is NOT findable as the live one",
      live_root().name != released.name and not released.name.startswith(build.ROOT_NAME + "."),
      released.name)
check("apply_shading/slot_objects still see the live root, not the released one",
      build.slot_objects(bpy.context, mesh_slot) == [], "live root is empty")

print("\n[3] it moved aside as one piece")
bpy.context.view_layer.update()
offset = released[build.RELEASE_INDEX_PROP] * build.RELEASE_SPACING
check("empty offset by index * spacing", abs(released.location.x - offset) < 1e-6,
      f"x={released.location.x:.3f} expected {offset:.3f}")
worst = 0.0
for child in released.children:
    was = kids_world_before[child.name].translation
    now = child.matrix_world.translation
    worst = max(worst, (now - was - __import__("mathutils").Vector((offset, 0, 0))).length)
check("children travelled with it and nothing else moved", worst < 1e-5,
      f"max residual {worst:.8f}")

# ------------------------------------------------ the next character -----
print("\n[4] the next build lands in the live empty and leaves the released one alone")
second = pools.default_appearance(pools_data, GENDER)
second[mesh_slot] = mesh_item
second[ovl_slot] = ovl_item
build_one(second)

check("live empty has a fresh character", bool(live_root().children),
      f"{len(list(live_root().children))} children")
check("released empty still exists", released.name in bpy.data.objects)
check("released character still has its objects", len(list(released.children)) ==
      len(children_before), f"{len(list(released.children))}")
check("released objects are all still valid",
      all(o.name in bpy.data.objects
          for c in released.children for o in [c] + list(build._descendants(c))))
check("the two characters are different objects",
      not ({c.name for c in released.children} & {c.name for c in live_root().children}))

print("\n[5] the released body texture is its own, not the shared composite")
shared = bpy.data.images.get(build.BODY_COMPOSITE_NAME)
check("the shared composite exists", shared is not None)


def body_images(root_obj):
    out = []
    for obj in [root_obj] + list(build._descendants(root_obj)):
        for mat in getattr(obj.data, "materials", []) or []:
            if mat is None or not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname == "ShaderNodeTexImage" and node.image is not None:
                    out.append(node.image)
    return out


released_imgs = body_images(released)
check("no released material still points at the shared composite",
      all(img is not shared for img in released_imgs),
      f"{len(released_imgs)} image slots checked")

# The decisive test: repaint the shared image the way the next tint edit or
# build would, and confirm the released character's pixels do not follow.
import numpy as np  # noqa: E402

released_copy = next((img for img in released_imgs
                      if img.name.startswith(build.BODY_COMPOSITE_NAME)), None)
check("released character carries a copy of it", released_copy is not None,
      released_copy.name if released_copy else "none found")
if released_copy is not None:
    before = np.array(released_copy.pixels[:], dtype=np.float32)
    # FIRST, and not merged into the checks below: on Blender 4.5 an
    # Image.copy() of a 256x256 packed generated image reads back as an EMPTY
    # pixel sequence, and every comparison below then passes vacuously -
    # np.array_equal([], []) is True. This check is what caught that; keep it
    # ahead of the others.
    check("the released copy actually has pixels", before.size > 0, f"{before.size}")
    check("it is the same size as the shared image",
          tuple(released_copy.size) == tuple(shared.size),
          f"{tuple(released_copy.size)} vs {tuple(shared.size)}")
    shared.pixels[:] = [0.0] * len(shared.pixels)
    after = np.array(released_copy.pixels[:], dtype=np.float32)
    check("wiping the shared image does not touch the released one",
          before.size > 0 and np.array_equal(before, after))
    check("the released copy is not blank", before.size > 0 and float(before.max()) > 0.0,
          f"max {float(before.max()) if before.size else float('nan'):.3f}")
    check("the copy is packed so it survives a reload",
          released_copy.packed_file is not None)

print("\n[6] a released character keeps its baked animation")
clips = pools.filter_clip_ids(ROOT, category="Bob")
clip_id = clips[0] if clips else None
check("found a clip to bake", clip_id is not None, clip_id or "")

if clip_id:
    from pz_character_viewer.character import animation_build  # noqa: E402

    arm = next((o for o in live_root().children if o.type == "ARMATURE"), None)
    action = animation_build.build_action(
        clip_id, reader.get_clip(clip_id), reader.skeleton(), arm
    )
    arm.animation_data_create()
    arm.animation_data.action = action
    check("action carries the purge tag", action.get(build.BAKED_ACTION_TAG) is not None)

    released2, _ = build.release_character(bpy.context)
    arm2 = next((o for o in released2.children if o.type == "ARMATURE"), None)
    kept_name = arm2.animation_data.action.name if arm2.animation_data and \
        arm2.animation_data.action else None
    check("the released rig still has an action", kept_name is not None, kept_name or "")
    check("it was renamed off its clip id", kept_name != clip_id, f"{kept_name}")
    check("it lost the purge tag",
          bpy.data.actions[kept_name].get(build.BAKED_ACTION_TAG) is None)

    # Bake a SECOND action, on the new live character, so purge has something
    # tagged to take. Without this the check is vacuous - the release untagged
    # the only tagged action there was, so purge would correctly remove 0.
    build_one(pools.default_appearance(pools_data, GENDER))
    arm3 = next((o for o in live_root().children if o.type == "ARMATURE"), None)
    live_action = animation_build.build_action(
        clip_id, reader.get_clip(clip_id), reader.skeleton(), arm3
    )
    arm3.animation_data_create()
    arm3.animation_data.action = live_action
    check("the new bake is tagged again",
          live_action.get(build.BAKED_ACTION_TAG) is not None)
    check("and got the clip id back, so nothing is squatting on the name",
          live_action.name == clip_id, live_action.name)

    removed = build.purge_baked_actions()
    check("purge takes the live one", removed >= 1, f"{removed} removed")
    check("and leaves the released one alone", kept_name in bpy.data.actions)
    check("the released rig is still pointing at it",
          arm2.animation_data.action is not None
          and arm2.animation_data.action.name == kept_name)

print("\n[7] indices keep advancing, even with a gap")
n_before = len(released_empties())
bpy.data.objects.remove(released, do_unlink=True)
build_one(pools.default_appearance(pools_data, GENDER))
released3, _ = build.release_character(bpy.context)
check("a deleted release does not free its slot",
      released3[build.RELEASE_INDEX_PROP] > 2,
      f"index {released3[build.RELEASE_INDEX_PROP]} after removing #1 of {n_before}")
xs = sorted(round(o.location.x, 4) for o in released_empties())
check("no two released characters share a position", len(xs) == len(set(xs)), f"{xs}")

print("\n[8] releasing with nothing built is a no-op")
build.remove_existing(bpy.context)
bpy.data.objects.new(build.ROOT_NAME, None)  # an empty root, no children
none_released, none_moved = build.release_character(bpy.context)
check("returns (None, 0)", none_released is None and none_moved == 0,
      f"{none_released} {none_moved}")

print(chr(10) + "[9] the operator the button calls")
# Everything above drives build.release_character() directly. This drives the
# operator, which is what the panel wires up - poll, the randomize, the
# rebuild and the status line.
build.remove_existing(bpy.context)
props.preview_enabled = False
props_mod.rebuild_layers(props, pools_data, GENDER)
props_mod.apply_appearance(props, pools.default_appearance(pools_data, GENDER), {})

check("poll is False with no character built",
      not bpy.ops.pz_character.release_character.poll())

build_one(pools.default_appearance(pools_data, GENDER))
check("poll is True once one is built",
      bpy.ops.pz_character.release_character.poll())

before_appearance = props_mod.appearance_dict(props)
n_released = len(released_empties())
result = bpy.ops.pz_character.release_character()
check("operator finished", result == {"FINISHED"}, str(result))
check("one more released empty", len(released_empties()) == n_released + 1,
      f"{len(released_empties())}")
check("the live empty was refilled", bool(live_root().children))
check("it generated a DIFFERENT character, not a copy",
      props_mod.appearance_dict(props) != before_appearance)
check("status names what was released", "Released" in props.status, props.status)

print(chr(10) + "[10] Randomize builds when there is nothing, rerolls when there is")
# There used to be a separate Generate/Replace button whose only real job was
# the very first press. Randomize (and Default, and every layer control) now
# builds one if there is none, which is what let that button go.
build.remove_existing(bpy.context)
props.preview_enabled = False
props_mod.rebuild_layers(props, pools_data, GENDER)
props_mod.apply_appearance(props, pools.default_appearance(pools_data, GENDER), {})

check("nothing is built", live_root() is None or not list(live_root().children))
result = bpy.ops.pz_character.randomize()
check("Randomize finished", result == {"FINISHED"}, str(result))
check("it built a character from nothing", bool(live_root().children))
check("preview_enabled is set, so tint edits and Release work",
      props.preview_enabled)
first = props_mod.appearance_dict(props)

result = bpy.ops.pz_character.randomize()
check("a second press rerolls rather than doing nothing",
      props_mod.appearance_dict(props) != first)
check("and there is still exactly one character", len(list(live_root().children)) >= 1)

build.remove_existing(bpy.context)
props.preview_enabled = False
result = bpy.ops.pz_character.set_default()
check("Default also builds from nothing", bool(live_root().children))
check("and it is the default appearance",
      props_mod.appearance_dict(props) == pools.default_appearance(pools_data, "male"))

# Every layer control does it too, so no control in the panel changes state
# without showing the result.
build.remove_existing(bpy.context)
props.preview_enabled = False
layer = next(l for l in props.layers if l.slot == mesh_slot)
op = bpy.ops.pz_character.cycle_layer(slot=mesh_slot, direction=1)
check("cycling a layer builds from nothing too", bool(live_root().children), str(op))

print(chr(10) + "[11] Clear empties the live empty and nothing else")
n_released = len(released_empties())
check("Clear is available", bpy.ops.pz_character.clear_character.poll())
selections = props_mod.appearance_dict(props)
result = bpy.ops.pz_character.clear_character()
check("Clear finished", result == {"FINISHED"}, str(result))
check("the live empty is gone or empty",
      live_root() is None or not list(live_root().children))
check("preview_enabled is cleared", not props.preview_enabled)
check("Clear is unavailable now", not bpy.ops.pz_character.clear_character.poll())
check("released characters were not touched",
      len(released_empties()) == n_released, f"{len(released_empties())} vs {n_released}")
check("the layer selections survived",
      props_mod.appearance_dict(props) == selections)

print(chr(10) + "[12] the Animation Library tracks the target armature, not the scene")
from pz_character_viewer.character import anim_props, anim_ops  # noqa: E402,F401

anim = bpy.context.scene.pz_character_anim


def active_clip():
    """What the panel would show as "Playing: ..." right now - reads the
    active-clip custom property off build.target_armature(), which is what
    anim_props.get_active_clip()/panel.py do post per-rig tracking."""
    return anim_props.get_active_clip(build.target_armature(bpy.context))


# Nothing selected in these background-mode calls, so target_armature() falls
# back to the live root throughout this section - same object live_armature()
# would have returned before per-rig tracking existed.
check("character_exists() is False with nothing built", not anim_props.character_exists(bpy.context))

bpy.ops.pz_character.randomize()
check("character_exists() is True once built", anim_props.character_exists(bpy.context))

clips = pools.filter_clip_ids(ROOT, category="Bob")
clip_id = clips[0]
played = bpy.ops.pz_character.play_animation(clip_id=clip_id)
check("Play assigned a clip", played == {"FINISHED"}, str(played))
check("the library records what is playing", active_clip() == clip_id, active_clip())

# A rebuild is a brand-new armature object with no ACTIVE_CLIP_PROP yet, so
# the library must not go on claiming the clip is playing.
bpy.ops.pz_character.randomize()
check("a rebuild reads no active clip", active_clip() == "", repr(active_clip()))
check("...and the action really is gone", bpy.data.actions.get(clip_id) is None)

# Same on release: the armature (and its action, and the ACTIVE_CLIP_PROP
# naming it) go to the released empty and the live one is rebuilt fresh.
bpy.ops.pz_character.play_animation(clip_id=clip_id)
check("playing again", active_clip() == clip_id)
bpy.ops.pz_character.release_character()
check("the new live armature reads no active clip", active_clip() == "",
      repr(active_clip()))
check("the released character kept its own action",
      any(o.animation_data is not None and o.animation_data.action is not None
          for e in released_empties() for o in build._descendants(e)))

# And on removal.
bpy.ops.pz_character.play_animation(clip_id=clip_id)
check("playing once more", active_clip() == clip_id)
bpy.ops.pz_character.clear_character()
check("a clear leaves no target armature to read", active_clip() == "",
      repr(active_clip()))
check("character_exists() is False again", not anim_props.character_exists(bpy.context))

# THE bug this section was written to catch. release_character() moves the
# armature out keeping its name, so from the first release onward
# bpy.data.objects.get(ARMATURE_NAME) returns a RELEASED character's rig -
# and Play/Stop/Layer Animations were all driving that one. Resolve through
# the live empty instead.
bpy.ops.pz_character.randomize()
live_arm = build.live_armature(bpy.context)
by_name = bpy.data.objects.get(build.ARMATURE_NAME)
check("there are released characters to be confused with",
      len(released_empties()) > 0, str(len(released_empties())))
check("a name lookup finds the WRONG armature (this is why live_armature exists)",
      by_name is not None and by_name.name != live_arm.name,
      f"name->{by_name.name if by_name else None}  live->{live_arm.name}")
check("live_armature() returns the one under the live empty",
      live_arm.parent is not None and live_arm.parent.name == build.ROOT_NAME,
      live_arm.parent.name if live_arm.parent else "no parent")

released_arms = {o.name for e in released_empties()
                 for o in build._descendants(e) if o.type == "ARMATURE"}
check("and it is not one of the released ones", live_arm.name not in released_arms,
      f"{live_arm.name} vs {sorted(released_arms)}")

before = {n: (bpy.data.objects[n].animation_data.action.name
              if bpy.data.objects[n].animation_data
              and bpy.data.objects[n].animation_data.action else None)
          for n in released_arms}
bpy.ops.pz_character.play_animation(clip_id=clip_id)
check("Play landed on the live armature",
      live_arm.animation_data is not None and live_arm.animation_data.action is not None,
      str(live_arm.animation_data.action.name if live_arm.animation_data
          and live_arm.animation_data.action else None))
after = {n: (bpy.data.objects[n].animation_data.action.name
             if bpy.data.objects[n].animation_data
             and bpy.data.objects[n].animation_data.action else None)
         for n in released_arms}
check("and left every released character's animation alone", before == after,
      f"{before} -> {after}")
bpy.ops.pz_character.stop_animation()
check("Stop cleared the live one", active_clip() == "")
check("and still left the released ones alone",
      {n: (bpy.data.objects[n].animation_data.action.name
           if bpy.data.objects[n].animation_data
           and bpy.data.objects[n].animation_data.action else None)
       for n in released_arms} == before)

print(chr(10) + "[13] selecting a released empty points the library at its rig")
# The whole point of per-rig tracking: pick a released character back up
# later and Stop it into T-pose without disturbing whatever the live one is
# doing. Play on the live character first, so a wrong target would be easy
# to spot.
bpy.ops.pz_character.play_animation(clip_id=clip_id)
live_arm = build.live_armature(bpy.context)
check("live is playing", active_clip() == clip_id, active_clip())

released, _ = build.release_character(bpy.context)
released_arm = next(o for o in released.children if o.type == "ARMATURE")
bpy.ops.pz_character.randomize()  # a fresh, unrelated live character
new_live_arm = build.live_armature(bpy.context)
check("release produced a genuinely new live armature",
      new_live_arm.name != live_arm.name, new_live_arm.name)

bpy.context.view_layer.objects.active = released
try:
    check("target_root() follows the selected released empty",
          build.target_root(bpy.context) is released, build.target_root(bpy.context).name)
    check("target_armature() is the released rig, not the live one",
          build.target_armature(bpy.context) is released_arm,
          build.target_armature(bpy.context).name)
    check("the panel would show the clip the released rig kept",
          anim_props.get_active_clip(build.target_armature(bpy.context)) == clip_id,
          anim_props.get_active_clip(build.target_armature(bpy.context)))

    bpy.ops.pz_character.stop_animation()
    check("Stop reset the released rig's pose bones to rest (T-pose)",
          all(pb.matrix_basis == pb.matrix_basis.__class__.Identity(4)
              for pb in released_arm.pose.bones))
    check("...and cleared its own active-clip record",
          anim_props.get_active_clip(released_arm) == "")
    check("the live character (a different object) was left alone",
          anim_props.get_active_clip(new_live_arm) == "")
finally:
    # Selecting an object for this check must not leak into the rest of the
    # file - every call below relies on target_armature() falling back to
    # the live root the way live_armature() always did.
    bpy.context.view_layer.objects.active = None

check("with nothing selected, target_armature() is back to the live one",
      build.target_armature(bpy.context) is new_live_arm)

# The two layering picks are inputs, not armature state - they survive, the
# same way the appearance layer selections do.
bpy.ops.pz_character.randomize()
bpy.ops.pz_character.set_layer_clip(role="base", clip_id=clips[0])
bpy.ops.pz_character.set_layer_clip(role="overlay", clip_id=clips[1])
bpy.ops.pz_character.randomize()
check("the layering picks survive a rebuild",
      anim.layer_base_clip == clips[0] and anim.layer_overlay_clip == clips[1],
      f"{anim.layer_base_clip} / {anim.layer_overlay_clip}")

# Search results are manifest-driven and have nothing to do with the
# character, so they must survive too.
bpy.ops.pz_character.refresh_anim_results()
n_results = len(anim.results)
check("there are search results", n_results > 0, str(n_results))
bpy.ops.pz_character.randomize()
check("results survive a rebuild", len(anim.results) == n_results)

reader.close()
pz_character_viewer.unregister()

print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
