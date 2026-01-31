# WhereHaveIBeen API

User management and privacy-enforcing API proxy for the [WhereHaveIBeen](https://github.com/romangarms/wherehaveibeen) location tracker.

## Overview

This API server sits between the WhereHaveIBeen frontend and OwnTracks Recorder to provide:

1. **User Registration** - Allow new users to create accounts dynamically
2. **Authentication** - JWT-based authentication for secure API access
3. **Privacy Enforcement** - Ensure users can ONLY access their own location data
4. **ForwardAuth** - Traefik middleware authentication against SQLite database

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
           (registration,         (location data,
            login, proxy)          mobile app)
```

**Routing:**
- `/api/register`, `/api/login`, `/api/locations`, `/api/last` → UserManagementAPI (handles its own auth)
- `/auth/verify` → UserManagementAPI (ForwardAuth endpoint, internal only)
- `/pub`, `/api/0/*` → OwnTracks Recorder (protected by ForwardAuth)

**Key Benefits:**
- Single external endpoint (no extra port needed)
- Dynamic user registration - users can authenticate immediately (no restart)
- All authentication against SQLite database

## Features

- User registration with password validation
- JWT-based authentication (30-day token expiry)
- Privacy-enforcing proxy to OwnTracks `/api/0/locations` endpoint
- Privacy-enforcing proxy to OwnTracks `/api/0/last` endpoint
- ForwardAuth endpoint for Traefik middleware
- Rate limiting to prevent brute force attacks
- bcrypt password hashing
- SQLite database for user accounts
- Docker containerization

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

### 2. Configure Environment Variables

Create a `.env` file in your docker-compose directory:

```bash
# Generate secret keys
JWT_SECRET_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")

# Privileged OwnTracks user (for API proxy access)
OWNTRACKS_PRIVILEGED_USER=api
OWNTRACKS_PRIVILEGED_PASS=your-secure-password-here
```

### 3. Docker Compose Configuration

Add to your `docker-compose.yml`:

```yaml
services:
  usermanagement-api:
    build:
      context: /path/to/WhereHaveIBeen-API
      dockerfile: Dockerfile
    environment:
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
      - OWNTRACKS_URL=http://owntracks-recorder:8083
      - OWNTRACKS_PRIVILEGED_USER=${OWNTRACKS_PRIVILEGED_USER}
      - OWNTRACKS_PRIVILEGED_PASS=${OWNTRACKS_PRIVILEGED_PASS}
      - DATABASE_PATH=/data/users.db
    volumes:
      - ./usermanagement-data:/data
    restart: unless-stopped
    labels:
      - traefik.enable=true
      # API endpoints (no auth middleware - API handles its own auth)
      - traefik.http.routers.usermanagement-api.rule=Host(`your.domain.com`) && PathPrefix(`/api`)
      - traefik.http.routers.usermanagement-api.entrypoints=websecure
      - traefik.http.routers.usermanagement-api.tls=true
      - traefik.http.routers.usermanagement-api.priority=20
      - traefik.http.services.usermanagement-api.loadbalancer.server.port=5002

  owntracks-recorder:
    image: owntracks/recorder
    labels:
      - traefik.enable=true
      # OwnTracks endpoints (protected by ForwardAuth)
      - traefik.http.routers.owntracks.rule=Host(`your.domain.com`) && (PathPrefix(`/pub`) || PathPrefix(`/api/0`))
      - traefik.http.routers.owntracks.middlewares=owntracks-forwardauth
      - traefik.http.routers.owntracks.priority=10
      # ForwardAuth middleware
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.address=http://usermanagement-api:5002/auth/verify
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.authResponseHeaders=X-Forwarded-User
```

### 4. Migrate Existing Users (Optional)

If you have existing users in an htpasswd file:

```bash
python3 migrate_htpasswd_users.py /path/to/htpasswd ./usermanagement-data/users.db
```

### 5. Deploy

```bash
docker compose up -d --build
```

### 6. Verify

```bash
# Test health endpoint
curl https://your.domain.com/api/health

# Test ForwardAuth
curl -u username:password https://your.domain.com/api/0/last -v
```

## API Endpoints

### Health Check

```bash
GET /health
```

### ForwardAuth (Internal)

```bash
GET /auth/verify
Authorization: Basic <base64(username:password)>
```

Returns 200 with `X-Forwarded-User` header on success, 401 on failure.

### Register New User

```bash
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
- `429`: Rate limit exceeded (3 attempts per hour)

### Login

```bash
POST /api/login
Content-Type: application/json

{
  "username": "alice",
  "password": "SecurePass123!"
}
```

**Response:**
```json
{
  "jwt_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "username": "alice",
  "device": "phone"
}
```

### Get Locations (Privacy-Enforcing)

```bash
GET /api/locations?device=phone&from=2024-01-01T00:00:00Z&to=2024-12-31T23:59:59Z
Authorization: Bearer <jwt_token>
```

**Important:** The `user` parameter is automatically set from the JWT token. Users cannot query other users' data.

### Get Devices (Privacy-Enforcing)

```bash
GET /api/last
Authorization: Bearer <jwt_token>
```

Returns only the authenticated user's devices.

## Configuration

All configuration is via environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `JWT_SECRET_KEY` | Secret key for JWT signing (CRITICAL - must match frontend) | `abc123...` |
| `SECRET_KEY` | Flask secret key | `xyz789...` |
| `PORT` | Server port | `5002` |
| `DATABASE_PATH` | SQLite database path | `/data/users.db` |
| `OWNTRACKS_URL` | OwnTracks Recorder URL (internal Docker network) | `http://owntracks-recorder:8083` |
| `OWNTRACKS_PRIVILEGED_USER` | Privileged OwnTracks username | `api` |
| `OWNTRACKS_PRIVILEGED_PASS` | Privileged OwnTracks password | `secure-password` |

## Security Features

### Rate Limiting

- Registration: 3 attempts per hour per IP
- Login: 10 attempts per 15 minutes per IP

### Password Security

- bcrypt hashing with cost factor 12
- Minimum 12 character passwords
- Complexity requirements enforced

### JWT Tokens

- 30-day expiration
- Unique token ID (jti) for potential revocation
- HS256 algorithm

### Privacy Enforcement

The API enforces privacy at multiple layers:

1. **JWT Validation** - All requests must have valid JWT token
2. **User Parameter Forcing** - The `user` parameter is ALWAYS set to the authenticated user from the JWT
3. **Response Filtering** - Filters out any data not belonging to the authenticated user

### ForwardAuth

The `/auth/verify` endpoint validates HTTP Basic Auth credentials against the SQLite database, enabling Traefik to protect OwnTracks endpoints without htpasswd files.

## Integration with WhereHaveIBeen Frontend

The frontend needs these environment variables:

```bash
# On Fly.io
fly secrets set WHIB_USER_API_URL=https://mini.romangarms.com
fly secrets set WHIB_JWT_SECRET_KEY=<same-as-api-jwt-secret>
```

## Files

| File | Description |
|------|-------------|
| `app.py` | Main Flask application with all endpoints |
| `auth.py` | JWT and password utilities |
| `config.py` | Configuration from environment variables |
| `models.py` | SQLAlchemy database models |
| `Dockerfile` | Container build configuration |
| `migrate_htpasswd_users.py` | Migration script for existing htpasswd users |
| `owntracks_manager.py` | **DEPRECATED** - htpasswd management (no longer used) |

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

### JWT token errors

```bash
# Ensure JWT_SECRET_KEY matches between API and frontend
# Generate a new one:
python3 -c "import os; print(os.urandom(32).hex())"

# Update environment and restart containers
docker compose down && docker compose up -d
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

Backup the SQLite database regularly:

```bash
cp ./usermanagement-data/users.db ./usermanagement-data/users.db.backup
```

## License

MIT License - See main WhereHaveIBeen project for details.
