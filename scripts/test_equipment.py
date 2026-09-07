"""Headless check of the two-hand equip rule.

    python scripts/test_equipment.py

Bpy-free, reads the real assets/items/item_manifest.json and
assets/characters/appearance_pools.json (for weapon_options()) shipped in
this repo - no Blender needed, same split as the other test_*.py files here.
"""
import os
import sys

# This file lives in pz-character/scripts/; PZCHAR_ROOT is pz-character
# itself, one level up.
PZCHAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PZCHAR_ROOT, "src", "addon", "pz_character_viewer", "character"))

import equipment
import pools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _charroot  # noqa: E402

CHARACTERS_ROOT = _charroot.require()


def main() -> int:
    failures = 0

    def check(label, cond):
        nonlocal failures
        status = "ok" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"[{status}] {label}")

    # --- synthetic manifest: full control over both_hands/two_hand/type ---
    fake_manifest = {
        "Base.Shovel": {"weapon": {"type": "2handed", "two_hand": True, "both_hands": False}},
        "Base.BarBell": {"weapon": {"type": "heavy", "two_hand": True, "both_hands": True}},
        "Base.Torch": {"weapon": {"type": "1handed", "two_hand": False, "both_hands": False}},
        "Base.HuntingKnife": {"weapon": {"type": "knife", "two_hand": False, "both_hands": False}},
    }

    eq = equipment.Equipment(fake_manifest)
    check("empty hands: both slots blank", eq.primary() == "" and eq.secondary() == "")
    check("empty hands: unarmed", eq.weapon_type() == equipment.UNARMED)

    # Independent left/right access with a plain one-handed item each.
    eq.equip_primary("Torch")
    eq.equip_secondary("HuntingKnife")
    check("independent hands: right = Torch", eq.primary() == "Torch")
    check("independent hands: left = HuntingKnife", eq.secondary() == "HuntingKnife")

    # A both_hands weapon in the right hand takes over the left too.
    eq.equip_primary("BarBell")
    check("both_hands weapon fills both slots", eq.primary() == "BarBell" and eq.secondary() == "BarBell")
    check("both_hands weapon type resolves", eq.weapon_type() == "heavy")

    # Only one two-handed item can ever be held: putting anything else in the
    # OTHER hand must evict the both-hands weapon from both slots, never
    # leave it half-held.
    eq.equip_secondary("Torch")
    check("equipping other hand evicts the both-hands weapon entirely",
          eq.primary() == "" and eq.secondary() == "Torch")

    # A "two_hand" (but not both_hands) weapon like the shovel does NOT
    # auto-fill the other hand - PZ only forces occupancy for
    # RequiresEquippedBothHands items. It can coexist with something in the
    # other hand (a torch), but only counts as its two-handed animation type
    # while the SAME item instance is in both slots.
    eq.unequip_all()
    eq.equip_primary("Shovel")
    check("two_hand-but-not-both_hands: does not auto-fill the left hand",
          eq.secondary() == "")
    check("held one-handed, so it demotes to its fallback type",
          eq.weapon_type() == "1handed")

    eq.equip_secondary("Shovel")
    check("the SAME shovel in both hands: real two-handed grip",
          eq.primary() == "Shovel" and eq.secondary() == "Shovel")
    check("genuinely two-handed now", eq.weapon_type() == "2handed")

    # Only one two-handed item, again: a different item bumping the primary
    # must not leave the old shovel stuck in the secondary.
    eq.equip_primary("Torch")
    check("swapping the primary out of a two-handed grip clears the old item from both hands",
          eq.primary() == "Torch" and eq.secondary() == "")

    # knife/heavy/throwing never demote (PZ returns them before the
    # both-hands check) - held one-handed or "two-handed", same type.
    eq.unequip_all()
    eq.equip_primary("HuntingKnife")
    check("knife type never demotes", eq.weapon_type() == "knife")

    # --- real repo data: Base.Shovel / Base.BarBell via item_manifest.json ---
    manifest_path = equipment._item_manifest_path(CHARACTERS_ROOT)
    if os.path.exists(manifest_path):
        real_manifest = equipment.load_item_manifest(CHARACTERS_ROOT)
        check("real item_manifest.json: Base.Shovel resolves by suffix join",
              equipment.weapon_info(real_manifest, "Shovel") is not None)
        check("real data: Shovel is 2handed, not both_hands",
              equipment.weapon_type_of(real_manifest, "Shovel") == "2handed"
              and not equipment.requires_both_hands(real_manifest, "Shovel"))
        check("real data: BarBell requires both hands",
              equipment.requires_both_hands(real_manifest, "BarBell"))

        real_eq = equipment.Equipment(real_manifest)
        real_eq.equip_primary("BarBell")
        check("real BarBell auto-fills both hands", real_eq.secondary() == "BarBell")
    else:
        print(f"[skip] {manifest_path} not found - run tools/pz_items.py first")

    # weapon_options() should list Shovel/BarBell and start with the empty
    # sentinel, if appearance_pools.json is present.
    pools_path = os.path.join(CHARACTERS_ROOT, "appearance_pools.json")
    if os.path.exists(pools_path):
        pools_data = pools.load(CHARACTERS_ROOT)
        options = pools.weapon_options(pools_data)
        check("weapon_options starts with the empty sentinel", options[:1] == [pools.NONE_ITEM])
        check("weapon_options lists Shovel", "Shovel" in options)

        layer_slots = [layer["slot"] for layer in pools.layers_for_gender(pools_data, "male")]
        check("layers_for_gender no longer emits a single 'weapon' layer",
              "weapon" not in layer_slots)
        check("layers_for_gender emits both 'weapon_right' and 'weapon_left'",
              "weapon_right" in layer_slots and "weapon_left" in layer_slots)
        right_layer = next(l for l in pools.layers_for_gender(pools_data, "male") if l["slot"] == "weapon_right")
        left_layer = next(l for l in pools.layers_for_gender(pools_data, "male") if l["slot"] == "weapon_left")
        check("both hand layers share the same option list", right_layer["options"] == left_layer["options"])

        # Schema regression guard - see src/docs/
        # weapon_attachment.md. A weapon entry must ship attachments KEYED
        # BY SOCKET ID (build._build_held_item() looks one up by the bone
        # it's actually binding to), never a single flattened bone/
        # attach_offset/attach_rotate pick - that shape is exactly the bug
        # this schema replaces.
        weapon_entries = pools_data.get("clothing", {}).get("weapon", [])
        check("weapon pool entries exist", bool(weapon_entries))
        check("no weapon entry carries the old flattened bone/attach_offset/attach_rotate keys",
              all("bone" not in e and "attach_offset" not in e and "attach_rotate" not in e
                  for e in weapon_entries))
        check("every weapon entry carries an 'attachments' dict",
              all(isinstance(e.get("attachments"), dict) for e in weapon_entries))

        shovel = next((e for e in weapon_entries if e.get("item") == "Shovel"), None)
        check("Shovel entry found", shovel is not None)
        if shovel is not None:
            check("Shovel declares Bip01_Prop2 (secondary-hand socket)",
                  "Bip01_Prop2" in shovel["attachments"])
            check("Shovel does NOT declare Bip01_Prop1 - "
                  "so a right-hand (primary) shovel gets no grip rotation, matching PZ",
                  "Bip01_Prop1" not in shovel["attachments"])
            pose = shovel["attachments"]["Bip01_Prop2"]
            check("Shovel's Bip01_Prop2 pose has offset+rotate lists",
                  len(pose.get("offset", [])) == 3 and len(pose.get("rotate", [])) == 4)

        # At least one real item declares a Bip01_Prop1 block - the rare
        # (10 of 294 models) primary-hand-grip-correction case. Identified
        # by grepping models_weapons.txt for "attachment Bip01_Prop1" and
        # cross-referencing weapon.txt for the item name; EngineMaul is one.
        prop1_item = next((e for e in weapon_entries if "Bip01_Prop1" in e.get("attachments", {})), None)
        check("at least one weapon entry declares a Bip01_Prop1 block",
              prop1_item is not None)
        if prop1_item is not None:
            pose = prop1_item["attachments"]["Bip01_Prop1"]
            check(f"{prop1_item['item']}'s Bip01_Prop1 pose has offset+rotate lists",
                  len(pose.get("offset", [])) == 3 and len(pose.get("rotate", [])) == 4)
    else:
        print(f"[skip] {pools_path} not found - run tools/pz_characters.py first")

    # resolve_hands(): the same collapse rule as Equipment.equip_primary()/
    # equip_secondary(), but as a pure function over a (right, left) pair -
    # what ops._rebuild_scene() runs the two layers' raw picks through.
    right, left, wtype = equipment.resolve_hands(fake_manifest, "BarBell", "Torch")
    check("resolve_hands: a both-hands weapon on the right evicts an unrelated left pick",
          right == "" and left == "Torch")
    right, left, wtype = equipment.resolve_hands(fake_manifest, "Shovel", "Shovel")
    check("resolve_hands: same item both sides -> real two-handed grip",
          right == "Shovel" and left == "Shovel" and wtype == "2handed")
    right, left, wtype = equipment.resolve_hands(fake_manifest, "Torch", "HuntingKnife")
    check("resolve_hands: two independent one-handed picks pass through unchanged",
          right == "Torch" and left == "HuntingKnife")
    right, left, wtype = equipment.resolve_hands(fake_manifest, "", "")
    check("resolve_hands: both empty -> unarmed", right == "" and left == "" and wtype == equipment.UNARMED)
    # Idempotent: re-resolving an already-legal pair returns it unchanged -
    # this is what makes writing the corrected values back into the layers
    # (ops._rebuild_scene) safe to do on every rebuild, not just the one
    # that actually changed something.
    once = equipment.resolve_hands(fake_manifest, "BarBell", "BarBell")
    twice = equipment.resolve_hands(fake_manifest, once[0], once[1])
    check("resolve_hands is idempotent", once == twice)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
