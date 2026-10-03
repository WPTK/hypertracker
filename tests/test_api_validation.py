"""Body validation: malformed input is always a 400 in the contract's error shape,
row-level errors name the row, and accept_unverified follows the contract."""

import pytest
from helpers import day, manual_row

from app import config, db

pytestmark = pytest.mark.usefixtures("fake_resolver")


def create(client, body, **kw):
    return client.post("/api/trips", json=body, **kw)


def row_errors(resp):
    d = resp.json()["detail"]
    assert isinstance(d, dict) and isinstance(d["message"], str)
    return d["rows"]


@pytest.mark.parametrize("raw", ["[]", "not json", '"str"', "42", "null", "", "{", "\xff\xfe"])
def test_malformed_json_bodies_are_400(client, raw):
    r = client.post("/api/trips", content=raw.encode("latin-1"), headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "A", "out": "abc"},
        {"name": "A", "out": [1]},
        {"name": "A", "out": [[]]},
        {"name": "A", "out": {"from": "JAX"}},
        {"name": "A", "ret": "x"},
        {"name": "A", "out": [manual_row()], "tz": ["America/New_York"]},
        {"name": ["A"], "out": [manual_row()]},
        {"name": "A", "out": [{"flight_no": 1200, "date": day(1)}]},
        {"name": "A", "out": [{"from": ["JAX"], "to": "DEN"}]},
        {"name": "A", "out": [manual_row()], "accept_unverified": "yes"},
        {"name": "A", "out": [manual_row()], "uid": 5},
    ],
)
def test_wrong_types_are_400_not_422_or_500(client, body):
    r = create(client, body)
    assert r.status_code == 400, r.text
    assert "detail" in r.json()


def test_put_malformed_bodies_are_400(client):
    j = create(client, {"name": "A", "out": [manual_row()]}).json()
    h = {"X-Manage-Token": j["manage_token"], "Content-Type": "application/json"}
    for raw in (b"[]", b"nope", b'{"out": "abc"}', b'{"out": [1]}'):
        r = client.put(f"/api/trips/{j['trip_id']}", content=raw, headers=h)
        assert r.status_code == 400, (raw, r.text)


@pytest.mark.parametrize("trip_id", ["99999999999999999999", "0", "-1", "abc", "2147483648"])
def test_bad_trip_ids_are_400(client, trip_id):
    assert client.put(f"/api/trips/{trip_id}", json={"out": [manual_row()]}).status_code == 400
    assert client.delete(f"/api/trips/{trip_id}").status_code == 400


def test_unknown_but_valid_trip_id_is_404(client):
    assert client.delete("/api/trips/2147483647").status_code == 404


def test_oversized_body_is_413(client):
    big = {"name": "A", "out": [manual_row()], "junk": "x" * (config.MAX_BODY_BYTES + 10)}
    assert create(client, big).status_code == 413


def test_empty_trip_is_400(client):
    r = create(client, {"name": "A", "out": [], "ret": []})
    assert r.status_code == 400 and "at least one" in r.json()["detail"].lower()
    assert (
        create(client, {"name": "A", "out": [{}, {"date": day(1)}]}).status_code == 400
    )  # blank rows ignored


def test_name_clipped_and_tz_validated(client):
    j = create(client, {"name": "N" * 100, "out": [manual_row()], "tz": "Not/AZone"}).json()
    with db.get_conn() as c:
        row = c.execute("SELECT owner_name, submitter_tz FROM trips WHERE id = ?", (j["trip_id"],)).fetchone()
    assert row["owner_name"] == "N" * 40 and row["submitter_tz"] is None
    j = create(client, {"name": "A", "out": [manual_row()], "tz": "America/New_York"}).json()
    with db.get_conn() as c:
        assert (
            c.execute("SELECT submitter_tz FROM trips WHERE id = ?", (j["trip_id"],)).fetchone()[0]
            == "America/New_York"
        )
    for bad in ("../../etc/passwd", "/etc/localtime", "x" * 500, ""):
        j = create(client, {"name": "A", "out": [manual_row()], "tz": bad}).json()
        with db.get_conn() as c:
            assert (
                c.execute("SELECT submitter_tz FROM trips WHERE id = ?", (j["trip_id"],)).fetchone()[0]
                is None
            )


def test_oversized_uid_and_proof_do_not_crash(client):
    r = create(client, {"name": "A", "out": [manual_row()], "uid": "m_" + "x" * 5000, "proof": "y" * 5000})
    assert r.status_code == 200 and len(r.json()["uid"]) < 40


def test_too_many_rows_per_direction(client):
    ok = [manual_row()] * config.MAX_LEGS_PER_TRIP
    assert create(client, {"name": "A", "out": ok, "ret": ok}).status_code == 200
    assert create(client, {"name": "A", "out": ok + [manual_row()]}).status_code == 400
    assert create(client, {"name": "A", "ret": ok + [manual_row()]}).status_code == 400


# ---------------- row-level errors ----------------
def test_invalid_flight_number_row_error(client):
    r = create(client, {"name": "A", "out": [manual_row(), {"flight_no": "../../x", "date": day(1)}]})
    assert r.status_code == 400
    assert row_errors(r) == [
        {"direction": "out", "index": 1, "status": "invalid", "message": row_errors(r)[0]["message"]}
    ]
    assert row_errors(r)[0]["message"]


def test_row_errors_report_every_bad_row_with_direction(client):
    r = create(
        client,
        {
            "name": "A",
            "out": [{"flight_no": "!!", "date": day(1)}],
            "ret": [{"from": "JAX", "to": "ZZZZ", "date": day(1)}, {"from": "JAX"}],
        },
    )
    errs = {(e["direction"], e["index"]): e["status"] for e in row_errors(r)}
    assert errs == {("out", 0): "invalid", ("ret", 0): "airport_unknown", ("ret", 1): "invalid"}


@pytest.mark.parametrize(
    "date,status",
    [
        ("2020-01-01", "out_of_window"),
        ("2999-01-01", "out_of_window"),
        ("not-a-date", "invalid"),
        ("2026-13-45", "invalid"),
    ],
)
def test_bad_dates(client, date, status):
    r = create(client, {"name": "A", "out": [{"from": "JAX", "to": "DEN", "date": date}]})
    assert r.status_code == 400 and row_errors(r)[0]["status"] == status
    r = create(client, {"name": "A", "out": [{"flight_no": "DL1200", "date": date}]})
    assert r.status_code == 400 and row_errors(r)[0]["status"] == status


def test_manual_airports_must_exist_and_differ(client):
    r = create(client, {"name": "A", "out": [{"from": "JAX", "to": "QQQ"}]})
    assert row_errors(r)[0]["status"] == "airport_unknown"
    r = create(client, {"name": "A", "out": [{"from": "JAX", "to": "KJAX"}]})
    assert row_errors(r)[0]["status"] == "invalid"
    r = create(client, {"name": "A", "out": [{"from": "J!X", "to": "DEN"}]})
    assert row_errors(r)[0]["status"] == "invalid"


def test_airport_codes_accept_iata_or_icao(client):
    r = create(client, {"name": "A", "out": [{"from": "jax", "to": "kden"}]})
    assert r.status_code == 200
    leg = client.get("/api/trips").json()["trips"][0]["out"][0]
    assert (leg["from"], leg["to"], leg["from_iata"], leg["to_iata"]) == ("KJAX", "KDEN", "JAX", "DEN")


# ---------------- accept_unverified ----------------
def one_flight(client, fake_resolver, status, accept):
    fake_resolver.override["DL1200"] = status
    return create(
        client, {"name": "A", "accept_unverified": accept, "out": [{"flight_no": "DL1200", "date": day(2)}]}
    )


@pytest.mark.parametrize("status", ["not_found", "upstream_unavailable", "quota"])
def test_soft_failures_need_accept_unverified(client, fake_resolver, status):
    r = one_flight(client, fake_resolver, status, False)
    assert r.status_code == 400 and row_errors(r)[0]["status"] == status
    assert client.get("/api/trips").json()["trips"] == []
    r = one_flight(client, fake_resolver, status, True)
    assert r.status_code == 200, r.text
    leg = client.get("/api/trips").json()["trips"][0]["out"][0]
    assert leg["unverified"] is True and leg["resolved"] is False and leg["flight_no"] == "DL1200"


@pytest.mark.parametrize("status", ["invalid", "out_of_window", "airport_unknown", "ambiguous"])
def test_hard_failures_always_fail(client, fake_resolver, status):
    r = one_flight(client, fake_resolver, status, True)
    assert r.status_code == 400 and row_errors(r)[0]["status"] == status


def test_one_bad_flight_blocks_the_whole_trip(client, fake_resolver):
    r = create(
        client,
        {
            "name": "A",
            "accept_unverified": False,
            "out": [{"flight_no": "DL1200", "date": day(2)}, {"flight_no": "ZZ9999", "date": day(2)}],
        },
    )
    assert r.status_code == 400
    assert [(e["index"], e["status"]) for e in row_errors(r)] == [(1, "not_found")]
    assert client.get("/api/trips").json()["trips"] == []


def test_resolver_crash_is_upstream_unavailable_not_500(client, monkeypatch):
    import app.resolver as resolver_mod

    async def boom(rows, *, deadline=20.0):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(resolver_mod, "resolve_rows", boom)
    body = {"name": "A", "out": [{"flight_no": "DL1200", "date": day(2)}]}
    r = create(client, body)
    assert r.status_code == 400 and row_errors(r)[0]["status"] == "upstream_unavailable"
    assert create(client, {**body, "accept_unverified": True}).status_code == 200


def test_resolver_wrong_length_is_handled(client, monkeypatch):
    import app.resolver as resolver_mod

    async def short(rows, *, deadline=20.0):
        return []

    monkeypatch.setattr(resolver_mod, "resolve_rows", short)
    r = create(client, {"name": "A", "out": [{"flight_no": "DL1200", "date": day(2)}]})
    assert r.status_code == 400


def test_resolver_gets_validated_rows(client, fake_resolver):
    create(
        client,
        {
            "name": "A",
            "out": [{"flight_no": " dl-1200 ", "date": day(3), "from": "jax", "to": ""}],
            "ret": [{"flight_no": "BA 217"}],
        },
    )
    rows = fake_resolver.calls[0]
    assert rows[0] == {
        "direction": "out",
        "seq": 0,
        "flight_no": "DL1200",
        "date": day(3),
        "from": "JAX",
        "to": None,
    }
    assert rows[1]["direction"] == "ret" and rows[1]["flight_no"] == "BA217" and rows[1]["date"] == day(0)


def test_manual_row_never_calls_resolver(client, fake_resolver):
    assert create(client, {"name": "A", "out": [manual_row()]}).status_code == 200
    assert fake_resolver.calls == []
