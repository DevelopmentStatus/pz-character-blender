"""Headless regression test for the character preview + animation bake.

Run it:

    "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" \
        --background --factory-startup \
        --python scripts/test_character_animation.py

Exits non-zero on failure, so it can be wired into CI next to
tools/ci_check_scripts.gd.

Why this exists
---------------
`docs/` and the add-on's own comments claimed for a long time that this project
had "no headless Blender", and every change to the transform maths was
therefore shipped unverified. That claim was wrong - Blender is installed, and
`--background --python` runs this file against the real
`assets/characters/characters.pzc` in a couple of seconds. Three consecutive
wrong-bone-orientation fixes were shipped before anyone checked.

Verify on the evaluated mesh, never by re-deriving the maths
------------------------------------------------------------
The one trap worth spelling out, because it already produced a confident false
pass: an earlier attempt "verified" the bake by reimplementing Blender's
armature maths in numpy and comparing. That is circular. `matrix_local` appears
in both the pose solve (`pose = W_anim @ rest^-1 @ matrix_local`) and the
deform (`v' = pose @ matrix_local^-1 @ v`), so it cancels, and the check passes
for *any* value of it - including a wrong one. It reported the fix as correct
while the add-on was still visibly broken on screen.

So: assert on `evaluated_get(depsgraph).to_mesh()` - what Blender actually put
on screen - and nothing else.
"""

import math
import os
import sys

import bpy

# This file lives in pz-character/scripts/; PZCHAR_ROOT is pz-character
# itself, one level up.
PZCHAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _charroot  # noqa: E402
# pz-character's own src/addon/, at the FRONT of sys.path - see
# _assert_repo_module() below for what goes wrong without it.
ADDON_DIR = os.path.join(PZCHAR_ROOT, "src", "addon")
sys.path.insert(0, ADDON_DIR)

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


PZC = os.path.join(_charroot.require(), "characters.pzc")

#: (clip, min_height, max_height, why). Heights are of the deformed body mesh
#: in Blender units; the undeformed body is 1.764 tall. These are deliberately
#: loose - the test is "is this a plausible human", not a pixel lock, because a
#: tight bound would fail on any legitimate retune of the bake.
CASES = (
    (None, 1.70, 1.80, "rest pose, no action"),
    ("Bob/Bob_Idle", 1.65, 1.85, "standing idle"),
    ("Bob/Bob_Walk", 1.65, 1.85, "walking, upright"),
    ("Bob/Bob_ClimbRope", 1.75, 2.00, "arms overhead on a rope"),
    ("Bob/Bob_ActionToSitIdle_heavy", 0.70, 1.20, "sitting - must NOT be full height"),
)

#: A posed human is narrow. The rest pose is a T-pose at +-0.888 in X, so a
#: value near that under an *animated* clip means the arms never left T - i.e.
#: the action did not apply at all, which is how a silently-unbound action slot
#: presents. Catching that is half the point of this file.
MAX_HALF_WIDTH = 0.75

FRAMES = (0, 1, 3, 7, 11)

#: A rigid prop (mesh_data["bones"] empty - see build._build_rigid_prop) rides
#: one attach bone via its own baked Action (animation_build.build_prop_action)
#: rather than skin deform. Verified against the packs: this hat has no skin
#: data and attaches at the head.
PROP_MESH_ID = "Clothes/M_PoliceHat"
PROP_ATTACH_BONE = "Bip01_Head"

#: A clip with real head motion, so a prop that never left its rest position
#: (the original floating-hat bug, and the exact way a silently-broken bake
#: would present) is distinguishable from one that's actually tracking.
HEAD_MOTION_CLIP = "Bob/Bob_EmoteSneeze2H"


def fail(msg):
    print(f"FAIL: {msg}")
    fail.count += 1


fail.count = 0


def body_bounds(mesh_obj):
    """Bounding box of the *evaluated* mesh - armature modifier applied."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = mesh_obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.vertices:
            return None
        xs = [v.co.x for v in mesh.vertices]
        zs = [v.co.z for v in mesh.vertices]
        return min(xs), max(xs), min(zs), max(zs)
    finally:
        evaluated.to_mesh_clear()


def main():
    from pz_character_viewer.character import animation_build, build

    _assert_repo_module(build, ADDON_DIR, "pz_character_viewer")
    from pz_character_viewer.character.pzc_reader import PZCReader

    if not os.path.exists(PZC):
        print(f"FAIL: no pack at {PZC}")
        return 1

    context = bpy.context
    reader = PZCReader(PZC)
    skeleton = reader.skeleton()
    body = reader.get_mesh("MaleBody")

    root = bpy.data.objects.new("PZ Character", None)
    context.scene.collection.objects.link(root)
    arm_obj, _rest = build._build_armature(
        context, skeleton, root, build.bind_pose_from(body)
    )
    mesh_obj = build._build_skinned_mesh(context, "MaleBody", body, arm_obj, root, None)

    missing = sum(1 for b in arm_obj.data.bones if b.get("pz_rest_source") is None)
    if missing:
        fail(f"{missing} bones have no pz_rest_source - animation_build cannot correct them")

    for clip_id, lo, hi, why in CASES:
        if clip_id is None:
            frames = (0,)
        else:
            clip = reader.get_clip(clip_id)
            if clip is None:
                fail(f"{clip_id}: not in the pack")
                continue
            action = animation_build.build_action(clip_id, clip, skeleton, arm_obj)
            arm_obj.animation_data_create()
            animation_build.assign_action(arm_obj.animation_data, action)

            interps = {
                kp.interpolation
                for fc in animation_build.iter_fcurves(action)
                for kp in fc.keyframe_points
            }
            if interps != {"LINEAR"}:
                fail(f"{clip_id}: keyframe interpolation is {sorted(interps)}, expected LINEAR")
            frames = FRAMES

        for frame in frames:
            context.scene.frame_set(frame)
            bounds = body_bounds(mesh_obj)
            if bounds is None:
                fail(f"{clip_id or 'rest'} frame {frame}: evaluated mesh is empty")
                continue
            min_x, max_x, min_z, max_z = bounds
            height = max_z - min_z
            label = f"{clip_id or 'rest'} frame {frame}"
            if not (lo <= height <= hi):
                fail(f"{label}: height {height:.3f} outside [{lo}, {hi}] ({why})")
            if clip_id is not None and max(abs(min_x), abs(max_x)) > MAX_HALF_WIDTH:
                fail(f"{label}: half-width {max(abs(min_x), abs(max_x)):.3f} > "
                     f"{MAX_HALF_WIDTH} - action likely never applied (still T-posed)")
            print(f"  ok  {label:44s} height {height:.3f}  x +-{max(abs(min_x), abs(max_x)):.3f}")

    # --- rigid prop (hat/glasses) stays rigidly attached to its bone across
    # an entire clip, not just at rest. This is the correct thing to assert,
    # and an earlier version of this test asserted something else - "frame 0
    # of a real clip should barely differ from the static no-animation rest
    # placement" - which is simply wrong: a clip's own first sample is not
    # required to be the skeleton's rest pose (Bob_EmoteSneeze2H's head starts
    # already mid-expression), so that check produced a false failure on
    # correct code. What actually matters is that the prop's position
    # RELATIVE TO ITS OWN BONE never changes, at any frame - if it does, the
    # prop is drifting off the head as the clip plays, which is a subtler
    # version of the original floating-prop bug. This check is what caught a
    # real composition-order bug (world-space vs local-space delta) that the
    # naive "did it move from rest" check above had already passed.
    prop_mesh = reader.get_mesh(PROP_MESH_ID)
    if prop_mesh is None:
        fail(f"{PROP_MESH_ID}: not in the pack")
    else:
        prop_obj = build._build_rigid_prop(
            context, PROP_MESH_ID, prop_mesh, arm_obj, _rest, PROP_ATTACH_BONE, None
        )
        rest_translation = prop_obj.matrix_world.translation.copy()

        clip = reader.get_clip(HEAD_MOTION_CLIP)
        if clip is None:
            fail(f"{HEAD_MOTION_CLIP}: not in the pack")
        else:
            prop_action = animation_build.build_prop_action(
                HEAD_MOTION_CLIP, prop_obj, clip, skeleton
            )
            bone_action = animation_build.build_action(
                HEAD_MOTION_CLIP, clip, skeleton, arm_obj
            )
            if prop_action is None:
                fail(f"{PROP_MESH_ID}: build_prop_action returned None - "
                     f"pz_attach_bone or the attach bone lookup is broken")
            else:
                prop_obj.animation_data_create()
                animation_build.assign_action(prop_obj.animation_data, prop_action)
                arm_obj.animation_data_create()
                animation_build.assign_action(arm_obj.animation_data, bone_action)

                offsets = []
                moved = 0.0
                last_frame = int(prop_action.frame_range[1])
                sample_frames = sorted({0, last_frame // 4, last_frame // 2,
                                         3 * last_frame // 4, last_frame})
                for frame in sample_frames:
                    context.scene.frame_set(frame)
                    dg = context.evaluated_depsgraph_get()
                    bone_world = (arm_obj.matrix_world
                                  @ arm_obj.evaluated_get(dg).pose.bones[PROP_ATTACH_BONE].matrix)
                    prop_world = prop_obj.evaluated_get(dg).matrix_world
                    rel = bone_world.inverted() @ prop_world
                    offsets.append((frame, rel))
                    moved = max(moved, (prop_world.translation - rest_translation).length)

                base_frame, base_rel = offsets[0]
                base_t, base_q = base_rel.translation, base_rel.to_quaternion()
                worst_pos = worst_rot = 0.0
                for frame, rel in offsets[1:]:
                    dpos = (rel.translation - base_t).length
                    drot = math.degrees(base_q.rotation_difference(rel.to_quaternion()).angle)
                    worst_pos = max(worst_pos, dpos)
                    worst_rot = max(worst_rot, drot)
                if worst_pos > 0.001 or worst_rot > 0.05:
                    fail(f"{PROP_MESH_ID}: drifted {worst_pos:.5f} units / {worst_rot:.3f} deg "
                         f"off {PROP_ATTACH_BONE} across {HEAD_MOTION_CLIP} - not rigidly "
                         f"attached (pz_prop_base composition order is likely wrong)")
                else:
                    print(f"  ok  {PROP_MESH_ID} stays rigid on {PROP_ATTACH_BONE} "
                          f"(drift {worst_pos:.5f} units / {worst_rot:.3f} deg)")

                if moved < 0.02:
                    fail(f"{PROP_MESH_ID}: only {moved:.4f} units of motion across "
                         f"{HEAD_MOTION_CLIP} - looks frozen at rest "
                         f"(the original floating-prop bug)")
                else:
                    print(f"  ok  {PROP_MESH_ID} moved {moved:.4f} units from rest "
                          f"over the clip - tracking its attach bone")

    if fail.count:
        print(f"\n{fail.count} failure(s)")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
