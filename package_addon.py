"""Zip src/addon/pz_character_viewer into an installable Blender add-on archive.

    python package_addon.py

Only needed if you want to install through *Edit > Preferences > Add-ons >
Install from Disk* rather than by copying the folder (which is what
setup.bat does). Exists because a hand-made zip goes stale silently: it
keeps installing an old copy of the add-on long after the source moved on,
and nothing about the Blender install path makes that visible.
"""

import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src", "addon", "pz_character_viewer")
OUT = os.path.join(HERE, "pz_character_viewer.zip")

SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = (".import", ".pyc")


def main():
    written = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in sorted(files):
                if name.endswith(SKIP_SUFFIXES):
                    continue
                full = os.path.join(root, name)
                # Paths inside the zip are relative to src/addon/, so the
                # archive unpacks to a single pz_character_viewer/ folder -
                # which is what Blender's installer expects.
                rel = os.path.relpath(full, os.path.join(HERE, "src", "addon"))
                z.write(full, rel.replace(os.sep, "/"))
                written += 1
    print(f"wrote {OUT} ({written} files)")


if __name__ == "__main__":
    main()
