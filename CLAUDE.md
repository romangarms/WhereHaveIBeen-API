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
```

No test suite exists — testing is done manually against the running server.

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

## Key Design Decisions

- **Waitress** as WSGI server (not gunicorn) — runs in `app.py` directly, no separate process manager
- **In-memory rate limiting** — registration is limited to 10 attempts/hour per client IP; resets on container restart. The client IP is taken from the rightmost `X-Forwarded-For` entry (the address Traefik appends), since `request.remote_addr` behind the proxy is just Traefik's internal IP.
- **bcrypt** with cost factor 12 for password hashing
- **No auth on `/api/*`** — registration endpoint is open; ForwardAuth only protects OwnTracks routes
- **Single `users` table** — flat schema with `username`, `password_hash`, `owntracks_device`, `is_active`, timestamps
