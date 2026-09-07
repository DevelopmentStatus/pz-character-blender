"""Builds Blender objects (armature + skinned meshes + materials) from
pzc_reader/pools output.

Everything under one empty, "PZ Character", so toggling preview off is one
recursive delete. Rebuilt from scratch on every layer change/randomize/
default - character meshes are small (a body is a few hundred verts, a
dressed character touches ~6 of them) so this is simpler and cheap enough,
unlike the per-frame rebuild ChunkBatcher deliberately avoids on the Godot
side for a whole chunk ring.
"""

import math
import os

import bpy
import mathutils

ROOT_NAME = "PZ Character"

#: Base name for the empty a released character is moved under. Deliberately
#: NOT ROOT_NAME with a suffix Blender would add itself: remove_existing() and
#: apply_shading() look the live character up by exact name, and a released
#: one must never be findable that way.
RELEASED_NAME = "PZ Character (released)"

#: Custom property on a released empty holding its slot number, so the next
#: release can take the next free position even after earlier ones are deleted
#: or moved. Counting the empties instead would put two characters in the same
#: place as soon as one in the middle is removed.
RELEASE_INDEX_PROP = "pz_release_index"

#: Metres between released characters along +X. The bind pose is a T-pose with
#: hands at x +-0.672, i.e. 1.344 wide, so this leaves a small gap rather than
#: letting fingertips intersect.
RELEASE_SPACING = 1.5
ARMATURE_NAME = "PZ Character Armature"

#: A beard's own authored geometry sits essentially touching the face mesh
#: (measured: 0.001 units to the nearest body vertex, inside the face's own
#: z-range at chin height) - baked into characters.pzc, not introduced by
#: anything armature-related (skin deformation is a no-op at rest pose, so it
#: can't move a vertex regardless of bone construction). A per-vertex-normal
#: push was tried first and reverted - too subtle to read as "moved" and it
#: alters the mesh's own shape (each vertex travels a different amount/
#: direction), not just its position. This is a plain rigid translation along
#: Blender's own -Y (= Godot +Z, the character's forward axis - see the
#: backpack/dress-bone note elsewhere in this file), applied to the whole
#: beard object after it's built, so the mesh data itself is untouched.
BEARD_FORWARD_NUDGE = 0.003

#: Per-item forward correction for a RIGID PROP, the same idea as
#: BEARD_FORWARD_NUDGE but keyed by mesh id rather than applying to a whole
#: slot, because it does NOT generalise to "hats" or "props" as a category -
#: confirmed by rendering two other rigid head props (M_ArmyHelmet,
#: M_Glasses_Aviators) at their unmodified placement and finding both sit
#: correctly with zero adjustment. Only M_PoliceHat's own mesh sits back far
#: enough to expose the hairline above the eyebrows instead of the brim
#: sitting near it - an authoring quirk of this one item, not a bug in
#: _build_rigid_prop's placement maths (which the other two props verify).
#: Tuned by eye against a render, the same way BEARD_FORWARD_NUDGE was -
#: retune here if a re-export changes this mesh, not by touching the general
#: placement formula.
RIGID_PROP_FORWARD_NUDGE = {
    "Clothes/M_PoliceHat": 0.05,
}

#: Which real skeleton bone a held item binds to, by hand slot - PZ's own
#: rule (`ModelManager.java:670-690`, decompiled): a primary-hand item binds
#: to `Bip01_Prop1`, a secondary-hand item to `Bip01_Prop2`. Both hang off
#: `Bip01` (the root), not off a hand bone - they only look welded to the
#: hands because clip animators keyed them that way, per clip. Two empirical
#: "just add a 90-degree world-space rotation" attempts were made here before
#: this was known and are gone now - the actual bug was this bone mapping
#: being backwards (this file bound both hands to Bip01_Prop2) and the
#: mined attach_offset/attach_rotate being applied unconditionally instead of
#: only when the item declares a block for the bone it's actually bound to.
#: See src/docs/weapon_attachment.md for the full
#: derivation (decompiled Java + raw `.x` clip evidence) - do not reintroduce
#: a guessed correction here; if a held item still looks wrong after this,
#: the bug is elsewhere (stale exported mesh geometry, a different item's
#: own axis convention - see that doc's "Traps worth not re-learning" - or a
#: genuine mining error), not a missing twist.
HELD_ITEM_HAND_BONES = {"weapon_right": "Bip01_Prop1", "weapon_left": "Bip01_Prop2"}

#: Godot is right-handed Y-up; Blender is right-handed Z-up. This is the same
#: axis remap Blender's own glTF importer applies ((x,y,z) -> (x,-z,y)) - a
#: fixed +90 degree rotation about X, expressed as a change-of-basis matrix
#: so it can be used to conjugate a whole rest transform, not just a bare
#: position. **Verified** in Blender 5.2 by
#: scripts/test_character_animation.py: the built body stands upright at
#: 1.765 units and poses correctly under real clips. (This comment previously
#: said "not verified - no headless runner in this project"; there is one now,
#: and the belief that there could not be one is what let three wrong fixes
#: ship.) If the character ever comes up rotated/mirrored, flip the sign here,
#: don't redesign the transform.
_AXIS_FIX = mathutils.Matrix(((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)))
_AXIS_FIX_4 = _AXIS_FIX.to_4x4()
_AXIS_FIX_4_INV = _AXIS_FIX_4.inverted()


def _to_blender_vec(v):
    return _AXIS_FIX @ mathutils.Vector(v)


def _to_blender_matrix(m):
    """Conjugate a Godot-space (right-handed Y-up) 4x4 by the axis-fix
    rotation, so both the translation and the rotation/scale basis land in
    Blender's Z-up convention."""
    return _AXIS_FIX_4 @ m @ _AXIS_FIX_4_INV


def _godot_transform_matrix(offset, quat_xyzw):
    """(position, quaternion xyzw) -> a 4x4, both still in Godot space -
    the shape `attach_offset`/`attach_rotate` are stored in (see
    src/extractor/pz_characters.py's `_weapon_attach_transform`, and
    CharacterAssetRegistry.gd's doc comment on the same two keys: "position
    in meters and a quaternion xyzw, both already in this bone's local
    space"). Run this through `_to_blender_matrix()` before composing it
    against anything already-converted (a bone rest, another Blender-space
    matrix) - conjugation distributes over a product, so converting the
    piece and converting the whole agree."""
    qx, qy, qz, qw = quat_xyzw
    m = mathutils.Quaternion((qw, qx, qy, qz)).to_matrix().to_4x4()
    m.translation = mathutils.Vector(offset)
    return m


def _flatten_matrix(m):
    """4x4 -> flat row-major 16 floats, for an ID property (which takes plain
    sequences, not mathutils types) - same layout `_build_armature()` already
    uses for `pz_rest_source`."""
    return [v for row in m for v in row]


def _matrix_from_flat16(flat):
    return mathutils.Matrix([flat[0:4], flat[4:8], flat[8:12], flat[12:16]])

_texture_cache = {}  # rel path -> bpy.types.Image, keyed by characters_root + rel


def clear_texture_cache():
    _texture_cache.clear()


def _load_image(characters_root, rel):
    if not rel:
        return None
    key = (characters_root, rel)
    cached = _texture_cache.get(key)
    if cached is not None:
        return cached
    path = os.path.normpath(os.path.join(characters_root, rel.replace("textures/", "textures/", 1)))
    if not os.path.exists(path):
        return None
    img = bpy.data.images.load(path, check_existing=True)
    _texture_cache[key] = img
    return img


#: Marks an Action as one this addon baked, so purge_baked_actions() can find
#: it again. Set by animation_build.build_action() (and the NLA masking copy in
#: anim_ops) rather than matching on name - a masked action's name is derived
#: and can pick up Blender's automatic ".001" suffix, so names are not a
#: reliable key. Value is the clip id it was baked from.
BAKED_ACTION_TAG = "pz_clip"

#: The one image every character's body texture is composited into. Shared and
#: overwritten in place on purpose - that is what makes a tint edit cheap - so
#: anything that has to outlive the next build needs its own copy. See
#: _detach_body_composite().
BODY_COMPOSITE_NAME = "pz_character_body_composite"

#: The panel exposes blood/dirt as plain on/off checkboxes (see
#: props.PZCharacterDamageRegion's docstring for why - PZ tracks a float
#: level, nothing here asked for a tunable one), so a checked box always
#: sends this fixed intensity into composite_body_texture()/
#: composite_garment_texture() rather than a per-region float.
DEFAULT_BLOOD_INTENSITY = 0.6
DEFAULT_DIRT_INTENSITY = 0.5


def purge_baked_actions():
    """Delete every Action this addon baked.

    Must run whenever the armature is rebuilt. A baked action's keyframes are
    solved against *that* armature's rest matrices, so it is only valid for the
    armature it was built against - and both build_action() and the Play
    operator short-circuit on bpy.data.actions.get(clip_id), which means a
    stale action is returned in preference to baking a correct one. Nothing
    here used to clear them, so an action baked before a fix to the bake math
    survived every character rebuild and the fix appeared to do nothing.
    Returns how many were removed.
    """
    doomed = [a for a in bpy.data.actions if a.get(BAKED_ACTION_TAG) is not None]
    for action in doomed:
        bpy.data.actions.remove(action)
    return len(doomed)


def apply_shading(smooth):
    """Flat- or smooth-shade every mesh under the preview character.

    Per-polygon `use_smooth` through the data API rather than
    bpy.ops.object.shade_smooth(): the operator needs the right mode, an active
    object and a selection, all of which this can be called without (a panel
    toggle, or the tail of build_character()). It also avoids disturbing the
    user's current selection as a side effect.

    Returns how many meshes were touched.
    """
    root = bpy.data.objects.get(ROOT_NAME)
    if root is None:
        return 0
    touched = 0
    for obj in [root] + list(_descendants(root)):
        if obj.type != "MESH" or obj.data is None:
            continue
        mesh = obj.data
        mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))
        mesh.update()
        touched += 1
    return touched


def reset_to_rest_pose(arm_obj):
    """Put `arm_obj` back in the pose _build_armature() built it in - the bind
    (T) pose - exactly as a Clear / Generate-Replace cycle would.

    Clearing `animation_data.action` is NOT enough on its own, which is the
    whole reason this exists: an action drives each pose bone's `matrix_basis`,
    and dropping the action just stops it being *written*. Whatever values the
    last evaluated frame left behind stay there, so the character freezes in
    mid-animation instead of returning to rest. The pose channels have to be
    zeroed by hand.

    NLA tracks are removed rather than muted: every track on this rig was put
    there by pz_character.layer_animations, they reference actions that
    purge_baked_actions() frees on the next rebuild anyway, and a muted track
    would silently come back the moment someone unmuted it.
    """
    anim = arm_obj.animation_data
    if anim is not None:
        anim.action = None
        for track in list(anim.nla_tracks):
            anim.nla_tracks.remove(track)

    identity = mathutils.Matrix.Identity(4)
    for pose_bone in arm_obj.pose.bones:
        pose_bone.matrix_basis = identity

    # Rigid props (hats, glasses - see _build_rigid_prop) aren't part of the
    # armature deform, so the loop above doesn't touch them. Each one carries
    # its own baked Action (build_prop_action, assigned in
    # PZCHAR_OT_play_animation) and the exact same staleness problem applies -
    # dropping the action leaves it at whatever the last evaluated frame put
    # it. "pz_rest_source" on its attach bone IS the matrix it was placed at
    # when built, so this is a straight read-back, no skeleton/clip data
    # needed. obj.parent is arm_obj directly (not the root empty), so this
    # walk finds every one without needing that reference passed in.
    for obj in arm_obj.children:
        bone_name = obj.get("pz_attach_bone")
        if bone_name is None:
            continue
        if obj.animation_data is not None:
            obj.animation_data.action = None
        bone = arm_obj.data.bones.get(bone_name)
        raw = bone.get("pz_rest_source") if bone is not None else None
        if raw is not None:
            placement = _matrix_from_flat16(list(raw))
            # A weapon (or anything else placed with a per-item local pose,
            # see _build_rigid_prop's attach_offset/attach_rotate) stashes
            # that same local matrix on the object - compose it back in, or
            # resetting would snap a held item to the bone's bare rest
            # instead of the grip pose it was actually built at.
            attach_local = obj.get("pz_attach_local")
            if attach_local is not None:
                placement = placement @ _matrix_from_flat16(list(attach_local))
            # matrix_world, not matrix_basis - matches how _build_rigid_prop()
            # placed it originally and doesn't assume anything about the
            # armature's own parent chain being at the origin.
            obj.matrix_world = arm_obj.matrix_world @ placement


def remove_existing(context):
    # Before the objects go, so this runs on a rebuild *and* on Clear -
    # in both cases the armature these were solved against stops existing.
    purge_baked_actions()

    root = bpy.data.objects.get(ROOT_NAME)
    if root is None:
        return
    to_remove = [root] + list(_descendants(root))
    for obj in to_remove:
        data = obj.data
        obj_type = obj.type
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is None:
            continue
        if obj_type == "MESH" and data.users == 0:
            bpy.data.meshes.remove(data)
        elif obj_type == "ARMATURE" and data.users == 0:
            bpy.data.armatures.remove(data)


def _descendants(obj):
    for child in obj.children:
        yield child
        yield from _descendants(child)


def bind_pose_from(mesh_data):
    """{bone name: Godot-space world matrix} for the pose `mesh_data` was
    actually skinned in, from its bind list.

    A mesh's bind entry is the *inverse* bind matrix for one bound bone
    (_pack_mesh's "skin bind list"), so inverting it gives that bone's absolute
    transform in the pose the vertices were authored against - which is the
    pose an armature has to rest in for those vertices to sit on their bones.

    This is not the same pose as skel:Human, and the difference is not small.
    Measured on MaleBody (28 bound bones): the bind pose is a T-pose, exactly
    mirror-symmetric to 1e-6 - hands at x +-0.672, y 1.323, feet straight under
    the hips - while skel:Human (models_X/Skinned/Male_Skeleton.x) is a
    relaxed, visibly asymmetric pose with the hands down at y ~0.88 and the
    left foot 0.57 units in front of the right. Mean displacement across the 28
    shared bones is 0.242 units and the worst is 0.805 (Bip01_L_Finger1).
    Building the armature from skel:Human alone - what this used to do - is
    what put the bones through and beside the body instead of inside it.

    Only the bones a mesh actually binds appear here (28 of the skeleton's 46
    for a body: no nubs, no prop sockets, no Dummy01). The rest keep their
    skel:Human offset, composed onto whatever their parent resolved to, so the
    hierarchy stays intact either way.
    """
    out = {}
    if mesh_data is None:
        return out
    binds = mesh_data.get("binds") or []
    for slot, name in enumerate(mesh_data.get("bones") or []):
        if slot >= len(binds):
            break
        pos, quat, _scale = binds[slot]
        inverse_bind = mathutils.Matrix.Translation(pos) @ mathutils.Quaternion(
            (quat[3], quat[0], quat[1], quat[2])
        ).to_matrix().to_4x4()
        out[name] = inverse_bind.inverted()
    return out


#: Bip01 is a 3ds Max Biped rig: a bone's own axis - the direction its child
#: sits along - is local **+X**, not +Y. Measured, not assumed: every chain
#: child's rest offset in characters.pzc is purely +X (Spine 0.093,0,0;
#: Neck 0.235,0,-0.008; Head 0.061,0,0; Forearm/Hand 0.247,0,0; Calf
#: 0.428,0,0). Blender's bones run along local +Y and there is no way to tell
#: it otherwise, so the source basis has to be re-expressed rather than
#: copied: assigning the source matrix straight to EditBone.matrix - which is
#: what this used to do - makes each bone's *side* axis its length axis, and
#: the reach-toward-children pass then stretches it that wrong way by the full
#: limb distance. That is the spray of disconnected shards - a separate fault
#: from the wrong rest pose bind_pose_from() fixes, and both were in play.
#:
#: Names, parent indices and hierarchy are carried through untouched, which is
#: what keeps PZ's own clips retargetable: a clip's tracks are keyed by bone
#: name and hold absolute parent-relative transforms, so they overwrite the
#: rest pose rather than being applied as offsets from it.
_CHAIN_AXIS = 0  # column of the source basis that points down the chain

#: How closely a child must continue the parent's own axis to be treated as
#: the chain continuation rather than a branch, as a dot product. Only
#: consulted where a bone has more than one child. 0.7 (=45 degrees) keeps
#: Spine->Spine1 and Neck->Head (both 1.0) while rejecting the clavicles off
#: the neck, the thighs off the spine, and Bip01_Prop1 off Bip01 (0.45) -
#: each of which still gets its own bone branching from the shared joint.
_CHAIN_DOT_MIN = 0.7

#: A terminal bone (a *Nub, a prop socket, an unparented marker) has no child
#: joint to aim at, so it continues the direction its parent was already
#: travelling - a finger nub carries on down the finger, a toe nub down the
#: toe. Its own +X axis is the obvious alternative and is wrong here: Biped
#: mirrors the right-hand side, so Bip01_R_Finger0Nub / R_Finger1Nub and the
#: toe and dress nubs carry an axis exactly negated from the chain they cap,
#: and would fire backwards through their own parent. Only a bone with no
#: parent at all (Translation_Data, Skeleton) falls back to its own axis.
_LEAF_LENGTH = 0.05           # fallback for a bone with nothing to point at
_LEAF_PARENT_FRACTION = 0.4   # a nub reads as a stub of the bone it caps
_LEAF_MIN, _LEAF_MAX = 0.02, 0.08
_EPS = 1e-5


def _plain_rest_world(skeleton):
    """Forward-composed Godot-space world transform per bone using ONLY the
    skeleton's own rest tuples - no bind-pose override. This is the same
    baseline a clip's own zero-point uses (animation_build._world_matrices_at()
    falls back to exactly this whenever a bone has no track), so it is the
    correct thing to measure a clip's motion against for driving a rigid prop.

    It is NOT the same as _build_armature()'s bind-pose-corrected rest used for
    display placement, and the difference is not small: measured on the
    shipped pack, **every one of the 28 bones a mesh actually skins** differs
    from its plain rest, from 0.016/5.8deg (Bip01_Spine) up to 0.92/123deg
    (finger nubs) - Bip01_Head alone is 0.068 units / 9.5 degrees.

    "pz_prop_base" below computes a correction from this gap, but it is
    currently unused - see the long comment where it's stored for why (the
    skin bake has this exact same gap, unreconciled, so a prop using this
    correction drifted off the head mesh it's supposed to sit on instead of
    matching it).
    """
    world = []
    for bone in skeleton:
        pos, quat, _scale = bone["rest"]
        local = mathutils.Matrix.Translation(pos) @ mathutils.Quaternion(
            (quat[3], quat[0], quat[1], quat[2])
        ).to_matrix().to_4x4()
        parent_idx = bone["parent"]
        world.append(world[parent_idx] @ local if parent_idx >= 0 else local)
    return world


def _build_armature(context, skeleton, parent, bind_pose=None):
    arm_data = bpy.data.armatures.new(ARMATURE_NAME)
    arm_obj = bpy.data.objects.new(ARMATURE_NAME, arm_data)
    context.collection.objects.link(arm_obj)
    arm_obj.parent = parent

    context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = arm_data.edit_bones

    # Rest transforms are parent-relative (same convention Skeleton3D's
    # set_bone_rest() expects and composes on the Godot side) - Blender's
    # EditBone.matrix has no such mode, it is always armature/absolute space.
    # Bones arrive parents-first (guaranteed by _pack_skeleton()'s write
    # order), so composing world = parent_world @ local in one forward pass
    # is enough; skipping this and assigning `local` directly is what
    # collapsed every bone into a tiny cluster near the origin.
    bind_pose = bind_pose or {}
    godot_world = []  # Godot-space (parent-composed, pre axis-fix) per bone
    rest_mats = []    # the same transforms in Blender space, source basis intact
    for bone in skeleton:
        pos, quat, _scale = bone["rest"]
        local = mathutils.Matrix.Translation(pos) @ mathutils.Quaternion(
            (quat[3], quat[0], quat[1], quat[2])
        ).to_matrix().to_4x4()
        parent_idx = bone["parent"]
        # A bone the meshes actually bind takes its absolute bind transform
        # verbatim; anything else (nubs, prop sockets, Dummy01) keeps its
        # skel:Human offset and is composed onto whatever its parent resolved
        # to, so an unbound bone still follows a bound parent. See
        # bind_pose_from() for why the two poses differ at all.
        world = bind_pose.get(bone["name"])
        if world is None:
            world = godot_world[parent_idx] @ local if parent_idx >= 0 else local
        godot_world.append(world)
        rest_mats.append(_to_blender_matrix(world))

    heads = [m.translation.copy() for m in rest_mats]
    axes = [m.to_3x3().col[_CHAIN_AXIS].normalized() for m in rest_mats]
    rolls = [m.to_3x3().col[2].normalized() for m in rest_mats]

    children_of = {}
    for i, bone in enumerate(skeleton):
        if bone["parent"] >= 0:
            children_of.setdefault(bone["parent"], []).append(i)

    created = []
    lengths = []
    for i, bone in enumerate(skeleton):
        eb = edit_bones.new(bone["name"])
        eb.head = heads[i]

        # Point the tail at the joint this bone actually drives, so the spine
        # runs up the body, an arm runs shoulder->hand, a leg hip->foot, and a
        # foot points at its toe rather than straight down its own X axis.
        # Branch roots (neck, spine, pelvis, Bip01) have several children and
        # only one of them continues the chain; the others start chains of
        # their own and get their own bones, so they must not drag this bone's
        # tail sideways - which averaging every child's head, the previous
        # behaviour, did.
        tail = None
        candidates = [c for c in children_of.get(i, []) if (heads[c] - heads[i]).length > _EPS]
        if len(candidates) == 1:
            tail = heads[candidates[0]]
        elif candidates:
            best = max(candidates, key=lambda c: (heads[c] - heads[i]).normalized().dot(axes[i]))
            if (heads[best] - heads[i]).normalized().dot(axes[i]) >= _CHAIN_DOT_MIN:
                tail = heads[best]

        if tail is None:
            parent_idx = bone["parent"]
            direction = axes[i]
            length = _LEAF_LENGTH
            if parent_idx >= 0 and lengths[parent_idx] > _EPS:
                direction = (created[parent_idx].tail - created[parent_idx].head).normalized()
                length = min(_LEAF_MAX, max(_LEAF_MIN, lengths[parent_idx] * _LEAF_PARENT_FRACTION))
            tail = heads[i] + direction * length

        eb.tail = tail
        # A zero-length bone is silently dropped by Blender on leaving edit
        # mode, and would take its children's parenting with it.
        if eb.length < _EPS:
            eb.tail = heads[i] + axes[i] * _LEAF_LENGTH

        # Roll last: head/tail fix two of the three axes, and align_roll spins
        # the bone about its own length until its local +Z sits as close as it
        # can to the bone's own rest basis third axis. Every bone in every
        # chain is rolled from the same source axis, so the mapping is
        # one fixed permutation rather than Blender's arbitrary per-bone
        # default - which is what makes pose rotations, IK and retargeting
        # behave the same way at every joint instead of per-bone.
        eb.align_roll(rolls[i])

        created.append(eb)
        lengths.append(eb.length)

    for i, bone in enumerate(skeleton):
        if bone["parent"] >= 0:
            created[i].parent = created[bone["parent"]]
            # NEVER use_connect, even where the parent's tail really does land
            # on this child's head. This used to be conditional on exactly
            # that (a nicer connected display), and it is wrong for an
            # ANIMATED rig: a connected bone's head is rigidly locked to its
            # parent's tail, so Blender silently DISCARDS that bone's own
            # location channel entirely - the keyframed values are baked and
            # stored, `fcurve.evaluate()` returns them correctly, but
            # PoseBone.matrix ignores them regardless. Measured on the shipped
            # skeleton: 31 of 46 bones - the whole spine/neck/head chain among
            # them - satisfied the coincidence test above and got connected,
            # and every one of them was carrying a REAL positional offset from
            # its clip (root-motion-like data, not just rotation). One frame
            # of one clip measured a 0.0656 unit error on Bip01_Head alone,
            # constant for every descendant once introduced. This is very
            # likely the true shape of the "floating hat" symptom, since the
            # head bone itself was corrupted upstream of anything prop-
            # specific. Confirmed by disabling use_connect entirely and
            # re-measuring: the error drops to exactly 0.000000.
            created[i].use_connect = False

    bpy.ops.object.mode_set(mode="OBJECT")

    # Blender-space rest matrix per bone name, kept for rigid props (hats,
    # glasses, ...): a mesh with no skin data carries no vertex groups for the
    # Armature modifier to deform, so PZ places it at its attach bone's rest
    # transform directly - see _build_rigid_prop().
    #
    # These are *our* matrices (source basis, as composed above), deliberately
    # not arm_data.bones[...].matrix_local. The two used to be identical and
    # reading Blender's own value was the safer choice; re-expressing each bone
    # in Blender's +Y convention above breaks that equality by design, and a
    # prop's vertices are authored in the *source* bone space (M_PoliceHat
    # spans x 0.162..0.326 along the head bone's own +X, i.e. the top of the
    # skull). Placing one with a re-oriented display matrix would rotate it off
    # the head by exactly the convention change.
    bone_rest_by_name = {bone["name"]: rest_mats[i] for i, bone in enumerate(skeleton)}

    # Each bone's source-basis rest matrix, stashed on the Bone itself so
    # animation_build.py can recover it. It cannot be read back off the built
    # armature: head/tail/align_roll above re-express every bone in Blender's
    # Y-down-the-bone convention, so arm_data.bones[name].matrix_local agrees
    # with rest_mats[i] on translation and *not* on rotation. Measured on the
    # shipped pack: the two differ by a mean of 79.5 degrees, up to 121.3, on
    # 42 of the 46 bones - this is the dominant term, not a rounding detail.
    #
    # The raw matrix is stored rather than the derived correction
    # (rest^-1 @ matrix_local) on purpose: if the tail/roll heuristic above is
    # ever retuned, the correction follows automatically instead of going
    # stale against a value baked here. Flat row-major 16 floats - ID
    # properties take plain sequences, not mathutils types.
    for i, bone in enumerate(skeleton):
        arm_data.bones[bone["name"]]["pz_rest_source"] = [
            v for row in rest_mats[i] for v in row
        ]

    # A fixed per-bone offset that WOULD correctly re-anchor a rigid prop's
    # animated placement onto its own bind-pose rest: plain_rest[b]^-1 @
    # bind_rest[b], meant to be composed on the RIGHT of a clip's animated
    # world transform (prop_world(t) = A(t)[b] @ pz_prop_base[b] - never on
    # the left, which looks equivalent at t=0 but drifts under rotation, since
    # it applies the correction in world space rather than the bone's own
    # local frame).
    #
    # Currently UNUSED - animation_build.build_prop_action() does not read
    # this. It was used, and measured internally consistent (0.000 rotational
    # drift against a bone's own motion), but the SKIN bake
    # (animation_build.build_action(), what the head mesh itself deforms
    # through) never reconciles this same bind-vs-plain-rest gap for ANY bone,
    # so a prop using this correction and the head mesh it sits on ended up
    # each self-consistent but referenced to DIFFERENT poses - measured as a
    # ~7cm / ~9deg divergence the instant any clip played, reproduced
    # identically across two unrelated clips (Bob_Idle, Bob_EmoteSneeze2H),
    # which is what proved it was structural rather than clip content.
    # build_prop_action() now drives straight off the same A(t) the skin
    # bake's own matrix_basis solve uses instead, so a prop tracks the mesh
    # it's actually resting on.
    #
    # Left computed and stored rather than deleted: the real fix is teaching
    # build_action() this SAME bind-pose reconciliation (so the skin bake
    # matches its own build-time rest the way the static prop placement
    # already does), at which point this stops being dead weight and both
    # corrections apply together correctly. Removing this now would only mean
    # re-deriving it later.
    plain_world = _plain_rest_world(skeleton)
    for i, bone in enumerate(skeleton):
        plain_blender = _to_blender_matrix(plain_world[i])
        prop_base = plain_blender.inverted() @ rest_mats[i]
        arm_data.bones[bone["name"]]["pz_prop_base"] = [
            v for row in prop_base for v in row
        ]

    return arm_obj, bone_rest_by_name


def _build_skinned_mesh(context, name, mesh_data, arm_obj, parent, material):
    # Mesh vertices are already in the same absolute space the composed
    # skeleton lives in (Godot-space, right-handed Y-up), so they need only
    # the axis remap - no parent-chain composition, that's a bones-only
    # concept.
    verts = [tuple(_to_blender_vec(p)) for p in mesh_data["positions"]]
    tris = mesh_data["indices"]
    faces = [tuple(tris[i : i + 3]) for i in range(0, len(tris), 3)]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    if mesh_data["uvs"] is not None:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                u, v = mesh_data["uvs"][vi]
                uv_layer.data[li].uv = (u, 1.0 - v)

    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)
    obj.parent = parent

    if material is not None:
        mesh.materials.append(material)

    bones = mesh_data["bones"]
    bone_ids = mesh_data["bone_ids"]
    weights = mesh_data["weights"]
    if bones and bone_ids is not None:
        groups = [obj.vertex_groups.new(name=n) for n in bones]
        for vi, (ids, wts) in enumerate(zip(bone_ids, weights)):
            for slot, w in zip(ids, wts):
                if w > 0.0 and 0 <= slot < len(groups):
                    groups[slot].add([vi], w, "REPLACE")
        if arm_obj is not None:
            mod = obj.modifiers.new("Armature", "ARMATURE")
            mod.object = arm_obj
            obj.parent = arm_obj  # deform relative to the shared skeleton, not the empty root

    return obj


def _build_rigid_prop(context, name, mesh_data, arm_obj, bone_rest_by_name, bone_name, material,
                       attach_offset=None, attach_rotate=None):
    """A hat/glasses/etc: no skin data (mesh_data["bones"] is empty), so it
    can't deform through the Armature modifier like a garment. PZ places
    these with a single attach bone instead - CharacterAssetRegistry.gd's
    doc comment on get_mesh() and CharacterTestScene.gd's _add_skinned()
    (BoneAttachment3D at the bone's own rest transform, origin at the head -
    Godot bones have no tail). Blender's native "Bone" parent type is NOT the
    same thing - it anchors to the bone's *tail* - so this sets matrix_world
    directly from the same rest matrix _build_armature() already computed,
    rather than fighting that convention.

    `attach_offset`/`attach_rotate` (Godot-space position + quaternion xyzw)
    are the per-item local pose a weapon pool entry carries - see
    src/extractor/pz_characters.py's `_weapon_attach_transform`. None for
    anything else (a hat/glasses entry has no such keys), which leaves this
    identical to the plain bone-rest placement it always did."""
    verts = [tuple(_to_blender_vec(p)) for p in mesh_data["positions"]]
    tris = mesh_data["indices"]
    faces = [tuple(tris[i : i + 3]) for i in range(0, len(tris), 3)]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    if mesh_data["uvs"] is not None:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                u, v = mesh_data["uvs"][vi]
                uv_layer.data[li].uv = (u, 1.0 - v)

    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)
    if material is not None:
        mesh.materials.append(material)

    resolved_bone = bone_name if bone_name in bone_rest_by_name else "Bip01_Head"
    bone_rest = bone_rest_by_name.get(resolved_bone)
    # RIGID_PROP_FORWARD_NUDGE, composed on the RIGHT - i.e. in the bone's own
    # local frame, not a fixed world direction. This is NOT the same as
    # BEARD_FORWARD_NUDGE's `obj.location.y -= nudge`: the beard sits on a
    # SKINNED mesh with an identity rest transform, where "world -Y" and
    # "the head's own local -Y" happen to be the same direction, so that
    # shortcut is invisible there. A rigid prop's rest transform is NOT
    # identity (bone_rest carries the head's own rotation), and composing the
    # nudge on the right is what makes it travel WITH the bone under
    # animation instead of pointing at a fixed world direction regardless of
    # how the head has turned - confirmed by measurement: a world-space
    # version of this nudge passed at rest and then drifted the prop 2.8cm
    # off the head bone across a clip with real head rotation; this form
    # measured exactly 0.000.
    nudge = RIGID_PROP_FORWARD_NUDGE.get(name, 0.0)
    # Converted once here rather than passed in pre-converted: attach_offset/
    # attach_rotate are still in Godot space (see _weapon_attach_transform),
    # and _to_blender_matrix() is a similarity transform - converting the
    # piece and composing equals composing and converting the whole, so this
    # can compose directly against bone_rest (already Blender-space) below.
    attach_local = None
    if attach_offset is not None and attach_rotate is not None:
        attach_local = _to_blender_matrix(_godot_transform_matrix(attach_offset, attach_rotate))
    obj.parent = arm_obj
    if bone_rest is not None:
        placement = bone_rest
        if nudge:
            placement = placement @ mathutils.Matrix.Translation((0.0, -nudge, 0.0))
        if attach_local is not None:
            placement = placement @ attach_local
        obj.matrix_world = arm_obj.matrix_world @ placement
    # Which bone this prop rides, and in QUATERNION mode so animation_build's
    # baked rotation_quaternion keyframes (see build_prop_action) actually
    # drive the object - Blender's default for a plain Object is Euler XYZ,
    # which would silently ignore that channel. Stored as the *resolved* name
    # (post Bip01_Head fallback), not the one requested, or a later look-up
    # for this bone would land on a rest that was never the one placed.
    obj["pz_attach_bone"] = resolved_bone
    obj.rotation_mode = "QUATERNION"
    # Stashed so build_prop_action() can fold the identical right-composed
    # nudge into its own per-bone correction - see it applied there.
    obj["pz_forward_nudge"] = nudge
    # Stashed the same way, for the same reason: build_prop_action() (the
    # per-frame bake) and reset_to_rest_pose() (the stale-pose clear) both
    # need this exact local pose again and neither has attach_offset/
    # attach_rotate to hand at that point - only the built object does.
    if attach_local is not None:
        obj["pz_attach_local"] = _flatten_matrix(attach_local)

    # Diagnostic, kept until the placement is confirmed in Blender itself.
    # The reason a prop used to come out floating is now known and fixed: the
    # attach bone was resting in skel:Human's pose rather than the pose the
    # meshes were skinned in (see bind_pose_from()). Against the bind pose,
    # scripts/test_character_armature.py puts M_PoliceHat at z
    # 1.647..1.810 on a 1.764-tall body and the aviators at 1.579..1.647, both
    # centred in x - i.e. brim, crown and eye line. Those are the numbers to
    # compare this print against.
    print(
        f"[PZ Character] {name} attach_bone={bone_name!r} "
        f"bone_rest.translation={tuple(bone_rest.translation) if bone_rest is not None else None} "
        f"obj.matrix_world.translation={tuple(obj.matrix_world.translation)} "
        f"arm_obj.matrix_world.translation={tuple(arm_obj.matrix_world.translation)} "
        f"mesh_local_vert0_raw={mesh_data['positions'][0] if mesh_data['positions'] else None} "
        f"mesh_local_vert0_converted={verts[0] if verts else None} "
        f"mesh_bbox_local=({min((v[0] for v in verts), default=0):.4f},{min((v[1] for v in verts), default=0):.4f},{min((v[2] for v in verts), default=0):.4f})"
        f"-({max((v[0] for v in verts), default=0):.4f},{max((v[1] for v in verts), default=0):.4f},{max((v[2] for v in verts), default=0):.4f})"
    )
    return obj


#: Name given to the Vector Math node that carries a garment's tint, so
#: apply_tint() can find it again without walking the graph by type. A
#: ShaderNodeVectorMath in MULTIPLY mode rather than the more obvious
#: ShaderNodeMixRGB: Mix RGB is the legacy node on modern Blender (4.x
#: replaced it with ShaderNodeMix, whose sockets are named differently
#: again), while Vector Math's name, operation enum and socket order have
#: been stable across the whole 2.8+ line. A colour is a vec3 as far as a
#: per-channel multiply is concerned, so nothing is lost by treating it as
#: one.
TINT_NODE_NAME = "PZ Tint"

#: Custom object property naming the clothing slot an object was built for.
SLOT_PROP = "pz_slot"


def _material_for(name, image, tint=None):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    if image is None:
        return mat
    nodes = mat.node_tree.nodes
    tex_node = nodes.new("ShaderNodeTexImage")
    tex_node.image = image
    bsdf = nodes.get("Principled BSDF")
    if bsdf is not None:
        # PZ's tint is a per-channel multiply on the base texture
        # (media/shaders/hueChange.frag), so it sits between the image and
        # Base Color. Built even when the tint is white, so set_material_tint()
        # has something to write into if the colour is changed later - a
        # multiply by white is the identity and costs nothing.
        mul = nodes.new("ShaderNodeVectorMath")
        mul.name = TINT_NODE_NAME
        mul.label = TINT_NODE_NAME
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = tuple(tint or (1.0, 1.0, 1.0))
        mat.node_tree.links.new(tex_node.outputs["Color"], mul.inputs[0])
        mat.node_tree.links.new(mul.outputs["Vector"], bsdf.inputs["Base Color"])
        if "Alpha" in tex_node.outputs and "Alpha" in bsdf.inputs:
            mat.node_tree.links.new(tex_node.outputs["Alpha"], bsdf.inputs["Alpha"])

    # Without this, the body composite's skirt/dress cutout (and any garment
    # texture with real alpha holes) renders fully opaque regardless of what
    # the pixels say - Blender materials default to an opaque blend mode and
    # ignore an unconnected Alpha entirely. HASHED rather than CLIP/BLEND:
    # the cutout is a hard-edged stencil, not a soft gradient, and hashed
    # avoids the depth-sort artifacts BLEND gets from the body mesh and a
    # garment mesh overlapping at the same surface.
    #
    # The property name/enum here is EEVEE's; Blender 4.2's EEVEE Next
    # reorganized transparency settings and the exact attribute on whatever
    # Blender version is running this has not been verified (no headless
    # Blender in this project) - guarded with hasattr so a renamed/missing
    # attribute is a no-op, not a crash.
    for attr, value in (("blend_method", "HASHED"), ("shadow_method", "HASHED")):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except TypeError:
                pass  # enum value moved/renamed on this Blender version
    if hasattr(mat, "show_transparent_back"):
        mat.show_transparent_back = False
    return mat


def apply_bone_visibility(context, show):
    """Show/hide the preview character's armature (and thus its bones) in the
    viewport. Hides the whole armature object via hide_viewport rather than
    per-bone hide flags or a display-type change - the meshes deform through
    an Armature modifier that references the object, not its bones' own
    visibility, so this hides the rig's octahedrons without touching a single
    deformed mesh.

    Returns True if there was a live armature to toggle.
    """
    arm_obj = live_armature(context)
    if arm_obj is None:
        return False
    arm_obj.hide_viewport = not show
    return True


def live_armature(context=None):
    """The armature of the character in the live "PZ Character" empty, or None.

    **Never `bpy.data.objects.get(ARMATURE_NAME)`.** Blender uniquifies a
    colliding object name, and release_character() moves the armature out
    under its own empty *keeping its name* - so the first release leaves the
    released character holding "PZ Character Armature" and every armature
    built afterwards is ".001", ".002", ... A name lookup therefore returns a
    RELEASED character's armature from the first release onward, and the
    Animation Library's Play / Stop / Layer Animations were all driving that
    one while the panel said otherwise. Measured, not theorised: with one
    release made, `get(ARMATURE_NAME)` returned the released rig and the live
    one was ".001".

    ARMATURE_NAME is still what the object is *named* - it is just not a key.
    """
    import bpy as _bpy

    scene = getattr(context, "scene", None) if context is not None else None
    root = (scene.objects.get(ROOT_NAME) if scene is not None
            else _bpy.data.objects.get(ROOT_NAME))
    if root is None:
        return None
    for child in root.children:
        if child.type == "ARMATURE":
            return child
    return None


def _is_pz_root(obj):
    """Whether `obj` is a "PZ Character" family empty - the live root or a
    release_character() empty (tagged RELEASE_INDEX_PROP)."""
    return obj is not None and obj.type == "EMPTY" and (
        obj.name == ROOT_NAME or RELEASE_INDEX_PROP in obj
    )


def target_root(context=None):
    """The "PZ Character" family empty the Animation Library should act on.

    Walks up from the active object's parent chain looking for a live-or-
    released root, so selecting a released empty (or anything under it - its
    armature, a skinned garment parented to the armature, a rigid prop) points
    Play/Stop/Layer Animations at *that* rig instead of the live one. This is
    what lets a released character be posed back to rest later - Stop would
    otherwise always reset the live character regardless of what's selected.
    Falls back to the live root when nothing applicable is selected, which was
    this module's only behaviour before released rigs became editable too.
    """
    obj = getattr(context, "active_object", None) if context is not None else None
    seen = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        if _is_pz_root(obj):
            return obj
        obj = obj.parent

    import bpy as _bpy

    scene = getattr(context, "scene", None) if context is not None else None
    return (scene.objects.get(ROOT_NAME) if scene is not None
            else _bpy.data.objects.get(ROOT_NAME))


def target_armature(context=None):
    """The armature under target_root(context), or None."""
    root = target_root(context)
    if root is None:
        return None
    for child in root.children:
        if child.type == "ARMATURE":
            return child
    return None


def slot_objects(context, slot):
    """Every object under the preview character that was built for `slot`."""
    root = context.scene.objects.get(ROOT_NAME)
    if root is None:
        return []
    return [obj for obj in _descendants(root) if obj.get(SLOT_PROP) == slot]


def set_slot_tint(context, slot, tint):
    """Repaint one garment in place. Returns the number of materials written,
    so a caller can tell "nothing to do" from "this slot needs a rebuild"."""
    written = 0
    for obj in slot_objects(context, slot):
        for mat in getattr(obj.data, "materials", []) or []:
            if set_material_tint(mat, tint):
                written += 1
    return written


def set_material_tint(mat, tint):
    """Write a new colour into an existing material's tint node.

    Returns False if the material has no such node - a material built for a
    garment with no texture at all, say - so the caller can fall back to a
    full rebuild rather than silently doing nothing.
    """
    if mat is None or not mat.use_nodes:
        return False
    node = mat.node_tree.nodes.get(TINT_NODE_NAME)
    if node is None:
        return False
    node.inputs[1].default_value = tuple(tint)
    return True


def _next_release_index():
    highest = 0
    for obj in bpy.data.objects:
        value = obj.get(RELEASE_INDEX_PROP)
        if isinstance(value, int) and value > highest:
            highest = value
    return highest + 1


def _copy_image(source, name):
    """A new image datablock with `source`'s pixels.

    **Not `Image.copy()`.** Measured on Blender 4.5.5: copying a packed
    generated image of the body composite's real size (256x256) yields an
    image whose `pixels` reads back as an EMPTY sequence - not zeroed, empty -
    so a released character ends up with no body texture at all. Blender 5.2
    copies it correctly, and a 4x4 probe copies correctly on both, which is
    exactly how this would slip through a smaller test. images.new() plus an
    explicit pixel copy behaves identically on both versions at both sizes.
    """
    duplicate = bpy.data.images.new(name, source.size[0], source.size[1], alpha=True)
    duplicate.pixels[:] = source.pixels[:]
    # Packed, or it is an unsaved generated image that does not survive a
    # save/reload of the .blend.
    duplicate.pack()
    return duplicate


def _detach_body_composite(objects):
    """Give these objects their own copy of the shared body texture.

    composite_body_texture() writes into ONE image datablock, looked up by a
    fixed name and reused - so a released character whose outfit includes any
    texture-overlay garment would have its skin repainted by the next
    character built, silently and only for overlay garments. The fix has to
    happen at release time, because up to that moment sharing the image is
    exactly what makes the live tint edits cheap.
    """
    shared = bpy.data.images.get(BODY_COMPOSITE_NAME)
    if shared is None:
        return 0
    copied = {}
    swapped = 0
    for obj in objects:
        for mat in getattr(obj.data, "materials", []) or []:
            if mat is None or not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname != "ShaderNodeTexImage" or node.image is not shared:
                    continue
                if mat.name not in copied:
                    copied[mat.name] = _copy_image(
                        shared, BODY_COMPOSITE_NAME + "_released"
                    )
                node.image = copied[mat.name]
                swapped += 1
    return swapped


def _keep_baked_actions(objects):
    """Untag and rename the baked Actions on these objects so the released
    character keeps whatever pose or animation it was released in.

    purge_baked_actions() deletes every Action carrying BAKED_ACTION_TAG, and
    it runs on the very next build - so without this, releasing an animated
    character and then generating the next one snaps the released one back to
    rest with nothing to say why. Renaming as well as untagging matters
    separately: build_action() short-circuits on bpy.data.actions.get(clip_id),
    so an action left under its clip id would be handed back to the *next*
    character, whose armature it was not solved against.
    """
    kept = 0
    seen = set()
    for obj in objects:
        anim = obj.animation_data
        if anim is None:
            continue
        actions = [anim.action] if anim.action else []
        for track in anim.nla_tracks:
            actions.extend(strip.action for strip in track.strips if strip.action)
        for action in actions:
            if action is None or action.name in seen:
                continue
            seen.add(action.name)
            if action.get(BAKED_ACTION_TAG) is None:
                continue
            del action[BAKED_ACTION_TAG]
            action.name = "released_" + action.name
            # use_fake_user, or an Action whose only user is an object in a
            # collection can still be dropped on the next file load.
            action.use_fake_user = True
            kept += 1
    return kept


def release_character(context):
    """Move the built character out of the live "PZ Character" empty and into
    an empty of its own, so the next build cannot delete it.

    The live empty itself is NOT renamed or reused - it is left in place and
    emptied, and build_character() recreates it. Renaming it instead would let
    Blender hand the *next* empty a ".001" name, at which point every lookup by
    ROOT_NAME finds the released character rather than the live one.

    Returns (released_empty, moved_object_count), or (None, 0) if there was
    nothing built to release.
    """
    root = bpy.data.objects.get(ROOT_NAME)
    if root is None:
        return None, 0
    children = list(root.children)
    if not children:
        return None, 0

    moved = [obj for child in children for obj in [child] + list(_descendants(child))]

    released = bpy.data.objects.new(RELEASED_NAME, None)
    for collection in root.users_collection:
        collection.objects.link(released)
    if not released.users_collection:
        context.collection.objects.link(released)
    released.matrix_world = root.matrix_world.copy()
    released.empty_display_size = getattr(root, "empty_display_size", 1.0)

    index = _next_release_index()
    released[RELEASE_INDEX_PROP] = index

    # Reparent keeping each child exactly where it is on screen, THEN move the
    # new empty - so the whole character travels as one and the live empty is
    # free for the next one to be built at the origin.
    for child in children:
        world = child.matrix_world.copy()
        child.parent = released
        child.matrix_parent_inverse = released.matrix_world.inverted()
        child.matrix_world = world

    _keep_baked_actions(moved)
    _detach_body_composite(moved)

    released.location.x += index * RELEASE_SPACING
    return released, len(moved)


def build_character(context, characters_root, reader, pools_data, appearance, gender,
                     tints=None, damage=None):
    """Removes any existing 'PZ Character' and builds a fresh one for
    `appearance` (a slot -> item/"" dict from pools.default_appearance /
    pools.randomize_appearance / the panel's current layer selections).

    `appearance["weapon_right"]`/`["weapon_left"]` are expected to already be
    collapsed through PZ's both-hands rule by the time this is called (see
    ops._rebuild_scene(), which runs them through equipment.resolve_hands()
    before building) - this function does not re-check that rule, only
    _build_held_item()'s "at most one visible model" choice below.

    `tints` is an optional {slot: linear rgb} map from props.tint_dict(); a
    slot missing from it is untinted. Only slots whose current item actually
    carries PZ's m_AllowRandomTint are honoured, so a colour left over on a
    slot that has since been changed to an untintable garment cannot leak
    into the render - the panel hides the swatch in that case, and this makes
    the same thing true of the geometry.

    `damage` is the {"holes": [...], "blood": [...], "dirt": [...]} shape
    from props.damage_dict() - passed straight to composite_body_texture()
    for the skin/overlay layer, and to composite_garment_texture() (see
    below) for every mesh-family garment. It is one GLOBAL region set shared
    by both, not tracked per garment slot - a v1 simplification, see
    pools.roll_random_damage()'s docstring.
    """
    from . import pools as pools_mod

    tints = tints or {}
    damage = damage or {}
    damage_active = bool(damage.get("holes") or damage.get("blood") or damage.get("dirt"))

    def tint_for(slot, item):
        if not pools_mod.item_allows_tint(pools_data, item):
            return pools_mod.DEFAULT_TINT
        return tints.get(slot, pools_mod.DEFAULT_TINT)

    remove_existing(context)

    root = bpy.data.objects.new(ROOT_NAME, None)
    context.collection.objects.link(root)

    skeleton = reader.skeleton()

    # The body is read before the armature is built, not after: its bind list
    # is what tells the armature which pose to rest in, and every garment,
    # hat and hair mesh in the pack is authored against that same pose.
    body_mesh_id = pools_data.get("body", {}).get(gender, {}).get("mesh", "")
    body_data = reader.get_mesh(body_mesh_id)
    arm_obj, bone_rest_by_name = _build_armature(
        context, skeleton, root, bind_pose_from(body_data)
    )

    def _add_mesh_or_prop(mesh_id, mesh_data, material, bone_name, attach_offset=None, attach_rotate=None):
        if mesh_data["bones"]:
            return _build_skinned_mesh(context, mesh_id, mesh_data, arm_obj, root, material)
        return _build_rigid_prop(context, mesh_id, mesh_data, arm_obj, bone_rest_by_name, bone_name, material,
                                  attach_offset=attach_offset, attach_rotate=attach_rotate)

    skin_tone = appearance.get("skin_tone", "")
    body_texture = composite_body_texture(
        characters_root, pools_data, skin_tone, appearance, gender, tints, damage
    )
    if body_data is not None:
        mat = _material_for(f"{body_mesh_id}_mat", body_texture)
        _build_skinned_mesh(context, body_mesh_id, body_data, arm_obj, root, mat)

    for slot, item in appearance.items():
        # "weapon_right"/"weapon_left" go through _build_held_item() below
        # instead of this generic loop - both name the same pool entry's own
        # "bone" (Bip01_Prop2), so building each independently here would
        # place two meshes on top of each other whenever both hands hold the
        # same both-hands weapon, and would show two different models at
        # once for two different items - which this project deliberately
        # never does (see _build_held_item()'s doc).
        if slot in ("skin_tone", "hair", "beard", "weapon_right", "weapon_left") or not item:
            continue
        resolved = pools_mod.resolve_item(pools_data, item, gender)
        if resolved is None or resolved[0] != "mesh":
            continue
        mesh_id = resolved[1]
        mesh_data = reader.get_mesh(mesh_id)
        if mesh_data is None:
            continue
        entry = resolved[2]
        textures = entry.get("textures", [])
        image = None
        if textures and damage_active:
            # See composite_garment_texture()'s doc: this is a fresh Image,
            # never the one _load_image() would hand back and cache/share.
            image = composite_garment_texture(characters_root, pools_data, textures[0], damage)
        if image is None and textures:
            image = _load_image(characters_root, textures[0])
        mat = _material_for(f"{mesh_id}_mat", image, tint_for(slot, item))
        obj = _add_mesh_or_prop(mesh_id, mesh_data, mat, entry.get("bone") or "Bip01_Head",
                                 entry.get("attach_offset"), entry.get("attach_rotate"))
        # Which slot built this object, recorded on the object rather than
        # inferred from its name later. Blender uniquifies a name that
        # collides (mesh_id -> mesh_id.001), and two garments can legitimately
        # share a mesh, so a name is not a key - this is what lets
        # set_slot_tint() find the right material without a full rebuild.
        if obj is not None:
            obj[SLOT_PROP] = slot

    # Hair/beard ids are mesh ids directly (unlike clothing, which stores an
    # "item" name resolved through pools.resolve_item()) - both are already
    # skinned to Bip01_Head in the pack, so they go through the same skinned
    # path as a garment, not the rigid-prop one.
    for slot in ("hair", "beard"):
        item = appearance.get(slot, "")
        if not item:
            continue
        pool = pools_data.get("beard", []) if slot == "beard" else pools_data.get("hair", {}).get(gender, [])
        entry = next((e for e in pool if e.get("id") == item), None)
        if entry is None:
            continue
        mesh_data = reader.get_mesh(item)
        if mesh_data is None:
            continue
        image = _load_image(characters_root, entry.get("texture", ""))
        # Hair/beard art is a greyscale mask (see composite_body_texture's
        # neighbour, CharacterAssetRegistry.gd's HAIR_COLORS comment) - unlike
        # a garment, there is no m_AllowRandomTint to gate this on, PZ tints
        # every style unconditionally. So this always reads `tints`, never
        # `tint_for()`'s item_allows_tint() check.
        mat = _material_for(f"{item}_mat", image, tints.get(slot, pools_mod.DEFAULT_TINT))
        obj = _add_mesh_or_prop(item, mesh_data, mat, "Bip01_Head")
        # Tagged the same way a garment is, so set_slot_tint() can repaint a
        # live drag without a full rebuild - see ops.apply_tint().
        if obj is not None:
            obj[SLOT_PROP] = slot
        if slot == "beard" and obj is not None:
            # Rigid translation only - moves the object, not its mesh data,
            # so the beard's own shape is exactly what shipped in the pack.
            obj.location.y -= BEARD_FORWARD_NUDGE

    _build_held_item(context, characters_root, pools_data, reader, appearance, gender,
                      tint_for, _add_mesh_or_prop)

    # Last, so it catches every mesh above regardless of which path built it.
    # This is what makes the toggle survive a randomize/gender switch, which
    # rebuilds the whole character from scratch (see this module's docstring).
    apply_shading(context.scene.pz_character.shade_smooth)
    apply_bone_visibility(context, context.scene.pz_character.show_bones)

    return root


def _build_held_item(context, characters_root, pools_data, reader, appearance, gender,
                      tint_for, add_mesh_or_prop):
    """Builds at most ONE held-item mesh, from `appearance["weapon_right"]`
    or, failing that, `appearance["weapon_left"]` - never both.

    This is a deliberate simplification, not a shortcut: vanilla PZ itself
    only ever renders one weapon model at a time. Building a second mesh for
    a genuinely different off-hand item would need real per-item off-hand
    pose data this project mostly doesn't have (see the "identity" case
    below) - so the right/left hand *slots* still track two independent
    picks (and still enforce PZ's real both-hands rule, in
    equipment.resolve_hands()), but only whichever one is actually the
    held/visible item gets a mesh. Right wins when both are set to different
    items - matching PZ's own `getWeaponType()`, which "reads the primary
    hand and nothing else" (see ItemRegistry.gd's doc comment on the Godot
    side).

    The bind bone is chosen by *hand*, not by anything on the item -
    `HELD_ITEM_HAND_BONES`: `Bip01_Prop1` for the right/primary hand,
    `Bip01_Prop2` for the left/secondary hand, PZ's own rule
    (`ModelManager.java`, decompiled - see
    src/docs/weapon_attachment.md).

    A grip pose is only ever composed when the item's own mined data
    declares a block for THAT bone specifically - `entry["attachments"]` is
    keyed by socket id (`_mine_weapon_attachments()`), and PZ's own
    `ModelScript.getAttachmentById()` is an exact-match hash lookup with no
    fallback. For a right-hand item this is almost always a miss (284 of
    294 weapon models only declare a `Bip01_Prop2` block, not
    `Bip01_Prop1`), and a miss means identity - no grip rotation composed at
    all, just the bone's own rest/animated pose. That is not a gap to patch
    with a guessed correction: it is what PZ itself actually does.
    """
    from . import pools as pools_mod

    slot = "weapon_right" if appearance.get("weapon_right") else "weapon_left"
    item = appearance.get(slot)
    if not item:
        return
    resolved = pools_mod.resolve_item(pools_data, item, gender)
    if resolved is None or resolved[0] != "mesh":
        return
    mesh_id = resolved[1]
    mesh_data = reader.get_mesh(mesh_id)
    if mesh_data is None:
        return
    entry = resolved[2]
    textures = entry.get("textures", [])
    image = _load_image(characters_root, textures[0]) if textures else None
    mat = _material_for(f"{mesh_id}_mat", image, tint_for(slot, item))
    bind_bone = HELD_ITEM_HAND_BONES[slot]
    pose = entry.get("attachments", {}).get(bind_bone)
    offset, rotate = (pose["offset"], pose["rotate"]) if pose else (None, None)
    obj = add_mesh_or_prop(mesh_id, mesh_data, mat, bind_bone, offset, rotate)
    if obj is not None:
        obj[SLOT_PROP] = slot


def composite_body_texture(characters_root, pools_data, skin_tone, appearance, gender,
                            tints=None, damage=None):
    """Port of CharacterAssetRegistry.gd's composite_body_texture(): skin
    tone base, alpha-erase the skirt/dress stencil, blend garment texture
    overlays, erase worn-mesh-garment masks, then blood/dirt and holes last.
    Uses numpy (ships with Blender's Python) over bpy.data.images pixel
    buffers.

    A texture-overlay garment has no material of its own - it is painted into
    this one image - so its tint is multiplied into the layer here rather
    than by a shader node. That is also where PZ applies it: the composite is
    an ItemSmartTexture, and its addTexture() defers to addTint() for exactly
    the layers whose colour is not white.

    `damage` is the {"holes": [...], "blood": [...], "dirt": [...]} shape
    from props.damage_dict() - see _apply_damage_passes(). Applied AFTER the
    garment-mask erase above, on purpose: damage has to cut through
    everything already painted (skin + overlay clothing), the same way PZ's
    own SmartTexture.addHole() cuts through a fully-assembled texture stack,
    not just the base tone.

    Returns a bpy.types.Image, or None if the skin tone can't be loaded.
    """
    import numpy as np
    from . import pools as pools_mod

    tints = tints or {}

    base_img = _load_image(characters_root, skin_tone)
    if base_img is None:
        return None
    w, h = base_img.size
    base = np.array(base_img.pixels[:], dtype=np.float32).reshape(h, w, 4)

    stencil = _body_alpha_stencil(characters_root, pools_data, gender)
    if stencil is not None:
        base[stencil > 0.0] = 0.0

    # Bare skin, before any clothing is painted over it - what a hole in
    # THIS composite has to fall back to. Skin and overlay clothing are
    # baked into the same flat image here (unlike a mesh garment, which is
    # its own separate object/material over the body mesh in 3D), so there
    # is no "layer underneath" left to reveal once a pixel is erased to
    # alpha 0 - erasing would carve a hole straight through the body itself,
    # which is what punched the see-through gaps reported on bare arms/legs
    # in testing. See _apply_damage_passes()'s `restore` parameter.
    bare_skin = base.copy()

    masks = []
    for slot, item in appearance.items():
        if slot in ("skin_tone", "hair", "beard") or not item:
            continue
        resolved = pools_mod.resolve_item(pools_data, item, gender)
        if resolved is None:
            continue
        kind, _mesh_id, entry = resolved
        if kind == "overlay":
            textures = entry.get("textures", [])
            if textures:
                overlay_img = _load_image(characters_root, textures[0])
                if overlay_img is not None:
                    ow, oh = overlay_img.size
                    layer = np.array(overlay_img.pixels[:], dtype=np.float32).reshape(oh, ow, 4)
                    if (ow, oh) != (w, h):
                        continue  # every shipped overlay is 256x256 like the body; skip a mismatch rather than guess a resize
                    if pools_mod.item_allows_tint(pools_data, item):
                        # RGB only. hueChange.frag writes vec4(col, col4.a) -
                        # tinting alpha would eat the garment's own cutout.
                        layer[:, :, :3] *= np.asarray(
                            tints.get(slot, pools_mod.DEFAULT_TINT), dtype=np.float32
                        )
                    alpha = layer[:, :, 3:4]
                    base[:, :, :3] = layer[:, :, :3] * alpha + base[:, :, :3] * (1.0 - alpha)
                    base[:, :, 3:4] = np.maximum(base[:, :, 3:4], alpha)
        else:
            masks.extend(entry.get("masks", []))

    for rel in masks:
        mask_img = _load_image(characters_root, rel)
        if mask_img is None:
            continue
        mw, mh = mask_img.size
        if (mw, mh) != (w, h):
            continue
        mask = np.array(mask_img.pixels[:], dtype=np.float32).reshape(mh, mw, 4)
        base[mask[:, :, 3] > 0.0] = 0.0

    _apply_damage_passes(base, characters_root, pools_data, damage, restore=bare_skin)

    out_name = BODY_COMPOSITE_NAME
    out_img = bpy.data.images.get(out_name)
    if out_img is None or tuple(out_img.size) != (w, h):
        if out_img is not None:
            bpy.data.images.remove(out_img)
        out_img = bpy.data.images.new(out_name, w, h, alpha=True)
    out_img.pixels[:] = base.flatten().tolist()
    out_img.pack()
    return out_img


def _apply_damage_passes(base, characters_root, pools_data, damage, restore=None):
    """Blends blood/dirt then punches holes into `base` - an (h,w,4) float32
    numpy array, mutated in place - using pools_data["damage"] (the 18
    per-region hole masks and the two shared tinted overlays extracted from
    PZ's HoleTextures/BloodTextures). Shared by composite_body_texture() and
    composite_garment_texture(): both need the identical two passes, just
    starting from a different base image.

    Holes run after blood/dirt on purpose, mirroring composite_body_texture()'s
    own doc comment: a torn hole should win over a blood tint in the same
    spot, since a hole has nothing left to be bloody.

    `restore`, if given, is an (h,w,4) array a hole falls back to instead of
    alpha 0 - composite_body_texture() passes its bare-skin snapshot here,
    since skin and overlay clothing share one flat image with nothing left
    to reveal once erased (a real hole in a shirt should show the arm under
    it, not a see-through gap in the character). composite_garment_texture()
    passes nothing: a mesh garment is its own separate object/material over
    the body mesh in 3D, so alpha 0 there correctly reveals the body drawn
    underneath in the normal way, and there is no flat "skin" to fall back
    to on an isolated garment texture.

    A no-op when `damage` is falsy or the pools file predates the damage
    extractor step (no "damage" key) - old assets/characters exports still
    build a character, just without this layer.
    """
    import numpy as np

    if not damage:
        return
    damage_pool = pools_data.get("damage") or {}
    hole_masks = damage_pool.get("hole_masks", {})
    if not hole_masks:
        return
    h, w = base.shape[:2]

    def region_mask(region):
        rel = hole_masks.get(region)
        if not rel:
            return None
        mask_img = _load_image(characters_root, rel)
        if mask_img is None:
            return None
        mw, mh = mask_img.size
        if (mw, mh) != (w, h):
            return None
        return np.array(mask_img.pixels[:], dtype=np.float32).reshape(mh, mw, 4)

    for overlay_key, intensity, regions_key in (
        ("blood_overlay", DEFAULT_BLOOD_INTENSITY, "blood"),
        ("dirt_overlay", DEFAULT_DIRT_INTENSITY, "dirt"),
    ):
        regions = damage.get(regions_key) or []
        overlay_rel = damage_pool.get(overlay_key)
        if not regions or not overlay_rel:
            continue
        overlay_img = _load_image(characters_root, overlay_rel)
        if overlay_img is None:
            continue
        ow, oh = overlay_img.size
        if (ow, oh) != (w, h):
            continue
        overlay = np.array(overlay_img.pixels[:], dtype=np.float32).reshape(oh, ow, 4)
        alpha = overlay[:, :, 3:4] * intensity
        for region in regions:
            mask = region_mask(region)
            if mask is None:
                continue
            gate = mask[:, :, 3] > 0.0
            base[gate, :3] = overlay[gate, :3] * alpha[gate] + base[gate, :3] * (1.0 - alpha[gate])
            base[gate, 3:4] = np.maximum(base[gate, 3:4], alpha[gate])

    for region in damage.get("holes") or []:
        mask = region_mask(region)
        if mask is None:
            continue
        gate = mask[:, :, 3] > 0.0
        base[gate] = restore[gate] if restore is not None else 0.0


def composite_garment_texture(characters_root, pools_data, base_texture_rel, damage):
    """The mesh-garment analogue of composite_body_texture(): blood/dirt and
    holes on a single garment's OWN texture, for a garment slot that has
    damage assigned.

    **Must never mutate the Image _load_image() returns.** That call caches
    and shares one bpy.data.images datablock per source path across every
    build, so writing into it in place would corrupt every other character
    (and every other build of this one) using the same garment texture - the
    exact bug _detach_body_composite() exists to prevent for the body
    composite, one level up. This function always reads pixels into a numpy
    array and hands back a brand-new bpy.data.images.new() result instead.

    Returns a bpy.types.Image, or None if `base_texture_rel` can't be loaded.
    """
    import numpy as np

    base_img = _load_image(characters_root, base_texture_rel)
    if base_img is None:
        return None
    w, h = base_img.size
    base = np.array(base_img.pixels[:], dtype=np.float32).reshape(h, w, 4)

    _apply_damage_passes(base, characters_root, pools_data, damage)

    out_img = bpy.data.images.new(f"{base_img.name}_damage", w, h, alpha=True)
    out_img.pixels[:] = base.flatten().tolist()
    out_img.pack()
    return out_img


def _body_alpha_stencil(characters_root, pools_data, gender):
    """Where the canonical (index-0) skin tone is transparent - opaque there,
    everything else transparent - so it can zero out the same region on
    whichever tone is actually worn. Mirrors _body_alpha_stencil() in
    CharacterAssetRegistry.gd."""
    import numpy as np

    tones = pools_data.get("body", {}).get(gender, {}).get("skin_tones", [])
    if not tones:
        return None
    canonical = _load_image(characters_root, tones[0])
    if canonical is None:
        return None
    w, h = canonical.size
    arr = np.array(canonical.pixels[:], dtype=np.float32).reshape(h, w, 4)
    alpha = arr[:, :, 3]
    if not np.any(alpha <= 0.0):
        return None
    return (alpha <= 0.0).astype(np.float32)
