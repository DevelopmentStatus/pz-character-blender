#!/usr/bin/env python3
"""
Packs PZ's character meshes and animations into a single indexed .pzc file.

Why
---
Third offline export, sibling to `pz_pack.py` (world geometry) and
`pz_textures.py` (sprite art). Godot cannot read DirectX `.x` at all, so the
conversion has to happen offline no matter what; and there are ~3,000 source
files, against a filesystem measured at ~130 files/sec — the same wall that
made per-chunk JSON unworkable. One packed file with an index fixes both.

Full rationale, and what the runtime does with this:
docs/project/pipeline/character_assets.md

Not glTF, and why
-----------------
The obvious answer was to emit `.glb`. It costs twice: a skinned-and-animated
glTF *encoder* written against the spec in stdlib Python, and then a
`GLTFDocument` *decode* on every load to recover the arrays this exporter
already had. glTF earns that when assets cross tool boundaries; nothing here
does. So payloads are the arrays themselves, laid out the way
`ArrayMesh.add_surface_from_arrays()` wants them — the same choice `pz_pack.py`
makes in packing chunk JSON rather than a scene format.

Format (.pzc, all little-endian)
--------------------------------
    [4]   magic "PZCH"
    [4]   version = 1
    [4]   entry_count
    [4]   id_table_length
    [n]   id table: entry_count NUL-terminated UTF-8 ids, in index order
    entry_count x 16-byte index entry (struct "<qii"):
          [8] offset (int64, absolute)  [4] stored  [4] raw
    then the compressed payloads, back to back

`.pzw` packs its (chunk_x, chunk_y) key inline in a fixed-width entry. Models
are keyed by string, so the ids move into a table ahead of the index and the
index stays fixed-width and seekable — the property that makes the format work.

**zlib-wrapped deflate, not raw.** Godot's `decompress(size,
FileAccess.COMPRESSION_DEFLATE)` calls `inflateInit2()` with `windowBits = 15`,
which is zlib's *wrapped* format, so plain `zlib.compress()` is exactly right.
Raw deflate decompresses to an empty buffer with `Condition "err != 1"` as the
only clue. This is documented in `pz_pack.py` too; it has cost time once.

Payloads
--------
    MESH  tag, flags, counts, then position/normal/uv/bone/weight/index arrays,
          then the bone table (name, parent, rest transform) and the skin
          bind list (bone index + inverse bind matrix).
    CLIP  tag, ticks/sec, then per-bone position/rotation/scale tracks with
          times already in seconds.

Both are described field-by-field at `_pack_mesh` / `_pack_clip` below, which
are the authority — `CharacterAssetRegistry.gd` reads exactly what they write.

Three conversions are baked here, not left to the runtime
--------------------------------------------------------
Same principle as the art export baking sprite rotation: do it once, offline,
where it can be checked, rather than in a per-instance code path.

1. **Handedness.** DirectX is left-handed; Godot is right-handed. Both are
   Y-up here (measured: the bodies are ~0.98 tall in Y). The change of basis is
   `C = diag(1, 1, -1)`, applied as `C @ M @ C` to every transform rather than
   by negating quaternion components by hand — see `_convert_matrix`. Negating
   an axis also flips triangle winding, so index order is reversed to match, or
   every face would be backfacing.

2. **Scale.** Raw bodies are ~0.98 units tall. This project's world is
   `TILE_SIZE 1.0` / `FLOOR_HEIGHT 3.0`, i.e. ~1 unit per metre, so a human
   wants ~1.8. `--scale` defaults to 1.8. That is a derivation, not a
   measurement — confirm it against a doorway in a running scene and retune.

3. **Rotation convention.** `.x` stores `AnimationKey` type 0 as `w,x,y,z` and
   DirectX's quaternion is the conjugate of Godot's. Rather than reason about
   sign flips per component — which is where this kind of code goes wrong —
   every key is converted to a matrix, put through the same `C @ M @ C` as
   everything else, and decomposed back. One correct path, used everywhere.

Usage
-----
  # everything: bodies, clothes, hair, beards, and the human clip sets
  python pz_characters.py --out assets/characters/characters.pzc

  # a slice, for iterating on the runtime without a 10-minute wait
  python pz_characters.py --only FemaleBody --clips Bob_Idle --out test.pzc

  # what it would do, without writing anything
  python pz_characters.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import math
import re
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import xfile
from progress import Bar

#: Where the Build 42 install lives. `--game-dir` overrides it; so does
#: PZ_GAME_DIR in the environment, which is how `extract.bat` and
#: `setup.bat` pass along the folder they found or you picked. The literal
#: below is only the last resort: Steam's default install location, which is
#: very often wrong. Pass --game-dir or set PZ_GAME_DIR rather than relying
#: on it.
GAME_DIR = os.environ.get(
    "PZ_GAME_DIR", r"C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid"
)

MAGIC = b"PZCH"
VERSION = 1
HEADER = struct.Struct("<4siii")
INDEX_ENTRY = struct.Struct("<qii")
DEFLATE_LEVEL = 6

#: Raw bodies measure ~0.98 units tall (xfile.py, frame chain applied). The
#: world is TILE_SIZE 1.0 / FLOOR_HEIGHT 3.0 — roughly a metre per unit and a
#: 3 m storey — so a human wants ~1.8. Derived, not measured in-scene: check it
#: against a doorway once something is actually standing in the world.
DEFAULT_SCALE = 1.8

#: Skeleton sets under media/anims_X/ that belong to humans. The other 14 are
#: animals, which share this pipeline exactly but are out of scope for now
#: — nothing here would need changing to add them.
HUMAN_CLIP_SETS = ("Bob", "Kate", "Zombie")

#: Godot takes 4 or 8 bone influences per vertex (8 via
#: ARRAY_FLAG_USE_8_BONE_WEIGHTS). Measured over all 743 skinned meshes:
#: 98.77% of vertices use <= 4, and only two meshes exceed 8. Truncating
#: everything to 4 would be simpler, but the p99 vertex loses 11% of its weight
#: and the worst loses 27%, so meshes that need more get the 8-wide layout and
#: only the handful above 8 lose anything.
MAX_INFLUENCES = 8


class ExportError(Exception):
    pass


# --------------------------------------------------------------------------
# Matrix helpers
#
# .x matrices are row-major with a row-vector convention (v * M), which is also
# what a flat 16-float list from FrameTransformMatrix gives. These work on that
# layout directly and only convert to Godot's Transform3D decomposition at the
# very end, in `_decompose`.
# --------------------------------------------------------------------------

IDENTITY = (1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0)


def _matmul(a, b):
    return tuple(
        sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
        for r in range(4) for c in range(4)
    )


def _convert_matrix(m):
    """Left-handed .x -> right-handed Godot, as a change of basis.

    `C = diag(1, 1, -1)` and `C` is its own inverse, so the similarity
    transform is just `C @ M @ C`. Doing it this way rather than by flipping
    signs on individual translation and quaternion components is the whole
    reason the animation path is trustworthy: there is one conversion, it is
    obviously correct, and rest transforms and keyframes both go through it.
    """
    c = (1.0, 0.0, 0.0, 0.0,
         0.0, 1.0, 0.0, 0.0,
         0.0, 0.0, -1.0, 0.0,
         0.0, 0.0, 0.0, 1.0)
    return _matmul(_matmul(c, m), c)


def _is_close_identity(m, tol=1e-4):
    for i in range(16):
        want = 1.0 if i in (0, 5, 10, 15) else 0.0
        if abs(m[i] - want) > tol:
            return False
    return True


def _transform_points(flat, m, w):
    """Row-vector transform of flat xyz triples by `m` (row-major 4x4),
    matching `_matmul`'s convention. `w=1.0` for positions (translation
    applies), `w=0.0` for directions like normals (it does not)."""
    out = []
    for i in range(0, len(flat), 3):
        x, y, z = flat[i], flat[i + 1], flat[i + 2]
        for c in range(3):
            out.append(x * m[0 * 4 + c] + y * m[1 * 4 + c] + z * m[2 * 4 + c] + w * m[3 * 4 + c])
    return out


def _apply_static_frame_transform(mesh: xfile.Mesh, m) -> None:
    """Bakes an unskinned mesh's dropped ancestor-frame transform into its
    own vertices, in file space, before the usual scale/mirror conversion in
    `_pack_mesh` runs. Only meaningful for a mesh with no skin data - a
    skinned mesh's placement comes from its own `SkinWeights.offset_matrix`,
    already absolute, not from this chain. See
    `xfile.mesh_frame_ancestors()` for why this is needed at all."""
    mesh.positions = _transform_points(mesh.positions, m, 1.0)
    if mesh.normals:
        nrm = _transform_points(mesh.normals, m, 0.0)
        out = []
        for i in range(0, len(nrm), 3):
            x, y, z = nrm[i:i + 3]
            length = math.sqrt(x * x + y * y + z * z) or 1.0
            out += [x / length, y / length, z / length]
        mesh.normals = out


def _decompose(m, scale=1.0):
    """Flat row-major 4x4 -> (position, quaternion xyzw, scale).

    Returns the basis rows as Godot reads them. `scale` multiplies translation
    only: bone rest transforms are parent-relative, so scaling the whole
    hierarchy means scaling each offset, not each basis.
    """
    tx, ty, tz = m[12] * scale, m[13] * scale, m[14] * scale

    # Basis rows, with any non-uniform scale factored out so the quaternion
    # extraction below sees a pure rotation.
    rows = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
    lens = [math.sqrt(sum(v * v for v in r)) or 1.0 for r in rows]
    r = [[v / l for v in row] for row, l in zip(rows, lens)]

    trace = r[0][0] + r[1][1] + r[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw, qx, qy, qz = 0.25 * s, (r[1][2] - r[2][1]) / s, (r[2][0] - r[0][2]) / s, (r[0][1] - r[1][0]) / s
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2.0
        qw, qx, qy, qz = (r[1][2] - r[2][1]) / s, 0.25 * s, (r[1][0] + r[0][1]) / s, (r[2][0] + r[0][2]) / s
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2.0
        qw, qx, qy, qz = (r[2][0] - r[0][2]) / s, (r[1][0] + r[0][1]) / s, 0.25 * s, (r[2][1] + r[1][2]) / s
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2.0
        qw, qx, qy, qz = (r[0][1] - r[1][0]) / s, (r[2][0] + r[0][2]) / s, (r[2][1] + r[1][2]) / s, 0.25 * s

    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) or 1.0
    return (tx, ty, tz), (qx / n, qy / n, qz / n, qw / n), tuple(lens)


# --------------------------------------------------------------------------
# Binary writing
# --------------------------------------------------------------------------


def _put_str(out: bytearray, s: str) -> None:
    b = s.encode("utf-8")
    out += struct.pack("<i", len(b))
    out += b


def _put_f32(out: bytearray, values) -> None:
    out += struct.pack(f"<{len(values)}f", *values)


def _put_i32(out: bytearray, values) -> None:
    out += struct.pack(f"<{len(values)}i", *values)


MESH_HAS_NORMALS = 1 << 0
MESH_HAS_UVS = 1 << 1
MESH_HAS_SKIN = 1 << 2
MESH_8_WEIGHTS = 1 << 3


def _pack_mesh(mesh: xfile.Mesh, bones: dict, scale: float, stats: dict) -> bytes:
    """Serialise one mesh. Layout, in order:

        [4]  "MESH"
        [4]  flags   (see MESH_* above)
        [4]  vertex_count
        [4]  index_count
        [4]  bone_count      (0 if unskinned)
        [4]  bind_count
        [4]  influences      (4 or 8; 0 if unskinned)
             positions  vertex_count * 3  float32
             normals    vertex_count * 3  float32   (if MESH_HAS_NORMALS)
             uvs        vertex_count * 2  float32   (if MESH_HAS_UVS)
             bone_ids   vertex_count * influences  int32    (if MESH_HAS_SKIN)
             weights    vertex_count * influences  float32  (if MESH_HAS_SKIN)
             indices    index_count int32
        bone_count x:  str name, int32 parent, 3 float32 pos,
                       4 float32 quat(xyzw), 3 float32 scale
        bind_count x:  int32 bone_index, 3 float32 pos, 4 float32 quat, 3 float32 scale

    A previous revision of this function took a `weapon_correction` flag that
    baked a fixed -90 degree rotation into every `weapons/` mesh's vertices,
    to compensate for `Bip01_R_Hand`'s rest frame not matching the axis a
    weapon's length was authored along. That correction is gone: weapons now
    attach at `Bip01_Prop2`/`Bip01_Prop1` with a real per-weapon offset/
    rotation read from PZ's own `models_weapons.txt` (see
    `_weapon_attach_transform`), so a second, fixed correction baked into the
    mesh itself would double up with it rather than replace it.
    """
    out = bytearray()
    n_verts = mesh.vertex_count

    bone_names = [s.bone for s in mesh.skin]
    skinned = bool(bone_names)

    # --- vertex influences, gathered per vertex then trimmed ---
    influences = 0
    per_vertex: list[list[tuple[int, float]]] = []
    if skinned:
        per_vertex = [[] for _ in range(n_verts)]
        for bone_index, sw in enumerate(mesh.skin):
            for vi, w in zip(sw.indices, sw.weights):
                if 0 <= vi < n_verts:
                    per_vertex[vi].append((bone_index, w))
        widest = max((len(v) for v in per_vertex), default=0)
        influences = 4 if widest <= 4 else 8
        for v in per_vertex:
            if len(v) > influences:
                stats["truncated_vertices"] = stats.get("truncated_vertices", 0) + 1
                v.sort(key=lambda t: t[1], reverse=True)
                del v[influences:]
            total = sum(w for _, w in v)
            if total > 0:
                # Renormalise: always, not only after truncation. PZ's own
                # weights do not reliably sum to 1, and Godot does not
                # normalise for you.
                v[:] = [(b, w / total) for b, w in v]
            elif v:
                v[:] = [(v[0][0], 1.0)]

    flags = 0
    if mesh.normals:
        flags |= MESH_HAS_NORMALS
    if mesh.uvs:
        flags |= MESH_HAS_UVS
    if skinned:
        flags |= MESH_HAS_SKIN
        if influences == 8:
            flags |= MESH_8_WEIGHTS

    indices: list[int] = []
    for face in mesh.faces:
        # Negating Z flips handedness, which flips winding — reverse it back or
        # every triangle faces inward. Faces are all triangles (measured across
        # all 743 skinned meshes: 322,050 tris, zero quads), but a fan keeps
        # this correct if that ever stops being true.
        for i in range(1, len(face) - 1):
            indices += [face[0], face[i + 1], face[i]]

    out += b"MESH"
    out += struct.pack("<iiiiii", flags, n_verts, len(indices),
                       len(bone_names), len(bone_names) if skinned else 0,
                       influences)

    pos = []
    for i in range(n_verts):
        x, y, z = mesh.positions[i * 3:i * 3 + 3]
        pos += (x * scale, y * scale, -z * scale)
    _put_f32(out, pos)

    if mesh.normals:
        nrm = []
        for i in range(n_verts):
            x, y, z = mesh.normals[i * 3:i * 3 + 3]
            nrm += (x, y, -z)
        _put_f32(out, nrm)

    if mesh.uvs:
        _put_f32(out, mesh.uvs[:n_verts * 2])

    if skinned:
        ids: list[int] = []
        wts: list[float] = []
        for v in per_vertex:
            padded = v + [(0, 0.0)] * (influences - len(v))
            ids += [b for b, _ in padded]
            wts += [w for _, w in padded]
        _put_i32(out, ids)
        _put_f32(out, wts)

    _put_i32(out, indices)

    # --- bone table: rest transforms, parent-relative, in skin order ---
    name_to_slot = {n: i for i, n in enumerate(bone_names)}
    for name in bone_names:
        bone = bones.get(name)
        rest = _convert_matrix(bone.transform) if bone else _convert_matrix(list(IDENTITY))
        parent = -1
        if bone and bone.parent is not None:
            # Bones the skin does not bind are not in the table, so walk up
            # until a bound ancestor is found — otherwise a parent index would
            # point at the wrong slot. Their transforms are folded in below.
            walk = bone.parent
            while walk is not None and walk not in name_to_slot:
                rest = _matmul(rest, _convert_matrix(bones[walk].transform))
                walk = bones[walk].parent
            parent = name_to_slot.get(walk, -1) if walk else -1
        pos_, quat, scl = _decompose(rest, scale)
        _put_str(out, name)
        out += struct.pack("<i", parent)
        _put_f32(out, pos_ + quat + scl)

    # --- skin bind list: the inverse bind matrix per bound bone ---
    if skinned:
        for slot, sw in enumerate(mesh.skin):
            inv = _convert_matrix(sw.offset_matrix)
            pos_, quat, scl = _decompose(inv, scale)
            out += struct.pack("<i", slot)
            _put_f32(out, pos_ + quat + scl)

    return bytes(out)


#: The file whose Frame hierarchy becomes the canonical shared skeleton.
#: Every mesh binds only the subset of bones it actually uses (a body 28, a
#: strap 8), so nothing can share a Skeleton3D unless there is one agreed
#: hierarchy to bind *into*. This has the full 46 frames, nubs included, and
#: matches what the clips animate.
SKELETON_SOURCE = "models_X/Skinned/Male_Skeleton.x"
SKELETON_ID = "Human"


def _pack_skeleton(bones: dict, scale: float) -> bytes:
    """Serialise the canonical skeleton. Layout:

        [4]  "SKEL"
        [4]  bone_count
        bone_count x: str name, int32 parent, 3 f32 pos, 4 f32 quat, 3 f32 scale

    Bones are written parents-first, so a consumer can build the hierarchy in
    one pass without deferring children.
    """
    order: list[str] = []

    def visit(name: str) -> None:
        order.append(name)
        for child in bones[name].children:
            visit(child)

    for name, bone in bones.items():
        if bone.parent is None:
            visit(name)

    slot = {n: i for i, n in enumerate(order)}
    out = bytearray()
    out += b"SKEL"
    out += struct.pack("<i", len(order))
    for name in order:
        bone = bones[name]
        pos, quat, scl = _decompose(_convert_matrix(bone.transform), scale)
        _put_str(out, name)
        out += struct.pack("<i", slot.get(bone.parent, -1) if bone.parent else -1)
        _put_f32(out, pos + quat + scl)
    return bytes(out)


def _pack_clip(clip: xfile.Clip, scale: float) -> bytes:
    """Serialise one clip. Layout:

        [4]  "CLIP"
        [4]  track_count
        [4]  float32 length_seconds
        track_count x:
            str bone_name
            [4] n_pos,   n_pos   x (f32 time, 3 f32 position)
            [4] n_rot,   n_rot   x (f32 time, 4 f32 quaternion xyzw)
            [4] n_scale, n_scale x (f32 time, 3 f32 scale)

    Times are seconds, already divided by the file's tick rate. Rotations are
    Godot-convention xyzw, having gone through the same basis change as
    everything else rather than being sign-flipped by hand.
    """
    out = bytearray()
    out += b"CLIP"
    out += struct.pack("<if", len(clip.tracks), clip.duration_seconds)
    tps = clip.ticks_per_second or xfile.DEFAULT_TICKS_PER_SECOND

    for track in clip.tracks:
        _put_str(out, track.bone)

        out += struct.pack("<i", len(track.position))
        for time, (x, y, z) in track.position:
            out += struct.pack("<ffff", time / tps, x * scale, y * scale, -z * scale)

        out += struct.pack("<i", len(track.rotation))
        for time, wxyz in track.rotation:
            w, x, y, z = wxyz
            # Straight to a matrix, through the same C @ M @ C, back out. The
            # DirectX-vs-Godot conjugate question never has to be answered by
            # hand, which is exactly why it is done this way.
            m = _quat_to_matrix(-x, -y, -z, w)
            _, quat, _ = _decompose(_convert_matrix(m))
            out += struct.pack("<f", time / tps)
            _put_f32(out, quat)

        out += struct.pack("<i", len(track.scale))
        for time, (x, y, z) in track.scale:
            out += struct.pack("<ffff", time / tps, x, y, z)

    return bytes(out)


def _quat_to_matrix(x, y, z, w):
    """Quaternion -> flat row-major 4x4, row-vector convention."""
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0.0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0.0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


# --------------------------------------------------------------------------
# Pack writer
# --------------------------------------------------------------------------


class PackWriter:
    """Writes payloads to a sidecar, then stitches header + index + payloads.

    Same shape as `pz_pack.py`: the index cannot be written until every
    payload's size is known, and holding a whole export in memory is not an
    option, so payloads stream to `<out>.payload` and are copied in at close.
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.payload_path = out_path.with_suffix(out_path.suffix + ".payload")
        self.payload_path.parent.mkdir(parents=True, exist_ok=True)
        self._payload = self.payload_path.open("wb")
        self._entries: list[tuple[str, int, int, int]] = []
        self._offset = 0
        self.raw_bytes = 0
        self.stored_bytes = 0

    def add(self, entry_id: str, blob: bytes) -> None:
        packed = zlib.compress(blob, DEFLATE_LEVEL)
        self._payload.write(packed)
        self._entries.append((entry_id, self._offset, len(packed), len(blob)))
        self._offset += len(packed)
        self.raw_bytes += len(blob)
        self.stored_bytes += len(packed)

    def close(self) -> None:
        self._payload.close()

        ids = b"".join(e[0].encode("utf-8") + b"\0" for e in self._entries)
        header_len = HEADER.size + len(ids) + INDEX_ENTRY.size * len(self._entries)

        with self.out_path.open("wb") as f:
            f.write(HEADER.pack(MAGIC, VERSION, len(self._entries), len(ids)))
            f.write(ids)
            for _, offset, stored, raw in self._entries:
                f.write(INDEX_ENTRY.pack(header_len + offset, stored, raw))
            with self.payload_path.open("rb") as p:
                while chunk := p.read(1 << 20):
                    f.write(chunk)

        self.payload_path.unlink()

    @property
    def count(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


#: Rigid props (hats, glasses, jewellery) live under Static/, not Skinned/ —
#: same clothing pipeline, just no bone weights, because nothing about a cap
#: needs to deform. `Hat_BaseballCap`'s `m_MaleModel` names
#: `static\clothes\m_baseballcap`: a real mesh, parented to `Bip01_Head` via
#: `m_AttachBone`, not a body-UV texture overlay. Scanning only Skinned/ made
#: `mesh_id_for()` silently drop that reference, so 173 of 183 hats (anything
#: with no skin) fell through to the texture-overlay path meant for shirts —
#: composited full-canvas onto the body's 256x256 UV, which is what painted
#: the whole character in one flat opaque colour. `_add_skinned()` in
#: CharacterTestScene.gd already handles an empty `bones` array as a rigid
#: prop on a BoneAttachment3D; only the exporter was missing this directory.
#: Held weapons live under a third root, `models_X/weapons/{1handed,2handed,
#: firearm,parts}` (485 files) -- not `ClothingItem`-driven at all, so nothing
#: reaches these through `mesh_id_for()`. `_mine_weapon_attachments()` is a
#: separate join for the same reason: PZ resolves a weapon's rendered model
#: from `HandWeapon.getStaticModel()` (jar bytecode, `zombie/inventory/types/
#: HandWeapon`), which returns the `StaticModel` script field if set, else the
#: `WeaponSprite` field -- an opaque string with **no shipped table** to a
#: filename anywhere (not in the jar's resources, not a companion file, not
#: the `.x` file's own internal Frame/Mesh name, which always matches its OS
#: filename). Measured against the 354 real combat weapons (`Categories =
#: base:*`, excluding traps/sensors that also carry `ItemType = base:weapon`):
#: exact case-insensitive filename match gets 100 (28%); normalizing
#: punctuation and a trailing "_Hand" gets 171 (48%). The rest (`BlockMace`,
#: `Gavel`, `CarpentryChisel`, ...) have no file under any transform that's
#: justifiable rather than guessed, which reads as B42's 3D-weapon rework
#: being genuinely incomplete rather than a gap in this search. Shipping the
#: resolvable 48% and leaving the rest unmapped -- same as every other "known
#: gap" in this file -- beats a fuzzier match that would silently attach the
#: wrong weapon to an item.
MODEL_ROOTS = (("Skinned", ""), ("Static", "Static/"), ("weapons", "Weapons/"))


def _mesh_sources(game: Path, only: list[str] | None) -> list[Path]:
    files: list[Path] = []
    for name, _prefix in MODEL_ROOTS:
        root = game / "media" / "models_X" / name
        if not root.is_dir():
            raise ExportError(f"not found: {root}")
        files += sorted(root.rglob("*.x"))
    if only:
        wanted = {o.lower() for o in only}
        files = [f for f in files if f.stem.lower() in wanted]
    return files


def _clip_sources(game: Path, sets: list[str], only: list[str] | None) -> list[Path]:
    root = game / "media" / "anims_X"
    if not root.is_dir():
        raise ExportError(f"not found: {root}")
    files = []
    for name in sets:
        d = root / name
        if not d.is_dir():
            raise ExportError(f"clip set not found: {d}")
        files += sorted(d.rglob("*.x"))
    if only:
        wanted = {o.lower() for o in only}
        files = [f for f in files if f.stem.lower() in wanted]
    return files


# --------------------------------------------------------------------------
# Appearance mining (stage 2, the slice the test scene needs)
#
# Deliberately NOT read from the .x files. Each one carries a Material with a
# TextureFilename, and it is stale: `FemaleBody.x` names `FootballPants2.png`.
# It is whatever the artist last had assigned, not what the game loads. The XML
# is the authority — see docs/project/pipeline/character_assets.md § Stage 2.
# --------------------------------------------------------------------------

#: Slots the randomiser dresses, split by which of the two systems the slot
#: actually uses. PZ defines 109 body locations; this is the everyday-clothing
#: subset, and it is a test-scene decision, not a claim about what PZ considers
#: a valid outfit.
#:
#: **The split is not arbitrary and was measured, because it contradicts the
#: obvious reading.** Of 1,426 clothing items with a body location, 845 are
#: meshes and 581 (41%) are texture-only — and it divides cleanly by slot, not
#: by item:
#:
#:     hat 183/0    pants 50/0   jacket 35/0   necklace 24/0   (mesh / texture)
#:     tshirt 0/64  shirt 0/28   shortsleeveshirt 0/24  underweartop 0/21
#:
#: Torso base layers are painted onto the body's shared UV; outer layers and
#: accessories are geometry. Which makes sense — a t-shirt under a jacket does
#: not need vertices. An earlier draft of the spec listed "shirt" first among
#: the mesh-based clothing; shirts are exactly the counterexample.
#:
#: `skirt` sits in MESH_SLOTS for one item out of sixteen. The other fifteen —
#: and **all** of `dress` and `longskirt` — are texture-only, painted onto the
#: body mesh's own dress-panel UV island. That is not a gap in the export; it is
#: why the body carries a dress panel at all (see `_rasterise_dress_uvs` in
#: CharacterAssetRegistry.gd). The mesh/overlay branch below routes per item, so
#: a slot listed here can still land in either pool.
#: `mask`/`maskeyes`/`maskfull` are PZ's three face-covering locations
#: (`ItemBodyLocation.MASK` / `MASK_EYES` / `MASK_FULL` in `BodyLocations.lua`)
#: — bandanas and surgical/dust masks (`mask`), gas and hockey masks that also
#: hide the eyes (`maskeyes`), and full-face coverings like the welding mask
#: (`maskfull`). All three are `m_Static` single-mesh models attached to
#: `Bip01_Head`, the same shape as `hat`/`eyes`, and PZ's own exclusivity pairs
#: (mask vs. eyes/glasses, etc.) are mined generically from `BodyLocations.lua`
#: once the slot is in `DRESS_SLOTS`. `ItemBodyLocation.FULL_HAT` (NBC suits,
#: helmets) is a broader head-occlusion category, not a mask, and stays out.
#: `back`/`fannypackfront`/`fannypackback`/`satchel`/`webbing`/`ammostrap`/
#: `shoulderholster` are PZ's worn-container locations -- backpacks, bags,
#: fanny packs, tactical webbing pouches, ammo straps and holsters. They are
#: `ItemType = base:container`, not `base:clothing` (see `_item_scripts()`'s
#: `CanBeEquipped` fallback), but render through the same `ClothingItem` mesh/
#: texture link, so once resolvable they need nothing else here: a backpack
#: like `Bag_ALICEpack` is `m_Static: false`, a real skinned mesh bound to the
#: spine/shoulders exactly like a jacket, so it goes through the same
#: `_build_skinned_mesh` path with no special-casing.
MESH_SLOTS = ("pants", "shortpants", "shortsshort", "skirt", "shoes",
              "sweater", "jacket", "hat", "eyes", "mask", "maskeyes", "maskfull",
              "back", "fannypackfront", "fannypackback", "satchel", "webbing",
              "ammostrap", "shoulderholster")
OVERLAY_SLOTS = ("underweartop", "underwearbottom", "tanktop", "tshirt",
                 "shortsleeveshirt", "shirt", "socks", "dress", "longskirt")
DRESS_SLOTS = MESH_SLOTS + OVERLAY_SLOTS

#: B42's skin tones: `textures/Body/MaleBody01..05.png` and `FemaleBody01..05`.
#:
#: **Five, not nine, and not the `Bob_*_Body` files.** This originally read
#: `media/textures/Bob_Body.png` + `Bob_2_Body` … `Bob_8_Body`, which are Build
#: 41 leftovers the B42 jar never names — `projectzomboid.jar` contains no
#: string or string-concat recipe producing "Bob_<n>_Body" anywhere. They are
#: 256x128 indexed PNGs whose content is one 128x128 image mirrored across the
#: width (left and right halves correlate at 1.000), on a UV layout that is not
#: the one `MaleBody.x` uses — which is exactly the dark streaking that got
#: reported, arms and torso shading landing over the wrong body parts. Bob_5 and
#: Bob_7 are byte-identical, as are Bob_6 and Bob_8, so the set was never nine
#: tones to begin with.
#:
#: Five is the game's own count: `CharacterCreationMain.lua` builds a 5-entry
#: `self.skinColors` table and passes `self.colorPickerSkin.index - 1` straight
#: into `HumanVisual:setSkinTextureIndex()`. The `*a` siblings
#: (`MaleBody01a.png`) are the chest-hair variants behind that screen's chest
#: hair tickbox, and are not tones — do not fold them into this list.
SKIN_TONE_COUNT = 5

#: Where the tones live, per gender. `%s` takes a zero-padded 1-based index.
SKIN_TONE_PATTERN = {"male": "textures/Body/MaleBody%02d.png",
                     "female": "textures/Body/FemaleBody%02d.png"}

#: PZ's 5 zombie body textures — gender-neutral (no `FemaleZombie*` variant
#: exists), and unlike the player tones they sit directly under
#: `textures/`, not `textures/Body/`, and index 1-based with NO leading
#: zero (`Zombie_Body1.png`, not `Zombie_Body01.png`). Appended to the END
#: of each gender's `skin_tones` list in `_mine_appearance` so index 0 stays
#: the canonical player tone `_body_alpha_stencil()` mirrors the skirt
#: cutout from.
ZOMBIE_SKIN_COUNT = 5
ZOMBIE_SKIN_PATTERN = "textures/Zombie_Body%d.png"

#: `<m_Masks>` id -> mask filename, and the folder they are read from.
#:
#: **This is how PZ stops skin showing through a garment, and it is a texture
#: operation, not a geometry one.** There is no hide-flag on the item and no
#: hide-bone on the body — both were searched for and neither exists. Each mask
#: is a 256x256 PNG, the same size as the body texture, where `alpha > 0` marks
#: the texels to erase; the RGB is an arbitrary per-region tint carrying no
#: meaning. See docs/project/pipeline/character_assets.md § Body masks.
#:
#: **The enum is derived, not read** — PZ ships no table for it. It was
#: recovered by cross-tabulating which ids appear on which kind of garment
#: across every `clothingItems` XML: gloves pin the hands, jackets the arms,
#: trousers the legs. The pattern is interleaved left/right over ids 3-10.
#:
#: Ids 2 and 11 are deliberately absent. Both candidate readings (`Dress` /
#: `Mask`) give an absurd result for one garment class, so the enum is wrong
#: somewhere or there is a region not present in this folder. Neither id affects
#: trousers — `Trousers_*` is `{7, 9, 14, 15}` — so they are skipped rather than
#: guessed at, and `_mine_appearance` counts them for the run summary.
#:
#: `Mask.png` is excluded on purpose: it is a colour-key legend holding every
#: region at once and covers 85.5% of the sheet. Treating it as a region would
#: erase nearly the whole body.
BODY_MASK_REGIONS = {
    0: "Head",
    3: "LeftArm",   4: "LeftHand",   5: "RightArm",  6: "RightHand",
    7: "LeftLeg",   8: "LeftFoot",   9: "RightLeg",  10: "RightFoot",
    12: "Chest",    13: "Waist",     14: "Belt",     15: "Crotch",
}
DEFAULT_MASK_FOLDER = "textures/Body/Masks"

#: The 18 body-part hole/blood masks PZ actually ships, at
#: `textures/HoleTextures/BloodMask<region>.png` — one per
#: `BloodBodyPartType`, finer-grained than `BODY_MASK_REGIONS` above (which
#: has one `LeftArm`/`RightArm` where this has an upper/lower split each,
#: and no `Back`/`Neck`/`Stomach`/`Groin` region at all). These are a
#: **separate vocabulary for a separate job**: `BODY_MASK_REGIONS` answers
#: "which skin region does this garment's own art cover, so erase it";
#: `DAMAGE_REGIONS` answers "where can a wound/tear appear". Do not try to
#: unify them.
DAMAGE_REGIONS = (
    "Back", "Chest", "FootL", "FootR", "Groin", "HandL", "HandR", "Head",
    "LArmL", "LArmR", "LLegL", "LLegR", "Neck", "Stomach",
    "UArmL", "UArmR", "ULegL", "ULegR",
)


def _item_scripts(game: Path) -> dict:
    """Item type -> `{"clothing": ClothingItem name, "location": body location}`.

    Both directions are needed and they are not the same name. The clothing XML
    and the appearance pools are keyed by `ClothingItem` (`Underwear_FrillyBra_
    Straps_Pink`), while every Lua table that *chooses* an item names the item
    type instead (`Bra_Straps_FrillyPink`). Resolving one to the other is what
    lets PZ's own outfit and underwear tables be read against our pools.

    A backpack, bag or webbing pouch is `ItemType = base:container`, not
    `base:clothing`, and carries no `BodyLocation` at all -- it declares where
    it can be worn with `CanBeEquipped` instead (`base:back`, `base:satchel`,
    ...). Same enum-derived string space as `BodyLocation`, same
    `ClothingItem` link to a mesh/texture in `clothing/clothingItems/`, just a
    different key, so falling back to it here is enough to make every
    container-slot item resolvable the same way a garment is. `clothing.txt`
    never sets `CanBeEquipped`, so this cannot shadow a real `BodyLocation`.
    """
    import re
    out = {}
    for path in sorted((game / "media" / "scripts" / "generated" / "items").glob("*.txt")):
        text = path.read_text(encoding="latin-1")
        for m in re.finditer(r"item\s+(\w+)\s*\{(.*?)\n    \}", text, re.S):
            body = m.group(2)
            ci = re.search(r"ClothingItem\s*=\s*([\w.]+)", body)
            bl = (re.search(r"BodyLocation\s*=\s*(?:base:)?(\w+)", body) or
                  re.search(r"CanBeEquipped\s*=\s*(?:base:)?(\w+)", body))
            if bl:
                out[m.group(1)] = {"clothing": ci.group(1) if ci else None,
                                   "location": bl.group(1).lower()}
    return out


def _item_body_locations(game: Path) -> dict:
    """`ClothingItem` name -> body location, from PZ's own item scripts."""
    return {e["clothing"]: e["location"]
            for e in _item_scripts(game).values() if e["clothing"]}


# --------------------------------------------------------------------------
# PZ's own clothing rules
#
# **These are mined, never authored.** Every rule below exists in the game's
# data, and the whole point of reading it is that the obvious guess is wrong
# often enough to matter: PZ's default character generation equips no sweater
# and no jacket (both commented out), no male ever gets a `Skirt` key, and the
# render order that decides which painted layer wins is the *declaration* order
# in BodyLocations.lua, not anything alphabetical.
#
# Three tables, three files:
#   BodyLocations.lua               render order + setExclusive
#   ClothingSelectionDefinitions    which slots get dressed, per gender
#   UnderwearDefinition.lua         gendered, weighted, matched underwear sets
# --------------------------------------------------------------------------

#: Sweater and Jacket are commented out of `ClothingSelectionDefinitions.default`
#: — PZ's character generator dresses neither. That is PZ being right about
#: layering and wrong for this scene, whose entire job is proving mesh garments
#: bind to the shared skeleton. So they are re-enabled here at the chance the
#: commented-out block itself carried, as **one** outerwear roll rather than two
#: independent ones: a sweater *and* a jacket is a combination PZ never builds,
#: and two mesh garments over one torso clip through each other.
#:
#: This is the only clothing rule in this file that is not PZ's own. Everything
#: else is read from the game.
OUTERWEAR_GROUP = ("sweater", "jacket")
OUTERWEAR_CHANCE = 30


def _location_resolver(items: dict):
    """`ItemBodyLocation.SHORT_SLEEVE_SHIRT` -> `"shortsleeveshirt"`.

    The Lua tables name locations by enum constant; item scripts and our pools
    name them by the `BodyLocation=` string. PZ ships no table joining the two,
    but the enum name is the location with underscores inserted, so squashing
    them and lowercasing recovers it. Verified across the whole set: 114
    declared locations, **zero** collisions after squashing, and the only ones
    that fail to resolve are the 5 no item in the game uses.
    """
    squashed = {}
    for entry in items.values():
        loc = entry["location"]
        squashed[loc.replace("_", "")] = loc
    return lambda enum: squashed.get(enum.lower().replace("_", ""))


def _mine_clothing_rules(game: Path, items: dict) -> dict:
    """PZ's render order, exclusivity, outfit tables and underwear sets."""
    import re
    media = game / "media"
    resolve = _location_resolver(items)
    by_clothing = {e["clothing"]: e for e in items.values() if e["clothing"]}
    rules: dict = {"render_order": [], "exclusive": [], "outfits": {},
                   "underwear": {"base_chance": 0, "sets": []}}

    def to_clothing(item_type: str) -> str | None:
        """`Base.Bra_Straps_FrillyPink` -> the item's ClothingItem name."""
        short = item_type.split(".")[-1]
        entry = items.get(short)
        if entry and entry["clothing"]:
            return entry["clothing"]
        # A few tables name the ClothingItem directly.
        return short if short in by_clothing else None

    # --- BodyLocations.lua: render order, then exclusivity ---
    lua_path = media / "lua" / "shared" / "NPCs" / "BodyLocations.lua"
    if lua_path.exists():
        lua = lua_path.read_text(encoding="latin-1")
        # "Locations must be declared in render-order" — the file's own first
        # line. This is the order painted layers composite in, and getting it
        # wrong puts underwear on top of a shirt.
        for enum in re.findall(r"getOrCreateLocation\(ItemBodyLocation\.(\w+)\)", lua):
            loc = resolve(enum)
            if loc in DRESS_SLOTS and loc not in rules["render_order"]:
                rules["render_order"].append(loc)

        seen = set()
        for a, b in re.findall(
                r"setExclusive\(\s*ItemBodyLocation\.(\w+)\s*,\s*"
                r"ItemBodyLocation\.(\w+)\s*\)", lua):
            ra, rb = resolve(a), resolve(b)
            if ra in DRESS_SLOTS and rb in DRESS_SLOTS and ra != rb:
                pair = tuple(sorted((ra, rb)))
                if pair not in seen:
                    seen.add(pair)
                    rules["exclusive"].append(list(pair))
        rules["exclusive"].sort()

    # --- ClothingSelectionDefinitions.lua: the default outfit, per gender ---
    csd_path = media / "lua" / "shared" / "Definitions" / "ClothingSelectionDefinitions.lua"
    unshipped: dict[str, int] = {}
    if csd_path.exists():
        csd = csd_path.read_text(encoding="latin-1")
        block = re.search(r"ClothingSelectionDefinitions\.default\s*=\s*\{(.*?)\n\}",
                          csd, re.S)
        for gender in ("male", "female"):
            table: dict = {}
            g = re.search(rf"\n\t{gender.capitalize()}\s*=\s*\{{(.*?)\n\t\}}",
                          block.group(1), re.S) if block else None
            if g:
                for m in re.finditer(r"\n\t\t(\w+)\s*=\s*\{(.*?)\n\t\t\}",
                                     g.group(1), re.S):
                    loc = m.group(1).lower()
                    body = m.group(2)
                    chance = re.search(r"chance\s*=\s*(\d+)", body)
                    listed = re.search(r"items\s*=\s*\{(.*?)\}", body, re.S)
                    names = re.findall(r'"([\w.]+)"', listed.group(1)) if listed else []
                    if loc not in DRESS_SLOTS:
                        unshipped[loc] = unshipped.get(loc, 0) + 1
                        continue
                    resolved = []
                    for n in names:
                        ci = to_clothing(n)
                        if ci and ci not in resolved:
                            resolved.append(ci)
                    table[loc] = {"chance": int(chance.group(1)) if chance else 100,
                                  "items": resolved}
            # See OUTERWEAR_GROUP: ours, not PZ's, and flagged as such.
            table["__outerwear__"] = {"chance": OUTERWEAR_CHANCE,
                                      "slots": list(OUTERWEAR_GROUP)}
            rules["outfits"][gender] = table

    # --- UnderwearDefinition.lua: matched sets, gendered and weighted ---
    uw_path = media / "lua" / "shared" / "Definitions" / "UnderwearDefinition.lua"
    if uw_path.exists():
        uw = uw_path.read_text(encoding="latin-1")
        base = re.search(r"UnderwearDefinition\.baseChance\s*=\s*(\d+)", uw)
        rules["underwear"]["base_chance"] = int(base.group(1)) if base else 100
        for m in re.finditer(r"UnderwearDefinition\.(\w+)\s*=\s*\{(.*?)\n\}", uw, re.S):
            name, body = m.group(1), m.group(2)
            if name == "baseChance":
                continue
            gender = re.search(r'gender\s*=\s*"(\w+)"', body)
            weight = re.search(r"chanceToSpawn\s*=\s*(\d+)", body)
            tops = []
            for t in re.finditer(r'\{\s*name\s*=\s*"([\w.]+)"\s*,\s*chance\s*=\s*(\d+)', body):
                ci = to_clothing(t.group(1))
                if ci:
                    tops.append({"item": ci, "chance": int(t.group(2))})
            bottom = re.search(r'bottom\s*=\s*"([\w.]+)"', body)
            bottom_ci = to_clothing(bottom.group(1)) if bottom else None
            if not tops and not bottom_ci:
                continue
            rules["underwear"]["sets"].append({
                "name": name,
                # An unmarked set is worn by either gender; only the female ones
                # carry the field in PZ's own data.
                "gender": gender.group(1).lower() if gender else "",
                "weight": int(weight.group(1)) if weight else 1,
                "top": tops,
                "bottom": bottom_ci,
            })

    rules["_unshipped_outfit_slots"] = sorted(unshipped)
    return rules


def _weapon_model_lookup(mesh_ids: set) -> tuple[dict, dict]:
    """Two id lookups over the packed `Weapons/...` meshes: exact lowercased
    filename, and a normalized form (punctuation stripped, a trailing "_Hand"
    dropped) for the `_Hand`/underscore-inconsistent stems -- see MODEL_ROOTS'
    doc comment for why both are needed and what they still miss.

    A normalized key can collide (`BallPeenHammer.x` and
    `BallPeenHammer_Hand.x` both normalize to "ballpeenhammer"); the tie goes
    to whichever isn't in `parts/` (a weapon attachment, not a held weapon)
    and, failing that, whichever filename doesn't itself end in "_Hand".
    """
    import re

    def norm(stem: str) -> str:
        s = re.sub(r"[^a-z0-9]", "", stem.lower())
        return s[:-4] if s.endswith("hand") else s

    exact: dict[str, str] = {}
    fuzzy: dict[str, str] = {}
    for mesh_id in mesh_ids:
        if not mesh_id.startswith("Weapons/"):
            continue
        stem = mesh_id.rsplit("/", 1)[-1]
        exact.setdefault(stem.lower(), mesh_id)

        key = norm(stem)
        rank = (mesh_id.startswith("Weapons/parts/"), stem.lower().endswith("_hand"))
        current = fuzzy.get(key)
        if current is None:
            fuzzy[key] = mesh_id
        else:
            cur_stem = current.rsplit("/", 1)[-1]
            cur_rank = (current.startswith("Weapons/parts/"), cur_stem.lower().endswith("_hand"))
            if rank < cur_rank:
                fuzzy[key] = mesh_id
    return exact, fuzzy


def _weapon_texture(media: Path, mesh_id: str) -> str | None:
    """`Weapons/2handed/FireAxe` -> `textures/weapons/2handed/FireAxe.png`.

    The `.x` file's own embedded `TextureFilename` (e.g. FireAxe.x names
    "Objects_Fireaxe.png") is a stale export artifact that matches no file
    anywhere in the install -- checked directly, not assumed. The real art
    mirrors the mesh path one folder over, `models_X/weapons/` ->
    `textures/weapons/`, with the same trailing-"_Hand" inconsistency as the
    mesh filenames themselves (`Bone_Club_Hand.x` pairs with `Bone_Club.png`,
    no "_Hand"). Measured on the 171 mined weapons: 92 match the mesh stem
    exactly, 32 more after dropping a trailing "_Hand"; the remaining ~47
    (`Firewood_Hand` -> `Firewood_Variations.png`, ...) rename outright and
    are left textureless rather than guessed at, same policy as the mesh
    join itself.
    """
    rel = mesh_id[len("Weapons/"):]
    candidates = [rel]
    if rel.lower().endswith("_hand"):
        candidates.append(rel[:-len("_Hand")])
    for c in candidates:
        tex = f"textures/weapons/{c}.png"
        if (media / tex).exists():
            return tex
    return None


_WEAPON_MODEL_BLOCK_RE = re.compile(r"model\s+(\w+)\s*\n\s*\{(.*?)\n    \}", re.S)
_WEAPON_ATTACH_BLOCK_RE = re.compile(r"attachment\s+(\S+)\s*\n\s*\{(.*?)\n        \}", re.S)
_VEC3_RE = r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)"


def _parse_weapon_model_attachments(media: Path) -> dict:
    """`media/scripts/generated/models_weapons.txt` -> `{model_name: {"mesh":
    "weapons/.../Stem" or None, "attachments": {bone: (offset_xyz,
    rotate_xyz_degrees)}}}`.

    This is PZ's own held-weapon pose data -- one `model <Name> { mesh = ...,
    attachment <bone> { offset = x y z, rotate = x y z } }` block per
    `WeaponSprite` name (joined in `_mine_weapon_attachments()`), sitting
    alongside a `world` attachment (the dropped/ground pose, not used here)
    and, for dual-wield weapons, both `Bip01_Prop1` and `Bip01_Prop2`.
    Confirmed by reading the shipped file directly: FireAxe's `Bip01_Prop2`
    block is `offset 0 0 0`, `rotate 180 -86.0322 180`.

    `attachments` keeps only `Bip01_*` sockets -- a firearm's block also
    declares `attachment muzzle { ... }` for the muzzle-flash point, which is
    not a hand-bone grip correction and would otherwise sit in this dict
    looking like one.

    `mesh` and `texture` are this data's second, more valuable use: they are
    PZ's own WeaponSprite-name -> mesh-path / texture-path join, and for a
    real chunk of the B42 firearm set they are the *only* correct join --
    `DoubleBarrelShotgun` (the WeaponSprite/item name) meshes to
    `weapons/firearm/JS_2000` but textures to `weapons/firearm/
    DoubleBarrelShotgun` (its own name, not the mesh's), and `L94_Rifle`
    meshes to `weapons/firearm/L92_Carbine` (it shares a mesh with the
    unrelated L92_Carbine item, distinguished only by texture) but textures
    to `weapons/firearm/L94_Rifle` (its own name again). Filename fuzzy
    matching against either field independently gets these wrong or misses
    them outright. See `_mine_weapon_attachments()`, which tries both before
    falling back to the exact/fuzzy filename lookup.

    Empty on any failure -- an install without this file (or a future PZ
    version that renames it) degrades to no weapon pose/mesh data, the same
    as before this existed, rather than raising.
    """
    path = media / "scripts" / "generated" / "models_weapons.txt"
    if not path.exists():
        return {}
    text = path.read_text(encoding="latin-1")
    out: dict[str, dict] = {}
    for m in _WEAPON_MODEL_BLOCK_RE.finditer(text):
        name, body = m.group(1), m.group(2)
        mesh_m = re.search(r"mesh\s*=\s*([\w/\-]+)", body)
        tex_m = re.search(r"texture\s*=\s*([\w/\-]+)", body)
        attachments = {}
        for am in _WEAPON_ATTACH_BLOCK_RE.finditer(body):
            bone, ab = am.group(1), am.group(2)
            if not bone.startswith("Bip01"):
                continue  # "muzzle" etc -- not a hand-bone socket
            off = re.search(r"offset\s*=\s*" + _VEC3_RE, ab)
            rot = re.search(r"rotate\s*=\s*" + _VEC3_RE, ab)
            if off and rot:
                attachments[bone] = (
                    tuple(float(g) for g in off.groups()),
                    tuple(float(g) for g in rot.groups()),
                )
        out[name] = {
            "mesh": mesh_m.group(1) if mesh_m else None,
            "texture": tex_m.group(1) if tex_m else None,
            "attachments": attachments,
        }
    return out


def _weapon_euler_deg_matrix(rx: float, ry: float, rz: float):
    """Row-major 4x4 rotation, row-vector convention -- matches `_matmul`
    and every other transform in this file (see `_decompose`'s row-reads).

    Composes as `v @ Rz(-rz) @ Ry(-ry) @ Rx(-rx)` -- the *inverse* of the
    naive `v @ Rx(rx) @ Ry(ry) @ Rz(rz)` reading of PZ's `rotate = x y z`
    field (X first, then Y, then Z, the classic `glRotatef` stacking order).

    **The naive reading was tried first and was visibly wrong**: on FireAxe
    (`Bip01_Prop2`, offset zero, rotate `180 -86.0322 180`) it held the axe
    with the blade drooping away from the body; the correct pose (checked in
    a running scene, held resting against the shoulder, matching a
    hand-drawn reference of where the haft should sit) is exactly this
    transform's *inverse* -- since a rotation matrix's inverse is its
    transpose, `(Rx@Ry@Rz)^T = Rz(-rz)@Ry(-ry)@Rx(-rx)`, which is what this
    builds directly rather than transposing after the fact. There is still
    no decompiled evidence for *why* PZ's own convention comes out inverted
    relative to the obvious glRotatef reading -- this is an empirical fix,
    not a derived one. Re-check against another weapon with three genuinely
    independent non-180 angles if one is ever found; FireAxe's rotate
    reduces to a near-single-axis rotation and can't fully rule out a
    per-axis sign error hiding inside this fix.
    """
    rxr, ryr, rzr = math.radians(-rx), math.radians(-ry), math.radians(-rz)
    cx, sx = math.cos(rxr), math.sin(rxr)
    cy, sy = math.cos(ryr), math.sin(ryr)
    cz, sz = math.cos(rzr), math.sin(rzr)
    rx_m = (1.0, 0.0, 0.0, 0.0, 0.0, cx, sx, 0.0, 0.0, -sx, cx, 0.0, 0.0, 0.0, 0.0, 1.0)
    ry_m = (cy, 0.0, -sy, 0.0, 0.0, 1.0, 0.0, 0.0, sy, 0.0, cy, 0.0, 0.0, 0.0, 0.0, 1.0)
    rz_m = (cz, sz, 0.0, 0.0, -sz, cz, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    return _matmul(_matmul(rz_m, ry_m), rx_m)


def _weapon_attach_transform(offset, rotate, scale: float):
    """PZ's `(offset, rotate)` pair -> `(position, quaternion_xyzw)` in this
    pack's already-converted Godot skeleton space.

    Run through the same `_convert_matrix`/`_decompose` change of basis
    every bone rest transform and skinned mesh in this file goes through --
    i.e. this treats PZ's attachment numbers as living in the same raw,
    pre-conversion `.x` space as everything else here, rather than
    inventing a second, independent coordinate convention. That is the
    working assumption; it has been checked against FireAxe (`Bip01_Prop2`,
    offset zero, rotate `180 -86.0322 180`) in a running scene, not derived
    from PZ source.
    """
    m = list(_weapon_euler_deg_matrix(*rotate))
    m[12], m[13], m[14] = offset
    pos, quat, _scl = _decompose(_convert_matrix(tuple(m)), scale)
    return pos, quat


def _mine_weapon_attachments(media: Path, mesh_ids: set, textures: set, scale: float) -> list[dict]:
    """Held weapons for the `weapon` appearance slot -- see MODEL_ROOTS' doc
    comment for the join this is doing and why it only ever resolves a
    fraction of the item list. Ships as a `pools["clothing"]["weapon"]` entry
    per resolved item, identical in shape to a garment entry plus
    `attachments` -- one `{"offset": [...], "rotate": [...]}` per socket id
    the model actually declares in `models_weapons.txt` (usually just
    `Bip01_Prop2`; 10 of 294 shipped models also/only declare
    `Bip01_Prop1`), NOT a single flattened pick.

    This is deliberately per-socket, not "the" attach bone, because PZ's own
    binding is per-*hand*, not per-item: `ModelManager.java` binds a
    primary-hand item to `Bip01_Prop1` and a secondary-hand item to
    `Bip01_Prop2`, and `ModelScript.getAttachmentById` is an exact-id hash
    lookup with **no fallback** -- an item bound to a bone it declares no
    block for gets no grip correction at all, just the bone's own animated
    pose. A prior revision of this function picked one bone via a fixed
    preference order (`Bip01_Prop2` first) and shipped a single flattened
    `attach_offset`/`attach_rotate`, which is backwards: it read "which
    socket id does this item declare" as "which bone is this item bound to,"
    when those are two independent things PZ only ever composes together on
    an exact match. See docs/weapon_attachment.md for the full derivation
    (decompiled Java + raw `.x` clip evidence). The consumer (Blender add-on
    / Godot runtime) is responsible for picking the bind bone per hand and
    looking up `attachments.get(that_bone)`, treating a miss as identity --
    not for guessing at a rotation that was never PZ's to begin with.
    """
    weapon_models = _parse_weapon_model_attachments(media)
    exact, fuzzy = _weapon_model_lookup(mesh_ids)
    if not exact and not fuzzy:
        return []

    def resolve(key: str) -> str | None:
        return exact.get(key.lower()) or fuzzy.get(re.sub(r"[^a-z0-9]", "", key.lower()))

    path = media / "scripts" / "generated" / "items" / "weapon.txt"
    if not path.exists():
        return []
    text = path.read_text(encoding="latin-1")
    entries = []
    for m in re.finditer(r"item\s+(\w+)\s*\{(.*?)\n    \}", text, re.S):
        item, body = m.group(1), m.group(2)
        if "ItemType = base:weapon" not in body:
            continue
        if "Categories = base:" not in body and "SubCategory = Firearm" not in body:
            # traps/sensors/noisemakers also carry base:weapon, and have
            # neither field. B42's firearms (Pistol, Shotgun, AssaultRifle,
            # ...) are the opposite case this comment used to miss: no
            # Categories at all, only SubCategory = Firearm -- measured
            # 2026-09-05, 18 of PZ's own guns were being silently dropped
            # here alongside the traps. Two firearm-tagged items really are
            # toys with no held model (Revolver_CapGun, Rifle_CapGun -- no
            # `model` block in models_weapons.txt, no StaticModel, 2D sprite
            # only) and correctly fall through mesh_id resolution below.
            continue
        candidates = []
        sm = re.search(r"StaticModel\s*=\s*([\w.]+)", body)
        ws = re.search(r"WeaponSprite\s*=\s*([\w.]+)", body)
        # PZ's own WeaponSprite -> mesh join (models_weapons.txt's `mesh =`
        # field) goes first: it is authoritative where filename fuzzy-match
        # is not just incomplete but wrong. `DoubleBarrelShotgun` (item and
        # WeaponSprite name) actually meshes to `weapons/firearm/JS_2000` --
        # nothing named "DoubleBarrelShotgun" exists on disk -- and
        # `L94_Rifle` meshes to `weapons/firearm/L92_Carbine`, sharing a mesh
        # with the unrelated L92_Carbine item and differing only by texture.
        # Both would resolve to nothing, or to the wrong file, off filename
        # matching alone.
        if ws:
            model_mesh = weapon_models.get(ws.group(1), {}).get("mesh")
            if model_mesh:
                candidates.append(model_mesh.rsplit("/", 1)[-1])
        if sm:
            candidates.append(sm.group(1))
        elif ws:
            candidates.append(ws.group(1))
        wsbi = re.search(r"WeaponSpritesByIndex\s*=\s*([\w.;]+)", body)
        if wsbi:
            candidates.extend(wsbi.group(1).split(";"))
        mesh_id = next((r for r in (resolve(c) for c in candidates) if r), None)
        if mesh_id is None:
            continue

        # Same authority-first order as the mesh join above: a model-declared
        # `texture =` (when it exists on disk) beats the mesh-stem guess,
        # because several firearms' art is named after the *item*, not the
        # mesh they share (see `_parse_weapon_model_attachments`'s docstring).
        tex = None
        model_texture = weapon_models.get(ws.group(1), {}).get("texture") if ws else None
        if model_texture:
            cand = f"textures/{model_texture}.png"
            if (media / cand).exists():
                tex = cand
        if not tex:
            tex = _weapon_texture(media, mesh_id)
        if tex:
            textures.add(tex)

        # The held-pose join is by `WeaponSprite` specifically, not whichever
        # candidate happened to resolve the mesh (`StaticModel`, if present,
        # wins mesh resolution above but names a different, dropped-item
        # model in some entries) -- `models_weapons.txt`'s `model <Name>`
        # blocks are keyed by the WeaponSprite string, confirmed against
        # FireAxe/SaucePan by hand.
        attachments = {}
        if ws:
            model_attach = weapon_models.get(ws.group(1), {}).get("attachments", {})
            for bone_id, (offset, rotate) in model_attach.items():
                pos, quat = _weapon_attach_transform(offset, rotate, scale)
                attachments[bone_id] = {"offset": list(pos), "rotate": list(quat)}

        entries.append({
            "item": item, "male": mesh_id, "female": mesh_id,
            "textures": [tex] if tex else [], "masks": [],
            "attachments": attachments,
        })
    return entries


def _mine_appearance(game: Path, mesh_ids: set, scale: float) -> tuple[dict, set]:
    """Build the appearance pools, and the set of texture paths they reference.

    Only entries whose mesh actually made it into the pack are kept — the pools
    are what `random_appearance()` samples, so an id in here that the registry
    cannot load is a runtime error waiting to happen rather than a note.
    """
    media = game / "media"
    scripts = _item_scripts(game)
    locations = {e["clothing"]: e["location"]
                 for e in scripts.values() if e["clothing"]}
    textures: set[str] = set()

    def mesh_id_for(model_path: str) -> str | None:
        """`media\\models_X\\Skinned\\Clothes\\Bob_X.X` -> `Clothes/Bob_X`, or
        `static\\clothes\\m_baseballcap` -> `Static/Clothes/m_baseballcap` —
        mirrors the id `_mesh_id()` gave that same file at export time, see
        `MODEL_ROOTS`."""
        if not model_path.strip():
            return None
        p = model_path.replace("\\", "/").lower()
        for root_name, id_prefix in MODEL_ROOTS:
            marker = root_name.lower() + "/"
            if marker not in p:
                continue
            rel = p.split(marker, 1)[1]
            if rel.endswith(".x"):
                rel = rel[:-2]
            rel = id_prefix.lower() + rel
            for known in mesh_ids:
                if known.lower() == rel:
                    return known
        return None

    pools: dict = {"clothing": {}, "overlays": {}, "hair": {"male": [], "female": []},
                   "beard": [], "body": {},
                   "mesh_slots": list(MESH_SLOTS),
                   "overlay_slots": list(OVERLAY_SLOTS)}

    # --- bodies and skin tones ---
    for gender, mesh in (("male", "MaleBody"), ("female", "FemaleBody")):
        tones = []
        for pattern in (SKIN_TONE_PATTERN[gender], SKIN_TONE_PATTERN["male"]):
            for i in range(1, SKIN_TONE_COUNT + 1):
                rel = pattern % i
                if (media / rel).exists():
                    tones.append(rel)
                    textures.add(rel)
            # PZ's female body uses the male texture set where the Female*
            # variants are absent; fall back rather than ship an empty pool.
            if tones:
                break
        # Zombie skins, appended last so they never shift index 0.
        for i in range(1, ZOMBIE_SKIN_COUNT + 1):
            rel = ZOMBIE_SKIN_PATTERN % i
            if (media / rel).exists():
                tones.append(rel)
                textures.add(rel)
        pools["body"][gender] = {"mesh": mesh if mesh in mesh_ids else None,
                                 "skin_tones": tones}

    # --- hair and beards ---
    for xml_name, dest in (("hairStyles.xml", "hair"), ("beardStyles.xml", "beard")):
        path = media / "hairStyles" / xml_name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig")
        # hairStyles.xml groups by <male>/<female>; beardStyles.xml has no
        # gender split at all and uses <style>. Same fields either way.
        pattern = r"<(male|female)>(.*?)</\1>" if dest == "hair" else r"<(style)>(.*?)</\1>"
        for m in re.finditer(pattern, text, re.S):
            gender, body = m.group(1), m.group(2)
            model = re.search(r"<model>([^<]*)</model>", body)
            name = re.search(r"<name>([^<]*)</name>", body)
            tex = re.search(r"<texture>([^<]*)</texture>", body)
            mid = mesh_id_for(model.group(1)) if model else None
            if not mid:
                continue  # bald, or a style whose mesh is not in the pack
            entry = {"id": mid, "name": name.group(1) if name else mid}
            if tex and tex.group(1).strip():
                rel = f"textures/{tex.group(1).strip()}.png"
                if (media / rel).exists():
                    entry["texture"] = rel
                    textures.add(rel)
            if dest == "hair":
                pools["hair"][gender].append(entry)
            else:
                pools["beard"].append(entry)

    # --- clothing ---
    unknown_mask_ids: dict[int, int] = {}
    for path in sorted((media / "clothing" / "clothingItems").glob("*.xml")):
        item = path.stem
        text = path.read_text(encoding="utf-8-sig")

        def tag(name):
            m = re.search(rf"<{name}>([^<]*)</{name}>", text)
            return m.group(1) if m else ""

        def mask_list():
            """`<m_Masks>` ids -> body-texture mask paths, via `<m_MasksFolder>`.

            The folder picks the *shape family* of the region, not the region:
            `Jacket/Masks` and `Jacket/Masks_Leather` both hold a `Chest.png`,
            which is how an open jacket masks less of the chest than a zipped
            one. It falls back to the default set per region rather than per
            item, since a folder can carry only some of the regions an item
            names.
            """
            raw = tag("m_MasksFolder").strip().replace("\\", "/")
            folder = ""
            if raw and raw.lower() != "none":
                folder = raw[len("media/"):] if raw.startswith("media/") else raw
            out = []
            for m in re.finditer(r"<m_Masks>(\d+)</m_Masks>", text):
                mid = int(m.group(1))
                region = BODY_MASK_REGIONS.get(mid)
                if region is None:
                    unknown_mask_ids[mid] = unknown_mask_ids.get(mid, 0) + 1
                    continue
                for base in (folder, DEFAULT_MASK_FOLDER):
                    if not base:
                        continue
                    src = f"{base}/{region}.png"
                    if not (media / src).exists():
                        continue
                    # Copied into a namespace this exporter owns outright rather
                    # than mirroring the source tree. PZ spells the same folder
                    # two ways -- `m_MasksFolder` says "Clothes/Jacket" while
                    # `textureChoices` says "clothes\jacket" -- and on Windows
                    # whichever spelling is written first fixes the directory's
                    # real case, so the other one loads with a case-mismatch
                    # warning and would fail outright on a case-sensitive
                    # filesystem. A flat lowercase key sidesteps it entirely.
                    key = base.replace("textures/", "", 1).strip("/")
                    key = key.lower().replace("/", "_")
                    rel = f"textures/masks/{key}/{region}.png"
                    if rel not in out:
                        out.append(rel)
                        textures.add((src, rel))
                    break
            return out

        def flag(name):
            """A boolean `<m_Foo>` tag, PZ-style (`true`/`false` text)."""
            return tag(name).strip().lower() == "true"

        slot = locations.get(item)
        if slot not in DRESS_SLOTS:
            continue

        def texture_list(*tags):
            out = []
            for t in tags:
                for m in re.finditer(rf"<{t}>([^<]*)</{t}>", text):
                    raw = m.group(1).strip()
                    if not raw:
                        continue
                    rel = "textures/" + raw.replace("\\", "/") + ".png"
                    if (media / rel).exists():
                        out.append(rel)
                        textures.add(rel)
            return out

        male = mesh_id_for(tag("m_MaleModel"))
        female = mesh_id_for(tag("m_FemaleModel"))

        if male or female:
            entry = {
                "item": item,
                "male": male,
                "female": female,
                "textures": texture_list("textureChoices", "m_BaseTextures"),
                "bone": tag("m_AttachBone") or None,
                # Only the mesh class carries masks. A texture-overlay garment is
                # painted onto the body's own UV, so masking its region would
                # erase the very layer it just drew; masks exist because
                # *geometry* covers the body.
                "masks": mask_list(),
            }
            # PZ's own "this garment has a colour picker" flag. `ItemVisual.
            # getTint()` returns white for anything without it and a stored
            # (or randomly rolled) colour for anything with it, and
            # `CharacterCreationMain:updateColorButton()` shows its colour
            # button on exactly this condition. 83 of the 1,795 clothingItems
            # that land in a pooled slot carry it. Written only when true, so
            # the key's absence means "not tintable" and the pools file does
            # not grow by 1,700 `false`s.
            #
            # The tint itself is a plain per-channel multiply on the base
            # texture's RGB, alpha untouched -- `media/shaders/hueChange.frag`
            # is three lines of `col.r *= R;`. Not a hue rotation, despite the
            # shader's name; PZ reuses one shader for both and leaves
            # `HueChange` at 0 for tinting.
            if flag("m_AllowRandomTint"):
                entry["tint"] = True
            # PZ hides hair (and sometimes beard) under certain hats client-side
            # via this flag rather than any mesh trick — a beanie has no hole
            # cut for hair to poke through. Values seen: "default", "Group0N",
            # "nobeard", "nohair", "nohairnobeard". Only the hair/beard-hiding
            # ones matter downstream; everything else is treated as "default".
            if slot == "hat":
                cat = tag("m_HatCategory").strip() or "default"
                entry["hat_category"] = cat
            pools["clothing"].setdefault(slot, []).append(entry)
        else:
            # No model: this is the texture-overlay class — a layer composited
            # onto the body mesh's own UV, not geometry. `m_BaseTextures` is
            # where those live. Some slots (e.g. "skirt") are declared as
            # mesh slots but still have texture-only variants in PZ's own
            # data, so this branch is reached for slots outside OVERLAY_SLOTS
            # too — `overlay_slots` below is derived from what actually landed
            # here, not the hardcoded constant, so those variants are not
            # silently dropped.
            choices = texture_list("m_BaseTextures", "textureChoices")
            if choices:
                overlay_entry = {"item": item, "textures": choices}
                # Same flag as the mesh branch above. A texture-overlay
                # garment is painted into the body composite rather than
                # given its own material, so the multiply happens there
                # instead -- see composite_body_texture() on the Blender side.
                if flag("m_AllowRandomTint"):
                    overlay_entry["tint"] = True
                # Most hats are painted onto the head texture rather than
                # modelled (173 of 183 in the current export) — the
                # hair/beard-hiding flag applies just as much to those as to
                # the mesh ones, so it has to be mined here too.
                if slot == "hat":
                    cat = tag("m_HatCategory").strip() or "default"
                    overlay_entry["hat_category"] = cat
                pools["overlays"].setdefault(slot, []).append(overlay_entry)

    weapons = _mine_weapon_attachments(media, mesh_ids, textures, scale)
    if weapons:
        pools["clothing"]["weapon"] = weapons

    pools["rules"] = _mine_clothing_rules(game, scripts)

    # Derived rather than the OVERLAY_SLOTS constant: a slot like "skirt" is
    # mostly texture-based but has a mesh variant too (and vice versa), and a
    # slot missing from these lists is never sampled at all.
    #
    # **Ordered by PZ's own render order, not sorted.** These lists are the
    # order the texture layers composite in, and `sorted()` — which is what
    # this was — puts `underwearbottom`/`underweartop` last, i.e. painted on
    # top of the shirt and trousers that should cover them. PZ declares its
    # body locations in render order precisely to fix that order in one place.
    order = pools["rules"]["render_order"]
    def in_render_order(slots):
        known = [s for s in order if s in slots]
        return known + sorted(s for s in slots if s not in order)
    pools["overlay_slots"] = in_render_order(set(pools["overlays"]))
    pools["mesh_slots"] = in_render_order(set(pools["clothing"]))

    if unknown_mask_ids:
        seen = ", ".join(f"{k}x{v}" for k, v in sorted(unknown_mask_ids.items()))
        print(f"appearance: skipped unmapped <m_Masks> ids ({seen}) "
              f"- see BODY_MASK_REGIONS")

    pools["damage"] = _mine_damage_assets(media, textures)

    return pools, textures


def _mine_damage_assets(media: Path, textures: set) -> dict:
    """Hole/blood/dirt masks for the damage system: PZ's own per-region wound
    masks plus the two shared tinted overlays they're combined with.

    Probed by fixed filename, same as the skin-tone block above — PZ ships no
    XML table for these, they're a hardcoded 1:1 name-per-region convention
    (`BloodMask<region>.png`). Found paths are added to `textures` (the same
    copy-whitelist set every other pool entry's art goes through) so
    `_copy_textures()` needs no changes at all.
    """
    hole_masks = {}
    for region in DAMAGE_REGIONS:
        rel = f"textures/HoleTextures/BloodMask{region}.png"
        if (media / rel).exists():
            hole_masks[region] = rel
            textures.add(rel)

    overlays = {}
    for key, rel in (("blood_overlay", "textures/BloodTextures/BloodOverlay.png"),
                     ("dirt_overlay", "textures/BloodTextures/GrimeOverlay.png")):
        if (media / rel).exists():
            overlays[key] = rel
            textures.add(rel)

    missing = [r for r in DAMAGE_REGIONS if r not in hole_masks]
    if missing:
        print(f"appearance: damage masks missing for {', '.join(missing)} "
              f"- HoleTextures/ art may have moved")

    return {"hole_masks": hole_masks, **overlays}


def _copy_textures(game: Path, rels: set, dest: Path) -> int:
    """Copy referenced PNGs into the export, preserving their relative path.

    Copied rather than atlased: unlike tile art, these are per-mesh textures
    with no shared UV space to pack into, and Godot imports loose PNGs fine.
    """
    import shutil
    copied = 0
    for entry in sorted(rels, key=lambda e: e if isinstance(e, str) else e[1]):
        # A plain string copies to the same relative path it was read from. A
        # (source, destination) pair renames on the way in — see mask_list(),
        # where PZ's two spellings of one folder would otherwise collide.
        src_rel, dest_rel = entry if isinstance(entry, tuple) else (entry, entry)
        src = game / "media" / src_rel
        if not src.exists():
            continue
        out = dest / dest_rel.split("textures/", 1)[1]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, out)
        copied += 1
    return copied


def _pick_mesh(nodes: list, stem: str) -> tuple:
    """Choose which `Mesh` in a file is the model, and say so when it is a guess.

    Four files under Skinned/ hold two meshes — a stray garment the artist left
    in alongside the real one. Taking the first was tried and is **wrong in
    three of those four**: `Bob_Cuirass_ALT`, `F_HydrationBackpack` and
    `F_Choker_Bone` all have the matching mesh second, so the export would have
    shipped a vambrace as a cuirass and an ALICE pack as a hydration pack.

    The `Mesh` block's own name matches the file stem in every one of those
    cases, so that is the rule; falling back to the first only when nothing
    matches, and recording it either way.
    """
    if len(nodes) == 1:
        return nodes[0], None
    for node in nodes:
        if node.name and node.name.lower() == stem.lower():
            return node, f"{len(nodes)} meshes, matched by name"
    return nodes[0], f"{len(nodes)} meshes, no name match, used first"


def _mesh_id(path: Path, game: Path) -> str:
    """Stable id: path under Skinned/, Static/ or weapons/, forward slashes,
    no extension.

    e.g. `Clothes/Bob_AmmoStrap` (Skinned), `Static/Clothes/M_BaseballCap`
    (Static) or `Weapons/2handed/FireAxe` (weapons — each root prefixed so
    none of the three can collide on the same relative path). Matching what
    the clothing XML's `<m_MaleModel>` points at is stage 2's job
    (`mesh_id_for()` for clothing, `_weapon_model_lookup()` for weapons);
    this just has to be unique and reproducible.
    """
    for name, prefix in MODEL_ROOTS:
        root = game / "media" / "models_X" / name
        if path.is_relative_to(root):
            rel = path.relative_to(root)
            return prefix + rel.with_suffix("").as_posix()
    raise ExportError(f"mesh path outside models_X/{{Skinned,Static,weapons}}: {path}")


def _clip_id(path: Path, game: Path) -> str:
    rel = path.relative_to(game / "media" / "anims_X")
    return rel.with_suffix("").as_posix()


def _export_appearance(game: Path, out: Path, manifest: dict) -> dict:
    """Mine the pools and copy the textures they reference.

    Split out of `export()` so `--pools-only` can redo it against an existing
    manifest. The pools are the part that changes when a *rule* changes, and
    re-reading 2,590 `.x` files to re-roll an outfit table is minutes of work
    for a file that takes under a second to write.
    """
    pools, textures = _mine_appearance(game, set(manifest["meshes"]), manifest["scale"])
    n_tex = _copy_textures(game, textures, out.parent / "textures")
    out.with_name("appearance_pools.json").write_text(
        json.dumps(pools, indent=1), encoding="utf-8")
    dressed = {s: len(v) for s, v in sorted(pools["clothing"].items())}
    overlays = {s: len(v) for s, v in sorted(pools["overlays"].items())}
    rules = pools["rules"]
    print(f"appearance: mesh garments {sum(dressed.values())} {dressed}")
    print(f"            texture overlays {sum(overlays.values())} {overlays}")
    print(f"            {len(pools['hair']['male'])} male / "
          f"{len(pools['hair']['female'])} female hair, "
          f"{len(pools['beard'])} beards, {n_tex} textures copied")
    print(f"rules:      render order {' > '.join(rules['render_order'])}")
    print(f"            {len(rules['exclusive'])} exclusive pair(s): "
          f"{['+'.join(p) for p in rules['exclusive']]}")
    for gender, table in rules["outfits"].items():
        worn = {s: v["chance"] for s, v in table.items() if s != "__outerwear__"}
        print(f"            {gender:6} outfit {worn}")
    sets = rules["underwear"]["sets"]
    by_gender: dict[str, int] = {}
    for s in sets:
        by_gender[s["gender"] or "any"] = by_gender.get(s["gender"] or "any", 0) + 1
    print(f"            {len(sets)} underwear sets {by_gender}, "
          f"base chance {rules['underwear']['base_chance']}")
    if rules["_unshipped_outfit_slots"]:
        print(f"            outfit slots not exported: "
              f"{', '.join(rules['_unshipped_outfit_slots'])}")
    return pools


def export(game: Path, out: Path, scale: float, clip_sets: list[str],
           only_meshes: list[str] | None, only_clips: list[str] | None,
           dry_run: bool) -> dict:
    meshes = _mesh_sources(game, only_meshes)
    clips = _clip_sources(game, clip_sets, only_clips)

    print(f"game     {game}")
    print(f"meshes   {len(meshes)} from models_X/{{Skinned,Static,weapons}}")
    print(f"clips    {len(clips)} from anims_X/{{{','.join(clip_sets)}}}")
    print(f"scale    {scale}")
    print(f"out      {out}")
    if dry_run:
        print("\n(dry run, nothing written)")
        return {}

    writer = PackWriter(out)
    stats: dict = {"truncated_vertices": 0, "frame_transform_applied": 0}
    missing: list[dict] = []
    manifest: dict = {"meshes": {}, "clips": {}}
    skipped = 0

    skel_path = game / "media" / SKELETON_SOURCE
    skel_bones = xfile.read_skeleton(xfile.parse_file(skel_path))
    writer.add("skel:" + SKELETON_ID, _pack_skeleton(skel_bones, scale))
    manifest["skeleton"] = {"id": SKELETON_ID, "source": SKELETON_SOURCE,
                            "bones": len(skel_bones)}
    print(f"skeleton {SKELETON_ID}: {len(skel_bones)} bones from {SKELETON_SOURCE}")

    with Bar(len(meshes), "meshes") as bar:
        for path in meshes:
            bar.update(1, path.stem)
            mesh_id = _mesh_id(path, game)
            try:
                xf = xfile.parse_file(path)
                bones = xfile.read_skeleton(xf)
                nodes = xf.find_all_deep("Mesh")
                if not nodes:
                    missing.append({"id": mesh_id, "reason": "no Mesh block"})
                    skipped += 1
                    continue
                node, note = _pick_mesh(nodes, path.stem)
                if note:
                    missing.append({"id": mesh_id, "reason": note})
                mesh = xfile.read_mesh(node, str(path))
                if not mesh.skin:
                    ancestors = xfile.mesh_frame_ancestors(xf, node)
                    if ancestors:
                        composed = ancestors[-1]
                        for anc in reversed(ancestors[:-1]):
                            composed = _matmul(composed, anc)
                        if not _is_close_identity(composed):
                            _apply_static_frame_transform(mesh, composed)
                            stats["frame_transform_applied"] = stats.get("frame_transform_applied", 0) + 1
                blob = _pack_mesh(mesh, bones, scale, stats)
                writer.add("mesh:" + mesh_id, blob)
                manifest["meshes"][mesh_id] = {
                    "vertices": mesh.vertex_count,
                    "triangles": len(mesh.faces),
                    "bones": [s.bone for s in mesh.skin],
                    "skinned": bool(mesh.skin),
                    "materials": mesh.materials,
                }
            except (xfile.XFileError, OSError, ValueError) as e:
                missing.append({"id": mesh_id, "reason": f"{type(e).__name__}: {e}"})
                skipped += 1

    with Bar(len(clips), "clips") as bar:
        for path in clips:
            bar.update(1, path.stem)
            clip_id = _clip_id(path, game)
            try:
                xf = xfile.parse_file(path)
                parsed = xfile.read_clips(xf)
                if not parsed:
                    missing.append({"id": clip_id, "reason": "no AnimationSet"})
                    skipped += 1
                    continue
                clip = parsed[0]
                writer.add("clip:" + clip_id, _pack_clip(clip, scale))
                manifest["clips"][clip_id] = {
                    "name": clip.name,
                    "seconds": round(clip.duration_seconds, 4),
                    "tracks": len(clip.tracks),
                }
            except (xfile.XFileError, OSError, ValueError) as e:
                missing.append({"id": clip_id, "reason": f"{type(e).__name__}: {e}"})
                skipped += 1

    writer.close()

    manifest["scale"] = scale
    manifest["clip_sets"] = clip_sets
    out.with_name("character_manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")

    _export_appearance(game, out, manifest)
    if missing:
        out.with_name("missing.json").write_text(
            json.dumps(missing, indent=1), encoding="utf-8")

    size = out.stat().st_size
    ratio = writer.raw_bytes / writer.stored_bytes if writer.stored_bytes else 0
    print(f"\n{writer.count} entries, {size / 1e6:.1f} MB "
          f"({writer.raw_bytes / 1e6:.1f} MB raw, {ratio:.1f}x)")
    print(f"skipped {skipped}, recorded {len(missing)} note(s) in missing.json")
    if stats["truncated_vertices"]:
        print(f"{stats['truncated_vertices']:,} vertices truncated to "
              f"{MAX_INFLUENCES} influences")
    if stats["frame_transform_applied"]:
        print(f"{stats['frame_transform_applied']} unskinned mesh(es) had a dropped "
              f"ancestor frame transform composed back in")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pack PZ character meshes and animations into a .pzc file.")
    ap.add_argument("--game-dir", default=GAME_DIR)
    ap.add_argument("--out", default="assets/characters/characters.pzc")
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                    help=f"world units per .x unit (default {DEFAULT_SCALE}; "
                         f"raw bodies are ~0.98 tall)")
    ap.add_argument("--clip-sets", nargs="*", default=list(HUMAN_CLIP_SETS),
                    help="anims_X subdirectories to pack")
    ap.add_argument("--only", nargs="*", default=None,
                    help="mesh stems to include, for a fast slice")
    ap.add_argument("--clips", nargs="*", default=None,
                    help="clip stems to include, for a fast slice")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pools-only", action="store_true",
                    help="re-mine appearance_pools.json from the existing "
                         "manifest, without touching the .pzc")
    args = ap.parse_args(argv)

    try:
        out = Path(args.out)
        if args.pools_only:
            manifest_path = out.with_name("character_manifest.json")
            if not manifest_path.exists():
                raise ExportError(f"--pools-only needs {manifest_path}; "
                                  f"run a full export first")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _export_appearance(Path(args.game_dir), out, manifest)
            return 0
        export(Path(args.game_dir), out, args.scale,
               args.clip_sets, args.only, args.clips, args.dry_run)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
