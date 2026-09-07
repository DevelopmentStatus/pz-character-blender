"""Scene state for the Animation Library panel - search/filter/pin/play,
plus the two-clip picker for NLA masked layering. Kept separate from
props.py (appearance customization) since it's a distinct concern."""

import bpy

#: Custom property on an armature object naming the clip currently assigned
#: to it (play_animation) or "" (stop_animation / never played). Kept on the
#: armature itself, not the scene, so a released rig remembers what it was
#: playing after the live "PZ Character" moves on to a new one - selecting
#: the released empty later (see build.target_armature()) reads this back
#: instead of the scene's now-unrelated state.
ACTIVE_CLIP_PROP = "pz_active_clip"


class PZAnimResultItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(default="")  # doubles as clip_id (UIList convention)
    display_name: bpy.props.StringProperty(default="")  # manifest clip "name"
    duration: bpy.props.FloatProperty(default=0.0)
    pinned: bpy.props.BoolProperty(default=False)


class PZCharacterAnimState(bpy.types.PropertyGroup):
    filter_text: bpy.props.StringProperty(name="Filter", default="")
    category_filter: bpy.props.EnumProperty(
        name="Category",
        items=[
            ("ALL", "All", ""),
            ("Bob", "Bob (Male)", ""),
            ("Kate", "Kate (Female)", ""),
            ("Zombie", "Zombie", ""),
        ],
        default="ALL",
    )
    results: bpy.props.CollectionProperty(type=PZAnimResultItem)
    results_index: bpy.props.IntProperty(default=0)
    result_total: bpy.props.IntProperty(default=0)

    # NLA layering picker (character/anim_ops.py's layer_animations)
    layer_base_clip: bpy.props.StringProperty(default="")
    layer_overlay_clip: bpy.props.StringProperty(default="")
    layer_mask_side: bpy.props.EnumProperty(
        name="Mask",
        items=[("upper", "Upper Body", ""), ("lower", "Lower Body", "")],
        default="upper",
    )


def character_exists(context=None):
    """Whether there is an armature for the Animation Library to act on.

    Reads build.target_armature(), which is the live "PZ Character" unless
    the current selection resolves to a released rig instead (see its
    docstring) - so this doubles as the panel's "is anything selected /
    live to act on" check, not just "was one ever built".
    """
    from . import build

    return build.target_armature(context) is not None


def get_active_clip(arm_obj):
    """The clip id last assigned to `arm_obj` by Play, or "" for none/Stop.

    Stored on the armature itself (ACTIVE_CLIP_PROP), not the scene, so it
    stays correct when the target rig changes with the selection - see
    build.target_armature(). A freshly built armature is a brand-new object
    with no custom properties yet, so a rebuilt live character reads "" here
    without anything needing to clear it by hand.
    """
    if arm_obj is None:
        return ""
    return arm_obj.get(ACTIVE_CLIP_PROP, "")


def set_active_clip(arm_obj, clip_id):
    if arm_obj is None:
        return
    if clip_id:
        arm_obj[ACTIVE_CLIP_PROP] = clip_id
    elif ACTIVE_CLIP_PROP in arm_obj:
        del arm_obj[ACTIVE_CLIP_PROP]


classes = (
    PZAnimResultItem,
    PZCharacterAnimState,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pz_character_anim = bpy.props.PointerProperty(type=PZCharacterAnimState)


def unregister():
    del bpy.types.Scene.pz_character_anim
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
