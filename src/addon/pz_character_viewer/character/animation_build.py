"""Bakes a PZ animation clip (pzc_reader.get_clip()) into a real Blender
Action on the preview armature build.py builds.

This is NOT a straight reuse of build.py's rest-pose axis-fix. Read
_build_armature()'s own comment in build.py before touching this: the
matrices it returns for rest-pose placement are deliberately *not*
arm_data.bones[...].matrix_local, because align_roll() re-permutes each
bone's local axes onto Blender's chain-direction convention (Y = bone
length, not the source's own basis). Blender's real per-bone pose-local
frame is that permuted one, so a clip's keyframes have to be solved against
Blender's own rest matrices (read back off the built armature, step 1
below), not conjugated the same way rest-pose was.

The approach: walk every sample time, compose each bone's Godot-space world
matrix the same parent-forward-pass shape _build_armature() already uses for
rest (world[i] = world[parent] @ local[i]), convert with the existing
build._to_blender_matrix(), then re-express it in Blender's own per-bone
rotation convention before solving matrix_basis - no per-frame
view_layer.update(), so this stays fast enough to bake on demand.

That re-expression step is not optional, and it is the dominant term rather
than a rounding detail. build._to_blender_matrix() applies only the single
global axis-fix rotation; it does NOT apply _build_armature()'s per-bone
head/tail/align_roll re-expression onto Blender's Y-down-the-bone convention.
So a matrix it produces lives in a different per-bone rotational frame than
arm_data.bones[...].matrix_local, and mixing the two when solving matrix_basis
- which the first version of this file did - is wrong even at the rest pose,
then compounds down every chain through each bone's children. That is what
threw the bones into a spray the moment any clip was played.

Measured on the shipped pack (46 bones, MaleBody bind pose): the source basis
and Blender's rest basis differ by a mean of **79.5 degrees**, up to 121.3, on
**42 of 46** bones.

The fix, derived rather than guessed. Blender's armature deform is
`v' = pose[b] @ matrix_local[b]^-1 @ v`, and correct skinning needs
`v' = W_anim[b] @ W_bind[b]^-1 @ v` (W_bind being the pose the vertices were
authored in - build.bind_pose_from()). Equating them gives

    pose[b] = W_anim[b] @ C[b],  where  C[b] = W_bind[b]^-1 @ matrix_local[b]

build.py stashes W_bind[b] on each Bone as "pz_rest_source" at build time;
_rest_corrections() rebuilds C[b] from it and _world_matrices_at()'s output is
composed with C[b] before matrix_basis is solved. C[b] is in fact a pure
rotation (the two matrices share a translation exactly - verified to 0.0), but
it is carried as a full 4x4 so nothing silently breaks if that stops holding.

Verified outside Blender against the real pack by replaying this whole path -
including Blender's own vec_roll_to_mat3/align_roll - in numpy and running the
actual skin deformation: MaleBody under Bob_Idle lands at Z -0.003..1.765
(upright, full height) with the correction and Z -0.039..1.574 (crushed)
without, and Bob_ActionToSitIdle_heavy correctly ends up 0.92 tall, i.e.
sitting. The corrected path is bit-identical to `W_anim @ W_bind^-1 @ ML`.
"""

import bpy
import mathutils

from . import build

#: PZ's own sampling rate. Measured across the shipped pack: every clip's
#: samples are spaced exactly 1/30 s (Bob_Idle 21 samples over 0.667 s,
#: Bob_ClimbRope 31 over 1.000 s). Blender's default scene fps is 24, which
#: puts every key on a fractional frame (0.0, 0.8, 1.6, ...) so that only 5 of
#: 21 land on a frame Blender ever evaluates - see PZCHAR_OT_play_animation,
#: which pins the scene to this so keys land on integers and playback runs at
#: true speed.
SOURCE_FPS = 30

#: Keyframe.interpolation is an enum; foreach_set needs its integer value.
#: Resolved from the RNA rather than hardcoded, because guessing an enum's
#: ordinal is the kind of silent-wrong this file has already paid for once.
_LINEAR_ENUM = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value


def _interp_vec3(samples, t, fallback):
    """Linear-interpolate a (t, x, y, z) sample list at time t, holding the
    first/last value outside the track's own range, falling back to
    `fallback` (a bone's rest position/scale) if the track has no samples
    at all - e.g. a clip with position+rotation keys but no scale keys."""
    if not samples:
        return fallback
    if t <= samples[0][0]:
        return samples[0][1:4]
    if t >= samples[-1][0]:
        return samples[-1][1:4]
    for k in range(len(samples) - 1):
        t0 = samples[k][0]
        t1 = samples[k + 1][0]
        if t0 <= t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            v0, v1 = samples[k][1:4], samples[k + 1][1:4]
            return tuple(a + (b - a) * f for a, b in zip(v0, v1))
    return samples[-1][1:4]


def _interp_quat(samples, t, fallback_xyzw):
    """Spherically interpolate a (t, x, y, z, w) sample list at time t - plain
    lerp pops at wide keyframe spacing, and each keyframe's quaternion was
    decomposed independently at export time with no continuity guarantee, so
    the shortest-path sign fix (dot < 0 -> negate) is applied per segment."""
    if not samples:
        return fallback_xyzw
    if t <= samples[0][0]:
        return samples[0][1:5]
    if t >= samples[-1][0]:
        return samples[-1][1:5]
    for k in range(len(samples) - 1):
        t0 = samples[k][0]
        t1 = samples[k + 1][0]
        if t0 <= t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            x0, y0, z0, w0 = samples[k][1:5]
            x1, y1, z1, w1 = samples[k + 1][1:5]
            q0 = mathutils.Quaternion((w0, x0, y0, z0))
            q1 = mathutils.Quaternion((w1, x1, y1, z1))
            if q0.dot(q1) < 0:
                q1 = -q1
            q = q0.slerp(q1, f)
            return (q.x, q.y, q.z, q.w)
    return samples[-1][1:5]


def _world_matrices_at(skeleton, tracks_by_bone, t):
    """Godot-space world matrix per skeleton bone (parents-first) at time t -
    same forward-pass shape as _build_armature()'s rest composition in
    build.py (world[i] = world[parent] @ local[i]), just with each bone's
    local transform sampled from its clip track instead of always its rest
    transform."""
    world = []
    for i, bone in enumerate(skeleton):
        track = tracks_by_bone.get(bone["name"])
        rest_pos, rest_quat, rest_scale = bone["rest"]
        if track is None:
            pos, quat, scale = rest_pos, rest_quat, rest_scale
        else:
            pos = _interp_vec3(track["position"], t, rest_pos)
            quat = _interp_quat(track["rotation"], t, rest_quat)
            scale = _interp_vec3(track["scale"], t, rest_scale)

        local = mathutils.Matrix.Translation(pos) @ mathutils.Quaternion(
            (quat[3], quat[0], quat[1], quat[2])
        ).to_matrix().to_4x4()
        local = local @ mathutils.Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))

        parent_idx = bone["parent"]
        world.append(world[parent_idx] @ local if parent_idx >= 0 else local)
    return world


def _clip_sample_times(clip_data):
    """Every distinct keyframe time across a clip's tracks, sorted - shared by
    build_action() and build_prop_action() so both bake onto exactly the same
    frames."""
    sample_times = sorted(
        {
            s[0]
            for track in clip_data["tracks"]
            for samples in (track["position"], track["rotation"], track["scale"])
            for s in samples
        }
    )
    return sample_times or [0.0]


def _flat16_to_matrix(flat):
    flat = list(flat)
    return mathutils.Matrix([flat[0:4], flat[4:8], flat[8:12], flat[12:16]])


class RestDataMissing(Exception):
    """The armature predates build.py stashing "pz_rest_source" - it has to be
    rebuilt (Randomize, or any layer control) before a clip can be baked
    against it."""


def _rest_corrections(arm_data, skeleton):
    """{bone name: correction matrix C} where `C = rest_source^-1 @ matrix_local`.

    Composing a clip's world pose on the right by C is what makes the bake
    correct - see the module docstring for the derivation. Deliberately *not*
    tolerant of a missing "pz_rest_source": falling back to identity here
    silently reproduces the old exploded-bones behaviour with no error
    anywhere, which is precisely how the first attempt at this fix looked like
    it had not been applied at all. Better to say so.
    """
    out = {}
    for bone in skeleton:
        name = bone["name"]
        b = arm_data.bones.get(name)
        if b is None:
            continue  # every skeleton bone is an edit bone; defensive only
        raw = b.get("pz_rest_source")
        if raw is None:
            raise RestDataMissing(
                f"bone '{name}' has no pz_rest_source - this armature was built "
                "by an older version of the addon"
            )
        out[name] = _flat16_to_matrix(raw).inverted() @ b.matrix_local
    return out


def _channelbag_for(action, armature_obj):
    """The FCurve collection to build this action's keyframes into, plus the
    slot (or None) that has to be assigned back wherever this action gets
    played - AnimData.action_slot / NlaStrip.action_slot.

    Blender's "layered actions" redesign (4.4+) moved fcurves off Action
    directly - they now live in a per-slot ActionChannelbag reached through
    one layer's keyframe strip, because a single Action can hold separate
    data for several different objects ("slots") at once. Playing an action
    on that Blender means telling it which slot applies. Older Blender still
    exposes fcurves straight off the Action and has no concept of a slot."""
    if hasattr(action, "fcurves"):
        return action.fcurves, None

    slot = action.slots.new(id_type="OBJECT", name=armature_obj.name)
    layer = action.layers.new(name="Layer")
    strip = layer.strips.new(type="KEYFRAME")
    if hasattr(strip, "channelbag"):
        channelbag = strip.channelbag(slot, ensure=True)
    else:
        channelbag = strip.channelbags.new(slot)
    return channelbag.fcurves, slot


def iter_fcurves(action):
    """Every FCurve in this action, regardless of which of the two fcurve-
    storage APIs above this Blender build uses."""
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    fcurves = []
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in getattr(strip, "channelbags", []):
                fcurves.extend(channelbag.fcurves)
    return fcurves


def get_action_slot(action):
    """The slot to assign via AnimData.action_slot/NlaStrip.action_slot when
    playing this action, or None on a Blender build with no slot concept -
    each action _channelbag_for() builds gets exactly one slot, so the first
    (only) one is always the right one."""
    slots = getattr(action, "slots", None)
    if not slots:
        return None
    return slots[0]


def assign_action(anim_data, action):
    """anim_data.action = action, plus the new API's required action_slot -
    a no-op past the plain assignment on a Blender build with no slots."""
    anim_data.action = action
    slot = get_action_slot(action)
    if slot is not None and hasattr(anim_data, "action_slot"):
        anim_data.action_slot = slot


def build_action(clip_id, clip_data, skeleton, armature_obj, fps=None):
    """The baked Action for this clip, building and caching it in
    bpy.data.actions on first use (bpy.data.actions.get(clip_id) short-
    circuits every call after the first) - build lazily, never eagerly for
    every clip in the pack, the bake is real per-frame matrix work."""
    existing = bpy.data.actions.get(clip_id)
    if existing is not None:
        return existing

    if fps is None:
        # SOURCE_FPS, not the scene's fps. The bake must not depend on whatever
        # the scene happens to be set to when it runs: at 30 every key lands on
        # a whole frame, and the Play operator pins the scene to the same value
        # so playback is real-time. Reading scene fps here also made the bake
        # order-dependent - the action was baked at 24 and then the scene moved
        # to 30 underneath it, retiming every cached action.
        fps = SOURCE_FPS

    arm_data = armature_obj.data
    # Blender's own already-built rest matrices - NOT re-derived from
    # _build_armature()'s source-basis matrices, see module docstring.
    bone_local = {b.name: b.matrix_local.copy() for b in arm_data.bones}
    corrections = _rest_corrections(arm_data, skeleton)

    tracks_by_bone = {track["bone"]: track for track in clip_data["tracks"]}
    sample_times = _clip_sample_times(clip_data)

    per_bone = {
        name: {"location": [], "rotation_quaternion": [], "scale": []} for name in bone_local
    }
    prev_quat = {}

    for t in sample_times:
        world_godot = _world_matrices_at(skeleton, tracks_by_bone, t)
        world_blender = [
            build._to_blender_matrix(w) @ corrections[bone["name"]]
            for w, bone in zip(world_godot, skeleton)
        ]

        for i, bone in enumerate(skeleton):
            name = bone["name"]
            if name not in bone_local:
                continue  # every skeleton bone becomes an edit bone in
                # _build_armature(), so this shouldn't trigger - defensive.

            parent_idx = bone["parent"]
            if parent_idx < 0:
                matrix_basis = bone_local[name].inverted() @ world_blender[i]
            else:
                parent_name = skeleton[parent_idx]["name"]
                matrix_basis = (
                    bone_local[name].inverted()
                    @ bone_local[parent_name]
                    @ world_blender[parent_idx].inverted()
                    @ world_blender[i]
                )

            loc, quat, scale = matrix_basis.decompose()
            if name in prev_quat and prev_quat[name].dot(quat) < 0:
                quat = -quat
            prev_quat[name] = quat.copy()

            per_bone[name]["location"].append(loc)
            per_bone[name]["rotation_quaternion"].append(quat)
            per_bone[name]["scale"].append(scale)

    action = bpy.data.actions.new(clip_id)
    # Tag before anything can fail, so a half-built action is still findable by
    # build.purge_baked_actions() rather than leaking into the next rebuild.
    action[build.BAKED_ACTION_TAG] = clip_id
    fcurves, _slot = _channelbag_for(action, armature_obj)
    frames = [t * fps for t in sample_times]

    for name, channels in per_bone.items():
        for channel, count in (("location", 3), ("rotation_quaternion", 4), ("scale", 3)):
            values = channels[channel]
            data_path = f'pose.bones["{name}"].{channel}'
            for axis in range(count):
                fc = fcurves.new(data_path=data_path, index=axis)
                fc.keyframe_points.add(len(sample_times))
                flat = []
                for frame, v in zip(frames, values):
                    flat.append(frame)
                    flat.append(v[axis])
                fc.keyframe_points.foreach_set("co", flat)
                # LINEAR, not Blender's BEZIER default. Two reasons, the second
                # measured: the samples above were produced by _interp_vec3 /
                # _interp_quat, which are linear/slerp, so Bezier would invent
                # curvature that is not in the source; and a new keyframe's auto
                # handles are fitted assuming 1-frame spacing, which these keys
                # do not have (PZ samples at 30 Hz). Measured in Blender 5.2 on
                # a perfectly linear ramp keyed at 0.0/0.8/1.6: frame 1.0
                # evaluated to 1.3672 instead of 1.25, a ~50% handle-slope
                # error. Bezier also overshoots quaternion components
                # independently, which shears a bone rather than rotating it.
                fc.keyframe_points.foreach_set(
                    "interpolation", [_LINEAR_ENUM] * len(sample_times)
                )
                fc.update()

    return action


def build_prop_action(clip_id, obj, clip_data, skeleton, fps=None):
    """Bakes the Action that drives a RIGID PROP (hat, glasses - a mesh with no
    skin data, placed by build._build_rigid_prop as a plain object riding one
    attach bone) through `clip_id`, or None if `obj` isn't a rigid prop or its
    attach bone isn't in `skeleton`.

    Not the same problem build_action() solves. That function reconciles the
    rotation-convention change align_roll() applies (source basis ->
    matrix_local) - irrelevant here, since a prop never goes through the
    armature deform; its mesh is authored directly in the attach bone's own
    local frame and placed with a plain matrix_world assignment.

    Deliberately drives straight off the clip's own animated world transform
    for the attach bone (`_world_matrices_at()`, no correction) rather than
    reconciling it against the bind-pose rest `_build_rigid_prop()` uses for
    the static/no-animation placement. An earlier version of this function
    used a "pz_prop_base" correction for exactly that reconciliation, and it
    was measurably self-consistent (0.000 rotational drift relative to the
    bone) - but the SKIN bake (build_action(), which is what the head mesh
    itself deforms through) never reconciles that same bind-vs-plain-rest gap
    for ANY bone, so the two were each internally consistent but consistent
    with DIFFERENT reference poses, and diverged by the same amount
    (measured: ~7cm / ~9deg) the instant any clip played - reproduced
    identically on Bob_Idle and Bob_EmoteSneeze2H, i.e. clip-independent,
    confirming it wasn't clip content. Driving straight off the same
    A(t) the skin bake's own matrix_basis solve is built from is what makes a
    prop track the head mesh it's actually sitting on, at the cost of a small
    jump from the prop's own static rest placement the instant a clip starts
    (real, and not yet reconciled - see the TODO this leaves: build_action()
    could get the same bind-pose reconciliation build.bind_pose_from()
    already computes, which would let both this function's correction AND
    that jump be reintroduced correctly instead of removed).
    """
    bone_name = obj.get("pz_attach_bone")
    if bone_name is None:
        return None
    armature_obj = obj.parent
    if armature_obj is None or armature_obj.data is None:
        return None

    action_name = f"{clip_id}::{bone_name}"[:63]
    existing = bpy.data.actions.get(action_name)
    if existing is not None:
        return existing

    if fps is None:
        fps = SOURCE_FPS

    bone_idx = next((i for i, b in enumerate(skeleton) if b["name"] == bone_name), None)
    if bone_idx is None:
        return None

    # build.RIGID_PROP_FORWARD_NUDGE, if this mesh has one - composed on the
    # RIGHT, i.e. in the bone's own rotating local frame, so it travels WITH
    # the bone's rotation instead of pointing at a fixed world direction. A
    # world-space version of this (subtracting from the composed matrix's
    # translation.y after the fact) was tried and measured wrong: matches at
    # rest, drifts once the head actually turns.
    nudge = obj.get("pz_forward_nudge", 0.0)
    nudge_local = (
        mathutils.Matrix.Translation((0.0, -nudge, 0.0)) if nudge else mathutils.Matrix.Identity(4)
    )

    # A held item's per-item grip pose (see build._build_rigid_prop's
    # attach_offset/attach_rotate), stashed on the object the same way the
    # nudge above is. Composed on the right for the same reason the nudge
    # is - it has to travel WITH the bone's animated rotation, not sit at a
    # fixed offset from wherever the bone happens to be this frame.
    attach_local_raw = obj.get("pz_attach_local")
    attach_local = (
        build._matrix_from_flat16(list(attach_local_raw))
        if attach_local_raw is not None else mathutils.Matrix.Identity(4)
    )
    tracks_by_bone = {track["bone"]: track for track in clip_data["tracks"]}
    sample_times = _clip_sample_times(clip_data)

    channels = {"location": [], "rotation_quaternion": [], "scale": []}
    prev_quat = None
    for t in sample_times:
        world = _world_matrices_at(skeleton, tracks_by_bone, t)
        m = build._to_blender_matrix(world[bone_idx]) @ nudge_local @ attach_local
        loc, quat, scale = m.decompose()
        if prev_quat is not None and prev_quat.dot(quat) < 0:
            quat = -quat
        prev_quat = quat.copy()
        channels["location"].append(loc)
        channels["rotation_quaternion"].append(quat)
        channels["scale"].append(scale)

    action = bpy.data.actions.new(action_name)
    action[build.BAKED_ACTION_TAG] = clip_id
    fcurves, _slot = _channelbag_for(action, obj)
    frames = [t * fps for t in sample_times]

    for channel, count in (("location", 3), ("rotation_quaternion", 4), ("scale", 3)):
        values = channels[channel]
        for axis in range(count):
            fc = fcurves.new(data_path=channel, index=axis)
            fc.keyframe_points.add(len(sample_times))
            flat = []
            for frame, v in zip(frames, values):
                flat.append(frame)
                flat.append(v[axis])
            fc.keyframe_points.foreach_set("co", flat)
            fc.keyframe_points.foreach_set(
                "interpolation", [_LINEAR_ENUM] * len(sample_times)
            )
            fc.update()

    return action

    return action
