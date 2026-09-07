import bpy

from . import pools


#: Set while code (rather than the user) is writing layer properties, so the
#: tint update callback below does not fire once per assignment during a
#: rebuild_layers()/apply_appearance()/randomize. Not re-entrant on purpose -
#: use the suspend_updates() context manager, never the flag directly.
_suspend_updates = False


class suspend_updates:
    """`with props_mod.suspend_updates():` around any bulk write to layer
    properties. Restores the previous value rather than clearing, so nesting
    is harmless."""

    def __enter__(self):
        global _suspend_updates
        self._previous = _suspend_updates
        _suspend_updates = True
        return self

    def __exit__(self, *exc):
        global _suspend_updates
        _suspend_updates = self._previous
        return False


def updates_suspended():
    return _suspend_updates


def _on_tint_changed(self, context):
    """Repaint just this slot rather than rebuilding the character.

    A colour picker fires its update callback continuously while dragged, and
    build_character() re-reads the skeleton, rebuilds the armature and
    re-skins every mesh - doing that per drag tick would lock the UI solid.
    ops.apply_tint() touches one material's multiply node, or re-composites
    the 256x256 body texture in place, and nothing else.
    """
    if _suspend_updates:
        return
    from . import ops as ops_mod

    ops_mod.apply_tint(context, self.slot)


class PZCharacterLayerItem(bpy.types.PropertyGroup):
    slot: bpy.props.StringProperty(default="")
    label: bpy.props.StringProperty(default="")
    # "" is the None/bald/clean-shaven sentinel (pools.NONE_ITEM); index 0 of
    # `options` on every layer except skin_tone, which has no such entry.
    options: bpy.props.StringProperty(default="")  # JSON-encoded list[str]
    index: bpy.props.IntProperty(default=0)
    # PZ's per-garment tint, for the items whose clothingItem XML sets
    # m_AllowRandomTint. Stored per SLOT, not per item, so cycling through a
    # slot's options keeps the colour you picked - which is what you want
    # while comparing two shirts, and differs from PZ only in that PZ hangs
    # the tint off the item instance.
    #
    # LINEAR, like every Blender COLOR property - see the tint section in
    # pools.py for why that is what makes the multiply match the game.
    tint: bpy.props.FloatVectorProperty(
        name="Colour",
        description="Tint multiplied into this garment's texture, as PZ's own colour picker does",
        subtype="COLOR",
        size=3,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0),
        update=_on_tint_changed,
    )


class PZCharacterDamageRegion(bpy.types.PropertyGroup):
    """One of the 18 `DAMAGE_REGIONS` (src/extractor/pz_characters.py) - a
    checkbox row in the panel's Manual damage mode. Kept as plain on/off
    flags rather than a blood/dirt intensity slider: PZ tracks a float
    level, but nothing here asked for tunable intensity, and a checkbox
    that rebuilds on click matches every other operator-driven control in
    this file (cycle/reset/randomize) rather than needing apply_tint()'s
    drag-safe in-place repaint path. build.composite_body_texture() and
    composite_garment_texture() still take a float 0..1 per region - a
    checked box just always sends a fixed intensity (see build.py's
    DEFAULT_BLOOD_INTENSITY / DEFAULT_DIRT_INTENSITY)."""
    region: bpy.props.StringProperty(default="")
    hole: bpy.props.BoolProperty(default=False)
    blood: bpy.props.BoolProperty(default=False)
    dirt: bpy.props.BoolProperty(default=False)


class PZCharacterState(bpy.types.PropertyGroup):
    preview_enabled: bpy.props.BoolProperty(default=False)
    gender: bpy.props.EnumProperty(
        name="Gender",
        items=[("male", "Male", ""), ("female", "Female", "")],
        default="male",
    )
    layers: bpy.props.CollectionProperty(type=PZCharacterLayerItem)
    layers_index: bpy.props.IntProperty(default=0)
    status: bpy.props.StringProperty(default="")
    # Sticky across rebuilds: build_character() re-applies it to every mesh it
    # makes, so randomize/gender-switch/layer changes don't silently drop back
    # to flat shading. PZ's art is low-poly and faceted by design, so flat is
    # the honest default and this is opt-in.
    shade_smooth: bpy.props.BoolProperty(
        name="Shade Smooth",
        description="Smooth-shade every mesh of the preview character",
        default=False,
    )
    # Sticky the same way shade_smooth is - build_character() re-applies it to
    # whatever armature it just built, so the toggle survives a rebuild.
    show_bones: bpy.props.BoolProperty(
        name="Show Bones",
        description="Show the preview character's armature/bones in the viewport",
        default=True,
    )
    damage_mode: bpy.props.EnumProperty(
        name="Damage Mode",
        description="Random rolls a hole onto a random covered region, "
                     "mirroring Clothing.addRandomHole(); Manual lets you "
                     "check exactly which regions are damaged",
        items=[("RANDOM", "Random", ""), ("MANUAL", "Manual", "")],
        default="RANDOM",
    )
    damage_regions: bpy.props.CollectionProperty(type=PZCharacterDamageRegion)


def current_item(layer):
    """The chosen id for one PZCharacterLayerItem, decoding its options blob."""
    import json

    options = json.loads(layer.options) if layer.options else []
    if not options:
        return ""
    idx = max(0, min(layer.index, len(options) - 1))
    return options[idx]


def rebuild_layers(props, pools_data, gender):
    import json

    # Preserve the current pick for a slot across a rebuild (e.g. a gender
    # switch keeps hair/skin index 0 rather than always resetting) when that
    # slot still exists; a slot the new gender doesn't have is dropped.
    previous = {layer.slot: current_item(layer) for layer in props.layers}
    previous_tints = {layer.slot: tuple(layer.tint) for layer in props.layers}

    with suspend_updates():
        props.layers.clear()
        for layer_def in pools.layers_for_gender(pools_data, gender):
            item = props.layers.add()
            item.slot = layer_def["slot"]
            item.label = layer_def["label"]
            item.options = json.dumps(layer_def["options"])
            prev_value = previous.get(layer_def["slot"])
            if prev_value is not None and prev_value in layer_def["options"]:
                item.index = layer_def["options"].index(prev_value)
            else:
                item.index = 0
            # A slot that survives the rebuild keeps its colour, for the same
            # reason it keeps its item: a gender switch should not silently
            # repaint the outfit.
            item.tint = previous_tints.get(layer_def["slot"], pools.DEFAULT_TINT)
        props.layers_index = 0


def rebuild_damage_regions(props, pools_data):
    """Ensures props.damage_regions has exactly one row per region in
    pools_data["damage"]["hole_masks"], in that order - mirrors
    rebuild_layers()'s "rebuild from the pools file, but keep what's
    already picked" shape. Call once after pools.load() (register, root
    switch, reload) the same places rebuild_layers() is called; a bare
    gender switch does not need this, damage is not gendered."""
    regions = list(pools_data.get("damage", {}).get("hole_masks", {}).keys())
    previous = {r.region: (r.hole, r.blood, r.dirt) for r in props.damage_regions}

    with suspend_updates():
        props.damage_regions.clear()
        for region in regions:
            row = props.damage_regions.add()
            row.region = region
            hole, blood, dirt = previous.get(region, (False, False, False))
            row.hole, row.blood, row.dirt = hole, blood, dirt


def clear_damage(props):
    """Resets every region's hole/blood/dirt flag - used by "Default" (reset
    to a clean naked character) and PZCHAR_OT_clear_damage. No update
    callback to suspend here - unlike tint, these flags are read once at
    rebuild time by an explicit operator, not live-repainted on every write."""
    for row in props.damage_regions:
        row.hole = row.blood = row.dirt = False


def damage_dict(props):
    """{"holes": [...], "blood": [...], "dirt": [...]} - the shape
    build.composite_body_texture()/composite_garment_texture() expect,
    decoded from the checkbox rows."""
    holes, blood, dirt = [], [], []
    for row in props.damage_regions:
        if row.hole:
            holes.append(row.region)
        if row.blood:
            blood.append(row.region)
        if row.dirt:
            dirt.append(row.region)
    return {"holes": holes, "blood": blood, "dirt": dirt}


def apply_appearance(props, appearance, tints=None):
    """Write an appearance (and optionally a {slot: linear rgb} tint map) back
    into the panel's layers. Any slot absent from `tints` is reset to white,
    so Default and Randomize cannot leave a stale colour behind on a slot they
    just changed the garment of."""
    import json

    with suspend_updates():
        for layer in props.layers:
            options = json.loads(layer.options) if layer.options else []
            value = appearance.get(layer.slot, "")
            if value in options:
                layer.index = options.index(value)
            if tints is not None:
                layer.tint = tints.get(layer.slot, pools.DEFAULT_TINT)


def tint_dict(props):
    """{slot: linear rgb tuple} for every layer, mirroring appearance_dict()."""
    return {layer.slot: tuple(layer.tint) for layer in props.layers}


def tint_for(props, slot):
    for layer in props.layers:
        if layer.slot == slot:
            return tuple(layer.tint)
    return pools.DEFAULT_TINT


def appearance_dict(props):
    return {layer.slot: current_item(layer) for layer in props.layers}


def set_layer_value(props, slot, value):
    """Write `value` into `slot`'s layer as if it had been cycled there -
    used by ops._rebuild_scene() to reflect equipment.resolve_hands()'s
    corrected picks back into the "weapon_right"/"weapon_left" layers, so
    the panel's cycle arrows show what actually ended up equipped rather
    than the raw pick that got collapsed. A no-op if `slot` has no layer or
    `value` isn't one of its options (should not happen for a value that
    just came out of the same options list, but silent rather than raising
    keeps a stale/edited pools file from crashing the rebuild over this)."""
    import json

    for layer in props.layers:
        if layer.slot != slot:
            continue
        options = json.loads(layer.options) if layer.options else []
        if value in options:
            layer.index = options.index(value)
        return


classes = (
    PZCharacterLayerItem,
    PZCharacterDamageRegion,
    PZCharacterState,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pz_character = bpy.props.PointerProperty(type=PZCharacterState)


def unregister():
    del bpy.types.Scene.pz_character
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
