# Held weapons: which bone, which rotation, and why the shovel points the wrong way

How Project Zomboid attaches a held item to a character, what this add-on
currently does instead, and the two defects that make a shovel read wrong in
`Bob_Dig_Shovel`.

Everything here was established by reading PZ's own data, its decompiled Java,
the `.x` files and the shipped pack — **not** by looking at the viewport. Each
claim carries where it came from. Measurements are dated, because this pack is
regenerated and the numbers move.

---

## 1. PZ's model — two named sockets, not a weld

An item is **not** parented to a hand bone. PZ uses a two-sided named-socket
system resolved every frame.

**The character** declares sockets in its model script
(`generation/ModelScriptGenerator.java`, the `MALE_BODY` / `FEMALE_BODY`
builders):

```java
.addAttachment(attachment(ModelAttachmentId.BIP01_PROP2)
    .offset(0,0,0).rotate(0,0,0).bone(SkeletonBone.Bip01_Prop2))
.addAttachment(attachment(ModelAttachmentId.BIP01_PROP1)
    .offset(0,0,0).rotate(0,0,0).bone(SkeletonBone.Bip01_Prop1))
```

Both are identity on `MaleBody` except a −0.0032 x offset on Prop2.

**The item** declares its own, in `media/scripts/generated/models_weapons.txt`:

```
model Shovel {
    mesh = weapons/2handed/Shovel,
    attachment world       { offset = -0.0009 0.2369 -0.0152, rotate = 0.0 -90.0    0.0 }
    attachment Bip01_Prop2 { offset =  0.0    0.0     0.0,    rotate = 0.0 -73.0526 0.0 }
}
```

`AnimatedModel$AnimatedModelInstanceRenderData.transformToParent()` composes
them (`decomp/src/zombie/core/skinnedmodel/advancedanimation/AnimatedModel.java`,
~1445-1490):

```
xfrm = boneModelTransform(attachBone)   # the live animated bone
     × parentAttachment                 # character's socket  (identity here)
     × selfAttachment                   # item's socket       (the grip pose)
     × mesh.transform                   # postMultiplyMeshTransform()
```

`makeAttachmentTransform` is `translation(offset) · rotateXYZ(rx,ry,rz) ·
scale(s)`, degrees, intrinsic XYZ
(`ModelInstanceRenderData.java:104-110`, decompiled-java).

So: functionally a rigid weld to a bone, but with an authored per-item grip
correction sandwiched in. The item mesh has no bones and no skin — `Shovel.x`
is one `Frame`, 367 verts, **identity** frame transform, zero skin weights
(verified by parsing the file with `src/extractor/xfile.py`).

### The socket bones

`media/AnimSets/Master_Bones.xml` lists 35 bones. Two skin nothing:

```
Bip01_Prop1    parent = Bip01    rest offset (0.1552, 0.1316, -0.0076)
Bip01_Prop2    parent = Bip01    rest offset (0.1191, 0.1615,  0.0555)
```

Note the parent: **`Bip01`, the root** — not the hand, not the forearm. They
are prop sockets hanging off the root, hand-keyed by the animator to follow
the hands. Anything that reasons about them as children of a hand is wrong.

### Which socket

`zombie/core/skinnedmodel/ModelManager.java:670-690` (decompiled-java):

| slot | bone |
|---|---|
| primary hand item | `Bip01_Prop1` |
| secondary hand item | `Bip01_Prop2` |
| action override models | same split |

A two-handed weapon is one item, so `primaryItem == secondaryItem`, the
secondary branch is guarded by `primaryItem != secondaryItem` and never fires
— the shovel goes on **`Bip01_Prop1`**.

`ModelManager.newStaticInstance()` sets only `inst.parentBoneName = boneName`;
`attachmentNameSelf` / `attachmentNameParent` stay null, so `transformToParent`
takes its `parentBoneName` fallback for both lookups.

### The grip rotation is NOT applied to a primary-hand weapon

Following that fallback: `selfAttachment =
shovelModel.getAttachmentById("Bip01_Prop1")`. The lookup bottoms out in a
plain hash lookup — **exact match, case-sensitive, no fallback**
(`ModelScript.java:190-192`):

```java
public ModelAttachment getAttachmentById(String id) {
   return this.attachmentById.get(id);
}
```

The Shovel declares only `world` and `Bip01_Prop2`. So `selfAttachment` is
`null` and **the −73.0526° grip rotation never runs.** For a primary-hand
weapon the composition collapses to:

```
xfrm = boneModelTransform(Bip01_Prop1) × mesh.transform
```

The rule PZ actually follows is: *apply the item's socket only when its id
matches the bone being bound to.* That is why only 10 weapon models bother to
declare a `Bip01_Prop1` block — those are the ones that need a primary-hand
grip correction. The 284 `Bip01_Prop2` blocks are for the secondary slot.

This was initially inferred the other way round, from geometry — the shovel
looked like it needed roughly that rotation. That inference was **wrong**; the
residual misalignment comes from §4 instead. Recorded because it is the kind
of plausible-but-wrong reading this repo has been burned by before.

---

## 2. The clip proves which bone

`media/anims_X/Bob/Bob_Dig_Shovel.x` — 3.000 s, 4800 ticks/s, 45 tracks.
Key counts, read with `src/extractor/xfile.py`:

| bone | rotation keys | position keys |
|---|---|---|
| `Bip01_Prop1` | **91** | 91 |
| `Bip01_Prop2` | **2** (constant) | 91 |
| `Bip01_R_Hand` | 91 | 2 |

Prop2 is parked for the whole clip and only translates. Prop1 is fully keyed.
The same pattern appears in `Bob_AimToIdle_2H_Heavy` and `_Chainsaw` — for
two-handed clips, Prop1 does the work.

Forward kinematics over the clip (own FK, from the raw `.x` tracks) puts
Prop1 **between the two hands** at every frame:

```
t=0.00s   R_Hand x=-0.064   L_Hand x=+0.078   Prop1 x=+0.015
t=0.75s   R_Hand x=-0.067   L_Hand x=+0.099   Prop1 x=+0.015
t=1.50s   R_Hand x=-0.038   L_Hand x=+0.036   Prop1 x=-0.004
```

Measured in the built rig (Blender, 2026-09-03), Prop1's perpendicular
distance from the line through both hands is **1.6-10 cm**, tightest (1.6-4.4)
during the swing; Prop2's is **0-37 cm** and erratic. Prop1 sits a rigidly
constant **16.3-17.3 cm** from the right wrist across all 90 frames.

**Conclusion: `Bip01_Prop1`, on three independent lines of evidence.**

---

## 3. Defect A — the add-on ignores the grip socket

`src/extractor/pz_characters.py` mines each weapon's socket out of
`models_weapons.txt` (`_mine_weapon_attachments`, `_weapon_attach_transform`)
and ships it per item in `appearance_pools.json`:

```json
{ "item": "Shovel",
  "bone": "Bip01_Prop2",
  "attach_offset": [0.0, 0.0, 0.0],
  "attach_rotate": [0.0, -0.5951917, 0.0, 0.8035837] }
```

That quaternion is −73.05° about Y, matching the script.

**Nothing reads either key.** Grepping the whole add-on for `attach_rotate` /
`attach_offset` returns zero hits. `build._build_rigid_prop()` places the prop
as `obj.matrix_world = arm.matrix_world @ bone_rest` (plus the unrelated
`RIGID_PROP_FORWARD_NUDGE`), and `animation_build.build_prop_action()` bakes
`_to_blender_matrix(world[bone_idx]) @ nudge_local`. Neither composes a grip
transform. The mined data is dead.

## 4. Defect B — the pack's meshes are older than the extractor

This is the one that makes Defect A dangerous to "just fix".

`_pack_mesh`'s docstring records that a previous revision baked a **fixed −90°
rotation** into every `weapons/` mesh's vertices, and that it was removed in
favour of the per-weapon socket, warning:

> a second, fixed correction baked into the mesh itself would double up with
> it rather than replace it.

The current code applies no such rotation — vertices go out as
`(x*scale, y*scale, -z*scale)` (`pz_characters.py:365-369`), and the Shovel's
only ancestor `Frame` is identity, so `_apply_static_frame_transform` is a
no-op here.

But the shipped pack disagrees. Bounds, measured 2026-09-03:

| | source `Shovel.x` | ×1.8, per current code | **actual `characters.pzc`** |
|---|---|---|---|
| length 0.6508 | on **Y** | → Y = 1.1715 | on **X** = 1.1715 |
| width 0.1585 | on X | → Z = 0.2853 | on Y = 0.2853 |
| thickness 0.0320 | on Z | → X = 0.0576 | on Z = 0.0576 |

Solving the permutation: the pack's vertices carry an extra
`(x,y,z) → (y,-x,z)` — a **−90° rotation about Z**. Exactly the old fixed
weapon correction the docstring says was removed.

The file timestamps confirm it:

```
characters.pzc          2026-08-28 23:48    <- meshes, OLD extractor
pz_characters.py        2026-09-02 22:42    <- current code
appearance_pools.json   2026-09-02 22:43    <- metadata, NEW extractor
```

**The pack is internally inconsistent**: geometry pre-rotated by the old fixed
correction, metadata written on the assumption that it isn't.

---

## 5. What this means for the fix

Applying `attach_rotate` on top of the current meshes is the exact double
correction the docstring warns about. Two coherent options:

Since §1 shows PZ applies **no** socket to a primary-hand weapon, the fix is
smaller than it first looked. It is not "compose the missing rotation" — it is
"stop carrying a rotation that shouldn't be there".

**Re-extract, and change nothing about the placement maths.**
Re-run `pz_characters.py` so the meshes lose the stale −90° and come from the
same revision as the metadata. After that, for a weapon on `Bip01_Prop1` the
correct object transform is what `build_prop_action` *already* bakes:

```
object_matrix = bone_world          # nothing else
```

`attach_rotate` stays unread, and that is now known to be correct rather than
an oversight — for the 161 of 171 weapons whose only socket is `Bip01_Prop2`.

**Only if a weapon declares a socket matching its bind bone** should the
add-on compose it, right-multiplied in the bone's own frame — the same side
`build_prop_action` already composes `pz_forward_nudge` on, and for the same
reason (a world-space version drifts once the bone rotates; measured at 2.8 cm
on the hat). That affects the 10 models with a `Bip01_Prop1` block, plus
anything genuinely equipped in the secondary hand. Both `_build_rigid_prop`
(static placement) and `build_prop_action` (the bake) would need it, or Stop
and Play will disagree.

To support that, the extractor should ship the socket **keyed by id** rather
than flattening one socket into `attach_offset`/`attach_rotate`, so the add-on
can do the same id-vs-bone match PZ does.

**Do not** derive a residual against the stale meshes. With `R₉₀` baked in it
would be `rotZ(+90°)` for every weapon — a fixed fudge that papers over the
version skew and breaks the moment anyone re-extracts.

Also fix, independently of both:

**`_WEAPON_ATTACH_BONE_PREFERENCE` names the wrong bone first.**
`pz_characters.py:1123` is `("Bip01_Prop2", "Bip01_Prop1")`, reasoned as
"Prop2 is what every single-hand-weapon example in the shipped file uses".
That reads the item's **self**-socket id out of `models_weapons.txt`, which is
not the bone the engine binds to — `ModelManager` binds a primary-hand item to
`Bip01_Prop1`. All 171 weapon entries currently carry `"bone": "Bip01_Prop2"`.

---

## 6. Traps worth not re-learning

**The item's socket is named `Bip01_Prop2` but that is an *id*, not a bone.**
The bone comes from `ModelManager`'s slot logic. 284 of the weapon models
declare a `Bip01_Prop2` block and only 10 declare `Bip01_Prop1`; reading that
distribution as "weapons go on Prop2" is the mistake this whole document
exists to correct.

**Prop bones hang off `Bip01`, not off a hand.** They only *look* welded to
the hands because the animator keyed them that way, per clip.

**A prop bone can be keyed in position but not rotation.** Prop2's 2 rotation
keys in `Bob_Dig_Shovel` make it translate rigidly through the dig — which
reads as "the tool follows but never swings", not as an error.

**`build_prop_action` caches by `f"{clip_id}::{bone_name}"`** and returns
early on a hit. Change `pz_attach_bone` without deleting the old action and
the stale bake is silently reused; the fix looks like it did nothing.

**A rigid prop's static rest placement and its baked action come from
different code.** Changing the attach bone on a live object updates only the
bake; the rest placement still holds the old bone until the character is
rebuilt, so the prop jumps on Stop.

**Weapon mesh axes are per-model, not a convention.** `Shovel.x` is authored
along +Y; the `CanoePadel` differs. Do not assume a shared tool axis.

---

## 7. Provenance

| Claim | Basis |
|---|---|
| socket system, composition order, `makeAttachmentTransform` | decompiled Java (`decomp/src/`) — reading aid, **not** authority for numbers |
| primary→Prop1 / secondary→Prop2 | decompiled Java, `ModelManager.java:670-690` |
| grip rotation does **not** apply to a Prop1-bound item | `ModelScript.java:190-192` — a bare `HashMap.get`, no fallback. Trivial enough that decompiler error is not a realistic risk, but it is still decompiled Java. |
| clip key counts, bone parents, FK positions | parsed from `.x` with `src/extractor/xfile.py` |
| `attach_rotate` present and unread | data file + grep of the add-on |
| pack meshes carry a −90° Z rotation | bounds arithmetic, source vs `characters.pzc` |
| pack/code version skew | file mtimes, 2026-09-03 |

Per the repo's standing rule: a decompiler is *usually* right and occasionally
silently wrong. Anything above that is about to become a constant in code
should be re-checked against raw bytecode with `tools/inspect_java.py` first.
