# Claude Code Instructions

## Server Access

The OwnTracks server runs on a Mac Mini homelab. SSH access is configured:

- **Host alias:** `macmini`
- **Hostname:** `romangos-mini-debian.local`
- **User:** `romangarms`
- **SSH config:** `~/.ssh/config` has the `macmini` shortcut

Run remote commands with:
```bash
ssh macmini "command here"
```

## Project Overview

Flask API for user registration and Traefik ForwardAuth, validating OwnTracks Basic Auth credentials against SQLite. Deployed via Docker on the Mac Mini server behind Traefik reverse proxy.

## Endpoints

- `POST /api/register` - Create a new OwnTracks user
- `GET /auth/verify` - Traefik ForwardAuth (validates Basic Auth against SQLite)
- `GET /health` - Health check

## Key Files

- `app.py` - Main Flask application with all endpoints
- `auth.py` - Password hashing and validation utilities
- `config.py` - Configuration from environment variables
- `models.py` - SQLAlchemy database models
- `Dockerfile` - Container build configuration
