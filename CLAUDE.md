# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask API for user registration and Traefik ForwardAuth, validating OwnTracks Basic Auth credentials against a SQLite database. Deployed via Docker on a Mac Mini homelab behind Traefik reverse proxy.

## Build and Run

```bash
# Install dependencies locally
pip install -r requirements.txt

# Run locally (uses waitress production server)
python app.py

# Build and run with Docker (from ~/owntracks, where docker-compose.yml lives)
docker compose up -d --build usermanagement-api
```

**Deploying changes:** the compose service builds directly from this working
tree — `~/owntracks/docker-compose.yml` sets
`build.context: /home/romangarms/Documents/GitHub/WhereHaveIBeen-API`. There is
**no separate clone in `~/owntracks` and no `git pull` step**; `--build` picks up
whatever is currently in this directory. So the deploy is:

```bash
# 1. ALWAYS back up the DB first (see warning below)
cp ~/owntracks/usermanagement-data/users.db \
   ~/owntracks/usermanagement-data/users.db.bak-$(date +%Y%m%d-%H%M%S)
# 2. Rebuild + restart (run on the Mini itself, or prefix with `ssh macmini`)
cd ~/owntracks && docker compose up -d --build usermanagement-api
```

Because the build reads the working tree rather than git, committing/pushing is
for record-keeping — it is not what deploys the code.

The app auto-creates the SQLite database on first run if it doesn't exist at the configured `DATABASE_PATH`.

## Testing Endpoints

```bash
# Health check
curl https://mini.romangarms.com/api/health

# Register a user
curl -X POST https://mini.romangarms.com/api/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"SecurePass123!","device":"phone"}'

# Test ForwardAuth (via OwnTracks endpoint)
curl -u testuser:SecurePass123! https://mini.romangarms.com/api/0/last

# Per-user endpoints (Basic auth, validated in-handler; no ?user= is accepted)
curl -u testuser:SecurePass123! https://mini.romangarms.com/api/me/devices
# All-time track: first call returns 202 {"status":"computing"} + Retry-After; poll until 200
curl -si -u testuser:SecurePass123! https://mini.romangarms.com/api/me/track | head -20
# Closed 30-day range with a 250 m corridor (computed inline)
curl -u testuser:SecurePass123! "https://mini.romangarms.com/api/me/track?from=2026-08-01T00:00:00Z&to=2026-09-01T00:00:00Z&buffer_m=250" | jq .stats
# One device only; an unknown device is a 400
curl -u testuser:SecurePass123! "https://mini.romangarms.com/api/me/track?device=phone&from=2026-09-01T00:00:00Z" | jq '.flights.features | length'
# Heatmap grid, then force a recompute
curl -u testuser:SecurePass123! "https://mini.romangarms.com/api/me/heatmap?from=2026-09-01T00:00:00Z" | jq '.cells | length'
curl -u testuser:SecurePass123! "https://mini.romangarms.com/api/me/heatmap?from=2026-09-01T00:00:00Z&refresh=1" | jq .computed_at
# Auth paths: no credentials -> 401 with WWW-Authenticate, inactive account -> 403
curl -si https://mini.romangarms.com/api/me/devices | head -3
```

Unit tests run without a recorder:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

They cover flight detection, segment grouping, buffer/union geometry, the
heatmap grid, incremental-vs-one-pass equivalence, and the auth/400 paths via
the Flask test client. Anything that touches the recorder is still checked
manually with the curl list above.

## Architecture

```
              mini.romangarms.com (HTTPS)
                        |
                     Traefik
                    /        \
        /api/*              /pub, /api/0/*
        (no auth)           (ForwardAuth)
            |                    |
  UserManagementAPI      OwnTracks Recorder
```

**ForwardAuth flow:** When a request hits OwnTracks endpoints (`/pub`, `/api/0/*`), Traefik calls `GET /auth/verify` on this API with the user's Basic Auth header. The API validates credentials against SQLite and returns 200 (with `X-Forwarded-User` header) or 401. Traefik then forwards or rejects the original request.

**Route priorities:** Traefik uses priority to resolve the `/api` prefix overlap — UserManagementAPI at priority 20 catches `/api/register` and `/api/health`, while OwnTracks at priority 10 handles `/api/0/*`.

## Server Access

The production server is a Mac Mini homelab:

- **SSH shortcut:** `ssh macmini` (configured in `~/.ssh/config`)
- **Host:** `romangos-mini-debian.local`, user `romangarms`
- **Deploy path:** `~/owntracks/` (contains `docker-compose.yml`). The API code
  is **not** cloned here — compose builds it from
  `~/Documents/GitHub/WhereHaveIBeen-API` via `build.context`.

**IMPORTANT: Always back up the SQLite database before deploying or making any changes on the server.** The database at `~/owntracks/usermanagement-data/users.db` contains all user accounts and is not replicated anywhere. A bad deployment or interrupted migration can corrupt or destroy it. Run this before every deploy:

```bash
ssh macmini "cp ~/owntracks/usermanagement-data/users.db \
  ~/owntracks/usermanagement-data/users.db.bak-\$(date +%Y%m%d-%H%M%S)"
```

```bash
# View logs
ssh macmini "cd ~/owntracks && docker compose logs -f usermanagement-api"

# Inspect database
ssh macmini "docker exec usermanagement-api python3 -c \"
from app import app, db
from models import User
with app.app_context():
    for u in User.query.all():
        print(f'{u.username}: active={u.is_active}')
\""
```

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `PORT` | `5002` | Waitress server port |
| `DATABASE_PATH` | `/data/users.db` | SQLite file path |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ENFORCE_USER_ISOLATION` | `true` (in compose) | **Now redundant** — per-user read isolation is enforced unconditionally in code (`/auth/verify`). The env var is no longer read; kept in compose only as documentation of intent. |
| `TRACK_CACHE_DIR` | `/data/tracks` | Per-user track/heatmap cache (`<dir>/<username>/<key>.pkl` + `index.json`). Lives on the `/data` volume so it survives restarts. |
| `TRACK_CLOSED_TTL_SECONDS` | `86400` | How long a closed-range (`to` given) entry is served before recompute. |
| `TRACK_MAX_ENTRIES_PER_USER` | `24` | LRU cap per user; open-ended all-time entries are evicted last. |
| `TRACK_INLINE_WINDOW_DAYS` | `31` | Fetch windows up to this long compute inline; longer ones run in a background thread behind a `202`. |
| `TRACK_MIN_REFRESH_SECONDS` | `15` | Open-ended entries validated this recently skip the `/api/0/last` freshness probe. Responses carry `ETag` + `Cache-Control: private, no-cache`; `If-None-Match` gets a `304`. |

## Key Design Decisions

- **Waitress** as WSGI server (not gunicorn) — runs in `app.py` directly, no separate process manager
- **In-memory rate limiting** — registration is limited to 10 attempts/hour per client IP; resets on container restart. The client IP is taken from the rightmost `X-Forwarded-For` entry (the address Traefik appends), since `request.remote_addr` behind the proxy is just Traefik's internal IP.
- **bcrypt** with cost factor 12 for password hashing
- **No auth on `/api/*`** — registration endpoint is open; ForwardAuth only protects OwnTracks routes. `/api/aggregate-roads` and `/api/me/*` validate Basic auth in-handler via `_require_user()`.
- **Per-user endpoints (`/api/me/devices`, `/api/me/track`, `/api/me/heatmap`)** — the recorder is only ever queried for the authenticated username. Geometry comes from `track.py` (episodic flight detection ported from the web app's `detectFlights`, buffer + dissolve in local AEQD projections); `track_cache.py` keeps one pickled entry per (user, devices, buffer, range) and extends open-ended entries incrementally by holding the streaming `DeviceTracker` state. `recorder.py` always passes `from`, because the recorder defaults an omitted `from` to six hours ago, and finds the start of history from the device's `YYYY-MM.rec` listing.
- **Single `users` table** — flat schema with `username`, `password_hash`, `owntracks_device`, `is_active`, timestamps
