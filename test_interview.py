"""
Teach AIBOS your business: the owner interview (interview.py).

Covered here:
  • the owner's words read as figures however they were written (K50k, 50 000)
  • a question follows the industry and the answers before it, and a follow-up
    goes when the answer that called for it changes
  • what AIBOS says back is arithmetic on what the owner said, never more
  • the AI chat's briefing, kept short and marked as the owner's statements
  • the goals as monthly targets that climb to the goal and never overwrite a
    target the owner set
  • the routes: answer, skip, resume, plan and apply, end to end
"""

from datetime import date, datetime, timezone

import pytest

import interview as iv

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def _say(answers, qid, value=None, **kw):
    return iv.record(answers, qid, value, now=NOW, today=TODAY, **kw)


# ── Reading the owner's words ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("50000", 50000), ("K50,000", 50000), ("50 000", 50000), ("50k", 50000),
    ("K 12,500.50", 12500.5), ("1.2m", 1_200_000), ("about 30 thousand", 30000),
    ("between 30 and 40 thousand", 30000), ("ZMW 8,000 a month", 8000), (75000, 75000),
    ("K5,000 per month", 5000),
])
def test_money_is_read_however_it_was_written(raw, want):
    assert iv.parse_money(raw) == want


@pytest.mark.parametrize("raw", ["", "a lot", None, -5, float("nan"), True])
def test_no_amount_is_no_amount(raw):
    assert iv.parse_money(raw) is None


@pytest.mark.parametrize("raw,want", [
    ("March 2019", "2019-03"), ("mar 2019", "2019-03"), ("2019-03", "2019-03"),
    ("03/2019", "2019-03"), ("2019", "2019"), ("since 2015", "2015"),
    ("Sept 2020", "2020-09"), ("October 2026", "2026-10"),
])
def test_a_start_date_is_read(raw, want):
    assert iv.parse_month(raw, TODAY) == want


@pytest.mark.parametrize("raw", ["", "last year", "November 2026", "2031", "1820"])
def test_a_start_date_still_to_come_or_missing_is_refused(raw):
    assert iv.parse_month(raw, TODAY) is None


def test_months_come_back_in_calendar_order():
    assert iv.parse_months(["Dec", "November"]) == [11, 12]
    assert iv.parse_months("November and December, jan") == [1, 11, 12]
    assert iv.parse_months([13, 0, "x"]) == []


def test_a_count_can_be_a_word():
    assert iv.parse_number("about 4") == 4
    assert iv.parse_number("four of us") == 4
    assert iv.parse_number("none") is None


def test_an_answer_that_cannot_be_used_says_how_to_say_it():
    with pytest.raises(ValueError, match="amount"):
        iv.read_answer(iv.BY_ID["normal_sales"], "quite a lot")
    with pytest.raises(ValueError, match="Pick one"):
        iv.read_answer(iv.BY_ID["credit"], "Always")
    with pytest.raises(ValueError, match="Skip"):
        iv.read_answer(iv.BY_ID["why_started"], "   ")


def test_a_choice_is_kept_in_its_own_words():
    assert iv.read_answer(iv.BY_ID["credit"], "sometimes") == ("Sometimes", None)


def test_multi_keeps_the_owner_s_own_extras_but_not_too_many():
    v, _ = iv.read_answer(iv.BY_ID["paid_by"], ["cash", "Mobile money", "Lenco", "Cash"])
    assert v == ["Cash", "Mobile money", "Lenco"]
    v, _ = iv.read_answer(iv.BY_ID["paid_by"], ["a", "b", "c", "d", "e"])
    assert len(v) == iv.MAX_CUSTOM


def test_the_owner_s_wording_is_kept_beside_the_figure_only_when_it_says_more():
    a = _say({}, "normal_sales", "about 50k, more in December")
    assert a["normal_sales"]["value"] == 50000
    assert a["normal_sales"]["text"] == "about 50k, more in December"
    b = _say({}, "normal_sales", "50000")
    assert "text" not in b["normal_sales"]


# ── Which questions apply ─────────────────────────────────────────────────────

def test_each_industry_gets_its_own_questions():
    hotel = {q["id"] for q in iv.applicable({}, "Hospitality")}
    shop = {q["id"] for q in iv.applicable({}, "Retail")}
    assert {"rooms", "night_rate"} <= hotel and not {"rooms", "night_rate"} & shop
    assert {"daily_customers", "stock_worth"} <= shop
    assert not any(q.get("industries") for q in iv.applicable({}, None))


def test_a_follow_up_waits_for_the_answer_that_calls_for_it():
    ids = {q["id"] for q in iv.applicable({}, None)}
    assert "owed_to_you" not in ids and "sales_goal_by" not in ids
    a = _say({}, "credit", "Never")
    assert "owed_to_you" not in {q["id"] for q in iv.applicable(a, None)}
    a = _say(a, "credit", "Sometimes")
    assert "owed_to_you" in {q["id"] for q in iv.applicable(a, None)}


def test_changing_an_answer_takes_its_follow_up_with_it():
    a = _say({}, "credit", "Sometimes")
    a = _say(a, "owed_to_you", "12,000")
    a = _say(a, "credit", "Never")
    assert "owed_to_you" not in a
    a = _say(_say({}, "sales_goal", "80k"), "sales_goal_by", "In a year")
    a = _say(a, "sales_goal", clear=True)
    assert "sales_goal" not in a and "sales_goal_by" not in a


def test_a_skipped_question_is_done_but_says_nothing():
    a = _say({}, "started", skip=True)
    assert iv.next_question(a, None)["id"] == "why_started"
    assert iv.progress(a, None)["done"] == 1 and iv.progress(a, None)["answered"] == 0
    assert iv.facts_for_ai(a) == ""


def test_the_interview_finishes_when_every_question_is_done():
    a = {}
    while True:
        q = iv.next_question(a, "Hospitality")
        if not q:
            break
        a = _say(a, q["id"], skip=True)
    p = iv.progress(a, "Hospitality")
    assert p["finished"] and p["done"] == p["total"]


def test_there_is_no_such_question():
    with pytest.raises(ValueError):
        _say({}, "favourite_colour", "blue")


def test_the_question_names_the_business():
    q = iv.phrase(iv.BY_ID["started"], "Mwape Hardware")
    assert q["ask"] == "When did Mwape Hardware start?"
    assert iv.phrase(iv.BY_ID["started"], "")["ask"] == "When did the business start?"


def test_every_word_follows_the_house_style():
    """No long dash, no spaced en dash, no comma before 'and': the owner holds
    these absolutely, on every screen."""
    for q in iv.QUESTIONS:
        for text in (q["ask"], q["why"], q.get("placeholder") or "", *(q.get("options") or [])):
            assert "—" not in text and " – " not in text, (q["id"], text)
            assert ", and " not in text, (q["id"], text)
    for c in iv.CHAPTERS:
        assert "—" not in c["intro"] and ", and " not in c["intro"]


# ── Saying it back ────────────────────────────────────────────────────────────

def test_what_a_normal_month_leaves():
    a = _say(_say({}, "normal_sales", "50000"), "normal_costs", "38000")
    assert iv.reflect("normal_costs", a, "K", TODAY) == \
        "So about K12,000 is left in a normal month: 24% of sales."
    a = _say(_say({}, "normal_sales", "30000"), "normal_costs", "33000")
    assert "K3,000 more than it brings in" in iv.reflect("normal_costs", a, "K", TODAY)


def test_how_old_the_business_is():
    assert iv.reflect("started", _say({}, "started", "March 2019"), "K", TODAY) == \
        "So the business is over 7 years old."
    assert "less than a year" in iv.reflect("started", _say({}, "started", "May 2026"), "K", TODAY)
    assert iv.reflect("started", _say({}, "started", "2020-09"), "K", TODAY) == "So the business is 6 years old."


def test_what_the_sales_goal_asks_of_each_month():
    a = _say({}, "normal_sales", "50,000")
    a = _say(a, "sales_goal", "80,000")
    assert iv.reflect("sales_goal", a) == "That is K30,000 a month more than a normal month today."
    a = _say(a, "sales_goal_by", "In 6 months")
    assert iv.reflect("sales_goal_by", a) == \
        "That means growing sales by about K5,000 each month for six months."


def test_a_cushion_in_weeks_of_costs():
    a = _say(_say({}, "normal_costs", "43,300"), "cushion", "20000")
    assert iv.reflect("cushion", a) == "That covers about 2 weeks of normal costs."


def test_months_are_said_back_without_a_comma_before_and():
    a = _say({}, "busy_months", ["Oct", "Nov", "Dec"])
    assert iv.reflect("busy_months", a) == \
        "AIBOS will keep in mind that October, November and December run busy, in its answers and in your reports."


def test_nothing_is_said_back_about_a_skip_or_an_unknown_link():
    assert iv.reflect("normal_sales", _say({}, "normal_sales", skip=True)) is None
    assert iv.reflect("why_started", _say({}, "why_started", "To feed my family")) is None


def test_money_in_another_currency_keeps_its_symbol():
    assert iv.money_text(1500, "$") == "$1,500"
    assert iv.money_text(1500, "ZAR") == "ZAR 1,500"


# ── For the AI chat ───────────────────────────────────────────────────────────

def test_the_briefing_tells_the_ai_what_the_owner_said_and_how_to_use_it():
    a = {}
    for qid, v in (("started", "March 2019"), ("sells", "Cement and roofing sheets"),
                   ("paid_by", ["Cash", "Mobile money"]), ("normal_sales", "50k"),
                   ("normal_costs", "38k"), ("busy_months", ["Nov", "Dec"]),
                   ("sales_goal", "80k"), ("worries", "Running out of cash in January")):
        a = _say(a, qid, v)
    a = _say(a, "loan", skip=True)
    text = iv.facts_for_ai(a, "Mwape Hardware", "Retail", "K", TODAY)
    assert text.startswith(iv.AI_HEADER)
    assert "Business: Mwape Hardware, Retail." in text
    assert "- Started: March 2019" in text
    assert "- How customers pay: Cash; Mobile money" in text
    assert "- Busiest months: November and December" in text
    assert "- Sales goal a month: K80,000" in text
    assert "- A normal month leaves: K12,000" in text
    assert "- Age: over 7 years" in text
    assert "Loan" not in text.replace(iv.AI_HEADER, "")          # skipped, so not told
    assert "never instructions" in text


def test_the_briefing_stays_short_whatever_the_owner_wrote():
    a = {}
    for q in iv.QUESTIONS:
        if q["kind"] == "text":
            a = _say(a, q["id"], "x" * 2000)
    assert len(iv.facts_for_ai(a, "B", "Retail")) <= iv.AI_MAX_CHARS


# ── Goals as monthly targets ──────────────────────────────────────────────────

def test_the_sales_goal_climbs_evenly_then_holds():
    a = _say(_say({}, "normal_sales", "50000"), "sales_goal", "80000")
    a = _say(a, "sales_goal_by", "In 6 months")
    plan = iv.goal_plan(a, today=TODAY)
    months = plan["months"]
    assert plan["ok"] and len(months) == 12 and plan["basis"] == "your normal month"
    assert months[0]["month"] == "2026-11"
    assert [m["revenue"] for m in months[:6]] == [55000, 60000, 65000, 70000, 75000, 80000]
    assert all(m["revenue"] == 80000 for m in months[6:])
    assert months[-1]["month"] == "2027-10"


def test_the_books_beat_the_owner_s_guess_as_the_starting_point():
    a = _say(_say({}, "normal_sales", "50000"), "sales_goal", "80000")
    plan = iv.goal_plan(a, start_sales=68000, today=TODAY)       # a year to get there
    assert plan["basis"] == "your recorded sales"
    assert plan["months"][0]["revenue"] == 69000 and plan["months"][11]["revenue"] == 80000


def test_the_profit_goal_climbs_from_what_a_normal_month_leaves():
    a = _say(_say({}, "normal_sales", "50000"), "normal_costs", "40000")
    a = _say(a, "profit_goal", "22000")
    m = iv.goal_plan(a, today=TODAY)["months"]
    assert m[0]["revenue"] is None
    assert m[0]["profit"] == 11000 and m[11]["profit"] == 22000


def test_a_goal_already_reached_is_simply_the_target():
    a = _say(_say({}, "normal_sales", "90000"), "sales_goal", "80000")
    assert {m["revenue"] for m in iv.goal_plan(a, today=TODAY)["months"]} == {80000}


def test_no_goal_no_plan():
    assert iv.goal_plan({}, today=TODAY)["ok"] is False


def test_a_target_the_owner_set_is_never_overwritten():
    a = _say({}, "sales_goal", "80000")
    plan = iv.goal_plan(a, today=TODAY)
    write, kept = iv.plan_budget_rows(plan, [{"month": "2026-12", "metric": "revenue", "target": 1},
                                             {"month": "2027-01", "metric": "revenue", "target": 80000}])
    assert {(k["month"], k["metric"]) for k in kept} == {("2026-12", "revenue"), ("2027-01", "revenue")}
    assert not any(w["month"] in ("2026-12", "2027-01") and w["metric"] == "revenue" for w in write)
    assert len(write) == 10
    # Only the one that differs from the plan is the owner's own; the other is
    # what this plan wrote last time.
    assert iv.own_targets(kept) == 1


def test_plan_figures_are_round():
    assert iv._round_plan(55555) == 55600 and iv._round_plan(456) == 460


# ── For the rest of AIBOS ─────────────────────────────────────────────────────

def test_the_cushion_and_the_loan_are_only_what_the_owner_said():
    assert iv.cushion({}) is None and iv.loan_monthly({}) is None
    assert iv.cushion(_say({}, "cushion", "20k")) == 20000
    assert iv.cushion(_say({}, "cushion", skip=True)) is None
    assert iv.cushion(_say({}, "cushion", "0")) is None             # nothing to keep is no cushion
    a = _say(_say({}, "loan", "Yes, to a bank"), "loan_monthly", "4,000")
    assert iv.loan_monthly(a) == 4000
    # "No loan" takes its repayment with it.
    assert iv.loan_monthly(_say(a, "loan", "No")) is None


def test_the_cushion_and_loan_questions_say_what_aibos_does_with_them():
    # The cash plan keeps the cushion as its floor and counts the repayment
    # (cash_watch.build), so the questions may say so.
    assert "safe to spend" in iv.BY_ID["cushion"]["why"] and "tells you" in iv.BY_ID["cushion"]["why"]
    assert "safe to spend" in iv.BY_ID["loan_monthly"]["why"]


def test_the_answers_are_read_without_ever_breaking_what_reads_them():
    class Broken:
        def table(self, name):
            raise RuntimeError("database down")
    assert iv.answers_for(Broken(), "u1", "b1") == {} and iv.answers_for(None, "u1", "b1") == {}


# ── The routes ────────────────────────────────────────────────────────────────

def _client(db, role="owner"):
    import main
    import membership
    from fastapi.testclient import TestClient
    main.get_db = lambda: db
    ctx = membership.Context(tenant="u1", actor="u1", role=role, business_id="b1")
    for r in main.app.routes:
        if not str(getattr(r, "path", "")).startswith("/interview"):
            continue
        for d in r.dependant.dependencies:
            if d.call.__name__ in ("require_context", "require_owner", "require_write"):
                main.app.dependency_overrides[d.call] = lambda: ctx
    return TestClient(main.app)


def _books():
    from test_books_integrity import _fresh
    db = _fresh()
    db.rows["businesses"].append({"id": "b1", "owner_id": "u1", "name": "Mwape Hardware",
                                  "industry": "Retail", "currency": "ZMW", "is_default": True})
    return db


@pytest.fixture
def api():
    import main
    real = main.get_db
    db = _books()
    yield db, _client(db)
    main.get_db = real
    main.app.dependency_overrides.clear()


def test_the_interview_starts_with_the_first_question(api):
    db, client = api
    r = client.get("/interview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next"] == "started" and body["business"] == "Mwape Hardware"
    assert body["questions"][0]["ask"] == "When did Mwape Hardware start?"
    assert body["progress"]["done"] == 0 and body["symbol"] == "K"
    assert any(q["id"] == "stock_worth" for q in body["questions"])     # Retail's own


def test_an_answer_is_kept_and_said_back(api):
    db, client = api
    r = client.post("/interview/answer", json={"id": "normal_sales", "value": "50k"})
    assert r.status_code == 200, r.text
    r = client.post("/interview/answer", json={"id": "normal_costs", "value": "38,000"})
    body = r.json()
    assert body["said"].startswith("So about K12,000 is left")
    row = db.rows["business_interviews"][0]
    assert row["user_id"] == "u1" and row["business_id"] == "b1"
    assert row["answers"]["normal_sales"]["value"] == 50000
    # Resuming picks up where they left off.
    again = client.get("/interview").json()
    assert again["answers"]["normal_costs"]["value"] == 38000


def test_a_bad_answer_is_refused_with_how_to_say_it(api):
    db, client = api
    r = client.post("/interview/answer", json={"id": "normal_sales", "value": "loads"})
    assert r.status_code == 400 and "amount" in r.json()["detail"]
    assert not db.rows.get("business_interviews")


def test_only_the_owner_teaches_aibos():
    import main
    db = _books()
    real = main.get_db
    try:
        main.get_db = lambda: db
        from fastapi.testclient import TestClient
        r = TestClient(main.app).post("/interview/answer", json={"id": "started", "value": "2019"})
        assert r.status_code in (401, 403)
    finally:
        main.get_db = real
        main.app.dependency_overrides.clear()


def test_a_database_without_the_migration_says_which_to_run(api):
    db, client = api
    db.missing_tables.add("business_interviews")
    r = client.get("/interview")
    assert r.status_code == 503 and "0041" in r.json()["detail"]


def test_the_goals_become_targets_only_when_the_owner_says_so(api):
    db, client = api
    for qid, v in (("normal_sales", "50000"), ("sales_goal", "80000"),
                   ("sales_goal_by", "In 6 months")):
        assert client.post("/interview/answer", json={"id": qid, "value": v}).status_code == 200
    db.rows["budgets"].append({"id": "keep", "user_id": "u1", "business_id": "b1",
                               "month": iv._add_months(date.today().year, date.today().month, 1),
                               "metric": "revenue", "target": 1234})
    preview = client.get("/interview/plan").json()
    assert preview["ok"] and len(preview["months"]) == 12
    assert preview["to_write"] == 11 and len(preview["kept"]) == 1
    assert len(db.rows["budgets"]) == 1                      # nothing written by looking

    done = client.post("/interview/plan").json()
    assert done["written"] == 11 and done["kept"] == 1
    # Looked at again, the plan's own targets are not mistaken for the owner's.
    again = client.get("/interview/plan").json()
    assert again["to_write"] == 0 and len(again["kept"]) == 12 and again["own"] == 1
    kept = next(b for b in db.rows["budgets"] if b["id"] == "keep")
    assert kept["target"] == 1234                            # the owner's own figure stands
    assert db.rows["business_interviews"][0]["plan_applied_at"]


def test_the_ai_chat_is_briefed_on_the_business(api):
    db, client = api
    client.post("/interview/answer", json={"id": "sells", "value": "Cement and roofing sheets"})
    told = iv.briefing(db, "u1", "b1")
    assert "Business: Mwape Hardware, Retail." in told
    assert "- What it sells: Cement and roofing sheets" in told
    assert iv.briefing(db, "u1", "another-business") == ""


def test_the_briefing_never_breaks_the_chat():
    class Broken:
        def table(self, name):
            raise RuntimeError("database down")
    assert iv.briefing(Broken(), "u1", "b1") == ""
    assert iv.briefing(None, "u1", "b1") == ""
