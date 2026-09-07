"""Character preview + customization: reads assets/characters/characters.pzc
directly (no Godot involved) and builds a real PZ character in the scene.

See docs/project/pipeline/character_assets.md for the source pipeline and
scripts/characters/CharacterAssetRegistry.gd for the format this mirrors.
"""

# Reload this subpackage's own members - the parent's importlib.reload() of
# `character` re-runs this file but does NOT recurse into what it imports. See
# the block in ../__init__.py for why any of this is needed at all.
#
# build/animation_build/pzc_reader/pools/anim_pins/equipment are listed even
# though they are not imported below: they are pulled in indirectly by
# ops/anim_ops, and they are precisely the modules that carry the transform
# (or, for equipment, the both-hands rule) maths, so they are the ones where
# a silently-stale copy does the most damage.
if "props" in locals():
    import importlib

    from . import anim_pins, animation_build, build, equipment, pools, pzc_reader  # noqa: F401

    for _stale in (pzc_reader, pools, anim_pins, equipment, build, animation_build,
                   props, anim_props, ops, anim_ops, panel):  # noqa: F821
        importlib.reload(_stale)

from . import anim_ops, anim_props, ops, panel, props

__all__ = ["anim_ops", "anim_props", "ops", "panel", "props"]


def register():
    props.register()
    anim_props.register()
    ops.register()
    anim_ops.register()
    panel.register()


def unregister():
    panel.unregister()
    anim_ops.unregister()
    ops.unregister()
    anim_props.unregister()
    props.unregister()
