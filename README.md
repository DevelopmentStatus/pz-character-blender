<div align="center">

# PZ Character Blender

### Build a real Project Zomboid character in Blender

Same body meshes, clothing, hair, beards, skin tones and animation clips
as **Build 42** itself — ready to customize, pose and play in the viewport.

<br>

![Python](https://img.shields.io/badge/Python_3-stdlib_only-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Blender](https://img.shields.io/badge/Blender-5.2-E87D0D?style=for-the-badge&logo=blender&logoColor=white)
![Build 42](https://img.shields.io/badge/Project_Zomboid-Build_42-8B0000?style=for-the-badge)

[**Quick start**](#-quick-start) &nbsp;·&nbsp;
[**Features**](#-features) &nbsp;·&nbsp;
[**How it works**](#-how-its-built) &nbsp;·&nbsp;
[**Details**](DETAILS.md)

</div>

<br>
<br>

---

<br>

<div align="center">

## 🚀 Quick start

### Option A &nbsp;·&nbsp; Full setup

<br>

```bash
setup.bat
```

<br>

**One script does all of it:**

</div>

<div align="center">

| | |
|:--:|:--|
| **1** | Checks Python is on your PATH |
| **2** | Finds your ProjectZomboid folder — folder picker if it can't guess |
| **3** | Extracts the game's models, textures and clips |
| **4** | Offers to copy the add-on into Blender for you |

</div>

<br>

> ⏱️ &nbsp;**About ten minutes**, almost all of it the extract.

<br>

<div align="center">

### Option B &nbsp;·&nbsp; Drag a zip in

Already extracted `assets/characters/`? Skip the setup script — build the
add-on zip and drag it straight into a Blender window, where it installs
itself.

<br>

**Make the zip:**

```bash
package_addon.bat
```

<br>

</div>

That writes `pz_character_viewer.zip` next to `setup.bat`. It isn't committed
to the repo — it's a build artifact, and a stale one silently keeps
reinstalling old code, so it's always built fresh. Prebuilt zips are attached
to each [**release**](../../releases).

<br>

> 📦 &nbsp;**No script?** Right-click `src/addon/pz_character_viewer`, then
> **Send to → Compressed (zipped) folder**. Same result. The script is easier,
> and it skips `__pycache__` for you.

<br>

**Then drag it in**, and point the add-on at your data: open the sidebar, and
next to **`(not set)`** at the top of the panel click the 📁 folder button.
Pick the **`assets/characters`** folder — it sits right beside `setup.bat`.

Blender remembers it from then on.

<br>

---

<br>

<div align="center">

### Then, in Blender

</div>

<br>

<table align="center">
<tr>
<td width="60"><h3 align="center">1</h3></td>
<td><b>Edit → Preferences → Add-ons</b><br>Enable <b>PZ Character Viewer</b></td>
</tr>
<tr>
<td><h3 align="center">2</h3></td>
<td>Press <kbd>N</kbd> in the 3D viewport<br>Opens the sidebar</td>
</tr>
<tr>
<td><h3 align="center">3</h3></td>
<td>Open the <b>PZ Character</b> tab</td>
</tr>
<tr>
<td><h3 align="center">4</h3></td>
<td>Click <b>Randomize</b> 🎲</td>
</tr>
</table>

<br>

<div align="center">

<details>
<summary><b>Prefer to install by hand?</b></summary>

<br>

[DETAILS.md](DETAILS.md) carries the manual install steps, every extractor
option, how to re-run it later, the test suite, and the internals of the
add-on.

</details>

</div>

<br>

---

<br>

<div align="center">

## ✨ Features

</div>

<br>

<table align="center">
<tr>
<td width="50%" valign="top">

### 👕 &nbsp;Full outfit builder

Body, underwear, tops, bottoms, feet, accessories and more — cycled layer
by layer.

</td>
<td width="50%" valign="top">

### 🎲 &nbsp;Randomize

Rolls a fresh outfit using the game's **own** weighting and exclusivity
rules.

</td>
</tr>
<tr>
<td valign="top">

### 🎨 &nbsp;Recolour

Any garment the game itself lets you recolour, with the game's own
muted-random-colour logic.

</td>
<td valign="top">

### 🧍 &nbsp;Line-ups

Release characters into a row, instead of overwriting your last one.

</td>
</tr>
<tr>
<td valign="top">

### 🏃 &nbsp;Real animation clips

Browse, search, pin and play the game's own.

</td>
<td valign="top">

### 🔀 &nbsp;Clip layering

Two clips at once — base + overlay — with real upper/lower body bone masks.

</td>
</tr>
</table>

<br>

> 📖 &nbsp;[**DETAILS.md**](DETAILS.md) — exactly how each of these works
>
> 🗡️ &nbsp;[**weapon_attachment.md**](src/docs/weapon_attachment.md) — how PZ
> attaches a held weapon, and the two defects that currently make one sit wrong

<br>

---

<br>

<div align="center">

## 🧩 How it's built

**Two halves, connected only by a folder of extracted files:**

</div>

<br>

<div align="center">

```
     your PZ install                                   Blender
            │                                             ▲
            ▼                                             │
   ┌──────────────────┐      ┌───────────────┐     ┌──────────────┐
   │  src/extractor/  │ ───► │    assets/    │ ──► │  src/addon/  │
   │   Python, once   │      │  characters/  │     │   the add-on │
   └──────────────────┘      └───────────────┘     └──────────────┘
     reads the game            gitignored            never touches
                                                     the install
```

</div>

<br>

<div align="center">

| Part | What it does |
|:--|:--|
| **`src/extractor/`** | Stdlib-only Python. Reads your PZ install once, writes `assets/characters/`. |
| **`src/addon/pz_character_viewer/`** | A Blender add-on. Reads `assets/characters/`. Never touches the game install. |

</div>

<br>

> ⚖️ &nbsp;**You need a legal Build 42 install.**
> Nothing extracted is redistributable, so `assets/` is gitignored — everyone
> who clones this runs the extractor against their own copy.
> See [LEGAL_DISCLAIMER.md](LEGAL_DISCLAIMER.md).

<br>

---

<br>

<div align="center">

## 📋 Requirements

</div>

<br>

<div align="center">

| | | |
|:--:|:--|:--|
| 🐍 | **Python 3** on PATH | For the extractor. No pip packages needed. |
| 🟠 | **Blender** | For the add-on. Developed against 5.2. |
| 🧟 | **Project Zomboid Build 42** | The source of everything. Needs `media/models_X`. |
| 🪟 | **Windows** | For the `.bat` scripts. Not needed — see below. |

</div>

<br>

<div align="center">

<details>
<summary><b>Running on Linux or macOS</b></summary>

<div align="left">

<br>

The add-on is plain Python and works anywhere Blender does. Only the
convenience wrappers — `setup.bat`, `extract.bat`, `package_addon.bat` and
`scripts/pick_folder.ps1` — are Windows-only.

Everything they wrap is portable. Run the extractor directly:

```bash
export PZ_GAME_DIR="/path/to/ProjectZomboid"
mkdir -p assets/characters
python3 src/extractor/pz_characters.py --out assets/characters/characters.pzc
python3 src/extractor/pz_animscript.py --out assets/characters/locomotion_manifest.json
```

The second pass takes about five seconds and supplies the upper/lower bone
masks for partial-body animation preview. The add-on runs without it.

Then build the add-on zip — `package_addon.py` is stdlib Python too:

```bash
python3 package_addon.py
```

and install it as in **Option B** above.

</div>

<br>

</details>

</div>

<br>

---

<br>

<div align="center">

## 🌱 Where this came from

</div>

<br>

This is the character half of the `pz_tile_viewer` add-on in
[**pz-testing**](../../), split out to stand alone.

Nothing in this folder needs Godot — though the same extracted data is also
read by a Godot runtime (`CharacterAssetRegistry.gd`) elsewhere in that
project.

<br>

---

<br>

<div align="center">

## ⚖️ Licence

</div>

<br>

Released under the **[GNU General Public License v3.0](LICENSE)** — forks and
derivatives must stay open source under the same licence.

That covers **this codebase only**. It grants no rights to The Indie Stone's
models, animations, textures or trademarks: extracted output is gitignored,
and redistributing it is between you and them.

[**LEGAL_DISCLAIMER.md**](LEGAL_DISCLAIMER.md) has the full position.

<br>

<div align="center">

<sub>Not affiliated with, endorsed by, or sponsored by The Indie Stone.<br>
Project Zomboid and related marks are their property.</sub>

</div>
