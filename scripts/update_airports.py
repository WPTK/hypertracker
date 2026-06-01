"""Download the OurAirports dataset and load it into the airports table.

OurAirports is public domain, worldwide (~78k airports), and regenerated daily.
Run on a schedule (a weekly systemd timer or cron is plenty — airports rarely
change):

    python -m scripts.update_airports

We keep airports that have an IATA or ICAO code and skip closed fields,
heliports, and seaplane bases (not useful for a flight board map).
"""
import csv
import io
import sys
import urllib.request

from app import config, db

KEEP_TYPES = {"large_airport", "medium_airport", "small_airport"}


def fetch_csv(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "hyperfixed-flight-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def load(rows: str) -> int:
    db.init_db()
    reader = csv.DictReader(io.StringIO(rows))
    inserted = 0
    with db.get_conn() as conn:
        conn.execute("DELETE FROM airports")
        for row in reader:
            if row.get("type") not in KEEP_TYPES:
                continue
            iata = (row.get("iata_code") or "").strip().upper() or None
            ident = (row.get("ident") or "").strip().upper()
            if not ident and not iata:
                continue
            try:
                lat = float(row["latitude_deg"]); lon = float(row["longitude_deg"])
            except (ValueError, KeyError):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO airports (ident, iata, name, lat, lon, type, iso_country, municipality) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ident, iata, row.get("name"), lat, lon, row.get("type"),
                 row.get("iso_country"), row.get("municipality")),
            )
            inserted += 1
    return inserted


def main():
    url = config.AIRPORTS_CSV_URL
    print(f"Fetching {url} ...")
    try:
        data = fetch_csv(url)
    except Exception as e:
        print(f"Download failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    n = load(data)
    print(f"Loaded {n} airports into {config.DB_PATH}")


if __name__ == "__main__":
    main()
