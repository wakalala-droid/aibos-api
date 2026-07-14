"""
Offline tests for rec_store.py (audit #20) — fingerprint identity, refresh-
not-duplicate, status persistence across runs, feedback transitions, and the
self-audit scoreboard. Run as a plain script like the other suites.
"""

import rec_store


class _Q:
    def __init__(self, db, name, op, payload=None):
        self.db, self.name, self.op, self.payload = db, name, op, payload
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "update":
            for r in match:
                r.update(self.payload)
            out.data = [dict(r) for r in match]
        elif self.op == "insert":
            row = {"id": f"rec_{len(rows)+1}", "status": "open", "times_shown": 1,
                   **self.payload}
            rows.append(row)
            out.data = [dict(row)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def update(self, patch): return _Q(self.db, self.name, "update", patch)
    def insert(self, row): return _Q(self.db, self.name, "insert", row)


class _DB:
    def __init__(self): self.rows = {"recommendations": []}
    def table(self, name): return _T(self, name)


def _rec(title="Extend your cash runway", engine="cash_runway", conf=0.8):
    return {"title": title, "source_engine": engine, "confidence": conf,
            "rationale": "why", "priority": "high",
            "evidence": [{"label": "Cash", "value": "K200"}], "impact": {}}


def test_fingerprint_stability():
    a = rec_store.fingerprint(_rec())
    b = rec_store.fingerprint({"title": "  EXTEND your cash runway! ", "source_engine": "cash_runway"})
    assert a == b == "cash_runway:extend your cash runway"


def test_refresh_not_duplicate():
    db = _DB()
    recs = [_rec()]
    rec_store.record_shown(db, "u1", recs)
    assert recs[0]["rec_id"] == "rec_1" and recs[0]["status"] == "open"

    # Same advice next run → same row, times_shown bumps, no duplicate.
    again = [_rec(conf=0.9)]
    rec_store.record_shown(db, "u1", again)
    assert len(db.rows["recommendations"]) == 1
    assert again[0]["times_shown"] == 2 and again[0]["rec_id"] == "rec_1"
    assert db.rows["recommendations"][0]["confidence"] == 0.9   # refreshed


def test_status_survives_reruns():
    db = _DB()
    recs = [_rec()]
    rec_store.record_shown(db, "u1", recs)
    rec_store.set_status(db, "u1", recs[0]["rec_id"], "dismissed")

    rerun = [_rec()]
    rec_store.record_shown(db, "u1", rerun)
    assert rerun[0]["status"] == "dismissed"      # dismissed stays dismissed


def test_feedback_validation_and_tenant_scope():
    db = _DB()
    recs = [_rec()]
    rec_store.record_shown(db, "u1", recs)
    try:
        rec_store.set_status(db, "u1", recs[0]["rec_id"], "loved_it")
        assert False
    except ValueError:
        pass
    try:
        rec_store.set_status(db, "u2", recs[0]["rec_id"], "accepted")   # not yours
        assert False
    except ValueError:
        pass


def test_track_record():
    db = _DB()
    batch = [_rec(), _rec("Improve thin profit margins", "profitability"),
             _rec("Reorder 2 products", "low_stock")]
    rec_store.record_shown(db, "u1", batch)
    rec_store.set_status(db, "u1", batch[0]["rec_id"], "accepted")
    rec_store.set_status(db, "u1", batch[1]["rec_id"], "dismissed")

    tr = rec_store.track_record(db, "u1")
    assert tr["available"] and tr["total"]["shown"] == 3
    assert tr["total"]["accepted"] == 1 and tr["total"]["dismissed"] == 1 and tr["total"]["open"] == 1
    assert tr["acceptance_rate"] == 50.0
    assert tr["engines"]["cash_runway"]["accepted"] == 1


def test_degrades_without_migration():
    class _NoTable:
        def table(self, name):
            raise Exception("Could not find the table 'public.recommendations' (PGRST205)")
    recs = [_rec()]
    out = rec_store.record_shown(_NoTable(), "u1", recs)     # must not raise
    assert out == {} and "rec_id" not in recs[0]
    assert rec_store.track_record(_NoTable(), "u1") == {"available": False}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} rec-store tests passed ===")
