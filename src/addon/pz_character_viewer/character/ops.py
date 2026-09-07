import bpy

from . import build, equipment, pools, props as props_mod
from .pzc_reader import PZCError, PZCReader

_reader = None
_reader_path = None


def _prefs(context):
    return context.preferences.addons.get(__package__.rsplit(".", 1)[0])


def _characters_root(context):
    addon = _prefs(context)
    if addon is not None and getattr(addon.preferences, "characters_root", ""):
        return addon.preferences.characters_root
    return pools.default_characters_root()


def _reset_for_new_root():
    """Drops the open reader and cached pools so the next access re-reads
    from whatever characters_root now points at - same idea as
    crop.clear_page_cache() on the tile-viewer side of browse_assets_root."""
    global _reader, _reader_path
    if _reader is not None:
        _reader.close()
    _reader = None
    _reader_path = None
    pools.clear_cache()


def _ensure_reader(context):
    global _reader, _reader_path
    import os

    root = _characters_root(context)
    pzc_path = os.path.join(root, "characters.pzc")
    if _reader is not None and _reader_path == pzc_path:
        return _reader, root
    if _reader is not None:
        _reader.close()
    _reader = PZCReader(pzc_path)
    _reader_path = pzc_path
    return _reader, root


def _rebuild_scene(context):
    """Rebuild the live character from the panel's current state.

    **The build-if-none rule.** Every operator that changes the character
    calls this unconditionally, where they used to be guarded by
    `if props.preview_enabled`. Under the guard, pressing Randomize or
    cycling a layer with nothing built changed state that nothing was showing;
    now it builds one. That is what removed the need for a separate
    "Generate/Replace Character" button, whose only real job was the very
    first press. The two operators that stay guarded are Browse and Reload
    Character Data - pointing at a folder should not conjure a character.
    """
    props = context.scene.pz_character
    reader, root = _ensure_reader(context)
    pools_data = pools.load(root)
    appearance = props_mod.appearance_dict(props)

    # Collapse the raw "weapon_right"/"weapon_left" picks through PZ's own
    # both-hands rule before anything gets built, and write the corrected
    # result back into those two layers so the panel's cycle arrows show
    # what actually ended up equipped (e.g. cycling Right to a both-hands
    # weapon has to make Left show it too; cycling Left to something else
    # afterward has to make Right show empty again). See
    # equipment.resolve_hands()'s own doc for why a fixed right-then-left
    # replay is enough, with no need to track which layer the user actually
    # just touched.
    if "weapon_right" in appearance:
        item_manifest = equipment.load_item_manifest(root)
        right, left, _weapon_type = equipment.resolve_hands(
            item_manifest, appearance.get("weapon_right", ""), appearance.get("weapon_left", "")
        )
        appearance["weapon_right"] = right
        appearance["weapon_left"] = left
        props_mod.set_layer_value(props, "weapon_right", right)
        props_mod.set_layer_value(props, "weapon_left", left)

    build.build_character(
        context, root, reader, pools_data, appearance, props.gender,
        props_mod.tint_dict(props), props_mod.damage_dict(props),
    )
    props.preview_enabled = True


def _tag_redraw(context):
    """Nudge the 3D viewport after an in-place edit. A property update
    callback is not a redraw, and a repainted image or material node value
    otherwise sits there until something else happens to trigger one."""
    screen = getattr(context, "screen", None)
    for area in getattr(screen, "areas", []) or []:
        if area.type in ("VIEW_3D", "IMAGE_EDITOR"):
            area.tag_redraw()


def apply_tint(context, slot):
    """Repaint one slot's colour without rebuilding the character.

    Called from the tint property's update callback, which fires on every
    tick of a colour-picker drag - so this deliberately does the smallest
    thing that can work. A mesh garment is one material-node write. A texture
    overlay has no material of its own, so its slot re-composites the shared
    body image; that is 256x256 of numpy and the image object is reused, so
    the body material keeps pointing at it and updates for free.

    Silent on every failure. It runs from a UI callback where an exception
    would surface as a console traceback with no operator to report it, and
    every reason it can fail (no preview, no pools file, the slot's item is
    not tintable) is a normal state rather than an error.
    """
    props = context.scene.pz_character
    if not props.preview_enabled:
        return
    try:
        root = _characters_root(context)
        pools_data = pools.load(root)
    except (OSError, ValueError):
        return

    appearance = props_mod.appearance_dict(props)
    item = appearance.get(slot, "")
    if not item:
        return
    tint = props_mod.tint_for(props, slot)

    # Hair/beard have no pools.resolve_item() entry (they're mesh ids
    # directly, not clothing items - see build.build_character()) and no
    # m_AllowRandomTint to check: PZ tints them unconditionally, so this is
    # always a plain in-place material repaint.
    if slot in ("hair", "beard"):
        if build.set_slot_tint(context, slot, tint) == 0:
            return
        _tag_redraw(context)
        return

    if not pools.item_allows_tint(pools_data, item):
        return
    resolved = pools.resolve_item(pools_data, item, props.gender)
    if resolved is None:
        return

    if resolved[0] == "mesh":
        if build.set_slot_tint(context, slot, tint) == 0:
            return
    else:
        try:
            build.composite_body_texture(
                root, pools_data, appearance.get("skin_tone", ""), appearance,
                props.gender, props_mod.tint_dict(props), props_mod.damage_dict(props),
            )
        except (OSError, ValueError):
            return
    _tag_redraw(context)


class PZCHAR_OT_browse_characters_root(bpy.types.Operator):
    bl_idname = "pz_character.browse_characters_root"
    bl_label = "Browse for assets/characters Folder"
    bl_description = "Pick this repo's assets/characters folder (holds characters.pzc, appearance_pools.json, textures/)"

    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        addon = _prefs(context)
        self.directory = addon.preferences.characters_root if addon else pools.default_characters_root()
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        addon = _prefs(context)
        if addon is None:
            self.report({"ERROR"}, "Enable the add-on in Preferences > Add-ons first")
            return {"CANCELLED"}

        addon.preferences.characters_root = self.directory
        _reset_for_new_root()

        props = context.scene.pz_character
        try:
            pools_data = pools.load(self.directory)
        except (OSError, ValueError) as exc:
            self.report({"WARNING"}, f"Set folder, but couldn't read appearance_pools.json there: {exc}")
            return {"FINISHED"}

        props_mod.rebuild_layers(props, pools_data, props.gender)
        props_mod.rebuild_damage_regions(props, pools_data)
        appearance = pools.default_appearance(pools_data, props.gender)
        props_mod.apply_appearance(props, appearance, {})

        if props.preview_enabled:
            try:
                _rebuild_scene(context)
            except (OSError, PZCError, KeyError, ValueError) as exc:
                self.report({"WARNING"}, f"Set folder, but couldn't rebuild the preview: {exc}")
                return {"FINISHED"}

        self.report({"INFO"}, f"assets/characters set to {self.directory}")
        return {"FINISHED"}


class PZCHAR_OT_reload_pools(bpy.types.Operator):
    bl_idname = "pz_character.reload_pools"
    bl_label = "Reload Character Data"
    bl_description = "Re-read characters.pzc and appearance_pools.json (they change after a re-export)"

    def execute(self, context):
        _reset_for_new_root()
        props = context.scene.pz_character
        try:
            root = _characters_root(context)
            pools_data = pools.load(root, force=True)
        except (OSError, PZCError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not reload character data: {exc}")
            return {"CANCELLED"}

        props_mod.rebuild_layers(props, pools_data, props.gender)
        props_mod.rebuild_damage_regions(props, pools_data)
        if props.preview_enabled:
            try:
                _rebuild_scene(context)
            except (OSError, PZCError, KeyError, ValueError) as exc:
                self.report({"ERROR"}, f"Could not rebuild character: {exc}")
                return {"CANCELLED"}

        self.report({"INFO"}, "Character data reloaded")
        return {"FINISHED"}


class PZCHAR_OT_clear_character(bpy.types.Operator):
    bl_idname = "pz_character.clear_character"
    bl_label = "Clear Character"
    bl_description = (
        "Remove the character from the PZ Character empty, leaving the scene "
        "empty. Released characters are not touched"
    )

    @classmethod
    def poll(cls, context):
        root = bpy.data.objects.get(build.ROOT_NAME)
        return bool(root is not None and root.children)

    def execute(self, context):
        props = context.scene.pz_character
        build.remove_existing(context)
        # The layer selections survive on purpose - clearing is "take it off
        # the screen", not "forget what I picked", and Randomize (or touching
        # any layer control) builds a character straight back.
        props.preview_enabled = False
        props.status = ""
        return {"FINISHED"}


class PZCHAR_OT_cycle_layer(bpy.types.Operator):
    bl_idname = "pz_character.cycle_layer"
    bl_label = "Cycle Layer"
    bl_description = "Step to the next/previous option for this layer"

    slot: bpy.props.StringProperty(default="")
    direction: bpy.props.IntProperty(default=1)  # +1 or -1

    def execute(self, context):
        import json

        props = context.scene.pz_character
        layer = next((l for l in props.layers if l.slot == self.slot), None)
        if layer is None:
            return {"CANCELLED"}
        options = json.loads(layer.options) if layer.options else []
        if not options:
            return {"CANCELLED"}
        layer.index = (layer.index + self.direction) % len(options)

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


# Blender's own fuzzy-search popup (WindowManager.invoke_search_popup) needs
# an EnumProperty whose items are computed on demand and a module-level spot
# to keep the returned list alive - the popup reads it lazily as you type, and
# a list that only exists inside the callback's stack frame can be collected
# out from under it before that happens.
_search_items_cache = []


def _layer_search_items(self, context):
    import json

    global _search_items_cache
    props = context.scene.pz_character
    layer = next((l for l in props.layers if l.slot == self.slot), None)
    items = []
    if layer is not None:
        options = json.loads(layer.options) if layer.options else []
        for i, item_id in enumerate(options):
            label = item_id.rsplit("/", 1)[-1] if item_id else "(none)"
            items.append((str(i), label, item_id))
    _search_items_cache = items
    return _search_items_cache


class PZCHAR_OT_search_layer(bpy.types.Operator):
    bl_idname = "pz_character.search_layer"
    bl_label = "Search Options"
    bl_description = "Fuzzy-search this layer's options by name and jump straight to one"
    bl_property = "option"

    slot: bpy.props.StringProperty(default="")
    option: bpy.props.EnumProperty(items=_layer_search_items, name="Option")

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        props = context.scene.pz_character
        layer = next((l for l in props.layers if l.slot == self.slot), None)
        if layer is None:
            return {"CANCELLED"}
        layer.index = int(self.option)

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_reset_layer(bpy.types.Operator):
    bl_idname = "pz_character.reset_layer"
    bl_label = "Reset to Default"
    bl_description = "Set this layer back to its default option (index 0 - none, for every slot but skin tone)"

    slot: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.pz_character
        layer = next((l for l in props.layers if l.slot == self.slot), None)
        if layer is None:
            return {"CANCELLED"}
        with props_mod.suspend_updates():
            layer.index = 0
            # The colour goes back to white with the garment. Leaving it set
            # would mean an invisible piece of state survives "reset", and
            # would repaint the next tintable item put in this slot with a
            # colour the user picked for something else.
            layer.tint = pools.DEFAULT_TINT

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_set_default(bpy.types.Operator):
    bl_idname = "pz_character.set_default"
    bl_label = "Default"
    bl_description = "Bald, naked, male. Builds one if there is no character yet"

    def execute(self, context):
        props = context.scene.pz_character
        try:
            root = _characters_root(context)
            pools_data = pools.load(root)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read appearance_pools.json: {exc}")
            return {"CANCELLED"}

        props.gender = "male"
        props_mod.rebuild_layers(props, pools_data, "male")
        props_mod.rebuild_damage_regions(props, pools_data)
        props_mod.clear_damage(props)
        appearance = pools.default_appearance(pools_data, "male")
        # {} rather than None: apply_appearance() resets any slot the map does
        # not name, so this clears every tint as well as every garment.
        # "Naked" includes empty-handed: "weapon_right"/"weapon_left" are
        # ordinary layers now (pools.layers_for_gender()), so this already
        # resets them along with everything else - no separate hand state
        # to clear.
        props_mod.apply_appearance(props, appearance, {})

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_randomize(bpy.types.Operator):
    bl_idname = "pz_character.randomize"
    bl_label = "Randomize"
    bl_description = (
        "Roll a full outfit the way PZ itself would dress a character. "
        "Builds one first if there is no character yet"
    )

    def execute(self, context):
        props = context.scene.pz_character
        try:
            root = _characters_root(context)
            pools_data = pools.load(root)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read appearance_pools.json: {exc}")
            return {"CANCELLED"}

        if not props.layers:
            props_mod.rebuild_layers(props, pools_data, props.gender)
        if not props.damage_regions:
            props_mod.rebuild_damage_regions(props, pools_data)

        appearance = pools.randomize_appearance(pools_data, props.gender)
        # PZ rolls a colour for every tintable garment as part of dressing a
        # character (ClothingItemReference.randomize()), so Randomize does too
        # rather than leaving a random outfit in flat white.
        tints = pools.randomize_tints(pools_data, appearance)
        props_mod.apply_appearance(props, appearance, tints)

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_randomize_tint(bpy.types.Operator):
    bl_idname = "pz_character.randomize_tint"
    bl_label = "Random Colour"
    bl_description = (
        "Roll this garment's colour the way PZ does - hue anywhere, but "
        "saturation under 0.6 and never fully black or white"
    )

    slot: bpy.props.StringProperty(default="")

    def execute(self, context):
        props = context.scene.pz_character
        layer = next((l for l in props.layers if l.slot == self.slot), None)
        if layer is None:
            return {"CANCELLED"}
        # Assigned outside suspend_updates() on purpose: this one write SHOULD
        # fire the update callback, which is what repaints the garment.
        layer.tint = pools.random_tint()
        return {"FINISHED"}


class PZCHAR_OT_release_character(bpy.types.Operator):
    bl_idname = "pz_character.release_character"
    bl_label = "Release & New"
    bl_description = (
        "Move the built character out of the PZ Character empty into one of "
        "its own, so it survives, then roll a fresh character into the empty"
    )

    @classmethod
    def poll(cls, context):
        props = context.scene.pz_character
        root = bpy.data.objects.get(build.ROOT_NAME)
        return bool(props.preview_enabled and root is not None and root.children)

    def execute(self, context):
        props = context.scene.pz_character
        try:
            root_dir = _characters_root(context)
            pools_data = pools.load(root_dir)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read appearance_pools.json: {exc}")
            return {"CANCELLED"}

        released, moved = build.release_character(context)
        if released is None:
            self.report({"WARNING"}, "Nothing built to release")
            return {"CANCELLED"}

        # A NEW character, not the same one rebuilt - two identical characters
        # side by side is not what "release and generate a new one" is for. The
        # roll is the same one Randomize does, tints included.
        appearance = pools.randomize_appearance(pools_data, props.gender)
        tints = pools.randomize_tints(pools_data, appearance)
        props_mod.apply_appearance(props, appearance, tints)

        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            # The release itself already happened and is not undone - the
            # released character is safe in the scene either way, and leaving
            # it half-detached would be worse than reporting a failed rebuild.
            self.report(
                {"ERROR"},
                f"Released {released.name}, but could not build the next character: {exc}",
            )
            return {"CANCELLED"}

        props.status = f"Released {released.name} ({moved} objects)"
        self.report({"INFO"}, props.status)
        return {"FINISHED"}


class PZCHAR_OT_set_gender(bpy.types.Operator):
    bl_idname = "pz_character.set_gender"
    bl_label = "Set Gender"
    bl_description = "Switch which gender's pools the layers below draw from"

    gender: bpy.props.StringProperty(default="male")

    def execute(self, context):
        props = context.scene.pz_character
        try:
            root = _characters_root(context)
            pools_data = pools.load(root)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read appearance_pools.json: {exc}")
            return {"CANCELLED"}

        props.gender = self.gender
        props_mod.rebuild_layers(props, pools_data, self.gender)
        if not props.damage_regions:
            props_mod.rebuild_damage_regions(props, pools_data)

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_toggle_shade_smooth(bpy.types.Operator):
    bl_idname = "pz_character.toggle_shade_smooth"
    bl_label = "Shade Smooth"
    bl_description = "Toggle smooth shading on every mesh of the preview character"

    def execute(self, context):
        props = context.scene.pz_character
        props.shade_smooth = not props.shade_smooth
        touched = build.apply_shading(props.shade_smooth)
        if touched == 0:
            # The flag is still flipped and stored, so it applies to whatever
            # gets built next - report rather than silently doing nothing.
            self.report({"INFO"}, "No preview character yet - will apply when one is built")
        return {"FINISHED"}


class PZCHAR_OT_toggle_show_bones(bpy.types.Operator):
    bl_idname = "pz_character.toggle_show_bones"
    bl_label = "Show Bones"
    bl_description = "Show or hide the preview character's armature in the viewport"

    def execute(self, context):
        props = context.scene.pz_character
        props.show_bones = not props.show_bones
        touched = build.apply_bone_visibility(context, props.show_bones)
        if not touched:
            # The flag is still flipped and stored, so it applies to whatever
            # gets built next - report rather than silently doing nothing.
            self.report({"INFO"}, "No preview character yet - will apply when one is built")
        return {"FINISHED"}


class PZCHAR_OT_toggle_damage_region(bpy.types.Operator):
    bl_idname = "pz_character.toggle_damage_region"
    bl_label = "Toggle Damage"
    bl_description = "Toggle a hole, blood or dirt on one body region"

    region: bpy.props.StringProperty(default="")
    field: bpy.props.EnumProperty(
        items=[("hole", "Hole", ""), ("blood", "Blood", ""), ("dirt", "Dirt", "")],
        default="hole",
    )

    def execute(self, context):
        props = context.scene.pz_character
        row = next((r for r in props.damage_regions if r.region == self.region), None)
        if row is None:
            self.report({"ERROR"}, f"Unknown damage region: {self.region}")
            return {"CANCELLED"}
        setattr(row, self.field, not getattr(row, self.field))

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_roll_random_damage(bpy.types.Operator):
    bl_idname = "pz_character.roll_random_damage"
    bl_label = "Roll Damage"
    bl_description = ("Add a hole to a random covered body region, the way "
                       "Clothing.addRandomHole() does in PZ itself")

    def execute(self, context):
        props = context.scene.pz_character
        try:
            root = _characters_root(context)
            pools_data = pools.load(root)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not read appearance_pools.json: {exc}")
            return {"CANCELLED"}

        if not props.damage_regions:
            props_mod.rebuild_damage_regions(props, pools_data)

        appearance = props_mod.appearance_dict(props)
        damage = props_mod.damage_dict(props)
        damage = pools.roll_random_damage(pools_data, appearance, props.gender, damage=damage)
        for row in props.damage_regions:
            row.hole = row.region in damage["holes"]

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class PZCHAR_OT_clear_damage(bpy.types.Operator):
    bl_idname = "pz_character.clear_damage"
    bl_label = "Clear Damage"
    bl_description = "Remove every hole, blood and dirt mark from the preview character"

    def execute(self, context):
        props = context.scene.pz_character
        props_mod.clear_damage(props)

        # Unconditional - see _rebuild_scene()'s note on the build-if-none rule.
        try:
            _rebuild_scene(context)
        except (OSError, PZCError, KeyError, ValueError) as exc:
            self.report({"ERROR"}, f"Could not rebuild character: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


classes = (
    PZCHAR_OT_browse_characters_root,
    PZCHAR_OT_reload_pools,
    PZCHAR_OT_clear_character,
    PZCHAR_OT_cycle_layer,
    PZCHAR_OT_search_layer,
    PZCHAR_OT_reset_layer,
    PZCHAR_OT_set_default,
    PZCHAR_OT_randomize,
    PZCHAR_OT_randomize_tint,
    PZCHAR_OT_release_character,
    PZCHAR_OT_set_gender,
    PZCHAR_OT_toggle_shade_smooth,
    PZCHAR_OT_toggle_show_bones,
    PZCHAR_OT_toggle_damage_region,
    PZCHAR_OT_roll_random_damage,
    PZCHAR_OT_clear_damage,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global _reader, _reader_path
    if _reader is not None:
        _reader.close()
        _reader = None
        _reader_path = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
