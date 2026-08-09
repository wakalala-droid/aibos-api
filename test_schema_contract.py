"""
The backend half of the schema (migration) contract guard.

/health reports the highest migration this code expects, and the owner uses a
green /health to confirm a deploy is really live. That number is a hand-written
declaration about files in the OTHER repo, so nothing stopped it drifting: bump
the code without adding the .sql, or add the .sql without bumping the code, and
both repos looked fine.

This side asserts main.EXPECTS_MIGRATION matches schema_contract.json. The
frontend's scripts/check_migration_contract.py fetches that same file and
asserts it equals the highest migration actually sitting in
aibos/supabase/migrations/ — the half that can see the files.

Run as a plain script like the other suites.
"""

import json
import pathlib

import main

CONTRACT = json.loads(
    (pathlib.Path(__file__).parent / "schema_contract.json").read_text("utf-8")
)


def test_contract_file_is_not_vacuous():
    # A guard that parses nothing passes forever while drift ships.
    assert "expects_migration" in CONTRACT
    assert isinstance(CONTRACT["expects_migration"], int)
    assert CONTRACT["expects_migration"] > 0


def test_expects_migration_matches_the_contract():
    assert main.EXPECTS_MIGRATION == CONTRACT["expects_migration"], (
        f"main.EXPECTS_MIGRATION is {main.EXPECTS_MIGRATION} but "
        f"schema_contract.json says {CONTRACT['expects_migration']}. "
        "Bump both, and add the matching .sql in aibos/supabase/migrations/."
    )


def test_health_reports_the_declared_migration():
    # The whole point of the number is that it reaches /health, where the owner
    # reads it after a deploy. An inlined literal here would defeat the guard.
    import asyncio

    body = asyncio.run(main.health())
    assert body["expects_migration"] == CONTRACT["expects_migration"]
    assert "build_sha" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} schema-contract tests passed ===")
