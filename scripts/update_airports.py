"""Download the OurAirports dataset and load it into the airports table.

OurAirports is public domain, worldwide (~78k airports), and regenerated daily.
Run on a schedule (deploy/hyperfixed-airports.timer does it weekly):

    python -m scripts.update_airports               # download and load
    python -m scripts.update_airports --file a.csv  # load a local CSV
    python -m scripts.update_airports --min-rows N  # sanity threshold

We keep large/medium/small airports with a usable ident and coordinates and
skip closed fields, heliports and seaplane bases. The whole CSV is parsed
BEFORE the table is touched; if fewer than MIN_ROWS usable rows come out
(truncated download, HTML error page) nothing changes and the exit code is 2.
The table swap happens in one transaction, so readers never see a half-loaded
or empty table.

Exit codes: 0 ok, 1 download/read failure, 2 too few usable rows.
"""

import argparse
import csv
import io
import sys
import urllib.request

from app import config, db

KEEP_TYPES = {"large_airport", "medium_airport", "small_airport"}
TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
MIN_ROWS = 5000
DOWNLOAD_TIMEOUT = 60
USER_AGENT = "hyperfixed-flight-tracker/1.0 (airport refresh)"

INSERT_SQL = (
    "INSERT INTO airports (ident, iata, name, lat, lon, type, iso_country, municipality) "
    "VALUES (?,?,?,?,?,?,?,?)"
)


class TooFewRows(Exception):
    """The CSV produced fewer usable rows than the sanity threshold."""


def fetch_csv(url: str, timeout: float = DOWNLOAD_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig")


def parse(rows_text: str) -> tuple[list[tuple], dict]:
    """Parse CSV text into insert tuples. Returns (rows, stats). Touches no DB.

    Duplicate idents: the last one wins. Duplicate IATA codes: the largest
    airport (then the lowest ident) keeps the code, the others get iata=NULL,
    so IATA lookups are deterministic."""
    reader = csv.DictReader(io.StringIO(rows_text))
    by_ident: dict[str, tuple] = {}
    stats = {"seen": 0, "skipped": 0, "duplicate_idents": 0, "iata_cleared": 0}
    for row in reader:
        stats["seen"] += 1
        typ = row.get("type")
        ident = (row.get("ident") or "").strip().upper()
        if typ not in KEEP_TYPES or not ident:
            stats["skipped"] += 1
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (ValueError, KeyError, TypeError):
            stats["skipped"] += 1
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            stats["skipped"] += 1
            continue
        iata = (row.get("iata_code") or "").strip().upper() or None
        if ident in by_ident:
            stats["duplicate_idents"] += 1
        by_ident[ident] = (
            ident,
            iata,
            row.get("name"),
            lat,
            lon,
            typ,
            row.get("iso_country"),
            row.get("municipality"),
        )

    best: dict[str, tuple] = {}  # iata -> (rank, ident)
    for t in by_ident.values():
        if t[1]:
            key = (TYPE_RANK[t[5]], t[0])
            if t[1] not in best or key < best[t[1]]:
                best[t[1]] = key
    rows = []
    for ident, iata, *rest in by_ident.values():
        if iata and best[iata][1] != ident:
            iata = None
            stats["iata_cleared"] += 1
        rows.append((ident, iata, *rest))
    stats["usable"] = len(rows)
    return rows, stats


def load(rows_text: str, min_rows: int = MIN_ROWS) -> int:
    """Replace the airports table with the CSV contents; return rows inserted.

    Raises TooFewRows (DB untouched) if fewer than min_rows usable rows parse.
    The DELETE + INSERTs run in one transaction: any failure rolls back and the
    old data stays."""
    rows, stats = parse(rows_text)
    if len(rows) < min_rows:
        raise TooFewRows(
            f"only {len(rows)} usable airport rows (need at least {min_rows}); "
            f"{stats['seen']} CSV rows seen. Existing data left untouched."
        )
    db.init_db()
    with db.get_conn() as conn:
        # sqlite3 opens an implicit transaction at the DELETE; get_conn commits
        # on success and closes (rolling back) if anything below raises.
        conn.execute("DELETE FROM airports")
        conn.executemany(INSERT_SQL, rows)
    load.last_stats = stats
    return len(rows)


load.last_stats = {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the airports table from OurAirports.")
    ap.add_argument("--file", metavar="PATH", help="load a local CSV instead of downloading")
    ap.add_argument(
        "--min-rows", type=int, default=MIN_ROWS, help=f"abort if fewer usable rows (default {MIN_ROWS})"
    )
    args = ap.parse_args(argv)

    try:
        if args.file:
            print(f"Reading {args.file} ...")
            with open(args.file, encoding="utf-8-sig") as f:
                data = f.read()
        else:
            url = config.AIRPORTS_CSV_URL
            print(f"Fetching {url} ...")
            data = fetch_csv(url)
    except Exception as e:
        src = args.file or config.AIRPORTS_CSV_URL
        print(
            f"Could not read airports data from {src}: {type(e).__name__}: {e}. "
            f"Existing airports table left untouched.",
            file=sys.stderr,
        )
        return 1

    try:
        n = load(data, min_rows=args.min_rows)
    except TooFewRows as e:
        print(f"Aborted: {e}", file=sys.stderr)
        return 2
    s = load.last_stats
    print(
        f"Loaded {n} airports into {config.DB_PATH} "
        f"(skipped {s.get('skipped', 0)}, duplicate idents {s.get('duplicate_idents', 0)}, "
        f"duplicate IATA codes cleared {s.get('iata_cleared', 0)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
