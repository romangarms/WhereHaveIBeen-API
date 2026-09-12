# WhereHaveIBeen API

User management API for the [WhereHaveIBeen](https://github.com/romangarms/wherehaveibeen) location tracker.

## Overview

This API provides:

1. **User Registration** - Create OwnTracks user accounts
2. **ForwardAuth** - Traefik middleware authentication against SQLite database
3. **Health Check** - Service health endpoint
4. **Per-user map data** - Finished track geometry, flights, heatmap grid and stats for native clients
5. **Aggregate roads** - Anonymised union of every user's visited roads

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
       (registration)         (location data,
                               mobile app)
```

**Routing:**
- `/api/register`, `/api/health`, `/api/delete-account`, `/api/me/*`, `/api/aggregate-roads` → UserManagementAPI
- `/auth/verify` → UserManagementAPI (ForwardAuth endpoint, internal only)
- `/pub`, `/api/0/*` → OwnTracks Recorder (protected by ForwardAuth)

## Requirements

- Docker and Docker Compose
- OwnTracks Recorder (Docker image)
- Traefik reverse proxy (Docker image)

## Installation (Docker)

### 1. Clone the Repository

```bash
git clone https://github.com/romangarms/WhereHaveIBeen-API.git
cd WhereHaveIBeen-API
```

### 2. Docker Compose Configuration

Add to your `docker-compose.yml`:

```yaml
services:
  usermanagement-api:
    build:
      context: /path/to/WhereHaveIBeen-API
      dockerfile: Dockerfile
    environment:
      - DATABASE_PATH=/data/users.db
    volumes:
      - ./usermanagement-data:/data
    restart: unless-stopped
    labels:
      - traefik.enable=true
      - traefik.http.routers.usermanagement-api.rule=Host(`your.domain.com`) && PathPrefix(`/api`)
      - traefik.http.routers.usermanagement-api.entrypoints=websecure
      - traefik.http.routers.usermanagement-api.tls=true
      - traefik.http.routers.usermanagement-api.priority=20
      - traefik.http.services.usermanagement-api.loadbalancer.server.port=5002

  owntracks-recorder:
    image: owntracks/recorder
    labels:
      - traefik.enable=true
      - traefik.http.routers.owntracks.rule=Host(`your.domain.com`) && (PathPrefix(`/pub`) || PathPrefix(`/api/0`))
      - traefik.http.routers.owntracks.middlewares=owntracks-forwardauth
      - traefik.http.routers.owntracks.priority=10
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.address=http://usermanagement-api:5002/auth/verify
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.authResponseHeaders=X-Forwarded-User
```

### 3. Deploy

```bash
docker compose up -d --build
```

### 4. Verify

```bash
# Test health endpoint
curl https://your.domain.com/api/health

# Test registration
curl -X POST https://your.domain.com/api/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"SecurePass123!","device":"phone"}'

# Test ForwardAuth (OwnTracks access with registered credentials)
curl -u testuser:SecurePass123! https://your.domain.com/api/0/last
```

## API Endpoints

### Health Check

```
GET /api/health
```

### ForwardAuth (Internal)

```
GET /auth/verify
Authorization: Basic <base64(username:password)>
```

Returns 200 with `X-Forwarded-User` header on success, 401 on failure.

### Register New User

```
POST /api/register
Content-Type: application/json

{
  "username": "alice",
  "password": "SecurePass123!",
  "device": "phone"
}
```

**Password Requirements:**
- Minimum 12 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one number

**Response:**
- `201`: User created successfully
- `400`: Validation error
- `429`: Rate limit exceeded (10 attempts per hour)

### Delete Account

```
POST /api/delete-account
Content-Type: application/json

{
  "username": "alice",
  "password": "SecurePass123!"
}
```

**Response:**
- `200`: Account deleted successfully
- `400`: Missing fields
- `401`: Invalid credentials

### Per-user endpoints

All three take HTTP Basic auth on every request and only ever read the
authenticated user's data (no `user` parameter exists). Missing or invalid
credentials return `401` with `WWW-Authenticate: Basic realm="WhereHaveIBeen"`,
an inactive account returns `403`, bad parameters return `400` with
`{"error": "<message>"}`. Units are metric: km, km², m, km/h. Coordinates are
`[lon, lat]` WGS84.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/me/devices` | `{"username": "alice", "devices": ["phone", "ipad"]}`. Also a cheap credential check at sign-in. |
| `GET /api/me/track` | Buffered "explored" corridor, flight lines, flight corridor and stats for a date range. |
| `GET /api/me/heatmap` | Visit-frequency grid for a date range. |

**Query parameters** (`/api/me/track` and `/api/me/heatmap`):

| Param | Default | Notes |
|-------|---------|-------|
| `from` | full history | ISO 8601 with offset or `Z`, inclusive. |
| `to` | now | ISO 8601. Omitting it keeps the entry open-ended and lets later requests extend it incrementally. |
| `device` | all devices | Must be one of the user's devices, else `400`. |
| `buffer_m` | `500` | Track only. Corridor radius in metres, clamped to `100..5000`. |
| `refresh` | unset | `1` discards the cached entry and recomputes. |

**Track response** (`200`):

```json
{
  "range": { "from": null, "to": "2026-09-10T17:00:00Z" },
  "computed_at": 1789000000,
  "latest_tst": 1788999000,
  "earliest_tst": 1722470400,
  "buffer_m": 500,
  "driving": { "type": "Feature", "properties": {}, "geometry": { "type": "MultiPolygon", "coordinates": [] } },
  "flights": { "type": "FeatureCollection", "features": [
    { "type": "Feature",
      "properties": { "start_tst": 1780000000, "end_tst": 1780010000, "distance_km": 1234.5 },
      "geometry": { "type": "LineString", "coordinates": [[-122.3, 47.4], [-118.4, 33.9]] } } ] },
  "flights_buffer": { "type": "Feature", "properties": {}, "geometry": null },
  "stats": {
    "driving": { "distance_km": 0.0, "area_km2": 0.0, "max_alt_m": 0.0, "max_vel_kmh": 0.0 },
    "flying":  { "distance_km": 0.0, "area_km2": 0.0, "max_alt_m": 0.0, "max_vel_kmh": 0.0 }
  }
}
```

`range.from` is `null` when `from` was omitted; `latest_tst` and
`earliest_tst` (oldest and newest fix in the range) are `null` with no points;
the two geometries are `Polygon`, `MultiPolygon` or `null`.

**Heatmap response** (`200`): `range`, `computed_at`, `latest_tst`,
`earliest_tst` as above plus
`"cell_deg": 0.0006` and `"cells": [[gx, gy, count], ...]` where
`gx = round(lon / cell_deg)`, `gy = round(lat / cell_deg)`; the cell centre is
`(gx * cell_deg, gy * cell_deg)`.

**`202`** `{"status": "computing"}` with a `Retry-After` header means the
result is not cached yet and a background compute is running; poll the same
URL. Full-history requests always start this way; ranges of about a month or
less compute inline.

Caching: one entry per (user, devices, buffer, range) under
`TRACK_CACHE_DIR/<username>/`. Open-ended entries store enough pipeline state
to be extended with only the points newer than `latest_tst`; closed entries
are recomputed after `TRACK_CLOSED_TTL_SECONDS`. Each user keeps at most
`TRACK_MAX_ENTRIES_PER_USER` entries (LRU, open-ended all-time entries evicted
last).

### Aggregate Roads

```
GET /api/aggregate-roads
Authorization: Basic <base64(username:password)>
```

One dissolved GeoJSON Feature covering every user's roads, with population-level
`properties`: `max_vel`, `max_alt`, `distance_km`, `area_km2`. Returns `503`
with `Retry-After` while the cache warms.

## Configuration

All configuration is via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Server port | `5002` |
| `DATABASE_PATH` | SQLite database path | `/data/users.db` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `RECORDER_URL` | OwnTracks Recorder on the internal network | `http://owntracks-recorder:8083` |
| `TRACK_CACHE_DIR` | Per-user track/heatmap cache (must be on a persistent volume) | `/data/tracks` |
| `TRACK_CLOSED_TTL_SECONDS` | Lifetime of a closed-range entry | `86400` |
| `TRACK_MAX_ENTRIES_PER_USER` | LRU cap on cache entries per user | `24` |
| `TRACK_INLINE_WINDOW_DAYS` | Longest fetch window computed inline (longer runs behind `202`) | `31` |

## Security Features

### Rate Limiting
- Registration: 10 attempts per hour per IP

### Password Security
- bcrypt hashing with cost factor 12
- Minimum 12 character passwords
- Complexity requirements enforced

### ForwardAuth
The `/auth/verify` endpoint validates HTTP Basic Auth credentials against the SQLite database, enabling Traefik to protect OwnTracks endpoints without htpasswd files.

## Files

| File | Description |
|------|-------------|
| `app.py` | Main Flask application with all endpoints |
| `auth.py` | Password hashing and validation utilities |
| `config.py` | Configuration from environment variables |
| `models.py` | SQLAlchemy database models |
| `recorder.py` | Read-only client for the OwnTracks Recorder HTTP API |
| `track.py` | Shared geometry pipeline: flight detection, thinning, buffer, dissolve, heat grid |
| `track_cache.py` | Per-user cache with incremental refresh behind `/api/me/track` and `/api/me/heatmap` |
| `aggregate.py` | Anonymised all-users road union behind `/api/aggregate-roads` |
| `tests/` | `pytest` suite that runs without a recorder |
| `Dockerfile` | Container build configuration |

## Troubleshooting

### ForwardAuth returns 401

```bash
# Check the API logs
docker logs usermanagement-api

# Verify user exists in database
docker exec usermanagement-api python3 -c "
from app import app, db
from models import User
with app.app_context():
    users = User.query.all()
    for u in users:
        print(f'{u.username}: active={u.is_active}')
"
```

### Database issues

```bash
# Check database permissions
ls -la ./usermanagement-data/

# Initialize fresh database
rm ./usermanagement-data/users.db
docker compose restart usermanagement-api
```

## Backup

```bash
cp ./usermanagement-data/users.db ./usermanagement-data/users.db.backup
```

## License

MIT License - See main WhereHaveIBeen project for details.
