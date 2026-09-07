# Details

Deeper reference material for `pz-character`. The [README](README.md) covers what
this is and how to get running; this file covers how it works.

---

> **Platform:** the `.bat` wrappers and `scripts/pick_folder.ps1` are
> Windows-only. Everything they wrap — the extractor, `package_addon.py`,
> the add-on itself — is stdlib Python and runs anywhere Blender does. See
> the README's *Running on Linux or macOS* section for the direct commands.

## What the extractor reads and writes

`src/extractor/pz_characters.py` walks the install's `media/models_X` (DirectX
`.x` meshes), `media/anims_X` (`.x` animation clips), `media/AnimSets` and
`media/textures`, and packs the lot into one indexed file:

| Output | What it is |
|---|---|
| `assets/characters/characters.pzc` | Every mesh and clip, zlib-deflated, behind a seekable string-keyed index |
| `assets/characters/character_manifest.json` | What ids are inside the `.pzc` |
| `assets/characters/appearance_pools.json` | The hair/beard/clothing pools and the rules that combine them, mined from the game's own XML |
| `assets/characters/locomotion_manifest.json` | Upper/lower body bone masks, from `media/AnimSets` |
| `assets/characters/textures/` | Per-mesh PNGs, copied out as-is |

**One packed file, not ~3,000 loose ones, and deliberately.** A real
filesystem creates small files at roughly 130/sec, so a directory of
converted meshes is minutes of pure file creation before anything reads it.
The `.pzc` index is fixed-width and seekable, so a mesh is a seek plus an
inflate.

**Three conversions are baked in offline** rather than left to whatever
reads the file: handedness (DirectX is left-handed, so `C = diag(1,1,-1)`
is applied as `C @ M @ C` and index order is reversed to keep winding),
scale (`--scale`, default 1.8 world units per `.x` unit; raw bodies are
~0.98 tall), and DirectX's quaternion convention. The long docstring at the
top of `src/extractor/pz_characters.py` is the authority on all of it.

### Re-running the extractor

```bash
extract.bat                    everything again
extract.bat --dry-run          list what it would do, write nothing
extract.bat --only FemaleBody  one mesh, for a fast loop
extract.bat --pools-only       re-mine appearance_pools.json only
```

`setup.bat` remembers the install it found in `game_dir.txt`; `extract.bat`
reads that. Override either with `PZ_GAME_DIR` in the environment, or pass
`--game-dir` yourself.

---

## What the add-on does, in detail

Everything lives in the **PZ Character** sidebar tab (<kbd>N</kbd> in the 3D
viewport).

**Character** — there is no separate "build a character" button, because
every control builds one if there is none. **Randomize** rolls a fresh outfit
through the game's own weighting, exclusivity and underwear-set rules;
**Default** gives you a plain bald body; and cycling any layer, or switching
Male/Female, rebuilds with that one change. Whichever you press first is what
brings the character into being. The small **X** beside them empties the scene
— it is the only thing that does, and released characters are never touched.

Layers are grouped into Body / Underwear / Tops / Bottoms / Feet / Accessories
/ Other, each cycled with the arrow buttons. Clearing keeps your selections, so
pressing anything afterwards brings the same character back.

Clothing is a **second skinned mesh**, not an attachment — that is how PZ
does it, and the add-on follows suit.

**Colour** — a garment that PZ lets you recolour gets a colour swatch and a
dice button on its row, and nothing else does. That is not a guess about
which items look tintable: it is PZ's own `m_AllowRandomTint` flag, mined out
of each item's XML, and it is the same condition the game uses to decide
whether to show its colour button in character creation. 83 items across 18
slots have it. The dice rolls a colour the way the game does — hue anywhere,
but saturation capped and never pure black or white, which is why PZ's
random clothing is muted. Randomize rolls colours too.

The tint is a per-channel multiply on the garment's texture, matching the
game's own shader to within 1.87/255 at worst; the arithmetic and the reason
it is not exactly zero are in `scripts/test_character_tint.py`.

**Release & New** — at the bottom of the panel once a character is built.
Everything the add-on makes goes into one empty called `PZ Character`, and
generating another character wipes it. Release moves the character you have
into an empty of its own, steps it aside so you can see both, and rolls a
fresh one into the live empty — so you can build up a line-up. A released
character keeps its own copy of its body texture and whatever animation it
was posed in, neither of which survives on its own.

**If no swatches appear at all**, your `appearance_pools.json` predates the
flag. Re-mine it — seconds, and it does not touch the `.pzc`:

```bash
extract.bat --pools-only
```

**PZ Animation Library** — search and filter the clips packed into the
`.pzc`, pin the ones you keep coming back to, and Play one on the built
character. Searching and pinning work with no character at all; Play and Stop
need one, and say so rather than erroring when you press them.

It follows the character underneath it: rebuilding, releasing or clearing one
all drop whatever was playing, so the panel never claims a clip is running on
a character that is back in its rest pose. Your search results, pins and NLA
clip picks are kept — those are yours, not the character's.

**Layer Animations (NLA)** — set one clip as a base and another as an
overlay, pick a body half, and combine them through Blender's NLA using the
upper/lower bone masks from `locomotion_manifest.json`. This is why that
file is worth generating even though the add-on runs without it.

### Install the add-on by hand

`setup.bat` offers to do this; if you'd rather not, or it guessed the wrong
Blender:

- **Copy** (or symlink) `src/addon/pz_character_viewer` into Blender's
  `scripts/addons`, e.g.
  `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\pz_character_viewer`.
- **Or zip**: run `package_addon.bat` (or `python package_addon.py`) and use
  *Edit → Preferences → Add-ons → Install from Disk* on the
  `pz_character_viewer.zip` it writes. Rebuild it after every edit to
  `src/addon/` — a stale zip goes stale *silently*, reinstalling an old copy
  of the add-on with nothing about the Blender install path to say so.
- **Or just drag the zip in**: build it with `package_addon.bat` as above,
  then drag the resulting `pz_character_viewer.zip` from Explorer onto an
  open Blender window — it installs itself, no Preferences trip at all. The
  zip is gitignored, so a fresh clone has none: build it first. You still
  have to point it at the data afterwards (next section) — dragging the zip
  in installs the add-on, not the extracted assets.
- **Or zip it by hand**: right-click `src/addon/pz_character_viewer` → *Send
  to → Compressed (zipped) folder*. Blender's installer wants a single
  `pz_character_viewer/` folder at the archive root, which is exactly what
  that produces. `package_addon.py` is still the better route — it skips
  `__pycache__` and `.pyc`, and it always writes a current copy.

On Blender 4.2+ you may need to switch the Add-ons filter dropdown from
"Extensions" to include **Legacy Add-ons** before "PZ Character Viewer"
appears.

### Pointing it at the data

Installed into Blender's own `scripts/addons` — by `setup.bat`, by hand,
by *Install from Disk*, or by dragging the zip in — the add-on has no way
to guess where you extracted to, so the panel opens saying **(not set)**. Click
its folder button (or set it in the add-on preferences) and pick this
folder's `assets\characters`. Blender remembers it.

Run in place — symlinked out of `src/addon/`, or with this whole folder on
Blender's script path — and it finds the sibling `assets/characters` by
itself.

---

## Checking a change

```bash
"C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" --background --factory-startup --python scripts/test_character_tint.py
```

```bash
"C:/Program Files/Blender Foundation/Blender 5.2/blender.exe" --background --factory-startup --python scripts/test_character_release.py
```

Both run headless against your own extracted `assets/characters` in a few
seconds and exit non-zero on failure. Verified on Blender 4.5 and 5.2 —
**run both versions if you have them**, since one real bug here (`Image.copy()`
returning an empty image) only appears on 4.5.

Extract locally, not over a network drive. Every step is file-I/O bound on
thousands of small files, and a mount turns minutes into hours.

---

## Editing the add-on

One trap, and it costs an hour every time someone hits it fresh: **Python
caches modules in `sys.modules`**, so copying edited `.py` files into
Blender's addons folder and then disabling/re-enabling the add-on (or
hitting Reload Scripts) re-runs `register()` against the code already
imported. The edit appears to do nothing, and nothing anywhere reports an
error. Both `__init__.py` files carry an `importlib.reload()` block for
exactly this; if you add a module, add it there too. Restarting Blender
always works.

---

## Reference — how the add-on is put together

A second feature inside the same add-on, independent of the tile/prop viewer
above. It reads `assets/characters/characters.pzc` (and
`appearance_pools.json` / `character_manifest.json`) directly and builds a
real, dressed, animatable PZ character in Blender — a Python/`bpy`
re-implementation of `CharacterAssetRegistry.gd`'s skinning and appearance
logic, used to verify that pipeline offline without a running Godot scene.
See [`character_assets.md`](../../docs/project/pipeline/character_assets.md)
for the format this reads.

### UI

Sidebar (`N`) → **PZ Character** tab, two panels:

- **PZ Character** — folder picker for `assets/characters`, **Preview
  Character** (builds body + armature at rest), Male/Female toggle, one
  collapsible sub-panel per customization group (Body, Underwear, Tops,
  Bottoms, Feet, Accessories, Other) with `◀ value ▶` cyclers and a
  reset-to-none button per slot, **Default**, **Randomize** (a full PZ-style
  outfit roll — weighted picks, slot exclusivity, matched underwear sets, all
  ported from `CharacterAssetRegistry.gd`'s `random_appearance()`), and
  **Shade Smooth** (persists across rebuilds).
- **PZ Animation Library** — filter text + category (Bob/Kate/Zombie/All) →
  **Search** → pick a clip, pin favourites to the top, **Play** (bakes the
  clip into a real Blender `Action` and assigns it — scrub with Blender's own
  timeline) / **Stop** (resets to bind T-pose). Nested **Layer Animations
  (NLA)** sub-panel picks a Base clip, an Overlay clip, and a mask side
  (upper/lower body), then stacks them as NLA tracks using PZ's own
  locomotion bone masks from `locomotion_manifest.json`.

### Repo-root files

| File | Role |
|---|---|
| `setup.bat` | First-run wrapper: checks Python, finds the game, extracts, offers to install the add-on. |
| `extract.bat` | Re-runs the extractor alone, passing your arguments through. |
| `package_addon.py` / `.bat` | Zips `src/addon/pz_character_viewer` into an installable archive. |
| `scripts/pick_folder.ps1` | The folder picker `setup.bat` falls back to when it cannot guess the install. |
| `.gdignore` | Marks this folder invisible to the **Godot** editor. Only meaningful while pz-character sits inside the pz-testing repo, where Godot would otherwise try to import ~80 MB of extracted assets on every scan. Harmless but pointless standalone — safe to delete once this repo is fully split out, and *not* before. |

### Files

| File | Role |
|---|---|
| `pzc_reader.py` | Pure-Python (`struct`+`zlib`, no `bpy`) `.pzc` reader — `skeleton()` / `get_mesh()` / `get_clip()`. Must stay in sync with `CharacterAssetRegistry.gd`'s `Cursor` field order and `tools/pz_characters.py`. |
| `pools.py` | Pure-Python appearance-pool loader — `layers_for_gender()`, `resolve_item()`, `default_appearance()`, `randomize_appearance()`. |
| `props.py` | `PropertyGroup`s for appearance state (`PZCharacterState`, `PZCharacterLayerItem`) plus `rebuild_layers()` / `apply_appearance()`. |
| `build.py` | The geometry builder — armature rest-pose solve, skinned body/garment meshes, rigid props (hats/glasses), skin-tone/overlay texture compositing. |
| `animation_build.py` | Bakes a `.pzc` clip into a real Blender `Action` — `build_action()` for the skinned mesh, `build_prop_action()` for a rigid prop's own motion. |
| `anim_pins.py` | Persists pinned clip ids to `assets/characters/blender_animation_pins.json` (same `.bak` pattern as `overrides.py`). |
| `anim_props.py` | `PropertyGroup`s for the Animation Library panel (search results, filters, NLA base/overlay pickers). |
| `ops.py` | Appearance-side operators (cycle/reset/default/randomize/gender/shade-smooth, folder picker) and shared `_ensure_reader()` / `_characters_root()` helpers. |
| `anim_ops.py` | Animation-side operators — search, pin toggle, play/stop (bake + assign), and `_layer_animations` (bone-masked NLA stacking). |
| `panel.py` | All UI for both panels above. |

### Conventions and gotchas

**Why a Blender add-on talks about Godot.** The `.pzc` format this reads was
written for a Godot renderer, so the extractor converts PZ's 3ds-Max-style
Z-up data into Godot's right-handed **Y-up** convention on the way out. That
is the convention baked into the file, so the add-on's comments describe
transforms in "Godot space" — it means *the coordinate system `.pzc` stores*,
not a Godot dependency. Nothing here imports or requires Godot.
`build.py`'s `_AXIS_FIX` (line 96) is the single conversion from that space into
Blender's Z-up; everything upstream of it is Y-up by definition.


Several of these were live bugs, fixed in the commit that also added
`scripts/test_character_animation.py` — worth reading before touching bone or prop
math again:

- **Godot is Y-up, Blender is Z-up.** `build._AXIS_FIX` is a fixed +90° X
  rotation, applied by conjugation to transforms and by direct multiply to
  points — same convention as Blender's own glTF importer.
- **The armature must be built from the body mesh's inverse-bind-matrix list,
  not `skel:Human`'s rest transforms.** The two differ by a mean 0.242 units
  (worst 0.805); building from the wrong one puts bones through or beside the
  body.
- **Bip01 bones point down local +X, not Blender's +Y.** Every bone's head/
  tail/roll has to be re-solved rather than copied — getting this wrong
  "sprays" the rig apart the moment any clip plays.
- **`use_connect` must always be `False`**, even where a bone's head and tail
  coincide. A connected bone silently discards its own keyframed location
  channel — this, not anything prop-specific, was the real cause of a
  "floating hat" symptom (a corrupted head bone). Measured error: 0.0656
  units on `Bip01_Head`, propagated to every descendant.
- **The skin bake needs a per-bone rotation-convention correction**
  (`C[b] = W_bind⁻¹ @ matrix_local`) because `align_roll()` re-permutes each
  bone's local axes onto Blender's "Y down the bone" convention — a ~79.5°
  mean divergence, not a rounding detail. Mixing the two conventions is what
  caused an earlier "exploded bones" bug.
- **Rigid props (hats, glasses) bake their own `Action` off the clip's
  animated world transform** (`build_prop_action`), not a bind-pose
  correction relative to the skin bake. An earlier version used
  `pz_prop_base`, a separate reference pose that was self-consistent in
  isolation but diverged ~7 cm / 9° from the skin bake under animation — the
  "hat/glasses drift" bug. `pz_prop_base` is left in the code, unused, with a
  comment explaining why. Per-item forward-nudge tuning
  (`RIGID_PROP_FORWARD_NUDGE`) is composed on the right, in the bone-local
  frame, so it travels with bone rotation instead of drifting under it.
- **A display-only bone-scale bug was silently trimming about two-thirds of
  the skeleton's movement** — fixed by composing scale explicitly
  (`local = local @ Diagonal(scale)`) in `_world_matrices_at`.
- **Stop must reset pose-bone `matrix_basis` by hand**, not just clear
  `animation_data.action` — and a rigid prop's rest is read back from a
  stashed `pz_rest_source` ID property on its bone.
- **Every rebuild purges previously baked actions** (tagged
  `BAKED_ACTION_TAG = "pz_clip"`), or a stale action solved against a
  since-replaced armature gets silently reused instead of re-baked.
- **Keyframes are linear, never Blender's default Bezier** — Bezier invents
  curvature that isn't in PZ's 30 Hz linear/slerp samples (measured ~50%
  handle-slope error on a synthetic ramp).
- **Blender 4.4+'s "layered actions" redesign** means fcurves may live behind
  an `ActionSlot`/`Channelbag` instead of directly on the `Action`;
  `_channelbag_for()` / `iter_fcurves()` / `get_action_slot()` /
  `assign_action()` abstract over both APIs — use them rather than touching
  `action.fcurves` directly.

### Tests

```bash
blender --background --python scripts/test_character_armature.py
blender --background --python scripts/test_character_animation.py
```

`test_character_armature.py` is `bpy`-free — it mirrors `build.py`'s head/
tail/roll solver in plain Python and checks the computed rest geometry
directly: bones point at the right child joint, branch roots (Bip01, prop
bones, nubs) fall back to their own axis instead of getting dragged sideways,
the T-pose is L/R mirror-symmetric within 1e-3, no bone has zero length, and
two real rigid props (police hat, aviator glasses) land in a plausible
bounding box on the skull.

`test_character_animation.py` runs a real headless Blender session against
the actual `assets/characters/characters.pzc`: builds the armature and body,
bakes several real clips, and asserts the **evaluated, deformed mesh's**
bounding box falls in a plausible human range per clip — deliberately reading
`evaluated_get(depsgraph).to_mesh()` rather than re-deriving the math in
numpy, because an earlier numpy "verification" was circular (the same
`matrix_local` term cancels out of both the pose solve and the deform, so it
passes even for a wrong value). It also bakes `Clothes/M_PoliceHat` as a
rigid prop under `Bob_EmoteSneeze2H` and asserts the prop barely drifts
relative to its own attach bone (<0.001 units / 0.05°) while still moving
noticeably from its static rest pose (>0.02 units) — the regression test for
the hat/glasses-drift fix above.

---

## Where this came from

This is the character half of the `pz_tile_viewer` add-on in
[pz-testing](../../), split out to stand alone. The extractor is that
repo's `tools/pz_characters.py` and `tools/pz_animscript.py`, unchanged
except that `GAME_DIR` now reads `PZ_GAME_DIR` from the environment. The
add-on is its `character/` subpackage, unchanged except for
`default_characters_root()`, which no longer assumes it is sitting inside
that repo.

Upstream, the same `.pzc` is read by a Godot runtime
(`CharacterAssetRegistry.gd`) — the format and every baked conversion exist
to serve that, and the Blender side mirrors it. Nothing in this folder needs
Godot.
