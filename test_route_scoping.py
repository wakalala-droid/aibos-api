"""
Who a route answers for.

Every hospitality and payroll route used to resolve its data from the CALLER's
own user id. For an owner working alone that is the same id as the business, so
it looked right for as long as nobody was invited. The moment an owner added a
receptionist, that receptionist signed in, saw an empty calendar, and was told
to upgrade a business they do not own.

These tests hold the answer in place: hospitality follows the business, payroll
belongs to the owner, and the public feed stays public.
"""

import inspect

from fastapi import params

import main
import membership


def _deps(endpoint) -> list:
    try:
        sig = inspect.signature(endpoint)
    except (TypeError, ValueError):
        return []
    return [p.default.dependency for p in sig.parameters.values()
            if isinstance(p.default, params.Depends)]


def _routes(prefix: str) -> list:
    return [r for r in main.app.routes
            if getattr(r, "path", "").startswith(prefix) and getattr(r, "endpoint", None)]


CONTEXT_DEPS = {membership.require_context, membership.require_write, membership.require_owner}

# Deliberately not tenant-scoped, each for its own reason.
UNSCOPED = {
    "/hospitality/ical/{token}.ics",   # public feed; the token IS the capability
    "/hospitality/sync-all",           # cron, authenticated by CRON_SECRET
}


def test_every_hospitality_route_follows_the_business():
    routes = _routes("/hospitality")
    assert routes, "no hospitality routes found"
    for r in routes:
        if r.path in UNSCOPED:
            continue
        assert CONTEXT_DEPS & set(_deps(r.endpoint)), \
            f"{r.path} does not resolve a business context"


def test_hospitality_writes_are_closed_to_read_only_roles():
    for r in _routes("/hospitality"):
        if r.path in UNSCOPED:
            continue
        methods = set(r.methods or [])
        if methods & {"POST", "PATCH", "PUT", "DELETE"}:
            deps = set(_deps(r.endpoint))
            assert membership.require_write in deps or membership.require_owner in deps, \
                f"{r.path} lets an accountant write"


def test_payroll_and_the_employee_register_are_owner_only():
    routes = _routes("/payroll") + _routes("/employees")
    assert routes, "no payroll routes found"
    for r in routes:
        assert membership.require_owner in _deps(r.endpoint), \
            f"{r.path} is not owner-only, so staff can read what everyone earns"


def test_the_public_ical_feed_stays_public():
    feed = [r for r in main.app.routes
            if getattr(r, "path", "") == "/hospitality/ical/{token}.ics"]
    assert feed, "the public feed route is missing"
    assert not _deps(feed[0].endpoint), "the public feed grew an auth dependency"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} route-scoping tests passed ===")
