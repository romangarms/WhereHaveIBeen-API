# WhereHaveIBeen API

User management API for the [WhereHaveIBeen](https://github.com/romangarms/wherehaveibeen) location tracker.

## Overview

This API provides:

1. **User Registration** - Create OwnTracks user accounts
2. **ForwardAuth** - Traefik middleware authentication against SQLite database
3. **Health Check** - Service health endpoint

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
- `/api/register`, `/api/health`, `/api/delete-account` → UserManagementAPI
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

## Configuration

All configuration is via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Server port | `5002` |
| `DATABASE_PATH` | SQLite database path | `/data/users.db` |
| `LOG_LEVEL` | Logging level | `INFO` |

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
