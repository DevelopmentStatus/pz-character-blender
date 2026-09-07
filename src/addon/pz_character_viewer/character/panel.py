import os

import bpy

from . import anim_props, build, pools
from .ops import _characters_root


class PZ_PT_character_viewer(bpy.types.Panel):
    bl_idname = "PZ_PT_character_viewer"
    bl_label = "PZ Character"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PZ Character"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pz_character
        characters_root = _characters_root(context)

        box = layout.box()
        row = box.row(align=True)
        row.label(text=os.path.basename(characters_root.rstrip("\\/")) or "(not set)", icon="FILE_FOLDER")
        row.operator("pz_character.browse_characters_root", text="", icon="FILEBROWSER")
        row.operator("pz_character.reload_pools", text="", icon="FILE_REFRESH")

        # One row, and no separate "build a character" button. Randomize and
        # Default both build one when there is none, so a dedicated button for
        # it was a large control whose only job was the very first press.
        # (Every layer control below behaves the same way, so there is nothing
        # in this panel that changes state without showing you the result.)
        # The X is the only way to end up with an empty scene.
        row = layout.row(align=True)
        row.operator("pz_character.randomize", icon="FILE_REFRESH")
        row.operator("pz_character.set_default", icon="LOOP_BACK")
        # Directly in `row`, not a nested row.row() - a sub-layout always
        # draws a small gap before it even under align=True, which is what
        # put visible daylight between Default and X. clear_character's own
        # poll() already greys the button out with nothing built, so the
        # separate `.enabled` line this replaced was redundant as well as
        # the reason a sub-layout existed at all.
        row.operator("pz_character.clear_character", text="", icon="X")

        # Not row.prop(props, "gender", expand=True) - that writes the enum
        # directly and skips pz_character.set_gender's rebuild_layers() call,
        # leaving props.layers full of the old gender's item ids. The preview
        # then resolves those ids against the new gender's pool, finds no
        # match, and drops the layer silently (e.g. a male hair id looked up
        # in the female pool) rather than switching to that gender's options.
        row = layout.row(align=True)
        for value, label in (("male", "Male"), ("female", "Female")):
            op = row.operator(
                "pz_character.set_gender", text=label, depress=(props.gender == value)
            )
            op.gender = value

        if not props.layers:
            layout.label(text="Click Randomize or Default to build a character", icon="INFO")
            return

        layout.separator()
        row = layout.row(align=True)
        row.operator(
            "pz_character.toggle_shade_smooth",
            text="Shade Smooth" if props.shade_smooth else "Shade Flat",
            icon="SHADING_RENDERED" if props.shade_smooth else "SHADING_SOLID",
            depress=props.shade_smooth,
        )
        row.operator(
            "pz_character.toggle_show_bones",
            text="Hide Bones" if props.show_bones else "Show Bones",
            icon="HIDE_OFF" if props.show_bones else "HIDE_ON",
            depress=not props.show_bones,
        )

        # Bottom of the panel, and only once there is something built to
        # release - poll() already greys it out, but a button that is never
        # usable before the first character is built is just noise there.
        root = bpy.data.objects.get(build.ROOT_NAME)
        if props.preview_enabled and root is not None and root.children:
            layout.separator()
            layout.operator(
                "pz_character.release_character", icon="UNLINKED",
            )

        if props.status:
            layout.label(text=props.status, icon="CHECKMARK")


def tintable_slots(context):
    """Slots whose currently-chosen item can take PZ's colour multiply.

    Two different reasons a slot ends up here: a garment carries PZ's own
    m_AllowRandomTint (checked via item_allows_tint(), against whatever is
    actually selected right now - tintability is a property of the item, not
    the slot), or the slot is hair/beard, whose greyscale art PZ tints
    unconditionally - there is no per-item flag to check, only "is anything
    worn there" (see build.build_character()'s hair/beard loop). skin_tone is
    never included: its colour comes from swapping the skin image itself, not
    a multiply.

    Empty on any failure, which is also what an appearance_pools.json written
    before the exporter mined the flag produces - in both cases the colour
    controls simply do not appear, rather than appearing and doing nothing.
    """
    try:
        pools_data = pools.load(_characters_root(context))
    except (OSError, ValueError):
        return set()
    props = context.scene.pz_character
    out = set()
    for layer in props.layers:
        if layer.slot == "skin_tone":
            continue
        item = _current_item(layer)
        if not item:
            continue
        if layer.slot in ("hair", "beard"):
            out.add(layer.slot)
        elif pools.item_allows_tint(pools_data, item):
            out.add(layer.slot)
    return out


def _draw_layer_row(layout, layer, tintable=False):
    # Label first - a Label in a row expands to fill available space by
    # default, which is what pushes the prev/reset/next cluster that follows
    # it to the right edge as one tight group, matching the target layout
    # (name: value on the left, controls flush right) without needing a
    # manual split().
    row = layout.row(align=True)
    row.label(text=f"{layer.label}: {_current_label(layer)}")

    # Only for the garments PZ itself would show a colour button on. The
    # swatch is given a fixed width - left to itself a COLOR prop expands and
    # swallows the row that the label is supposed to own.
    if tintable:
        swatch = row.row(align=True)
        swatch.ui_units_x = 2.0
        swatch.prop(layer, "tint", text="")
        dice = row.operator("pz_character.randomize_tint", text="", icon="COLOR")
        dice.slot = layer.slot

    search = row.operator("pz_character.search_layer", text="", icon="VIEWZOOM")
    search.slot = layer.slot

    reset = row.operator("pz_character.reset_layer", text="", icon="LOOP_BACK")
    reset.slot = layer.slot

    left = row.operator("pz_character.cycle_layer", text="", icon="TRIA_LEFT")
    left.slot = layer.slot
    left.direction = -1

    right = row.operator("pz_character.cycle_layer", text="", icon="TRIA_RIGHT")
    right.slot = layer.slot
    right.direction = 1


#: Slot -> group for the collapsible sub-panels below. Every slot
#: layers_for_gender() (character/pools.py) can emit - skin_tone/hair/beard
#: plus all 18 of appearance_pools.json's rules.render_order - is covered
#: here exactly once; anything render_order grows later that isn't added to
#: a named group here still shows up, in the catch-all "Other" panel below,
#: rather than silently vanishing from the UI.
LAYER_GROUPS = (
    ("PZ_PT_character_group_body", "Body", ("skin_tone", "hair", "beard")),
    ("PZ_PT_character_group_underwear", "Underwear", ("underwearbottom", "underweartop")),
    ("PZ_PT_character_group_tops", "Tops", ("tanktop", "tshirt", "shortsleeveshirt", "shirt", "sweater", "jacket")),
    ("PZ_PT_character_group_bottoms", "Bottoms", ("shortsshort", "shortpants", "pants", "skirt", "dress", "longskirt")),
    ("PZ_PT_character_group_feet", "Feet", ("socks", "shoes")),
    ("PZ_PT_character_group_accessories", "Accessories", ("hat", "eyes")),
    # Two ordinary layers, same cycle_layer/reset_layer as every group above -
    # see pools.layers_for_gender()'s "weapon" branch for where they come
    # from and equipment.resolve_hands() (called from ops._rebuild_scene())
    # for the both-hands rule applied to whatever ends up picked here.
    ("PZ_PT_character_group_hands", "Hands", ("weapon_right", "weapon_left")),
)


def _make_group_panel(panel_id, label, slots):
    # NOT `bl_idname = bl_idname` below - a class body doesn't close over an
    # enclosing-function local of the *same* name (LOAD_NAME finds nothing
    # bound yet in the class namespace and never falls through to the
    # function scope), so that spelling raises NameError at class-definition
    # time. Two distinct names side-step it.
    wanted = set(slots)

    class _GroupPanel(bpy.types.Panel):
        bl_idname = panel_id
        bl_label = label
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "PZ Character"
        bl_parent_id = "PZ_PT_character_viewer"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            props = context.scene.pz_character
            col = self.layout.column(align=True)
            tintable = tintable_slots(context)
            shown = False
            for layer in props.layers:
                if layer.slot in wanted:
                    _draw_layer_row(col, layer, layer.slot in tintable)
                    shown = True
            if not shown:
                col.label(text="(none for this gender)", icon="INFO")

    _GroupPanel.__name__ = f"Panel_{panel_id}"
    return _GroupPanel


_GROUP_PANEL_CLASSES = tuple(_make_group_panel(idn, lbl, slots) for idn, lbl, slots in LAYER_GROUPS)

_KNOWN_GROUP_SLOTS = set().union(*(slots for _, _, slots in LAYER_GROUPS))


def _draw_damage_region_row(layout, row):
    """One region's hole/blood/dirt toggles - doesn't reuse _draw_layer_row(),
    which is built for a single indexed pick (cycle/search/reset), not
    several independent boolean fields per row."""
    ui_row = layout.row(align=True)
    ui_row.label(text=row.region)
    for field in ("hole", "blood", "dirt"):
        on = getattr(row, field)
        op = ui_row.operator(
            "pz_character.toggle_damage_region", text=field.capitalize(),
            depress=on, icon="CHECKBOX_HLT" if on else "CHECKBOX_DEHLT",
        )
        op.region = row.region
        op.field = field


class PZ_PT_character_group_damage(bpy.types.Panel):
    bl_idname = "PZ_PT_character_group_damage"
    bl_label = "Damage"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PZ Character"
    bl_parent_id = "PZ_PT_character_viewer"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        props = context.scene.pz_character
        layout = self.layout

        if not props.damage_regions:
            layout.label(text="No damage regions - re-run extract.bat to mine them", icon="INFO")
            return

        row = layout.row(align=True)
        row.prop(props, "damage_mode", expand=True)

        if props.damage_mode == "RANDOM":
            r = layout.row(align=True)
            r.operator("pz_character.roll_random_damage", icon="FILE_REFRESH")
            r.operator("pz_character.clear_damage", text="", icon="X")
        else:
            layout.operator("pz_character.clear_damage", icon="X")
            col = layout.column(align=True)
            for damage_row in props.damage_regions:
                _draw_damage_region_row(col, damage_row)


class PZ_PT_character_group_other(bpy.types.Panel):
    bl_idname = "PZ_PT_character_group_other"
    bl_label = "Other"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PZ Character"
    bl_parent_id = "PZ_PT_character_viewer"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        props = context.scene.pz_character
        col = self.layout.column(align=True)
        tintable = tintable_slots(context)
        shown = False
        for layer in props.layers:
            if layer.slot not in _KNOWN_GROUP_SLOTS:
                _draw_layer_row(col, layer, layer.slot in tintable)
                shown = True
        if not shown:
            col.label(text="(none for this gender)", icon="INFO")


def _current_item(layer):
    from . import props as props_mod

    return props_mod.current_item(layer)


def _current_label(layer):
    item = _current_item(layer)
    if not item:
        return "(none)"
    # Item ids look like "Clothes/Bob_GhillieTrousers" or "Hair/M_Hair_Picard" -
    # the trailing segment is the readable part.
    return item.rsplit("/", 1)[-1]


class PZ_UL_anim_results(bpy.types.UIList):
    bl_idname = "PZ_UL_anim_results"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        pin_icon = "PINNED" if item.pinned else "UNPINNED"
        op = row.operator("pz_character.toggle_pin_animation", text="", icon=pin_icon, emboss=False)
        op.clip_id = item.name
        row.label(text=item.display_name or item.name)
        row.label(text=f"{item.duration:.2f}s")


class PZ_PT_animation_library(bpy.types.Panel):
    bl_idname = "PZ_PT_animation_library"
    bl_label = "PZ Animation Library"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PZ Character"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pz_character_anim
        # Searching and pinning read the manifest and work with no character
        # at all; playing needs an armature to assign the action to. Split so
        # the list stays browsable rather than the whole panel greying out.
        target = build.target_armature(context)
        has_character = target is not None
        active_clip_id = anim_props.get_active_clip(target)

        # target_root() prefers a released rig under the current selection
        # over the live one (see its docstring) - surface which rig Play/
        # Stop/Layer Animations will actually act on, so picking a released
        # empty back up later (e.g. to Stop it into T-pose) doesn't look like
        # it's silently controlling the live character instead.
        target_root_obj = build.target_root(context)
        if target_root_obj is not None and target_root_obj.name != build.ROOT_NAME:
            layout.label(text=f"Editing: {target_root_obj.name}", icon="ARMATURE_DATA")

        if not has_character:
            layout.label(text="No character - Randomize to build one", icon="INFO")

        row = layout.row(align=True)
        row.prop(props, "filter_text", text="", icon="VIEWZOOM")
        row.prop(props, "category_filter", text="")
        layout.operator("pz_character.refresh_anim_results", text="Search", icon="VIEWZOOM")

        if props.result_total > len(props.results):
            layout.label(text=f"showing {len(props.results)} of {props.result_total}", icon="INFO")

        layout.template_list(
            "PZ_UL_anim_results",
            "",
            props,
            "results",
            props,
            "results_index",
        )

        selected = None
        if props.results and 0 <= props.results_index < len(props.results):
            selected = props.results[props.results_index]

        row = layout.row(align=True)
        play = row.row(align=True)
        play.enabled = selected is not None and has_character
        op = play.operator("pz_character.play_animation", text=f"Play '{selected.display_name}'" if selected else "Play")
        if selected is not None:
            op.clip_id = selected.name

        # active_clip_id lives on the target armature itself (anim_props.
        # ACTIVE_CLIP_PROP), not the scene - a freshly rebuilt live character
        # is a brand-new object with no such property yet, so this can't go
        # on offering Stop for a clip that nothing is playing.
        stop = row.row(align=True)
        stop.enabled = bool(active_clip_id) and has_character
        stop.operator("pz_character.stop_animation", text="Stop")

        if has_character and active_clip_id:
            layout.label(text=f"Playing: {active_clip_id}", icon="PLAY")


class PZ_PT_animation_layering(bpy.types.Panel):
    bl_idname = "PZ_PT_animation_layering"
    bl_label = "Layer Animations (NLA)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "PZ Character"
    bl_parent_id = "PZ_PT_animation_library"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.pz_character_anim
        selected = None
        if props.results and 0 <= props.results_index < len(props.results):
            selected = props.results[props.results_index]

        layout.label(text="Select a clip above, then:")
        row = layout.row(align=True)
        row.enabled = selected is not None
        set_base = row.operator("pz_character.set_layer_clip", text="Set as Base")
        set_base.role = "base"
        set_overlay = row.operator("pz_character.set_layer_clip", text="Set as Overlay")
        set_overlay.role = "overlay"
        if selected is not None:
            set_base.clip_id = selected.name
            set_overlay.clip_id = selected.name

        col = layout.column(align=True)
        col.label(text=f"Base: {props.layer_base_clip or '(none)'}")
        col.label(text=f"Overlay: {props.layer_overlay_clip or '(none)'}")

        layout.row(align=True).prop(props, "layer_mask_side", expand=True)

        # The two picks above survive a character rebuild on purpose (they are
        # inputs, like the appearance layer selections); what does not survive
        # is the NLA setup they produce, which lives on the armature - so this
        # needs a character to act on even though the picks are still there.
        apply_row = layout.row()
        apply_row.enabled = bool(
            props.layer_base_clip and props.layer_overlay_clip
        ) and anim_props.character_exists(context)
        apply_row.operator("pz_character.layer_animations", icon="NLA")


classes = (
    (PZ_PT_character_viewer,)
    + _GROUP_PANEL_CLASSES
    + (
        PZ_PT_character_group_other,
        PZ_PT_character_group_damage,
        PZ_UL_anim_results,
        PZ_PT_animation_library,
        PZ_PT_animation_layering,
    )
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
