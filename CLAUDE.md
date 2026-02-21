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

# Build and run with Docker
docker compose up -d --build usermanagement-api

# Rebuild after code changes on server
ssh macmini "cd ~/owntracks && git -C WhereHaveIBeen-API pull && docker compose up -d --build usermanagement-api"
```

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
- **Deploy path:** `~/owntracks/` (contains `docker-compose.yml` and the cloned repo)

**IMPORTANT: Always back up the SQLite database before deploying or making any changes on the server.** The database at `~/owntracks/usermanagement-data/users.db` contains all user accounts and is not replicated anywhere. A bad deployment or interrupted migration can corrupt or destroy it. Run this before every deploy:

```bash
ssh macmini "cp ~/owntracks/usermanagement-data/users.db ~/owntracks/usermanagement-data/users.db.bak"
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
| `SECRET_KEY` | auto-generated | Flask secret key |
| `PORT` | `5002` | Waitress server port |
| `DATABASE_PATH` | `/data/users.db` | SQLite file path |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Key Design Decisions

- **Waitress** as WSGI server (not gunicorn) — runs in `app.py` directly, no separate process manager
- **In-memory rate limiting** — registration is limited to 3 attempts/hour/IP; resets on container restart
- **bcrypt** with cost factor 12 for password hashing
- **No auth on `/api/*`** — registration endpoint is open; ForwardAuth only protects OwnTracks routes
- **Single `users` table** — flat schema with `username`, `password_hash`, `owntracks_device`, `is_active`, timestamps
