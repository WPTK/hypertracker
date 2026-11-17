# Deploying

## One-command deploy

On the server, from the app directory (adjust `APP_DIR` if it is not `/opt/hyperfixed-flight-tracker`):

```bash
sudo APP_DIR=/opt/hyperfixed-flight-tracker SMOKE_FLIGHT=<a real flight number for the next few days> ./deploy/deploy.sh
```

It backs up the database first (a verified copy in `backups/`, taken before the new
version migrates it), creates `.env` if needed, generates `SECRET_KEY` only when it is
missing or a placeholder, sets `TRUSTED_PROXY=cloudflare` and `DEV_MODE=false`, leaves your
`AERODATABOX_*` values alone, installs dependencies, loads airports, refuses a systemd unit
with more than one worker, restarts the service and runs a smoke test. Secrets are never
printed. Running it again is safe: the secret stays the same and a new backup is added.

The smoke test fails (exit 1) if the flight lookup comes back as `quota` or
`upstream_unavailable`, which means a bad key, the wrong `AERODATABOX_AUTH`
(`rapidapi` or `apimarket`), an exhausted quota, or no network. A `not_found` result is only
a warning: the key works but that flight does not exist on that day.

Other switches: `SKIP_INSTALL=1`, `SKIP_AIRPORTS=1`, `AIRPORTS_FILE=<csv>`, `SKIP_SMOKE=1`,
`SKIP_RESTART=1`, `BASE=<url>`, `UNIT_FILE=<path>`.


Assumes the code is in `/opt/hyperfixed-flight-tracker` with a `.venv` and a `.env`. Adjust `User=` and paths in the unit files first.

## Install

```sh
sudo cp deploy/hyperfixed-web.service deploy/hyperfixed-airports.service \
        deploy/hyperfixed-airports.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperfixed-web.service hyperfixed-airports.timer
```

First airport load (also what the weekly timer runs):

```sh
sudo systemctl start hyperfixed-airports.service
journalctl -u hyperfixed-airports -n 20
# offline / manual:  .venv/bin/python -m scripts.update_airports --file airports.csv
```

Exit codes: 0 ok, 1 download failed, 2 CSV had fewer than 5000 usable rows. On 1 or 2 the existing airports table is kept.

## nginx

```sh
sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/hyperfixed.conf   # edit server_name
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d board.adsb.cc                                  # TLS
```

Set `TRUSTED_PROXY` in `.env` to match (see the comments at the top of the nginx example):

- `nginx`: nginx faces clients directly.
- `cloudflare`: Cloudflare (tunnel or proxy) faces clients.
- `none`: development only.

## Rules

- One uvicorn worker. Rate limits, caches and pollers are in-process. Do not pass `--workers`.
- Do not pass `--proxy-headers` to uvicorn; the app resolves the client IP itself via `TRUSTED_PROXY`.
- Keep `SECRET_KEY` unique and secret, and `DEV_MODE=false`.
- The service can only write to `data/` (SQLite file plus `-wal` and `-shm`). Back up that directory.

## Operate

```sh
systemctl status hyperfixed-web
journalctl -u hyperfixed-web -f
systemctl list-timers hyperfixed-airports.timer
sudo systemd-analyze security hyperfixed-web.service    # hardening score
```

## Docker on Gilead

On Gilead the app runs as a container instead of the old Windows service. The container
runs as an ordinary unprivileged user, cannot write anywhere except the data folder, and
has no extra Linux powers. Nothing about the database or settings changes: it uses the same
`C:\hypertracker\.env` and `C:\hypertracker\data\flightboard.db` as before.

The image is built by GitHub (the `container` workflow) and published as
`ghcr.io/wptk/hypertracker`. Do not build it on Gilead.

### First start

1. From the repo folder, pull the image while the old service is still running:
   `docker compose -f deploy/compose.gilead.yaml pull`
   If `pull` is denied, either set the package to public in the GitHub package settings for
   `ghcr.io/wptk/hypertracker`, or run `docker login ghcr.io` on Gilead first. The site
   stays up while you sort that out.
2. Stop the old service so only one copy runs and the port is free (this is the moment the
   site goes down; a few seconds): `Stop-Service Hypertracker`.
3. Start the container: `docker compose -f deploy/compose.gilead.yaml up -d hypertracker`
4. Check it: `docker ps` should show `hypertracker` as `healthy` after about 30 seconds, and
   https://tracker.adsb.cc should load. The tunnel needs no change; it still points at
   `host.docker.internal:8008`.
5. Set the old service to Manual (`Set-Service Hypertracker -StartupType Manual`) so it does not start at boot and
   fight for the port.

The container listens on `127.0.0.1:8008` only, so nothing on the network can reach it
except through the tunnel. This is fine for the tunnel: on Gilead it already reaches other
containers published on 127.0.0.1 through `host.docker.internal`.

### Update

```
docker compose -f deploy/compose.gilead.yaml pull
docker compose -f deploy/compose.gilead.yaml up -d hypertracker
```

Back up `C:\hypertracker\data` first if the release changes the database layout.

### Roll back to the old service

```
docker compose -f deploy/compose.gilead.yaml down
Set-Service Hypertracker -StartupType Automatic
Start-Service Hypertracker
```

`down` removes the container (a merely stopped one would come back after a Docker restart,
because of `restart: always`); it is safe here because this compose project holds only this
one service. The old copy in `C:\hypertracker` is untouched and
reads the same data folder. To go back to the container again, run the First start steps
from step 2. To pin an older container build instead, change `latest` in
`deploy/compose.gilead.yaml` to a `sha-xxxxxxx` tag shown on the package page.

### Notes

- Logs: `docker logs hypertracker` (kept to 3 files of 10 MB).
- Load or refresh airports inside the container:
  `docker exec hypertracker python -m scripts.update_airports`.
- Behind the tunnel the app sees Docker's private address as the peer, and
  `TRUSTED_PROXY=cloudflare` still takes the visitor address from `CF-Connecting-IP`. It
  ignores that header from any public address.
- `/docs`, `/redoc` and `/openapi.json` exist only when `DEV_MODE=true`.
