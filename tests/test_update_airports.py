import pytest

from app import config, db
from scripts import update_airports as ua

HEADER = "ident,type,name,latitude_deg,longitude_deg,iso_country,municipality,iata_code\n"


def row(ident, typ="large_airport", name="N", lat="1.0", lon="2.0", cc="US", city="City", iata=""):
    return f'{ident},{typ},{name},{lat},{lon},{cc},{city},{iata}\n'


def make_csv(rows):
    return HEADER + "".join(rows)


@pytest.fixture
def tmpdb(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "airports-test.db"))
    db.init_db()
    return config.DB_PATH


def seed(ident="OLD1"):
    with db.get_conn() as c:
        c.execute("INSERT INTO airports (ident,iata,name,lat,lon,type) VALUES (?,?,?,?,?,?)",
                  (ident, "OLD", "Old", 1, 1, "large_airport"))


def idents():
    with db.get_conn() as c:
        return {r["ident"] for r in c.execute("SELECT ident FROM airports")}


def test_good_csv(tmpdb):
    seed()
    text = make_csv([row("KJAX", iata="jax", city="Jacksonville"),
                     row("KXYZ", "medium_airport"), row("KABC", "small_airport")])
    assert ua.load(text, min_rows=3) == 3
    assert idents() == {"KJAX", "KXYZ", "KABC"}
    a = db.find_airport("JAX")
    assert a["ident"] == "KJAX" and a["municipality"] == "Jacksonville"


def test_row_skipping(tmpdb):
    text = make_csv([
        row("GOOD"),
        row("", iata="AAA"),              # empty ident
        row("HELI", "heliport"),
        row("CLSD", "closed"),
        row("BADLAT", lat="abc"),
        row("OOR", lat="95"),
        row("  pad  ", "small_airport"),
    ])
    assert ua.load(text, min_rows=1) == 2
    assert idents() == {"GOOD", "PAD"}


@pytest.mark.parametrize("text", ["", "<html><body>503 Service Unavailable</body></html>",
                                  HEADER, make_csv([row("A1")])])
def test_truncated_or_garbage_aborts(tmpdb, text):
    seed()
    with pytest.raises(ua.TooFewRows):
        ua.load(text, min_rows=5)
    assert idents() == {"OLD1"}


def test_default_min_rows_is_5000(tmpdb):
    seed()
    with pytest.raises(ua.TooFewRows):
        ua.load(make_csv([row(f"A{i}") for i in range(100)]))
    assert idents() == {"OLD1"}


def test_duplicate_iata_deterministic(tmpdb):
    text = make_csv([
        row("SMALL", "small_airport", iata="DUP"),
        row("BIGONE", "large_airport", iata="DUP"),
        row("MED", "medium_airport", iata="DUP"),
    ])
    assert ua.load(text, min_rows=3) == 3
    assert db.find_airport("DUP")["ident"] == "BIGONE"
    with db.get_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM airports WHERE iata='DUP'").fetchone()[0]
    assert n == 1


def test_duplicate_ident_last_wins(tmpdb):
    assert ua.load(make_csv([row("DUPE", name="first"), row("dupe", name="second")]),
                   min_rows=1) == 1
    assert db.find_airport("DUPE")["name"] == "second"


def test_atomic_on_mid_insert_failure(tmpdb, monkeypatch):
    seed()
    text = make_csv([row(f"A{i}") for i in range(10)])
    real = ua.INSERT_SQL
    # Make the insert blow up for the whole batch after DELETE has run.
    monkeypatch.setattr(ua, "INSERT_SQL", real.replace("airports", "no_such_table"))
    with pytest.raises(Exception):
        ua.load(text, min_rows=1)
    assert idents() == {"OLD1"}


def test_atomic_on_row_failure(tmpdb):
    """A NULL ident violates the PK mid-batch; the old data must survive."""
    seed()
    rows, _ = ua.parse(make_csv([row("A1"), row("A2")]))
    rows.append((None,) + rows[0][1:])
    with pytest.raises(Exception):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM airports")
            conn.executemany(ua.INSERT_SQL, [rows[0], (rows[0][0],) + rows[0][1:]])  # PK clash
    assert idents() == {"OLD1"}


def test_main_file_and_exit_codes(tmpdb, tmp_path, capsys):
    good = tmp_path / "good.csv"
    good.write_text(make_csv([row("A1"), row("A2")]))
    assert ua.main(["--file", str(good), "--min-rows", "2"]) == 0
    assert "Loaded 2 airports" in capsys.readouterr().out

    bad = tmp_path / "bad.csv"
    bad.write_text("<html>error</html>")
    assert ua.main(["--file", str(bad), "--min-rows", "2"]) == 2
    assert idents() == {"A1", "A2"}

    assert ua.main(["--file", str(tmp_path / "missing.csv")]) == 1


def test_main_download_failure(tmpdb, monkeypatch, capsys):
    seed()

    def boom(url, timeout=0):
        raise OSError("network down")
    monkeypatch.setattr(ua, "fetch_csv", boom)
    assert ua.main([]) == 1
    err = capsys.readouterr().err
    assert "network down" in err and "untouched" in err
    assert idents() == {"OLD1"}
