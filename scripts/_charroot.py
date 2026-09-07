"""Where the tests find the extractor's output.

Every test_*.py here reads `assets/characters/`, and all of them used to
locate it by counting `..` up to *pz-testing*, the repo pz-character was
split out of. That worked only while it lived there: standalone, the same
walk lands outside the checkout and the tests fail on a path nobody can
read.

Resolution order, most specific first:

1. `PZ_CHARACTERS_ROOT` in the environment - an explicit override, and the
   way to point a test at an export living somewhere else entirely.
2. `assets/characters/` beside `setup.bat`, i.e. this repo's own export.
   The normal case after `setup.bat` or `extract.bat`.
3. Whatever the add-on's own probe finds (`pools.default_characters_root()`),
   which keeps working when pz-character is nested inside a larger repo that
   already has an export - no need to duplicate ~80 MB to run a test.

`characters.pzc` must be present for a candidate to count; a directory that
exists but is empty is not a hit. If nothing resolves, `require()` exits
with the command to run rather than an IsADirectoryError three frames deep.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PZCHAR_ROOT = os.path.dirname(HERE)


def _has_pack(root):
    return bool(root) and os.path.isfile(os.path.join(root, "characters.pzc"))


def find():
    """Best characters root, or "" if there is no usable one."""
    env = os.environ.get("PZ_CHARACTERS_ROOT", "")
    if _has_pack(env):
        return env

    local = os.path.join(PZCHAR_ROOT, "assets", "characters")
    if _has_pack(local):
        return local

    # Import lazily: the add-on package is only on sys.path once the caller
    # has inserted src/addon/, which not every test does before importing us.
    try:
        sys.path.insert(0, os.path.join(PZCHAR_ROOT, "src", "addon"))
        from pz_character_viewer.character import pools
    except ImportError:
        return ""
    probed = pools.default_characters_root()
    return probed if _has_pack(probed) else ""


def require():
    """find(), or exit(1) with a message saying how to produce one."""
    root = find()
    if root:
        return root
    sys.stderr.write(
        "\n  No extracted character data found.\n\n"
        "  Looked for characters.pzc in:\n"
        "    $PZ_CHARACTERS_ROOT\n"
        f"    {os.path.join(PZCHAR_ROOT, 'assets', 'characters')}\n"
        "    the add-on's own probe\n\n"
        "  Run the extractor first:\n"
        "    setup.bat          (first run - finds the game, extracts)\n"
        "    extract.bat        (re-runs, once setup.bat has been through)\n\n"
        "  Or point at an existing export:\n"
        "    set PZ_CHARACTERS_ROOT=<path to assets/characters>\n\n"
    )
    raise SystemExit(1)
