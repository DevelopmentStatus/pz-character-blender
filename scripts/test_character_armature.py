"""Headless check of the character armature's rest pose and bone orientation.

    python scripts/test_character_armature.py

Bpy-free on purpose - it reads the shipped assets/characters/characters.pzc
through pz_character_viewer.character.pzc_reader and needs no Blender, the same
split that file documents for itself. The cost is that the head/tail/roll
solver in character/build.py's _build_armature() is *mirrored* here in plain
vector math rather than imported (build.py imports bpy at module scope). Change
one and change the other, the same standing deal pzc_reader.py has with
CharacterAssetRegistry.gd. The constants below must match build.py's.

What it actually pins down, all of it measured from the shipped pack:

  * The armature must rest in the pose the meshes were skinned in, taken from
    MaleBody's bind list - a T-pose, mirror-symmetric to 1e-6. The separate
    skel:Human rest (models_X/Skinned/Male_Skeleton.x) is a relaxed asymmetric
    pose that disagrees with it by 0.242 units on average and 0.805 at worst;
    building from it put the bones through and beside the body.
  * Bip01 is a 3ds Max Biped: a bone's chain axis is local +X, not Blender's
    +Y, so every bone has to be re-expressed rather than copied.
  * Each bone's tail reaches its real child joint, branch roots pick the child
    that continues the chain rather than averaging all of them, and a terminal
    nub continues its parent instead of reversing into it.
  * A rigid prop (hat, glasses) lands on the skull.

Blender itself is still the real test - nothing here runs edit_bones.
"""
import math
import os
import sys

# This file lives in pz-character/scripts/; PZCHAR_ROOT is pz-character
# itself, one level up.
PZCHAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _charroot  # noqa: E402
sys.path.insert(0, os.path.join(PZCHAR_ROOT, "src", "addon", "pz_character_viewer", "character"))

from pzc_reader import PZCReader

# Must match character/build.py.
CHAIN_DOT_MIN = 0.7
LEAF_LENGTH = 0.05
LEAF_PARENT_FRACTION = 0.4
LEAF_MIN, LEAF_MAX = 0.02, 0.08
EPS = 1e-5

AXIS_FIX = ((1, 0, 0), (0, 0, -1), (0, 1, 0))  # (x,y,z) -> (x,-z,y)


def qmat(q):
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def mul4(a, b):
    R = [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    t = [sum(a[i][k] * b[k][3] for k in range(3)) + a[i][3] for i in range(3)]
    return [R[0] + [t[0]], R[1] + [t[1]], R[2] + [t[2]]]


def fix4(m):
    """AXIS_FIX @ m @ AXIS_FIX_inv, on a 3x4."""
    A = [list(r) + [0.0] for r in AXIS_FIX]
    Ainv = [[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0]]
    return mul4(mul4(A, m), Ainv)


def col(m, j):
    return (m[0][j], m[1][j], m[2][j])


def norm(v):
    l = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / l for c in v)


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def scale(v, s):
    return tuple(c * s for c in v)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def length(v):
    return math.sqrt(dot(v, v))


r = PZCReader(os.path.join(_charroot.require(), "characters.pzc"))
skel = r.skeleton()

def inv_rigid(m):
    Rt = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [m[0][3], m[1][3], m[2][3]]
    ti = [-sum(Rt[i][k] * t[k] for k in range(3)) for i in range(3)]
    return [Rt[0] + [ti[0]], Rt[1] + [ti[1]], Rt[2] + [ti[2]]]


# The armature rests in the pose the meshes were skinned in, harvested from the
# body's bind list - not in skel:Human's own (different, asymmetric) pose.
BODY = r.get_mesh("MaleBody")
bind_pose = {}
for slot, nm in enumerate(BODY["bones"]):
    if slot < len(BODY["binds"]):
        pos, quat, _ = BODY["binds"][slot]
        R = qmat(quat)
        bind_pose[nm] = inv_rigid([R[0] + [pos[0]], R[1] + [pos[1]], R[2] + [pos[2]]])

godot_world, rest = [], []
for b in skel:
    pos, quat, _ = b["rest"]
    R = qmat(quat)
    local = [R[0] + [pos[0]], R[1] + [pos[1]], R[2] + [pos[2]]]
    w = bind_pose.get(b["name"])
    if w is None:
        w = mul4(godot_world[b["parent"]], local) if b["parent"] >= 0 else local
    godot_world.append(w)
    rest.append(fix4(w))

heads = [col(m, 3) for m in rest]
axes = [norm(col(m, 0)) for m in rest]

children_of = {}
for i, b in enumerate(skel):
    if b["parent"] >= 0:
        children_of.setdefault(b["parent"], []).append(i)

tails, lengths, chain_child = [], [], []
for i, b in enumerate(skel):
    tail = None
    picked = None
    cands = [c for c in children_of.get(i, []) if length(sub(heads[c], heads[i])) > EPS]
    if len(cands) == 1:
        picked = cands[0]
        tail = heads[picked]
    elif cands:
        best = max(cands, key=lambda c: dot(norm(sub(heads[c], heads[i])), axes[i]))
        if dot(norm(sub(heads[best], heads[i])), axes[i]) >= CHAIN_DOT_MIN:
            picked = best
            tail = heads[best]
    if tail is None:
        pi = b["parent"]
        d, ln = axes[i], LEAF_LENGTH
        if pi >= 0 and lengths[pi] > EPS:
            d = norm(sub(tails[pi], heads[pi]))
            ln = min(LEAF_MAX, max(LEAF_MIN, lengths[pi] * LEAF_PARENT_FRACTION))
        tail = add(heads[i], scale(d, ln))
    tails.append(tail)
    lengths.append(length(sub(tail, heads[i])))
    chain_child.append(picked)

names = [b["name"] for b in skel]
print("%-24s %-7s  dir(x,y,z)              -> tail child" % ("bone", "len"))
for i, b in enumerate(skel):
    d = norm(sub(tails[i], heads[i]))
    print("%-24s %6.3f  (%6.3f,%6.3f,%6.3f)  -> %s" % (
        names[i], lengths[i], d[0], d[1], d[2],
        names[chain_child[i]] if chain_child[i] is not None else "(terminal)"))

# ---- assertions -------------------------------------------------------
fails = []


def expect(cond, msg):
    if not cond:
        fails.append(msg)


idx = {n: i for i, n in enumerate(names)}


def direction(n):
    return norm(sub(tails[idx[n]], heads[idx[n]]))


def points_at(parent, child):
    expect(chain_child[idx[parent]] == idx[child], f"{parent} should point at {child}, got "
           f"{names[chain_child[idx[parent]]] if chain_child[idx[parent]] is not None else None}")


# spine runs up the body
for a, b in (("Bip01_Pelvis", "Bip01_Spine"), ("Bip01_Spine", "Bip01_Spine1"),
             ("Bip01_Spine1", "Bip01_Neck"), ("Bip01_Neck", "Bip01_Head"),
             ("Bip01_Head", "Bip01_HeadNub")):
    points_at(a, b)
    expect(direction(a)[2] > 0.7, f"{a} should run upward (+Z in Blender), got {direction(a)}")

# arms branch from the shoulders toward the hands
for side in ("L", "R"):
    for a, b in ((f"Bip01_{side}_Clavicle", f"Bip01_{side}_UpperArm"),
                 (f"Bip01_{side}_UpperArm", f"Bip01_{side}_Forearm"),
                 (f"Bip01_{side}_Forearm", f"Bip01_{side}_Hand")):
        points_at(a, b)
    sign = 1.0 if side == "L" else -1.0
    for n in (f"Bip01_{side}_UpperArm", f"Bip01_{side}_Forearm"):
        # The bind pose is a T-pose, so an arm runs almost purely outward.
        expect(direction(n)[0] * sign > 0.9, f"{n} should run outward on {side}, got {direction(n)}")

# legs branch from the hips toward the feet
for side in ("L", "R"):
    for a, b in ((f"Bip01_{side}_Thigh", f"Bip01_{side}_Calf"),
                 (f"Bip01_{side}_Calf", f"Bip01_{side}_Foot"),
                 (f"Bip01_{side}_Foot", f"Bip01_{side}_Toe0")):
        points_at(a, b)
    for n in (f"Bip01_{side}_Thigh", f"Bip01_{side}_Calf"):
        expect(direction(n)[2] < -0.7, f"{n} should run downward (-Z in Blender), got {direction(n)}")
    # The foot genuinely leaves its own +X axis to reach the toe joint - the
    # case the old averaging pass and a pure-axis pass both get wrong.
    # Forward is Blender -Y (Godot +Z): the backpack bone points +Y, i.e.
    # behind the spine, and the head/backpack bind positions agree.
    expect(direction(f"Bip01_{side}_Foot")[1] < -0.5,
           f"Bip01_{side}_Foot should run forward toward the toe, got {direction(f'Bip01_{side}_Foot')}")

# fingers follow the hand
for side in ("L", "R"):
    for n in ("Finger0", "Finger1"):
        points_at(f"Bip01_{side}_Hand", f"Bip01_{side}_Finger1")
        points_at(f"Bip01_{side}_{n}", f"Bip01_{side}_{n}Nub")

# accessory chains
points_at("Bip01_BackPack", "Bip01_BackPackNub")
for n in ("DressBack", "DressFront"):
    points_at(f"Bip01_{n}", f"Bip01_{n}02")
    points_at(f"Bip01_{n}02", f"Bip01_{n}Nub")
    expect(direction(f"Bip01_{n}")[2] < -0.7, f"Bip01_{n} should hang downward, got {direction(f'Bip01_{n}')}")

# branch roots must not be dragged sideways by a non-chain child
expect(chain_child[idx["Bip01"]] is None,
       "Bip01's only children are the zero-length pelvis and the two prop bones; "
       "it must fall back to its own axis, not stretch to a prop")
for n in ("Bip01_Prop1", "Bip01_Prop2", "Bip01_HeadNub", "Bip01_L_Toe0Nub",
          "Translation_Data", "Skeleton", "Bip01_L_Finger0Nub"):
    expect(chain_child[idx[n]] is None, f"{n} is a leaf and should use its own axis")

# the bind pose is a T-pose and must come out mirror-symmetric
for part in ("Thigh", "Calf", "Foot", "UpperArm", "Forearm", "Hand"):
    dl, dr = direction("Bip01_L_" + part), direction("Bip01_R_" + part)
    expect(abs(dl[0] + dr[0]) < 1e-3 and abs(dl[1] - dr[1]) < 1e-3 and abs(dl[2] - dr[2]) < 1e-3,
           f"{part} direction is not mirror-symmetric: L={dl} R={dr}")

# a terminal bone continues its chain rather than reversing into its parent
for i, b in enumerate(skel):
    if chain_child[i] is None and b["parent"] >= 0 and lengths[b["parent"]] > EPS:
        pd = norm(sub(tails[b["parent"]], heads[b["parent"]]))
        expect(dot(direction(names[i]), pd) > 0.0,
               f"{names[i]} points backwards into {names[b['parent']]}")

# nothing degenerate
for i, n in enumerate(names):
    expect(lengths[i] > EPS, f"{n} has zero length and Blender would delete it")

# connection: a bone connects iff its parent was pointed at it
connected = [i for i, b in enumerate(skel)
             if b["parent"] >= 0 and chain_child[b["parent"]] == i]
print("\nconnected bones: %d of %d" % (len(connected), len(skel)))
for i, b in enumerate(skel):
    if b["parent"] >= 0:
        touching = length(sub(heads[i], tails[b["parent"]])) < EPS
        expect(touching == (chain_child[b["parent"]] == i),
               f"{names[i]} connect flag disagrees with joint coincidence")

# ---- rigid props land on the head ------------------------------------
# A hat/glasses mesh carries no skin data; it is authored in its attach bone's
# own space and placed at that bone's rest transform. Check a real one ends up
# on the skull rather than beside it.
def _mul_point(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) + m[i][3] for i in range(3))


head = rest[idx["Bip01_Head"]]
for prop in ("Clothes/M_PoliceHat", "Static/Clothes/F_Glasses_Aviators"):
    md = r.get_mesh(prop)
    if md is None:
        continue
    expect(not md["bones"], f"{prop} is skinned; it should not go through the rigid-prop path")
    pts = [_mul_point(head, (p[0], -p[2], p[1])) for p in md["positions"]]  # axis fix, then bone rest
    zs = [p[2] for p in pts]
    ys = [p[1] for p in pts]
    xs = [p[0] for p in pts]
    print("%-34s placed bbox x[%6.3f,%6.3f] y[%6.3f,%6.3f] z[%6.3f,%6.3f]" % (
        prop, min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
    # skull top sits at z ~1.76 (mesh is 1.764 tall); head bone at 1.484
    expect(min(zs) > 1.4, f"{prop} sits below the head bone (min z {min(zs):.3f})")
    expect(max(zs) < 2.1, f"{prop} floats above the skull (max z {max(zs):.3f})")
    expect(abs((min(xs) + max(xs)) / 2.0) < 0.06, f"{prop} is off-centre in x")

print()
if fails and __name__ == "__main__":
    print("FAIL (%d)" % len(fails))
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
if __name__ == "__main__":
    print("all orientation checks passed")
