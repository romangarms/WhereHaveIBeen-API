# Server Deployment Guide: OwnTracks + UserManagementAPI with ForwardAuth

This guide walks through deploying OwnTracks with the UserManagementAPI using Traefik ForwardAuth for dynamic user authentication.

## Prerequisites

- Docker and Docker Compose installed
- Domain name pointing to your server (e.g., mini.romangarms.com)
- SSH access with sudo privileges

## Architecture Overview

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

**How it works:**
1. User requests `/pub` or `/api/0/*` (OwnTracks endpoints)
2. Traefik calls `/auth/verify` on UserManagementAPI with the user's Basic Auth credentials
3. UserManagementAPI validates against SQLite database
4. If valid (200), Traefik forwards to OwnTracks; if invalid (401), access denied

**Benefits over htpasswd:**
- New users can authenticate immediately after registration (no restart needed)
- Single source of truth (SQLite database)
- No file synchronization issues

## Phase 1: Prepare Directory Structure

### Step 1: Create Project Directory

```bash
mkdir -p ~/owntracks
cd ~/owntracks

# Create data directories
mkdir -p owntracks-recorder/config
mkdir -p owntracks-recorder/store
mkdir -p usermanagement-data
mkdir -p letsencrypt
```

### Step 2: Clone UserManagementAPI

```bash
# Clone the API repository
git clone https://github.com/romangarms/WhereHaveIBeen-API.git

# Verify Dockerfile exists
ls WhereHaveIBeen-API/Dockerfile
```

## Phase 2: Configure Environment

### Step 3: Create Environment File

```bash
cd ~/owntracks
nano .env
```

**Contents:**

```bash
# JWT Secret Key (CRITICAL: Must match frontend!)
# Generate with: python3 -c "import os; print(os.urandom(32).hex())"
JWT_SECRET_KEY=<PASTE_GENERATED_KEY_HERE>

# Privileged OwnTracks credentials (for API proxy access)
# This user needs to exist in the database for /api/locations and /api/last proxying
OWNTRACKS_PRIVILEGED_USER=api
OWNTRACKS_PRIVILEGED_PASS=<GENERATE_SECURE_PASSWORD>
```

**Generate the keys:**

```bash
# Generate JWT secret (SAVE THIS - needed for frontend too!)
echo "JWT_SECRET_KEY: $(python3 -c 'import os; print(os.urandom(32).hex())')"

# Generate privileged password
echo "OWNTRACKS_PRIVILEGED_PASS: $(python3 -c 'import os; print(os.urandom(16).hex())')"
```

### Step 4: Create docker-compose.yml

```bash
nano ~/owntracks/docker-compose.yml
```

**Contents:**

```yaml
version: "3.6"
services:
  owntracks-recorder:
    container_name: owntracks-recorder
    image: owntracks/recorder
    environment:
      - OTR_PORT=0 # disables MQTT
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ./owntracks-recorder/config:/config
      - ./owntracks-recorder/store:/store
    restart: unless-stopped
    labels:
      - traefik.enable=true

      # Router for OwnTracks endpoints (protected by ForwardAuth)
      - traefik.http.routers.owntracks-mini.rule=Host(`mini.romangarms.com`) && (PathPrefix(`/pub`) || PathPrefix(`/api/0`))
      - traefik.http.routers.owntracks-mini.entrypoints=websecure
      - traefik.http.routers.owntracks-mini.tls=true
      - traefik.http.routers.owntracks-mini.middlewares=owntracks-forwardauth
      - traefik.http.routers.owntracks-mini.tls.certresolver=cloudflare
      - traefik.http.routers.owntracks-mini.priority=10

      - traefik.http.services.owntracks.loadbalancer.server.port=8083

      # ForwardAuth middleware - delegates auth to UserManagementAPI
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.address=http://usermanagement-api:5002/auth/verify
      - traefik.http.middlewares.owntracks-forwardauth.forwardauth.authResponseHeaders=X-Forwarded-User

  usermanagement-api:
    container_name: usermanagement-api
    build:
      context: ./WhereHaveIBeen-API
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

      # Router for API endpoints (no auth - API handles its own auth)
      - traefik.http.routers.usermanagement-api.rule=Host(`mini.romangarms.com`) && PathPrefix(`/api`)
      - traefik.http.routers.usermanagement-api.entrypoints=websecure
      - traefik.http.routers.usermanagement-api.tls=true
      - traefik.http.routers.usermanagement-api.tls.certresolver=cloudflare
      - traefik.http.routers.usermanagement-api.priority=20

      - traefik.http.services.usermanagement-api.loadbalancer.server.port=5002

  reverse-proxy:
    image: traefik:v3.1
    command:
      - --api=false
      - --providers.docker
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.cloudflare.acme.tlschallenge=true
      - --certificatesresolvers.cloudflare.acme.email=your-email@example.com
      - --certificatesresolvers.cloudflare.acme.storage=/letsencrypt/acme.json
      # Uncomment for debugging:
      # - --log.level=DEBUG

    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./letsencrypt:/letsencrypt
    environment:
      - CF_API_EMAIL=your-email@example.com
      - CF_API_TOKEN=your-cloudflare-api-token
```

**Important:** Replace:
- `mini.romangarms.com` with your domain
- `your-email@example.com` with your email
- `your-cloudflare-api-token` with your Cloudflare API token (if using Cloudflare)

## Phase 3: Migrate Existing Users (Optional)

If you have existing users in an htpasswd file:

### Step 5: Run Migration Script

```bash
cd ~/owntracks/WhereHaveIBeen-API

# Create virtual environment for migration
python3 -m venv venv
source venv/bin/activate
pip install flask flask-sqlalchemy python-dotenv

# Run migration
python migrate_htpasswd_users.py /path/to/old/htpasswd ../usermanagement-data/users.db

# Deactivate venv
deactivate
```

## Phase 4: Deploy

### Step 6: Start Services

```bash
cd ~/owntracks

# Build and start all services
docker compose up -d --build

# Check logs
docker compose logs -f
# Press Ctrl+C to exit
```

### Step 7: Create Privileged API User

The privileged user is needed for the `/api/locations` and `/api/last` proxy endpoints:

```bash
# Register the privileged API user
curl -X POST https://mini.romangarms.com/api/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "api",
    "password": "YOUR_OWNTRACKS_PRIVILEGED_PASS_FROM_ENV",
    "device": "server"
  }'
```

**Note:** Use the same password you set in `.env` for `OWNTRACKS_PRIVILEGED_PASS`.

## Phase 5: Testing

### Step 8: Test Health Endpoint

```bash
curl https://mini.romangarms.com/api/health

# Expected: {"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"}
```

### Step 9: Test User Registration

```bash
curl -X POST https://mini.romangarms.com/api/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "password": "SecurePass123!",
    "device": "phone"
  }'

# Expected: {"message": "User created successfully", "username": "testuser"}
```

### Step 10: Test ForwardAuth (OwnTracks Access)

```bash
# This should work immediately after registration (no restart needed!)
curl -u testuser:SecurePass123! https://mini.romangarms.com/api/0/last -v

# Expected: HTTP 200 with JSON response
```

### Step 11: Test Login

```bash
curl -X POST https://mini.romangarms.com/api/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "password": "SecurePass123!"
  }'

# Expected: {"jwt_token": "eyJ...", "username": "testuser", "device": "phone"}
```

### Step 12: Test OwnTracks Mobile App

1. Open OwnTracks mobile app
2. Configure connection:
   - Mode: HTTP
   - Host: `https://mini.romangarms.com`
   - Username: `testuser`
   - Password: `SecurePass123!`
3. Send a location and verify it appears

## Phase 6: Frontend Integration

### Step 13: Configure WhereHaveIBeen Frontend

On your Fly.io deployment:

```bash
# Set the API URL (no port needed - same domain!)
fly secrets set WHIB_USER_API_URL=https://mini.romangarms.com

# Set the JWT secret (must match!)
fly secrets set WHIB_JWT_SECRET_KEY=<same-jwt-secret-from-server-env>
```

## Verification Checklist

- [ ] Docker containers running (`docker compose ps`)
- [ ] Health endpoint returns success
- [ ] User registration works
- [ ] New user can immediately authenticate to OwnTracks (ForwardAuth working)
- [ ] Login returns JWT token
- [ ] OwnTracks mobile app can connect
- [ ] WhereHaveIBeen frontend can register/login users

## Troubleshooting

### ForwardAuth returns 401

```bash
# Check UserManagementAPI logs
docker logs usermanagement-api

# Verify user exists
docker exec usermanagement-api python3 -c "
from app import app, db
from models import User
with app.app_context():
    users = User.query.all()
    for u in users:
        print(f'{u.username}: active={u.is_active}')
"
```

### Container won't start

```bash
# Check all logs
docker compose logs

# Check specific service
docker compose logs usermanagement-api
docker compose logs reverse-proxy
```

### Database issues

```bash
# Check database file exists
ls -la ~/owntracks/usermanagement-data/

# Check permissions
docker exec usermanagement-api ls -la /data/
```

### SSL certificate issues

```bash
# Check Traefik logs
docker compose logs reverse-proxy

# Verify ACME storage
ls -la ~/owntracks/letsencrypt/
```

## Rollback Plan

If something goes wrong, restore from backup:

```bash
cd ~/owntracks

# Stop services
docker compose down

# Restore backup (if you have one)
BACKUP_DIR=~/owntracks/backups/<timestamp>
cp "$BACKUP_DIR/docker-compose.yml" .
cp -r "$BACKUP_DIR/"* .

# Restart
docker compose up -d
```

## Updating

To update the UserManagementAPI:

```bash
cd ~/owntracks/WhereHaveIBeen-API
git pull

cd ~/owntracks
docker compose up -d --build usermanagement-api
```

## Backup

Backup the SQLite database regularly:

```bash
# Manual backup
cp ~/owntracks/usermanagement-data/users.db ~/owntracks/usermanagement-data/users.db.backup

# Automated backup (add to crontab)
0 2 * * * cp ~/owntracks/usermanagement-data/users.db ~/owntracks/usermanagement-data/users.db.$(date +\%Y\%m\%d)
```

## Support

If you encounter issues:
1. Check the logs: `docker compose logs -f`
2. Review this deployment guide
3. Check the main README: https://github.com/romangarms/WhereHaveIBeen-API
