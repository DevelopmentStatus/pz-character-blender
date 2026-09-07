"""Fuzzy hand/attachment-bone lookup over a PZ character armature.

Bpy-free on purpose, same split as pzc_reader.py / test_character_armature.py:
this module works on a plain list of bone names (or parent pairs) so it can be
unit-tested without Blender and reused from build.py, an operator, or a
headless script alike.

**Read this before using it to place a weapon or tool.** The skeleton (33
bones, `media/AnimSets/Master_Bones.xml`, 3ds Max Biped naming) does carry
real hand bones - `Bip01_L_Hand` / `Bip01_R_Hand` - and find_hand_bones()
below locates them reliably by name alone, no fuzz needed on the shipped
rig. But PZ does **not** attach held items to the hand bone. It ships a
dedicated pair of prop sockets, `Bip01_Prop1` / `Bip01_Prop2` (present in
Master_Bones.xml, siblings branching off `Bip01`, not children of either
hand - see build.py's `_CHAIN_DOT_MIN` doc comment, which measures the
Bip01->Prop1 branch angle directly), and `src/extractor/pz_characters.py`
(`_mine_weapon_attachments`) already reads PZ's own per-weapon offset/
rotation for those sockets out of `media/scripts/generated/models_weapons.txt`
-- keyed by socket id (`Bip01_Prop1`/`Bip01_Prop2`), not flattened to one
bone, since PZ binds a primary-hand item to `Bip01_Prop1` and a secondary-
hand item to `Bip01_Prop2` (see docs/weapon_attachment.md and build.py's
`HELD_ITEM_HAND_BONES`) and only composes a grip pose when the item declares
a block for the bone it's actually bound to. Use `find_attachment_bone()`
for "where does a held item go" and prefer real mined attach data over a
hand-relative guess whenever it exists - see that function's own doc for the
fallback order.

This module exists for the case the mined data does not cover: a different
or hand-authored skeleton (a custom rig, a modder's character, an armature
that never went through pz_characters.py) where nothing has named the hand
or prop bones for you and a fuzzy, naming-convention-agnostic search is the
only way in.
"""
from __future__ import annotations

import difflib
import re


#: Recognisable synonyms for "this bone is a hand", each scored against a
#: normalized bone name. Longer/more specific tokens first is not required -
#: normalize_bone_name() strips them all to the same shape before matching.
HAND_TOKENS = ("hand",)

#: Bones that *contain* the hand token as a substring but are not the hand -
#: these must outrank a plain "hand" match by name specificity, not lose to
#: it by fuzzy score. "lefthand"/"righthand" contains "hand" but so does
#: "handaxe"; the exclusion list is about bone *kind*, not just string
#: containment, so it only screens the bone's own trailing segment.
NON_HAND_HAND_LIKE = ("forearm", "wrist", "finger", "thumb", "prop", "weapon",
                       "grip", "attach", "socket", "hold")

#: Terms that push a bone toward being treated as the "prop"/weapon socket
#: rather than a body bone at all - checked first, since a rig with a named
#: weapon socket should never fall back to the hand.
ATTACHMENT_TOKENS = ("prop", "weapon", "grip", "attach", "socket", "hold", "item")

LEFT_TOKENS = ("left", "_l_", "l_", "_l", ".l", " l")
RIGHT_TOKENS = ("right", "_r_", "r_", "_r", ".r", " r")


def normalize_bone_name(name: str) -> str:
    """Lowercase, strip common rig-prefix noise, collapse separators.

    `Bip01_L_Hand` -> `l hand`, `mixamorig:LeftHand` -> `left hand`,
    `hand_r` -> `hand r`. Digits and case carry no meaning across the naming
    conventions this has to span (3ds Max Biped, Mixamo, Unreal, generic
    `hand_l`/`hand.L`), so both are discarded; word boundaries are kept as
    single spaces so token-level checks (`" l "` etc.) stay meaningful.
    """
    s = name.lower()
    # Common root/rig prefixes that carry no semantic content for this search.
    s = re.sub(r"^(bip0?1|mixamorig\d*:?|armature|root|def[-_])", "", s)
    s = re.sub(r"[._\-:]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _side_of(normalized: str) -> str | None:
    """'left' / 'right' / None, from token boundaries in a normalized name.

    Checked as whole words in the space-joined string, not raw substrings -
    `normalized` is already space-separated by normalize_bone_name(), so
    `" l "` only matches an isolated `l` token, not the l inside `pelvis`.
    """
    padded = f" {normalized} "
    for tok in ("left", " l "):
        if tok in padded:
            return "left"
    for tok in ("right", " r "):
        if tok in padded:
            return "right"
    return None


def _best_token_ratio(normalized: str, tokens: tuple[str, ...]) -> float:
    words = normalized.split(" ")
    best = 0.0
    for tok in tokens:
        for w in words:
            best = max(best, difflib.SequenceMatcher(None, w, tok).ratio())
        # Also score the whole string, so "hand" alone (a single-word bone
        # name) matches as well as a per-word split of a multi-word one.
        best = max(best, difflib.SequenceMatcher(None, normalized, tok).ratio())
    return best


def score_hand_candidate(name: str) -> float:
    """0..1: how confidently `name` names a hand bone specifically (not a
    wrist, forearm, finger, or attachment/prop bone).

    Exact substring beats fuzzy: a normalized name containing the literal
    word "hand" scores 1.0 outright rather than whatever
    SequenceMatcher.ratio() happens to compute, so `Bip01_L_Hand` and
    `LeftHand` are never second-guessed by a close-but-not-exact fuzzy score
    from an unrelated bone. Anything on NON_HAND_HAND_LIKE / ATTACHMENT_TOKENS
    is penalised even if "hand" appears in it (catches "offhand", "handguard",
    "handaxe_prop" naming from non-PZ rigs).
    """
    normalized = normalize_bone_name(name)
    words = set(normalized.split(" "))

    if words & set(ATTACHMENT_TOKENS):
        return 0.0
    if words & set(NON_HAND_HAND_LIKE):
        # Still allow a weak fuzzy score through (e.g. a rig that only has
        # "wristHand" as one fused word) rather than a hard zero, but never
        # let it win over a clean "hand" bone elsewhere on the same side.
        return 0.15 * _best_token_ratio(normalized, HAND_TOKENS)
    if "hand" in words:
        return 1.0
    return _best_token_ratio(normalized, HAND_TOKENS)


def score_attachment_candidate(name: str) -> float:
    """0..1: how confidently `name` names a dedicated item/weapon socket
    (PZ's `Bip01_Prop1`/`Bip01_Prop2`, or an equivalent on another rig)."""
    normalized = normalize_bone_name(name)
    words = set(normalized.split(" "))
    if "prop" in words or "weapon" in words:
        return 1.0
    if words & set(ATTACHMENT_TOKENS):
        return 0.85
    return _best_token_ratio(normalized, ATTACHMENT_TOKENS)


def find_hand_bones(bone_names: list[str]) -> dict:
    """Best left/right hand bone out of `bone_names`, fuzzy-matched and
    side-resolved by name.

    Returns `{"left": {"name", "score"} | None, "right": {...} | None,
    "unsided": [...]}` - `unsided` lists any bone that scored above the
    hand-candidate floor but whose name gave no left/right signal (e.g. a
    single-hand rig, or a name like "Hand" with no side marker), so a caller
    can resolve it from skeleton hierarchy (position relative to the spine's
    left/right split) instead of guessing.

    Does not consult bone hierarchy/position itself - name only. A caller
    that has the armature (Blender `bpy.types.Bone.parent` chain, or this
    pack's own `bones`/`parent` arrays from pzc_reader) should use hierarchy
    as a tie-breaker when two names score equally, or to resolve an
    `unsided` entry: walk up from the candidate and see which of the
    skeleton's two known arm chains it descends from.
    """
    MIN_SCORE = 0.55
    best: dict[str, dict | None] = {"left": None, "right": None}
    unsided: list[dict] = []
    for name in bone_names:
        score = score_hand_candidate(name)
        if score < MIN_SCORE:
            continue
        normalized = normalize_bone_name(name)
        side = _side_of(normalized)
        entry = {"name": name, "score": score}
        if side is None:
            unsided.append(entry)
            continue
        current = best[side]
        if current is None or score > current["score"]:
            best[side] = entry
    return {"left": best["left"], "right": best["right"], "unsided": unsided}


def find_attachment_bone(bone_names: list[str], preferred_side: str | None = "right") -> dict | None:
    """Best dedicated item/weapon-socket bone (PZ's Prop1/Prop2 or an
    equivalent), or None if the rig has none.

    Prefer this over `find_hand_bones()` for "where do I parent a held
    item" - see the module doc for why PZ itself never attaches to the hand
    bone. `preferred_side` breaks a tie between two equally-scored sockets
    (e.g. `Bip01_Prop1` vs `Bip01_Prop2`) by side only when the names carry
    a side marker; PZ's own Prop1/Prop2 do not. There is no single "the"
    attach bone on the real skeleton either way - PZ binds per *hand*, not
    per item (primary -> `Bip01_Prop1`, secondary -> `Bip01_Prop2`, see
    `build.HELD_ITEM_HAND_BONES` and docs/weapon_attachment.md), so a caller
    working with the actual shipped skeleton should pick the bone by which
    hand it's placing an item in rather than asking this function to guess
    at a fixed preference; `preferred_side` here is only a fallback for a
    rig with a genuinely ambiguous/unlabelled single socket.
    """
    scored = [(name, score_attachment_candidate(name)) for name in bone_names]
    scored = [(n, s) for n, s in scored if s >= 0.55]
    if not scored:
        return None
    if preferred_side:
        sided = [(n, s) for n, s in scored if _side_of(normalize_bone_name(n)) == preferred_side]
        if sided:
            scored = sided
    name, score = max(scored, key=lambda pair: pair[1])
    return {"name": name, "score": score}


def resolve_for_item(bone_names: list[str]) -> dict:
    """Convenience wrapper: the single bone this project's own convention
    (attach at the dedicated socket, never the hand) says to parent a held
    item to, plus the hand bones for reference/inspection.

    `{"attach_bone": {...} | None, "hand": find_hand_bones() result}`.
    `attach_bone` is None when the rig has no Prop-like socket at all, in
    which case a caller has to fall back to a hand bone plus a manually
    fitted offset - PZ itself never does this, so there is no mined
    transform to reuse in that case.
    """
    return {
        "attach_bone": find_attachment_bone(bone_names),
        "hand": find_hand_bones(bone_names),
    }
