"""Headless check of the fuzzy hand/attachment-bone finder.

    python scripts/test_hand_bones.py

Bpy-free, same split as test_character_armature.py: reads bone names out of
the shipped assets/characters/characters.pzc via pzc_reader, no Blender.
"""
import os
import sys

# This file lives in pz-character/scripts/; PZCHAR_ROOT is pz-character
# itself, one level up.
PZCHAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PZCHAR_ROOT, "src", "addon", "pz_character_viewer", "character"))

from pzc_reader import PZCReader
from hand_bones import find_hand_bones, find_attachment_bone, resolve_for_item

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _charroot  # noqa: E402

PACK_PATH = os.path.join(_charroot.require(), "characters.pzc")


def _synthetic_bone_sets():
    """A handful of naming conventions the module has to survive, beyond
    PZ's own Bip01 rig - none of these come from a real file, they exercise
    normalize_bone_name()/side resolution directly."""
    return {
        "mixamo": ["mixamorig:Hips", "mixamorig:LeftHand", "mixamorig:RightHand",
                   "mixamorig:LeftForeArm", "mixamorig:RightForeArm"],
        "unreal_style": ["pelvis", "hand_l", "hand_r", "lowerarm_l", "lowerarm_r"],
        "dotted": ["Hand.L", "Hand.R", "Forearm.L", "Forearm.R"],
        "generic_camel": ["LeftHand", "RightHand", "LeftWrist", "RightWrist"],
        "no_side_single_hand": ["Root", "Hand"],
    }


def main() -> int:
    failures = 0

    def check(label, cond):
        nonlocal failures
        status = "ok" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"[{status}] {label}")

    # --- real shipped skeleton, via the pack (bone names only) ---
    if os.path.exists(PACK_PATH):
        reader = PZCReader(PACK_PATH)
        bone_names = [b["name"] for b in reader.skeleton()]
        reader.close()

        result = find_hand_bones(bone_names)
        check("PZ pack: left hand found",
              result["left"] is not None and result["left"]["name"] == "Bip01_L_Hand")
        check("PZ pack: right hand found",
              result["right"] is not None and result["right"]["name"] == "Bip01_R_Hand")
        check("PZ pack: hand scores are exact (1.0)",
              result["left"] and result["left"]["score"] == 1.0
              and result["right"] and result["right"]["score"] == 1.0)
        check("PZ pack: forearm/finger bones not chosen as hands",
              result["left"]["name"] != "Bip01_L_Forearm"
              and result["right"]["name"] != "Bip01_R_Forearm")

        attach = find_attachment_bone(bone_names, preferred_side=None)
        check("PZ pack: attachment socket found (Prop1 or Prop2)",
              attach is not None and attach["name"] in ("Bip01_Prop1", "Bip01_Prop2"))

        bundle = resolve_for_item(bone_names)
        check("resolve_for_item: attach_bone != hand bone (PZ never attaches to the hand)",
              bundle["attach_bone"] is not None
              and bundle["attach_bone"]["name"] not in
              (bundle["hand"]["left"]["name"] if bundle["hand"]["left"] else None,
               bundle["hand"]["right"]["name"] if bundle["hand"]["right"] else None))
    else:
        print(f"[skip] {PACK_PATH} not found - run tools/pz_characters.py first")

    # --- synthetic naming conventions ---
    sets = _synthetic_bone_sets()

    r = find_hand_bones(sets["mixamo"])
    check("mixamo: left/right hand resolved",
          r["left"]["name"] == "mixamorig:LeftHand" and r["right"]["name"] == "mixamorig:RightHand")

    r = find_hand_bones(sets["unreal_style"])
    check("unreal-style hand_l/hand_r: left/right resolved",
          r["left"]["name"] == "hand_l" and r["right"]["name"] == "hand_r")

    r = find_hand_bones(sets["dotted"])
    check("dotted Hand.L/Hand.R: left/right resolved",
          r["left"]["name"] == "Hand.L" and r["right"]["name"] == "Hand.R")

    r = find_hand_bones(sets["generic_camel"])
    check("LeftHand/RightHand: resolved and outrank LeftWrist/RightWrist",
          r["left"]["name"] == "LeftHand" and r["right"]["name"] == "RightHand")

    r = find_hand_bones(sets["no_side_single_hand"])
    check("single unsided 'Hand' bone lands in unsided, not guessed to a side",
          r["left"] is None and r["right"] is None
          and len(r["unsided"]) == 1 and r["unsided"][0]["name"] == "Hand")

    # A rig with only a hand bone and no prop/weapon socket: attachment
    # lookup must return None, not silently fall back to the hand.
    check("no attachment bone on a plain hand-only rig -> None",
          find_attachment_bone(sets["generic_camel"]) is None)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
