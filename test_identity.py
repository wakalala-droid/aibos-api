"""
Offline tests for identity.py — the pure half (normalisation, phone matching,
industry mapping, hours, city, favicon, scoring, ranking). No network: search()
is the only function that touches Places, and it is guarded by an env key that
is absent here. Run as a plain script like the other suites.
"""

import identity as idt


# ── normalise / phones ──────────────────────────────────────────────────────

def test_normalize_strips_legal_forms_and_punctuation():
    assert idt.normalize("Mwansa General Dealers Ltd.") == "mwansa general dealers"
    assert idt.normalize("Chanda & Sons (Zambia) Limited") == "chanda and sons"
    assert idt.normalize("  DUNSLIM   Apartments  ") == "dunslim apartments"
    assert idt.normalize(None) == ""


def test_phones_match_across_formats():
    assert idt.phones_match("+260 977 123456", "0977123456")
    assert idt.phones_match("260977123456", "0977 123 456")
    assert not idt.phones_match("0977123456", "0966123456")
    assert not idt.phones_match("1234", "1234")          # too short to be proof
    assert not idt.phones_match(None, "0977123456")


# ── industry mapping ────────────────────────────────────────────────────────

def test_map_industry_specific_type_beats_generic_store():
    # Google tags many restaurants "store" too — the specific type must win.
    assert idt.map_industry(["store", "restaurant", "point_of_interest"]) == "Restaurant / Food"
    assert idt.map_industry(["lodging", "store"]) == "Hospitality"
    assert idt.map_industry(["grocery_store"]) == "Retail"
    assert idt.map_industry(["hair_salon"]) == "Services"
    assert idt.map_industry(["taxi_service"]) == "Transport"


def test_map_industry_unknown_is_blank_not_a_guess():
    assert idt.map_industry(["point_of_interest", "establishment"]) == ""
    assert idt.map_industry([]) == ""
    assert idt.map_industry(None) == ""


def test_map_industry_falls_back_on_store_suffix():
    assert idt.map_industry(["pet_store"]) == "Retail"


def test_map_industry_values_are_wizard_selectable():
    # Anything this returns is written straight into the wizard's <select>.
    allowed = {"Retail", "Restaurant / Food", "Services", "Wholesale", "Hospitality",
               "Manufacturing", "Agriculture", "Mining", "Transport", "Other", ""}
    for needles, industry in idt._INDUSTRY_RULES:
        assert industry in allowed, industry


# ── hours ───────────────────────────────────────────────────────────────────

def _period(day, oh, ch=None):
    p = {"open": {"day": day, "hour": oh, "minute": 0}}
    if ch is not None:
        p["close"] = {"day": day, "hour": ch, "minute": 0}
    return p


def test_format_hours_reports_the_modal_window():
    periods = [_period(d, 8, 18) for d in range(1, 6)] + [_period(6, 9, 13)]
    assert idt.format_hours({"periods": periods}) == "08:00 – 18:00"


def test_format_hours_open_24_hours():
    assert idt.format_hours({"periods": [_period(0, 0)]}) == "Open 24 hours"


def test_format_hours_missing_or_odd_is_blank():
    assert idt.format_hours(None) == ""
    assert idt.format_hours({}) == ""
    assert idt.format_hours({"periods": [{"open": {}}, "junk"]}) == ""


# ── city ────────────────────────────────────────────────────────────────────

def test_city_prefers_locality_component():
    comps = [{"longText": "Zambia", "types": ["country"]},
             {"longText": "Lusaka", "types": ["locality", "political"]}]
    assert idt.city_from("Plot 12, Kabulonga, Lusaka, Zambia", comps) == "Lusaka"


def test_city_falls_back_to_second_last_address_part():
    assert idt.city_from("Plot 12, Kabulonga, Lusaka, Zambia") == "Lusaka"
    assert idt.city_from("Lusaka") == "Lusaka"
    assert idt.city_from("") == ""


# ── favicon ─────────────────────────────────────────────────────────────────

def test_favicon_url_from_bare_and_full_urls():
    assert "domain=dunslim.co.zm" in idt.favicon_url("https://dunslim.co.zm/rooms")
    assert "domain=dunslim.co.zm" in idt.favicon_url("dunslim.co.zm")
    assert idt.favicon_url("") == ""
    assert idt.favicon_url(None) == ""
    assert idt.favicon_url("not a url") == ""


def test_favicon_url_never_carries_a_key():
    assert "key=" not in idt.favicon_url("https://dunslim.co.zm")


# ── scoring ─────────────────────────────────────────────────────────────────

def test_score_prefix_typing_scores_high():
    assert idt.score_match("Dunslim", "Dunslim Apartments") >= 0.8


def test_score_phone_match_is_near_proof():
    weak = idt.score_match("Mwansa", "Mwansa General Dealers")
    strong = idt.score_match("Mwansa", "Mwansa General Dealers",
                             "+260977123456", "0977 123 456")
    assert strong > weak and strong >= 0.9


def test_score_unrelated_is_low():
    assert idt.score_match("Dunslim Apartments", "Shoprite Manda Hill") < idt.MIN_CONFIDENCE


# ── candidate / ranking ─────────────────────────────────────────────────────

def _place(name, **kw):
    p = {"id": kw.pop("id", "p1"), "displayName": {"text": name}}
    p.update(kw)
    return p


def test_to_candidate_maps_the_fields_the_wizard_fills():
    c = idt.to_candidate(_place(
        "Dunslim Apartments",
        formattedAddress="Plot 12, Kabulonga, Lusaka, Zambia",
        types=["lodging"],
        nationalPhoneNumber="0977 123 456",
        websiteUri="https://dunslim.co.zm",
        regularOpeningHours={"periods": [_period(0, 0)]},
        rating=4.6, userRatingCount=38,
    ), "Dunslim")
    assert c["business_name"] == "Dunslim Apartments"
    assert c["industry"] == "Hospitality"
    assert c["location"] == "Lusaka"
    assert c["operating_hours"] == "Open 24 hours"
    assert c["phone"] == "0977 123 456"
    assert c["logo_url"].startswith("https://www.google.com/s2/favicons")
    assert c["rating"] == 4.6 and c["reviews"] == 38
    assert c["source"] == "google_places" and c["confidence"] >= 0.8
    assert c["closed"] is False


def test_to_candidate_survives_a_sparse_place():
    c = idt.to_candidate({"displayName": {"text": "Kabwe Hardware"}}, "Kabwe Hardware")
    assert c["industry"] == "" and c["location"] == "" and c["logo_url"] == ""
    assert c["rating"] is None and c["reviews"] is None


def test_rank_drops_noise_orders_by_confidence_and_caps():
    places = [_place("Shoprite Manda Hill", id="a"),
              _place("Dunslim Apartments", id="b"),
              _place("Dunslim Apartments Annex", id="c"),
              _place("Dunslim Lodge", id="d"),
              _place("Dunslim Court", id="e")]
    out = idt.rank(places, "Dunslim Apartments")
    assert len(out) <= idt.MAX_CANDIDATES
    assert out[0]["business_name"] == "Dunslim Apartments"
    assert all(c["confidence"] >= idt.MIN_CONFIDENCE for c in out)
    assert "Shoprite Manda Hill" not in [c["business_name"] for c in out]


def test_rank_drops_nameless_results():
    assert idt.rank([{"id": "x"}], "Dunslim") == []


def test_closed_listing_sinks_below_an_equal_open_one():
    places = [_place("Dunslim Apartments", id="a", businessStatus="CLOSED_PERMANENTLY"),
              _place("Dunslim Apartments", id="b")]
    out = idt.rank(places, "Dunslim Apartments")
    assert out[0]["place_id"] == "b" and out[0]["closed"] is False


# ── fail-soft contract ──────────────────────────────────────────────────────

def test_search_without_a_key_is_silent_and_empty():
    assert idt.available() is False or True   # env-dependent; the point is below
    if not idt.available():
        assert idt.search("Dunslim Apartments") == []


def test_search_ignores_too_short_queries():
    assert idt.search("Du") == []


def test_cache_key_is_stable_across_spellings():
    a = idt.cache_key("Dunslim Apartments Ltd", "ZM", "+260977123456")
    b = idt.cache_key("  dunslim apartments  ", "zm", "0977 123 456")
    assert a == b


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} identity tests passed ===")
