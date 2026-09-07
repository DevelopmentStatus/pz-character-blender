"""PZ Character Viewer - build a real Project Zomboid character in Blender
from the game's own meshes, textures and animations.

Standalone: this is the character half of pz-testing's `pz_tile_viewer`
add-on, split out so it can live in a repo of its own. It reads
`assets/characters/` - `characters.pzc`, `appearance_pools.json`,
`character_manifest.json`, `textures/` and (optionally)
`locomotion_manifest.json` - all of which are produced by the extractor in
`extractor/`, run for you by `setup.bat`. Nothing here talks to Godot, and
nothing here reads the game install directly.

Install and usage: see ../../README.md.
"""

import bpy

# Re-import submodules when the add-on is RELOADED, not just re-registered.
#
# Load-bearing, and it cost real time to learn upstream: Python caches modules
# in sys.modules, so copying edited .py files into Blender's addons folder and
# then disabling/re-enabling the add-on (or hitting Reload Scripts) re-runs
# register() against the ALREADY-IMPORTED code. The edit appears to do nothing,
# with no error anywhere to say so.
#
# "character" is a subpackage and reloading it does not reload *its* members,
# so it carries the same block in character/__init__.py.
if "character" in locals():
    import importlib

    importlib.reload(character)  # noqa: F821

from . import character
from .character import pools as character_pools

bl_info = {
    "name": "PZ Character Viewer",
    "author": "DevelopmentStatus",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > PZ Character",
    "description": "Build and customize real Project Zomboid characters from extracted game assets",
    "category": "3D View",
}


class PZCharacterViewerPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    characters_root: bpy.props.StringProperty(
        name="assets/characters folder",
        description=(
            "Folder the extractor wrote: characters.pzc, appearance_pools.json, "
            "character_manifest.json, textures/"
        ),
        subtype="DIR_PATH",
        default=character_pools.default_characters_root(),
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "characters_root")
        row.operator("pz_character.browse_characters_root", text="", icon="FILE_FOLDER")
        if not self.characters_root:
            layout.label(
                text="Run setup.bat first, then point this at its assets/characters folder",
                icon="INFO",
            )


def register():
    bpy.utils.register_class(PZCharacterViewerPreferences)
    character.register()


def unregister():
    character.unregister()
    bpy.utils.unregister_class(PZCharacterViewerPreferences)


if __name__ == "__main__":
    register()
