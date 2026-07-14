"""
Offline tests for rate_limit.py (audit #45) — fixed-window allow/deny, window
reset, per-identity + per-bucket isolation. Run as a plain script.
"""

import rate_limit


def setup():
    rate_limit._HITS.clear()


def test_allows_up_to_limit_then_blocks():
    setup()
    for i in range(5):
        allowed, retry = rate_limit.check("u1", "chat", limit=5, window_s=60, now=1000.0)
        assert allowed is True and retry == 0
    allowed, retry = rate_limit.check("u1", "chat", limit=5, window_s=60, now=1000.0)
    assert allowed is False and 1 <= retry <= 60


def test_window_resets():
    setup()
    for _ in range(5):
        rate_limit.check("u1", "chat", 5, 60, now=1000.0)
    # Blocked within the window…
    assert rate_limit.check("u1", "chat", 5, 60, now=1030.0)[0] is False
    # …allowed once the window has fully elapsed.
    assert rate_limit.check("u1", "chat", 5, 60, now=1061.0)[0] is True


def test_identity_and_bucket_isolation():
    setup()
    for _ in range(5):
        rate_limit.check("u1", "chat", 5, 60, now=1000.0)
    assert rate_limit.check("u1", "chat", 5, 60, now=1000.0)[0] is False   # u1 chat exhausted
    assert rate_limit.check("u2", "chat", 5, 60, now=1000.0)[0] is True    # different user
    assert rate_limit.check("u1", "transcribe", 5, 60, now=1000.0)[0] is True  # different bucket


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n=== {len(fns)}/{len(fns)} rate-limit tests passed ===")
