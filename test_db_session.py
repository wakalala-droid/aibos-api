"""The REST session retries what is safe to retry, and nothing else."""

import httpx

import db


def _client(fail_times, exc):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc("Server disconnected without sending a response.", request=request)
        return httpx.Response(200, json=[{"ok": True}])

    cls = db._retrying_client_class()
    return cls(base_url="https://example.supabase.co/rest/v1", transport=httpx.MockTransport(handler)), calls


def test_a_read_that_loses_its_connection_is_tried_again():
    c, calls = _client(1, httpx.RemoteProtocolError)
    assert c.request("GET", "/profiles").status_code == 200
    assert calls["n"] == 2


def test_a_write_that_loses_its_connection_is_not_sent_twice():
    c, calls = _client(1, httpx.RemoteProtocolError)
    try:
        c.request("POST", "/business_events", json={"amount": 1})
        assert False, "a write must not be retried after reaching the server"
    except httpx.RemoteProtocolError:
        pass
    assert calls["n"] == 1


def test_a_write_that_never_connected_is_tried_again():
    c, calls = _client(1, httpx.ConnectError)
    assert c.request("POST", "/business_events", json={"amount": 1}).status_code == 200
    assert calls["n"] == 2


def test_the_supabase_client_gets_the_hardened_session():
    from supabase import create_client
    client = create_client("https://example.supabase.co", "eyJhbGciOiJIUzI1NiJ9.e30.x")
    db.harden_rest_session(client)
    assert type(client.postgrest.session).__name__ == "RetryingClient"
    assert client.table("profiles").select("id").session is client.postgrest.session
