#!/usr/bin/env python3
"""
Reads Project Zomboid's character models and animations: DirectX .x, text form.

Why
---
Every character-relevant asset in Build 42 is DirectX `.x`, not FBX. Measured
against the install:

  * media/models_X/Skinned/          743 files, 100% .x  (bodies, clothes, hair)
  * media/anims_X/                 2,209 files, 100% .x  (17 skeleton sets)

FBX exists in media/models_X/ but only for props, IsoObjects and weapon parts.
Godot cannot parse .x at runtime or in the editor, so something has to read it
offline no matter what the runtime ends up consuming — see
docs/project/pipeline/character_assets.md.

This module is that reader, and only that. It does no conversion, no scaling,
no coordinate flipping and no packing; `pz_characters.py` does all of it. The
split is the same one `packfile.py` has from `pz_textures.py`: a format reader
that is worth testing on its own, and a pipeline stage that uses it.

The format
----------
Text .x is a header, a run of `template` declarations, then data:

    xof 0303txt 0032

    template Mesh {
     <3d82ab44-62da-11cf-ab39-0020af71e433>
     DWORD nVertices;
     array Vector vertices[nVertices];
     ...
    }

    Frame Dummy01 {
     FrameTransformMatrix { 1.0,0.0,...,1.0;; }
     Frame Bip01 { ... }
    }

Three things about the syntax are worth stating, because they are what makes a
naive line-based reader fail on real files:

  * **`;` and `,` are noise.** The spec assigns them meaning (field vs. array
    separator, `;;` closing a nested struct array) but that meaning is entirely
    redundant with the template's declared field order. Every reader that tries
    to enforce it ends up special-casing files that are punctuated slightly
    differently. This one tokenizes them away and reads values positionally.
  * **`{ Name }` inside a body is a reference**, not a nested block —
    `MeshMaterialList` ends with one, pointing at a `Material` declared earlier.
  * **A block may carry an instance name**: `Mesh Bob_AmmoStrap {`,
    `MeshTextureCoords c1 {`. Names contain `-` and can start with `_`
    (`Material _07_-_Defaultggg`).

So parsing is two layers, and they are separate on purpose. `parse()` builds a
generic tree that knows nothing about what a Mesh is; the extractors below read
the four templates that matter out of that tree, positionally. If PZ ships a
file using a template this module has never seen, layer one still reads it and
layer two ignores it, rather than the whole file failing.

Templates are parsed for their GUID and then skipped. The GUIDs are checked
against the standard ones (`EXPECTED_GUIDS`) rather than trusting the template
*name*, since the name is just an identifier in the file and the GUID is what
actually pins the field layout.

What is deliberately not done here
----------------------------------
**No scale factor is applied.** An early draft of the spec asserted a flat 0.01
centimetre-to-metre conversion. Vertex coordinates in these files are already
around 0.6-0.8 for a torso-height garment, so that number is wrong, and PZ also
carries its own per-model `scale` in media/scripts/generated/models_*.txt
(0.855 and 0.8037 on the human bodies). `--dump` reports real bounds so the
factor can be measured; this reader hands back the file's own numbers.

**Quaternions are handed back exactly as stored.** `AnimationKey` type 0 is
`w,x,y,z`, and DirectX's convention is the conjugate of Godot's. Converting is
the pipeline's job, not the reader's, and it is a thing to verify against a
playing animation rather than assume — see this project's own rule about
measuring before theorising.

Usage
-----
  # bone hierarchy, for diffing against media/AnimSets/Master_Bones.xml
  python xfile.py --bones "<game>/media/models_X/Skinned/FemaleBody.x"

  # mesh summary: counts, real coordinate bounds, skin bones, UV coverage
  python xfile.py --dump "<game>/media/models_X/Skinned/FemaleBody.x"

  # clip summary: ticks/sec, per-bone track and key counts
  python xfile.py --dump "<game>/media/anims_X/Bob/Bob_Idle.x"

  # raw tree, for a file whose shape is unexpected
  python xfile.py --tree --depth 3 "<game>/media/models_X/Skinned/MaleBody.x"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAGIC = "xof "

#: Template GUIDs from the DirectX .x specification. Checked against what a
#: file declares, because the template *name* is only an identifier — the GUID
#: is what actually pins down the field order the extractors below rely on.
EXPECTED_GUIDS = {
    "Mesh": "3d82ab44-62da-11cf-ab39-0020af71e433",
    "MeshNormals": "f6f23f43-7686-11cf-8f52-0040333594a3",
    "MeshTextureCoords": "f6f23f40-7686-11cf-8f52-0040333594a3",
    "MeshMaterialList": "f6f23f42-7686-11cf-8f52-0040333594a3",
    "Frame": "3d82ab46-62da-11cf-ab39-0020af71e433",
    "FrameTransformMatrix": "f6f23f41-7686-11cf-8f52-0040333594a3",
    "AnimationSet": "3d82ab50-62da-11cf-ab39-0020af71e433",
    "Animation": "3d82ab4f-62da-11cf-ab39-0020af71e433",
    "AnimationKey": "10dd46a8-775b-11cf-8f52-0040333594a3",
    "SkinWeights": "6f0d123b-bad2-4167-a0d0-80224f25fabb",
    "XSkinMeshHeader": "3cf169ce-ff7c-44ab-93c0-f78f62d172e2",
}

#: `AnimationKey.keyType`. Type 4 (full matrix) is in the spec and PZ does not
#: appear to use it, but a file that did would otherwise be silently misread as
#: something else, so it is named rather than left as a bare integer.
KEY_ROTATION = 0
KEY_SCALE = 1
KEY_POSITION = 2
KEY_MATRIX = 4


class XFileError(Exception):
    """Raised for anything that means the file is not readable as text .x."""


# --------------------------------------------------------------------------
# Layer one: tokenizer and generic tree
# --------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
      (?P<guid>   < [^>]* > )
    | (?P<string> " [^"]* " )
    | (?P<number> -? (?: \d+\.\d* | \.\d+ | \d+ ) (?: [eE][-+]?\d+ )? )
    | (?P<name>   [A-Za-z_] [A-Za-z0-9_.\-]* )
    | (?P<brace>  [{}] )
    | (?P<brack>  [\[\]] )
    """,
    re.VERBOSE,
)

_COMMENT = re.compile(r"(?://|\#)[^\n]*")


class XNode:
    """One `Type [name] { ... }` block, with its contents read positionally.

    `values` is every number and string in the block's own body, flattened in
    file order and with separators dropped. The extractors below walk it with a
    cursor, in the order the block's template declares — which is why dropping
    `;` and `,` is safe: the template, not the punctuation, is what says how
    many numbers a field takes.
    """

    __slots__ = ("type", "name", "values", "children", "refs")

    def __init__(self, type_: str, name: str | None = None):
        self.type = type_
        self.name = name
        self.values: list[float | int | str] = []
        self.children: list[XNode] = []
        self.refs: list[str] = []

    def find(self, type_: str) -> XNode | None:
        """First direct child of `type_`, or None."""
        for c in self.children:
            if c.type == type_:
                return c
        return None

    def find_all(self, type_: str) -> list[XNode]:
        return [c for c in self.children if c.type == type_]

    def walk(self):
        """Depth-first over this node and everything under it."""
        yield self
        for c in self.children:
            yield from c.walk()

    def __repr__(self) -> str:
        label = f"{self.type} {self.name}" if self.name else self.type
        return f"<XNode {label} values={len(self.values)} children={len(self.children)}>"


class XFile:
    """A parsed .x file: its top-level nodes, plus the templates it declared."""

    def __init__(self, path: Path | str = "<memory>"):
        self.path = str(path)
        self.nodes: list[XNode] = []
        self.templates: dict[str, str] = {}  # name -> guid

    def find(self, type_: str) -> XNode | None:
        for n in self.nodes:
            if n.type == type_:
                return n
        return None

    def walk(self):
        for n in self.nodes:
            yield from n.walk()

    def find_all_deep(self, type_: str) -> list[XNode]:
        """Every node of `type_` anywhere in the file, at any depth."""
        return [n for n in self.walk() if n.type == type_]

    def check_templates(self) -> list[str]:
        """Template names whose declared GUID is not the standard one.

        An empty list means every template this module cares about matches the
        spec and the positional extractors are safe. A non-empty one is a real
        signal — it means a file declares, say, a `Mesh` with a different field
        layout, and reading it positionally would produce plausible garbage.
        """
        bad = []
        for name, guid in self.templates.items():
            expected = EXPECTED_GUIDS.get(name)
            if expected and guid.lower() != expected:
                bad.append(f"{name}: declared {guid}, expected {expected}")
        return bad


def parse(text: str, path: Path | str = "<memory>") -> XFile:
    """Parse text-format .x into a generic tree. Raises XFileError on binary."""
    if not text.startswith(MAGIC):
        raise XFileError(f"{path}: not a .x file (no {MAGIC!r} magic)")

    header = text[:16]
    # Bytes 8..12 are the format: "txt " or "bin ". Compressed variants exist
    # ("tzip"/"bzip") but no file in the PZ install uses one, so rather than
    # ship an untested inflate path this reports what it found and stops.
    fmt = header[8:12]
    if fmt != "txt ":
        raise XFileError(
            f"{path}: .x format is {fmt!r}, only 'txt ' is supported "
            f"(every character file in the PZ install is text)"
        )

    text = _COMMENT.sub("", text[16:])

    tokens = _TOKEN.finditer(text)
    xf = XFile(path)
    stack: list[XNode] = []
    # A block header is read across iterations: seeing a name sets `pending`,
    # a second name fills `pending_name`, and `{` turns them into a node.
    pending: str | None = None
    pending_name: str | None = None
    in_template = False
    template_depth = 0

    for m in tokens:
        kind = m.lastgroup
        tok = m.group()

        if in_template:
            # Templates are read for the GUID and otherwise skipped: their
            # bodies are type declarations, not data, and letting them into
            # `values` would corrupt the positional reads.
            # Only the *first* GUID in a template is the template's own. The
            # ones that follow belong to optional members (`[Animation <guid>]`,
            # `[Material <guid>]`) and would otherwise overwrite it — which
            # showed up as MeshMaterialList claiming to be Material.
            if kind == "guid" and template_depth == 1 and pending:
                xf.templates.setdefault(pending, tok[1:-1])
            elif kind == "brace":
                if tok == "{":
                    template_depth += 1
                else:
                    template_depth -= 1
                    if template_depth == 0:
                        in_template = False
                        pending = None
            continue

        if kind == "brack" or kind == "guid":
            continue

        if kind == "name":
            if pending is None:
                pending = tok
            elif pending_name is None:
                pending_name = tok
            else:
                # Three identifiers with no brace between them is not something
                # the grammar produces; treating it as data would hide the real
                # problem behind a wrong number further down.
                raise XFileError(
                    f"{path}: unexpected identifier {tok!r} after "
                    f"{pending!r} {pending_name!r}"
                )
            continue

        if kind == "brace" and tok == "{":
            if pending == "template":
                in_template = True
                template_depth = 1
                pending = pending_name  # the template's own name
                pending_name = None
                continue

            if pending is None:
                # `{ Name }` — a reference to a node declared elsewhere.
                # Consumed below when the closing brace arrives; recorded now
                # so the name token that follows is not mistaken for a block.
                stack.append(_REFERENCE)
                continue

            node = XNode(pending, pending_name)
            if stack:
                stack[-1].children.append(node)
            else:
                xf.nodes.append(node)
            stack.append(node)
            pending = None
            pending_name = None
            continue

        if kind == "brace" and tok == "}":
            if not stack:
                raise XFileError(f"{path}: unbalanced '}}'")
            node = stack.pop()
            if node is _REFERENCE:
                # The reference's target name was read into `pending`.
                target = pending or pending_name
                if target and stack and stack[-1] is not _REFERENCE:
                    stack[-1].refs.append(target)
                pending = None
                pending_name = None
            continue

        # A number or string, i.e. actual data.
        if stack and stack[-1] is not _REFERENCE:
            if kind == "string":
                stack[-1].values.append(tok[1:-1])
            else:
                stack[-1].values.append(float(tok) if _is_float(tok) else int(tok))

    if stack:
        raise XFileError(f"{path}: {len(stack)} unclosed block(s) at end of file")

    return xf


#: Sentinel pushed for `{ Name }` reference blocks so the parser knows not to
#: treat the following name as a nested block header or its values as data.
_REFERENCE = XNode("<reference>")


def _is_float(tok: str) -> bool:
    return "." in tok or "e" in tok or "E" in tok


def parse_file(path: Path | str) -> XFile:
    p = Path(path)
    # .x text is ASCII; PZ's files include a few non-ASCII bytes in material
    # names, so latin-1 rather than utf-8 — it never raises, and no identifier
    # this module reads depends on the distinction.
    return parse(p.read_text(encoding="latin-1"), p)


# --------------------------------------------------------------------------
# Layer two: typed extractors
#
# Each reads its block's fields positionally, in the order the corresponding
# template declares them. `_Cursor` exists so that a file which is truncated or
# whose counts disagree with its data fails with the field name, rather than
# with an IndexError from somewhere in the middle of a vertex array.
# --------------------------------------------------------------------------


class _Cursor:
    def __init__(self, node: XNode, path: str = ""):
        self.v = node.values
        self.i = 0
        self.what = f"{path}:{node.type}" if path else node.type

    def take(self, n: int, field: str) -> list:
        if self.i + n > len(self.v):
            raise XFileError(
                f"{self.what}: ran out of values reading {field} "
                f"(wanted {n} at offset {self.i}, have {len(self.v)})"
            )
        out = self.v[self.i : self.i + n]
        self.i += n
        return out

    def one(self, field: str):
        return self.take(1, field)[0]

    def int1(self, field: str) -> int:
        return int(self.one(field))


class Mesh:
    """A `Mesh` block and its sibling sub-blocks, as flat parallel arrays.

    `positions`/`normals` are flat xyz triples and `uvs` flat uv pairs, indexed
    by vertex. `faces` is a list of index lists — usually triangles, but PZ's
    exporter emits the occasional quad, so they are not assumed to be uniform
    and triangulation is left to the pipeline.
    """

    def __init__(self) -> None:
        self.name: str | None = None
        self.positions: list[float] = []
        self.faces: list[list[int]] = []
        self.normals: list[float] = []
        self.normal_faces: list[list[int]] = []
        self.uvs: list[float] = []
        self.material_indices: list[int] = []
        self.materials: list[str] = []
        self.skin: list[SkinWeights] = []
        self.max_weights_per_vertex: int | None = None

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3

    @property
    def face_count(self) -> int:
        return len(self.faces)

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """(min_xyz, max_xyz) of the raw, unscaled coordinates in the file."""
        if not self.positions:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        xs = self.positions[0::3]
        ys = self.positions[1::3]
        zs = self.positions[2::3]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def bone_names(self) -> list[str]:
        return [s.bone for s in self.skin]


class SkinWeights:
    """One bone's influence over a mesh: which vertices, how much, and the
    inverse bind matrix (`matrixOffset`), stored row-major as 16 floats."""

    __slots__ = ("bone", "indices", "weights", "offset_matrix")

    def __init__(self, bone: str, indices: list[int], weights: list[float],
                 offset_matrix: list[float]):
        self.bone = bone
        self.indices = indices
        self.weights = weights
        self.offset_matrix = offset_matrix

    def __repr__(self) -> str:
        return f"<SkinWeights {self.bone} n={len(self.indices)}>"


def read_mesh(node: XNode, path: str = "") -> Mesh:
    """Read a `Mesh` block. Sub-blocks it does not recognise are ignored."""
    if node.type != "Mesh":
        raise XFileError(f"expected a Mesh block, got {node.type}")

    mesh = Mesh()
    mesh.name = node.name

    c = _Cursor(node, path)
    n_verts = c.int1("nVertices")
    mesh.positions = [float(x) for x in c.take(n_verts * 3, "vertices")]
    n_faces = c.int1("nFaces")
    mesh.faces = _read_faces(c, n_faces, "faces")

    normals = node.find("MeshNormals")
    if normals is not None:
        cn = _Cursor(normals, path)
        n = cn.int1("nNormals")
        mesh.normals = [float(x) for x in cn.take(n * 3, "normals")]
        mesh.normal_faces = _read_faces(cn, cn.int1("nFaceNormals"), "faceNormals")

    uvs = node.find("MeshTextureCoords")
    if uvs is not None:
        cu = _Cursor(uvs, path)
        n = cu.int1("nTextureCoords")
        mesh.uvs = [float(x) for x in cu.take(n * 2, "textureCoords")]

    mats = node.find("MeshMaterialList")
    if mats is not None:
        cm = _Cursor(mats, path)
        cm.int1("nMaterials")
        n_idx = cm.int1("nFaceIndexes")
        mesh.material_indices = [int(x) for x in cm.take(n_idx, "faceIndexes")]
        # Materials appear either as `{ Name }` references or inline blocks.
        mesh.materials = list(mats.refs) + [
            m.name for m in mats.find_all("Material") if m.name
        ]

    header = node.find("XSkinMeshHeader")
    if header is not None and header.values:
        mesh.max_weights_per_vertex = int(header.values[0])

    for sw in node.find_all("SkinWeights"):
        cs = _Cursor(sw, path)
        bone = cs.one("transformNodeName")
        n = cs.int1("nWeights")
        indices = [int(x) for x in cs.take(n, "vertexIndices")]
        weights = [float(x) for x in cs.take(n, "weights")]
        matrix = [float(x) for x in cs.take(16, "matrixOffset")]
        mesh.skin.append(SkinWeights(str(bone), indices, weights, matrix))

    return mesh


def _read_faces(c: _Cursor, n_faces: int, field: str) -> list[list[int]]:
    faces = []
    for _ in range(n_faces):
        n = c.int1(f"{field}[].nIndices")
        faces.append([int(x) for x in c.take(n, field)])
    return faces


class Bone:
    """One `Frame` in the hierarchy: its name, parent, and rest transform."""

    __slots__ = ("name", "parent", "transform", "children")

    def __init__(self, name: str, parent: str | None, transform: list[float]):
        self.name = name
        self.parent = parent
        self.transform = transform
        self.children: list[str] = []

    def __repr__(self) -> str:
        return f"<Bone {self.name} parent={self.parent}>"


def mesh_frame_ancestors(xf: XFile, mesh_node: XNode) -> list[list[float]]:
    """`FrameTransformMatrix` values (16 floats, row-major) for every `Frame`
    enclosing `mesh_node`, root-to-nearest order. Empty if the mesh sits at
    the top level with no wrapping Frame at all. A Frame with no
    `FrameTransformMatrix` of its own contributes identity, same default
    `read_skeleton()` uses for a bone.

    Frames form one hierarchy that includes both joint frames and
    mesh-wrapping frames - `read_skeleton()` flattens the same tree for
    bones, and a skinned mesh's world placement comes from its own
    `SkinWeights.offset_matrix` (already absolute) rather than this chain.
    But an *unskinned* mesh's vertices are stored in its wrapping Frame's
    local space with nothing else to place them, and nothing composed this
    chain in for that case - `Static/Clothes/M_FishingRainHat.x` carries a
    real (non-identity, ~90 degree rotate + mirror) transform on the Frame
    that directly wraps its Mesh, silently dropped. Measured across all 92
    static hat files this pack's appearance pools reference: 91 have
    identity frames throughout and this one does not.
    """
    path: list[XNode] = []
    found: list[list[float]] | None = None

    def visit(node: XNode) -> None:
        nonlocal found
        if found is not None:
            return
        if node is mesh_node:
            found = []
            for f in path:
                ftm = f.find("FrameTransformMatrix")
                found.append([float(x) for x in ftm.values[:16]] if ftm else _IDENTITY[:])
            return
        is_frame = node.type == "Frame"
        if is_frame:
            path.append(node)
        for c in node.children:
            visit(c)
            if found is not None:
                break
        if is_frame:
            path.pop()

    for n in xf.nodes:
        visit(n)
        if found is not None:
            break
    return found or []


def read_skeleton(xf: XFile) -> dict[str, Bone]:
    """Every `Frame` in the file, flattened, in depth-first declaration order.

    Frames that hold a mesh rather than a joint (`Frame Bob_AmmoStrap`, which
    wraps the `Mesh`) come back too — they are real frames and the caller has
    the transform hierarchy it needs to decide. `bone_names()` on a `Mesh` is
    what says which of these the skin actually binds to.
    """
    bones: dict[str, Bone] = {}

    def visit(node: XNode, parent: str | None) -> None:
        name = node.name or "<unnamed>"
        ftm = node.find("FrameTransformMatrix")
        transform = [float(x) for x in ftm.values[:16]] if ftm else _IDENTITY[:]
        bone = Bone(name, parent, transform)
        # A duplicate frame name would silently orphan whichever came first, so
        # say so rather than letting the hierarchy quietly lose a branch.
        if name in bones:
            raise XFileError(f"duplicate Frame name {name!r}")
        bones[name] = bone
        if parent in bones:
            bones[parent].children.append(name)
        for child in node.find_all("Frame"):
            visit(child, name)

    for node in xf.nodes:
        if node.type == "Frame":
            visit(node, None)
    return bones


_IDENTITY = [1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 0.0, 1.0]


class Track:
    """One bone's keyframes within a clip.

    Rotations are `(time, [w, x, y, z])` **exactly as stored** — DirectX's
    quaternion convention is the conjugate of Godot's, and converting is the
    pipeline's job, not the reader's.
    """

    __slots__ = ("bone", "rotation", "scale", "position", "matrix")

    def __init__(self, bone: str):
        self.bone = bone
        self.rotation: list[tuple[int, list[float]]] = []
        self.scale: list[tuple[int, list[float]]] = []
        self.position: list[tuple[int, list[float]]] = []
        self.matrix: list[tuple[int, list[float]]] = []

    @property
    def key_count(self) -> int:
        return len(self.rotation) + len(self.scale) + len(self.position) + len(self.matrix)

    def __repr__(self) -> str:
        return (f"<Track {self.bone} R={len(self.rotation)} "
                f"S={len(self.scale)} T={len(self.position)}>")


class Clip:
    """One `AnimationSet`: a named clip, its tick rate, and its bone tracks."""

    def __init__(self, name: str, ticks_per_second: int):
        self.name = name
        self.ticks_per_second = ticks_per_second
        self.tracks: list[Track] = []

    @property
    def duration_ticks(self) -> int:
        last = 0
        for t in self.tracks:
            for keys in (t.rotation, t.scale, t.position, t.matrix):
                if keys:
                    last = max(last, keys[-1][0])
        return last

    @property
    def duration_seconds(self) -> float:
        return self.duration_ticks / self.ticks_per_second if self.ticks_per_second else 0.0

    def __repr__(self) -> str:
        return f"<Clip {self.name} tracks={len(self.tracks)} {self.duration_seconds:.3f}s>"


#: The .x spec's default when a file declares no `AnimTicksPerSecond`. PZ's
#: files all declare one (4800), so this should never be reached — but reading
#: a clip at the wrong rate is the kind of error that looks like bad animation
#: rather than like a parse failure, so the fallback is named, not inlined.
DEFAULT_TICKS_PER_SECOND = 4800


def read_clips(xf: XFile) -> list[Clip]:
    """Every `AnimationSet` in the file."""
    tps_node = xf.find("AnimTicksPerSecond")
    tps = int(tps_node.values[0]) if tps_node and tps_node.values else DEFAULT_TICKS_PER_SECOND

    clips = []
    for node in xf.find_all_deep("AnimationSet"):
        clip = Clip(node.name or "<unnamed>", tps)
        for anim in node.find_all("Animation"):
            # The bone an Animation drives is a `{ Name }` reference.
            bone = anim.refs[0] if anim.refs else "<unbound>"
            track = Track(bone)
            for key in anim.find_all("AnimationKey"):
                _read_key(key, track, xf.path)
            clip.tracks.append(track)
        clips.append(clip)
    return clips


def _read_key(node: XNode, track: Track, path: str) -> None:
    c = _Cursor(node, path)
    key_type = c.int1("keyType")
    n_keys = c.int1("nKeys")

    if key_type == KEY_ROTATION:
        dest, width = track.rotation, 4
    elif key_type == KEY_SCALE:
        dest, width = track.scale, 3
    elif key_type == KEY_POSITION:
        dest, width = track.position, 3
    elif key_type == KEY_MATRIX:
        dest, width = track.matrix, 16
    else:
        raise XFileError(f"{path}: unknown AnimationKey keyType {key_type}")

    for _ in range(n_keys):
        time = c.int1("key.time")
        n_values = c.int1("key.nValues")
        if n_values != width:
            raise XFileError(
                f"{path}: AnimationKey type {key_type} declares {n_values} "
                f"values, expected {width}"
            )
        dest.append((time, [float(x) for x in c.take(width, "key.values")]))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _fmt_bounds(mn, mx) -> str:
    size = [b - a for a, b in zip(mn, mx)]
    return (f"min ({mn[0]:.4f}, {mn[1]:.4f}, {mn[2]:.4f})  "
            f"max ({mx[0]:.4f}, {mx[1]:.4f}, {mx[2]:.4f})  "
            f"size ({size[0]:.4f}, {size[1]:.4f}, {size[2]:.4f})")


def _print_bones(xf: XFile) -> None:
    bones = read_skeleton(xf)
    roots = [b for b in bones.values() if b.parent is None]
    print(f"{len(bones)} frame(s), {len(roots)} root(s)\n")

    def show(name: str, depth: int) -> None:
        print(f"{'  ' * depth}{name}")
        for child in bones[name].children:
            show(child, depth + 1)

    for root in roots:
        show(root.name, 0)

    print("\nflat, sorted (for diffing against Master_Bones.xml):")
    for name in sorted(bones):
        print(f"  {name}")


def _print_tree(node: XNode, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        return
    label = f"{node.type} {node.name}" if node.name else node.type
    detail = []
    if node.values:
        detail.append(f"{len(node.values)} values")
    if node.refs:
        detail.append(f"refs={node.refs}")
    suffix = f"  [{', '.join(detail)}]" if detail else ""
    print(f"{'  ' * depth}{label}{suffix}")
    for c in node.children:
        _print_tree(c, depth + 1, max_depth)


def _dump(xf: XFile) -> None:
    bad = xf.check_templates()
    if bad:
        print("TEMPLATE GUID MISMATCH - positional reads are not safe here:")
        for line in bad:
            print(f"  {line}")
        print()

    meshes = xf.find_all_deep("Mesh")
    clips = read_clips(xf)
    bones = read_skeleton(xf)

    print(f"{len(xf.templates)} template(s), {len(bones)} frame(s), "
          f"{len(meshes)} mesh(es), {len(clips)} clip(s)")

    for node in meshes:
        mesh = read_mesh(node, xf.path)
        mn, mx = mesh.bounds()
        print(f"\nMesh {mesh.name or '<unnamed>'}")
        print(f"  vertices     {mesh.vertex_count}")
        print(f"  faces        {mesh.face_count}"
              f"  (sizes: {sorted({len(f) for f in mesh.faces})})")
        print(f"  normals      {len(mesh.normals) // 3}")
        print(f"  uvs          {len(mesh.uvs) // 2}")
        print(f"  materials    {mesh.materials or '-'}")
        print(f"  bounds       {_fmt_bounds(mn, mx)}")
        if mesh.skin:
            total = sum(len(s.indices) for s in mesh.skin)
            print(f"  skin         {len(mesh.skin)} bone(s), {total} weight(s), "
                  f"max {mesh.max_weights_per_vertex} per vertex")
            for s in mesh.skin:
                print(f"                 {s.bone:<24} {len(s.indices)}")
        else:
            print("  skin         none (static mesh)")

    for clip in clips:
        print(f"\nClip {clip.name}")
        print(f"  ticks/sec    {clip.ticks_per_second}")
        print(f"  duration     {clip.duration_ticks} ticks "
              f"= {clip.duration_seconds:.3f}s")
        print(f"  tracks       {len(clip.tracks)}")
        total = sum(t.key_count for t in clip.tracks)
        print(f"  keys         {total}")
        for t in clip.tracks[:8]:
            print(f"                 {t.bone:<24} "
                  f"R={len(t.rotation)} S={len(t.scale)} T={len(t.position)}")
        if len(clip.tracks) > 8:
            print(f"                 ... {len(clip.tracks) - 8} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read Project Zomboid's DirectX .x models and animations.",
        epilog="With no mode flag, --dump is assumed.",
    )
    ap.add_argument("path", nargs="+", help="one or more .x files")
    ap.add_argument("--dump", action="store_true",
                    help="summary: counts, real coordinate bounds, skin, clips")
    ap.add_argument("--bones", action="store_true",
                    help="frame hierarchy, plus a sorted flat list for diffing")
    ap.add_argument("--tree", action="store_true",
                    help="raw generic tree, for a file whose shape is unexpected")
    ap.add_argument("--depth", type=int, default=3,
                    help="max depth for --tree (default 3)")
    args = ap.parse_args(argv)

    if not (args.dump or args.bones or args.tree):
        args.dump = True

    status = 0
    for i, path in enumerate(args.path):
        if len(args.path) > 1:
            print(f"{'=' * 70}\n{path}\n{'=' * 70}")
        try:
            xf = parse_file(path)
        except (XFileError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            status = 1
            continue

        if args.bones:
            _print_bones(xf)
        if args.tree:
            for node in xf.nodes:
                _print_tree(node, 0, args.depth)
        if args.dump:
            _dump(xf)
        print()

    return status


if __name__ == "__main__":
    raise SystemExit(main())
