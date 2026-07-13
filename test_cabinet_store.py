"""
Offline tests for cabinet_store.py (audit #10) — write-through, cold-read
fall-through, tenant scoping, and graceful degradation when the bucket or
table doesn't exist yet. Run as a plain script like the other suites.
"""

import cabinet_store


# ── Fake supabase chain + Storage ─────────────────────────────────────────────

class _Q:
    def __init__(self, db, name, op, payload=None, on_conflict=None):
        self.db, self.name, self.op = db, name, op
        self.payload, self.on_conflict = payload, on_conflict
        self.filters = {}

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k): return self
    def limit(self, n): return self

    def execute(self):
        class R:
            data: list = []
        out = R()
        rows = self.db.rows[self.name]
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.op == "select":
            out.data = [dict(r) for r in match]
        elif self.op == "delete":
            self.db.rows[self.name] = [r for r in rows if not all(
                r.get(k) == v for k, v in self.filters.items())]
            out.data = [dict(r) for r in match]
        elif self.op == "upsert":
            key = self.on_conflict or "id"
            for r in rows:
                if r.get(key) == self.payload.get(key):
                    r.update(self.payload)
                    out.data = [dict(r)]
                    return out
            rows.append(dict(self.payload))
            out.data = [dict(self.payload)]
        return out


class _T:
    def __init__(self, db, name): self.db, self.name = db, name
    def select(self, *_): return _Q(self.db, self.name, "select")
    def delete(self): return _Q(self.db, self.name, "delete")
    def upsert(self, row, on_conflict="id"): return _Q(self.db, self.name, "upsert", row, on_conflict)


class _Bucket:
    def __init__(self, blobs): self.blobs = blobs
    def upload(self, path, data, options=None): self.blobs[path] = bytes(data)
    def download(self, path): return self.blobs[path]
    def remove(self, paths):
        for p in paths:
            self.blobs.pop(p, None)


class _Storage:
    def __init__(self): self.blobs = {}
    def from_(self, bucket): return _Bucket(self.blobs)


class _DB:
    def __init__(self):
        self.rows = {"cabinet_files": []}
        self.storage = _Storage()
    def table(self, name): return _T(self, name)


def _entry(user="u1"):
    return {"user_id": user, "name": "sales.xlsx", "file_type": "excel",
            "engine": "engine1", "active_sheet": "Q2", "sheets": ["Q1", "Q2"],
            "df_json": "[{\"Month\":\"Jan\",\"Revenue\":900}]",
            "analysis": {"kpi": {"total_revenue": 900}}, "monthly": []}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_persist_then_cold_load():
    db = _DB()
    assert cabinet_store.persist(db, "cab1", _entry()) is True
    assert db.rows["cabinet_files"][0]["name"] == "sales.xlsx"       # listable row
    assert "u1/cab1.json" in db.storage.blobs                        # payload blob

    # A fresh process (empty CABINET dict) restores the full entry.
    loaded = cabinet_store.load(db, "cab1", "u1")
    assert loaded is not None
    assert loaded["analysis"]["kpi"]["total_revenue"] == 900
    assert loaded["df_json"].startswith("[{")


def test_tenant_scoping():
    db = _DB()
    cabinet_store.persist(db, "cab1", _entry(user="u1"))
    assert cabinet_store.load(db, "cab1", "u2") is None              # not yours
    assert cabinet_store.list_rows(db, "u2") == []


def test_list_and_delete():
    db = _DB()
    cabinet_store.persist(db, "cab1", _entry())
    cabinet_store.persist(db, "cab2", {**_entry(), "name": "pos.xls"})
    rows = cabinet_store.list_rows(db, "u1")
    assert {r["id"] for r in rows} == {"cab1", "cab2"}

    cabinet_store.delete(db, "cab1", "u1")
    assert [r["id"] for r in cabinet_store.list_rows(db, "u1")] == ["cab2"]
    assert "u1/cab1.json" not in db.storage.blobs


def test_persist_overwrites():
    db = _DB()
    cabinet_store.persist(db, "cab1", _entry())
    updated = {**_entry(), "active_sheet": "Q1"}
    cabinet_store.persist(db, "cab1", updated)
    assert len(db.rows["cabinet_files"]) == 1                        # upsert, no dupe
    assert cabinet_store.load(db, "cab1", "u1")["active_sheet"] == "Q1"


def test_graceful_before_migration():
    class _NoInfra:
        def table(self, name):
            raise Exception("Could not find the table 'public.cabinet_files' (PGRST205)")
    # No table/bucket → persist reports failure but never raises; loads say None.
    assert cabinet_store.persist(_NoInfra(), "cab1", _entry()) is False
    assert cabinet_store.load(_NoInfra(), "cab1", "u1") is None
    assert cabinet_store.list_rows(_NoInfra(), "u1") is None
    cabinet_store.delete(_NoInfra(), "cab1", "u1")                   # must not raise
    assert cabinet_store.persist(None, "cab1", _entry()) is False    # no db at all


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} cabinet-store tests passed ===")
