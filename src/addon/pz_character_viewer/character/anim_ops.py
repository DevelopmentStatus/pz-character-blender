"""Operators for the Animation Library panel: search/filter, pin, play/stop,
and NLA bone-masked layering. Parallels character/ops.py (appearance
customization), kept separate since it's a distinct concern."""

import bpy

from . import anim_pins, anim_props, animation_build, build, pools
from .ops import _characters_root, _ensure_reader
from .pzc_reader import PZCError, SKELETON_ID

MAX_RESULTS = 200

#: (overlay_clip_id, mask_side) -> bpy.types.Action. Masked-action names are
#: derived from the overlay clip id and can collide/get Blender's automatic
#: ".001" rename once truncated to fit the 63-char datablock limit, so the
#: cache key here - not the action's own name - is what layer_animations
#: relies on to avoid rebuilding one it already made this session.
_masked_action_cache = {}


class PZCHAR_OT_refresh_anim_results(bpy.types.Operator):
    bl_idname = "pz_character.refresh_anim_results"
    bl_label = "Filter PZ Animations"
    bl_description = "Filter animation clips by text/category"

    def execute(self, context):
        props = context.scene.pz_character_anim
        root = _characters_root(context)
        category = "" if props.category_filter == "ALL" else props.category_filter

        try:
            ids = pools.filter_clip_ids(root, props.filter_text, category)
            clips = pools.manifest(root).get("clips", {})
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not read character_manifest.json: {exc}")
            return {"CANCELLED"}

        pinned = anim_pins.load(root)
        ids.sort(key=lambda cid: (cid not in pinned, clips.get(cid, {}).get("name", cid).lower()))

        props.results.clear()
        for clip_id in ids[:MAX_RESULTS]:
            entry = clips.get(clip_id, {})
            item = props.results.add()
            item.name = clip_id
            item.display_name = entry.get("name", clip_id)
            item.duration = float(entry.get("seconds", 0.0))
            item.pinned = clip_id in pinned
        props.results_index = 0
        props.result_total = len(ids)
        return {"FINISHED"}


class PZCHAR_OT_toggle_pin_animation(bpy.types.Operator):
    bl_idname = "pz_character.toggle_pin_animation"
    bl_label = "Toggle Pin"
    bl_description = "Pin/unpin this animation to the top of the list"

    clip_id: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.pz_character_anim
        root = _characters_root(context)
        try:
            anim_pins.toggle(root, self.clip_id)
        except OSError as exc:
            self.report({"WARNING"}, f"Could not save pin: {exc}")
            return {"CANCELLED"}

        pinned = anim_pins.load(root)
        for item in props.results:
            item.pinned = item.name in pinned

        # CollectionProperty has no in-place sort, only move() - pull the
        # scalar fields out, clear, and re-add in the new pinned-first order
        # so the change is visible immediately without a full re-search.
        snapshot = sorted(
            ((it.name, it.display_name, it.duration, it.pinned) for it in props.results),
            key=lambda row: (not row[3], (row[1] or row[0]).lower()),
        )
        props.results.clear()
        for name, display_name, duration, pinned_flag in snapshot:
            item = props.results.add()
            item.name = name
            item.display_name = display_name
            item.duration = duration
            item.pinned = pinned_flag
        props.results_index = 0
        return {"FINISHED"}


class PZCHAR_OT_play_animation(bpy.types.Operator):
    bl_idname = "pz_character.play_animation"
    bl_label = "Play"
    bl_description = "Assign this clip to the target armature (the selected released rig, or else the live preview) - use Blender's own timeline/spacebar to play it"

    clip_id: bpy.props.StringProperty(default="")

    def execute(self, context):
        arm_obj = build.target_armature(context)
        if arm_obj is None:
            self.report({"ERROR"}, "No character - click Randomize to build one")
            return {"CANCELLED"}

        try:
            reader, _root = _ensure_reader(context)
            clip_data = reader.get_clip(self.clip_id)
        except (OSError, PZCError) as exc:
            self.report({"ERROR"}, f"Could not open characters.pzc: {exc}")
            return {"CANCELLED"}
        if clip_data is None:
            self.report({"ERROR"}, f"No clip '{self.clip_id}' in this pack")
            return {"CANCELLED"}

        try:
            action = bpy.data.actions.get(self.clip_id) or animation_build.build_action(
                self.clip_id, clip_data, reader.skeleton(), arm_obj
            )
        except animation_build.RestDataMissing:
            self.report(
                {"ERROR"},
                "This armature predates the animation fix - click Randomize "
                "to rebuild it, then Play again",
            )
            return {"CANCELLED"}

        arm_obj.animation_data_create()
        animation_build.assign_action(arm_obj.animation_data, action)

        # Rigid props (hats, glasses - build._build_rigid_prop) aren't skinned,
        # so they don't move when the armature poses; each needs its own baked
        # Action to follow its attach bone. arm_obj.children rather than
        # walking the whole "PZ Character" tree, since that's exactly what
        # build.reset_to_rest_pose() also walks to undo this on Stop - keeping
        # both loops looking at the same set is what keeps them in sync.
        skeleton = reader.skeleton()
        for prop_obj in arm_obj.children:
            if prop_obj.get("pz_attach_bone") is None:
                continue
            try:
                prop_action = animation_build.build_prop_action(
                    self.clip_id, prop_obj, clip_data, skeleton
                )
            except animation_build.RestDataMissing:
                self.report(
                    {"ERROR"},
                    "This armature predates the animation fix - click Preview "
                    "Character to rebuild it, then Play again",
                )
                return {"CANCELLED"}
            if prop_action is None:
                continue
            prop_obj.animation_data_create()
            animation_build.assign_action(prop_obj.animation_data, prop_action)

        # Match the scene to the rate PZ actually sampled at, so every baked key
        # lands on a whole frame instead of between two (Blender's 24 fps
        # default puts them at 0.0, 0.8, 1.6, ...) and the clip plays at real
        # speed rather than 25% slow. See animation_build.SOURCE_FPS.
        context.scene.render.fps = animation_build.SOURCE_FPS
        context.scene.render.fps_base = 1.0
        frame_start, frame_end = action.frame_range
        context.scene.frame_start = int(frame_start)
        context.scene.frame_end = int(frame_end)
        context.scene.frame_current = int(frame_start)

        anim_props.set_active_clip(arm_obj, self.clip_id)
        return {"FINISHED"}


class PZCHAR_OT_stop_animation(bpy.types.Operator):
    bl_idname = "pz_character.stop_animation"
    bl_label = "Stop"
    bl_description = "Clear the animation and return the target armature to its rest (T) pose"

    def execute(self, context):
        arm_obj = build.target_armature(context)
        if arm_obj is not None:
            build.reset_to_rest_pose(arm_obj)
            anim_props.set_active_clip(arm_obj, "")
        return {"FINISHED"}


class PZCHAR_OT_set_layer_clip(bpy.types.Operator):
    bl_idname = "pz_character.set_layer_clip"
    bl_label = "Set Layering Clip"
    bl_description = "Use the selected animation as the base or overlay clip for NLA layering"

    role: bpy.props.EnumProperty(items=[("base", "Base", ""), ("overlay", "Overlay", "")], default="base")
    clip_id: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.pz_character_anim
        if self.role == "base":
            props.layer_base_clip = self.clip_id
        else:
            props.layer_overlay_clip = self.clip_id
        return {"FINISHED"}


def _build_masked_action(overlay_action, mask_set, mask_side, armature_obj):
    """A copy of overlay_action containing only the fcurves whose bone is in
    mask_set. Stacked on its own NLA track above a base clip, this alone is
    what produces the layering - a bone outside the mask carries no fcurve
    at all here, so NLA evaluation falls through to the base track for it."""
    name = f"{overlay_action.name}__{mask_side}"[:63]
    masked = bpy.data.actions.new(name)
    # Same tag the baked clips carry, so a rebuild purges these too - a masked
    # copy inherits the rest frame of whatever armature its source was baked
    # against, so it goes stale for exactly the same reason.
    masked[build.BAKED_ACTION_TAG] = overlay_action.get(build.BAKED_ACTION_TAG, name)
    dst_fcurves, _slot = animation_build._channelbag_for(masked, armature_obj)

    for fc in animation_build.iter_fcurves(overlay_action):
        # data_path is pose.bones["<name>"].<channel> - PZ bone names are
        # plain ASCII identifiers, so splitting on the quote is safe.
        parts = fc.data_path.split('"')
        bone_name = parts[1] if len(parts) > 1 else ""
        if bone_name not in mask_set:
            continue
        new_fc = dst_fcurves.new(data_path=fc.data_path, index=fc.array_index)
        new_fc.keyframe_points.add(len(fc.keyframe_points))
        for i, kp in enumerate(fc.keyframe_points):
            new_fc.keyframe_points[i].co = kp.co
            new_fc.keyframe_points[i].interpolation = kp.interpolation
        new_fc.update()
    return masked


def _assign_strip_slot(strip, action):
    slot = animation_build.get_action_slot(action)
    if slot is not None and hasattr(strip, "action_slot"):
        strip.action_slot = slot


class PZCHAR_OT_layer_animations(bpy.types.Operator):
    bl_idname = "pz_character.layer_animations"
    bl_label = "Layer Animations"
    bl_description = "Layer the overlay clip's masked bones over the base clip on the NLA stack"

    def execute(self, context):
        props = context.scene.pz_character_anim
        base_clip_id = props.layer_base_clip
        overlay_clip_id = props.layer_overlay_clip
        mask_side = props.layer_mask_side

        if not base_clip_id or not overlay_clip_id:
            self.report({"ERROR"}, "Set both a base and an overlay clip first")
            return {"CANCELLED"}

        arm_obj = build.target_armature(context)
        if arm_obj is None:
            self.report({"ERROR"}, "No character - click Randomize to build one")
            return {"CANCELLED"}

        root = _characters_root(context)
        try:
            masks = pools.locomotion_masks(root).get(SKELETON_ID, {})
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not read locomotion_manifest.json: {exc}")
            return {"CANCELLED"}
        mask_set = set(masks.get(mask_side, []))
        if not mask_set:
            self.report({"ERROR"}, f"No '{mask_side}' bone mask in locomotion_manifest.json")
            return {"CANCELLED"}

        try:
            reader, _root = _ensure_reader(context)
            skeleton = reader.skeleton()
            base_clip = reader.get_clip(base_clip_id)
            overlay_clip = reader.get_clip(overlay_clip_id)
        except (OSError, PZCError) as exc:
            self.report({"ERROR"}, f"Could not open characters.pzc: {exc}")
            return {"CANCELLED"}
        if base_clip is None or overlay_clip is None:
            self.report({"ERROR"}, "Base or overlay clip not found in this pack")
            return {"CANCELLED"}

        try:
            base_action = bpy.data.actions.get(base_clip_id) or animation_build.build_action(
                base_clip_id, base_clip, skeleton, arm_obj
            )
            overlay_action = bpy.data.actions.get(overlay_clip_id) or animation_build.build_action(
                overlay_clip_id, overlay_clip, skeleton, arm_obj
            )
        except animation_build.RestDataMissing:
            self.report(
                {"ERROR"},
                "This armature predates the animation fix - click Randomize "
                "to rebuild it, then layer again",
            )
            return {"CANCELLED"}

        cache_key = (overlay_clip_id, mask_side)
        masked_action = _masked_action_cache.get(cache_key)
        # purge_baked_actions() frees these on a rebuild, and touching any
        # attribute of a freed datablock raises ReferenceError rather than
        # returning something falsy - so the staleness check has to be guarded,
        # not just a name lookup.
        try:
            stale = masked_action is None or masked_action.name not in bpy.data.actions
        except ReferenceError:
            stale = True
        if stale:
            masked_action = _build_masked_action(overlay_action, mask_set, mask_side, arm_obj)
            _masked_action_cache[cache_key] = masked_action

        arm_obj.animation_data_create()
        arm_obj.animation_data.action = None  # an active action overrides NLA evaluation

        base_track = arm_obj.animation_data.nla_tracks.new()
        base_track.name = f"Base: {base_clip_id}"[:63]
        base_strip = base_track.strips.new(base_clip_id[:63], 1, base_action)
        _assign_strip_slot(base_strip, base_action)

        overlay_track = arm_obj.animation_data.nla_tracks.new()
        overlay_track.name = f"Overlay ({mask_side}): {overlay_clip_id}"[:63]
        overlay_strip = overlay_track.strips.new(overlay_clip_id[:63], 1, masked_action)
        overlay_strip.blend_type = "REPLACE"
        _assign_strip_slot(overlay_strip, masked_action)

        self.report({"INFO"}, f"Layered '{overlay_clip_id}' ({mask_side}) over '{base_clip_id}'")
        return {"FINISHED"}


classes = (
    PZCHAR_OT_refresh_anim_results,
    PZCHAR_OT_toggle_pin_animation,
    PZCHAR_OT_play_animation,
    PZCHAR_OT_stop_animation,
    PZCHAR_OT_set_layer_clip,
    PZCHAR_OT_layer_animations,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    _masked_action_cache.clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
