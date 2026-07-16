"""
The backend half of the tier-contract guard.

AIBOS is two repos that deploy independently and each kept its own copy of what
a plan unlocks. Nothing compared them, so they drifted and shipped real bugs
(see tier_contract.json for the roll of honour). tier_contract.json is now the
authoritative statement; this suite asserts the SERVER matches it, and the
frontend's scripts/check_tier_contract.py asserts tiers.ts matches the same file.

No network and no parsing here — the backend can simply import its own maps,
which is why this side is the robust one. Run as a plain script like the others.
"""

import json
import pathlib

import entitlements
import main

CONTRACT = json.loads((pathlib.Path(__file__).parent / "tier_contract.json").read_text("utf-8"))


def test_contract_file_is_not_vacuous():
    # A guard that silently parses nothing would pass forever while drift ships.
    # Pin the shape so an empty or half-written contract fails loudly instead.
    assert set(CONTRACT["access"]) == {"free", "pro", "proplus", "growth"}
    assert CONTRACT["access"]["free"] == []
    assert len(CONTRACT["access"]["pro"]) >= 10
    assert CONTRACT["taster"]["ai_chat"] >= 1
    assert set(CONTRACT["prices"]) == {"pro", "proplus", "growth"}


def test_access_map_matches_entitlements():
    for tier, features in CONTRACT["access"].items():
        assert sorted(entitlements._ACCESS[tier]) == sorted(features), (
            f"entitlements.py _ACCESS[{tier!r}] disagrees with tier_contract.json.\n"
            f"  only in code:     {sorted(set(entitlements._ACCESS[tier]) - set(features))}\n"
            f"  only in contract: {sorted(set(features) - set(entitlements._ACCESS[tier]))}"
        )
    assert set(entitlements._ACCESS) == set(CONTRACT["access"])


def test_paid_tiers_are_supersets():
    # PRO ⊂ PROPLUS ⊂ GROWTH is the ladder's promise: upgrading must never take a
    # feature away. entitlements.py builds these by union so it holds by
    # construction — the contract file is hand-editable, so check it here.
    pro = set(CONTRACT["access"]["pro"])
    proplus = set(CONTRACT["access"]["proplus"])
    growth = set(CONTRACT["access"]["growth"])
    assert pro <= proplus <= growth


def test_taster_matches_entitlements():
    assert CONTRACT["taster"]["ai_chat"] == entitlements.CHAT_TASTER_PER_DAY


def test_prices_match_main():
    assert main.PLAN_PRICES == CONTRACT["prices"], (
        "main.py PLAN_PRICES disagrees with tier_contract.json — the frontend "
        "quotes these numbers on the pricing page and this is what gets charged."
    )


def test_annual_is_ten_months():
    # The ladder's stated deal: billed yearly, two months free.
    for plan, p in CONTRACT["prices"].items():
        assert p["annual"] == p["monthly"] * 10, f"{plan}: annual is not 10x monthly"


def test_reserved_flags_are_growth_only_and_unbuilt():
    # 'multi_location' and 'api_access' are deliberately reserved flags for
    # UNBUILT features. They may sit in the map, but must never leak into a
    # cheaper tier and be read as a live inclusion.
    for flag in ("multi_location", "api_access"):
        assert flag in CONTRACT["access"]["growth"]
        assert flag not in CONTRACT["access"]["pro"]
        assert flag not in CONTRACT["access"]["proplus"]


def test_hospitality_is_still_provisionally_in_pro():
    # PROVISIONAL by decision, not accident: parked in Pro so the v1 client can
    # use it, and it moves to its own add-on SKU later. If this fails, that move
    # has happened (or someone fat-fingered it) — update the contract knowingly.
    assert "hospitality" in CONTRACT["access"]["pro"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} tier-contract tests passed ===")
