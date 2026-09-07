"""Reads assets/characters/characters.pzc.

Pure Python (struct + zlib, no bpy) so it can be unit-tested outside Blender,
same split as manifest.py/crop.py in the parent package.

The format is written by tools/pz_characters.py and is documented there
(_pack_mesh, _pack_skeleton docstrings) and read on the Godot side by
scripts/characters/CharacterAssetRegistry.gd. This is a straight port of that
GDScript reader's field order - see its Cursor class - not a re-derivation of
the format from scratch. Do not change a field's order/size here without
making the same change in both of those.
"""

import struct
import zlib

MAGIC = b"PZCH"
SUPPORTED_VERSION = 1
HEADER = struct.Struct("<4siii")
INDEX_ENTRY = struct.Struct("<qii")

MESH_HAS_NORMALS = 1 << 0
MESH_HAS_UVS = 1 << 1
MESH_HAS_SKIN = 1 << 2
MESH_8_WEIGHTS = 1 << 3

SKELETON_ID = "Human"


class PZCError(Exception):
    pass


class _Cursor:
    __slots__ = ("buf", "pos")

    def __init__(self, buf, start=0):
        self.buf = buf
        self.pos = start

    def i32(self):
        (v,) = struct.unpack_from("<i", self.buf, self.pos)
        self.pos += 4
        return v

    def f32(self):
        (v,) = struct.unpack_from("<f", self.buf, self.pos)
        self.pos += 4
        return v

    def text(self):
        n = self.i32()
        s = self.buf[self.pos : self.pos + n].decode("utf-8")
        self.pos += n
        return s

    def floats(self, n):
        out = struct.unpack_from(f"<{n}f", self.buf, self.pos)
        self.pos += n * 4
        return out

    def ints(self, n):
        out = struct.unpack_from(f"<{n}i", self.buf, self.pos)
        self.pos += n * 4
        return out

    def transform(self):
        """(position xyz, quaternion xyzw, scale xyz), matching the writer's
        _decompose() layout - 10 floats: pos(3) + quat(4) + scale(3)."""
        v = self.floats(10)
        return v[0:3], v[3:7], v[7:10]


class PZCReader:
    """One open characters.pzc. Mirrors CharacterAssetRegistry.gd's open()/
    get_mesh()/_read_skeleton() - see that file for why the index is read
    once in full rather than per-id."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "rb")
        self._index = {}  # id -> (offset, stored_size, raw_size)
        self._mesh_cache = {}
        self._clip_cache = {}
        self._skeleton = None
        self._open()

    def _open(self):
        magic, version, count, id_len = HEADER.unpack(self._file.read(HEADER.size))
        if magic != MAGIC:
            raise PZCError(f"{self.path} is not a .pzc (magic {magic!r})")
        if version != SUPPORTED_VERSION:
            raise PZCError(f"{self.path}: version {version}, expected {SUPPORTED_VERSION}")

        id_bytes = self._file.read(id_len)
        ids = [b.decode("utf-8") for b in id_bytes.split(b"\0")[:-1]]
        if len(ids) != count:
            raise PZCError(f"{self.path}: {len(ids)} ids, header declares {count}")

        raw = self._file.read(count * INDEX_ENTRY.size)
        for i, entry_id in enumerate(ids):
            offset, stored, rawsize = INDEX_ENTRY.unpack_from(raw, i * INDEX_ENTRY.size)
            self._index[entry_id] = (offset, stored, rawsize)

    def close(self):
        self._file.close()

    def has_entry(self, entry_id):
        return entry_id in self._index

    def _payload(self, entry_id):
        entry = self._index.get(entry_id)
        if entry is None:
            return b""
        offset, stored, rawsize = entry
        self._file.seek(offset)
        blob = self._file.read(stored)
        # zlib.decompress handles the zlib-wrapped deflate stream directly -
        # Godot's COMPRESSION_DEFLATE is the same wrapped format, not raw
        # deflate (see the comment on the Godot side for why that distinction
        # matters: raw deflate through zlib needs wbits=-15).
        return zlib.decompress(blob)

    def skeleton(self):
        """Ordered list of {"name", "parent" (index, -1 = root), "rest": (pos, quat, scale)},
        parents-first - matches _pack_skeleton()'s write order."""
        if self._skeleton is not None:
            return self._skeleton
        blob = self._payload(f"skel:{SKELETON_ID}")
        if not blob:
            raise PZCError(f"{self.path}: no skeleton payload 'skel:{SKELETON_ID}'")
        c = _Cursor(blob, 4)  # skip "SKEL"
        out = []
        for _ in range(c.i32()):
            name = c.text()
            parent = c.i32()
            rest = c.transform()
            out.append({"name": name, "parent": parent, "rest": rest})
        self._skeleton = out
        return out

    def get_mesh(self, mesh_id):
        """{"positions": [(x,y,z),...], "normals": [...] or None, "uvs": [...] or None,
        "bone_ids": [[...],...] or None, "weights": [[...],...] or None,
        "indices": [i0,i1,i2,...], "influences": int,
        "bones": [name,...], "binds": [(pos,quat,scale),...]}
        or None if the id isn't in this pack."""
        if mesh_id in self._mesh_cache:
            return self._mesh_cache[mesh_id]

        blob = self._payload(f"mesh:{mesh_id}")
        if not blob:
            return None

        c = _Cursor(blob, 4)  # skip "MESH"
        flags = c.i32()
        n_verts = c.i32()
        n_indices = c.i32()
        n_bones = c.i32()
        n_binds = c.i32()
        influences = c.i32()

        raw = c.floats(n_verts * 3)
        positions = [raw[i * 3 : i * 3 + 3] for i in range(n_verts)]

        normals = None
        if flags & MESH_HAS_NORMALS:
            raw = c.floats(n_verts * 3)
            normals = [raw[i * 3 : i * 3 + 3] for i in range(n_verts)]

        uvs = None
        if flags & MESH_HAS_UVS:
            raw = c.floats(n_verts * 2)
            uvs = [raw[i * 2 : i * 2 + 2] for i in range(n_verts)]

        bone_ids = weights = None
        if flags & MESH_HAS_SKIN:
            flat_ids = c.ints(n_verts * influences)
            flat_wts = c.floats(n_verts * influences)
            bone_ids = [
                list(flat_ids[i * influences : (i + 1) * influences]) for i in range(n_verts)
            ]
            weights = [
                list(flat_wts[i * influences : (i + 1) * influences]) for i in range(n_verts)
            ]

        indices = c.ints(n_indices)

        bone_names = []
        for _ in range(n_bones):
            bone_names.append(c.text())
            c.i32()  # parent - implied by the shared skeleton, unused here
            c.floats(10)  # rest - likewise

        binds = []
        for _ in range(n_binds):
            c.i32()  # slot - always sequential
            binds.append(c.transform())

        result = {
            "positions": positions,
            "normals": normals,
            "uvs": uvs,
            "bone_ids": bone_ids,
            "weights": weights,
            "indices": list(indices),
            "influences": influences,
            "bones": bone_names,
            "binds": binds,
        }
        self._mesh_cache[mesh_id] = result
        return result

    def get_clip(self, clip_id):
        """{"tracks": [{"bone": str,
                         "position": [(t,x,y,z), ...],
                         "rotation": [(t,x,y,z,w), ...],
                         "scale": [(t,x,y,z), ...]}, ...],
            "duration": float}
        or None if clip_id isn't in this pack. Mirrors get_mesh() above;
        wire format is _pack_clip()'s docstring in tools/pz_characters.py -
        note track_count (i32) comes before length_seconds (f32), matching
        that function's struct.pack("<if", ...)."""
        if clip_id in self._clip_cache:
            return self._clip_cache[clip_id]

        blob = self._payload(f"clip:{clip_id}")
        if not blob:
            return None

        c = _Cursor(blob, 4)  # skip "CLIP"
        track_count = c.i32()
        duration = c.f32()

        tracks = []
        for _ in range(track_count):
            bone = c.text()
            n_pos = c.i32()
            position = [(c.f32(), *c.floats(3)) for _ in range(n_pos)]
            n_rot = c.i32()
            rotation = [(c.f32(), *c.floats(4)) for _ in range(n_rot)]
            n_scale = c.i32()
            scale = [(c.f32(), *c.floats(3)) for _ in range(n_scale)]
            tracks.append({"bone": bone, "position": position, "rotation": rotation, "scale": scale})

        result = {"tracks": tracks, "duration": duration}
        self._clip_cache[clip_id] = result
        return result
