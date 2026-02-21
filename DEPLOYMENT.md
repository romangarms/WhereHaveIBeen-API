# Server Deployment Guide: OwnTracks + UserManagementAPI with ForwardAuth

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
       (registration)         (location data,
                               mobile app)
```

**How it works:**
1. User requests `/pub` or `/api/0/*` (OwnTracks endpoints)
2. Traefik calls `/auth/verify` on UserManagementAPI with the user's Basic Auth credentials
3. UserManagementAPI validates against SQLite database
4. If valid (200), Traefik forwards to OwnTracks; if invalid (401), access denied

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
git clone https://github.com/romangarms/WhereHaveIBeen-API.git
ls WhereHaveIBeen-API/Dockerfile
```

## Phase 2: Configure docker-compose.yml

### Step 3: Create docker-compose.yml

```bash
nano ~/owntracks/docker-compose.yml
```

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

**Important:** Replace `mini.romangarms.com`, email, and Cloudflare token with your values.

## Phase 3: Deploy

### Step 4: Start Services

```bash
cd ~/owntracks
docker compose up -d --build
docker compose logs -f
```

## Phase 4: Testing

### Step 5: Test Health

```bash
curl https://mini.romangarms.com/api/health
# Expected: {"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"}
```

### Step 6: Test Registration

```bash
curl -X POST https://mini.romangarms.com/api/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"SecurePass123!","device":"phone"}'
# Expected: {"message": "User created successfully", "username": "testuser"}
```

### Step 7: Test ForwardAuth (OwnTracks Access)

```bash
# Should work immediately after registration
curl -u testuser:SecurePass123! https://mini.romangarms.com/api/0/last -v
# Expected: HTTP 200 with JSON response
```

### Step 8: Test OwnTracks Mobile App

1. Open OwnTracks mobile app
2. Configure connection:
   - Mode: HTTP
   - Host: `https://mini.romangarms.com`
   - Username: `testuser`
   - Password: `SecurePass123!`
3. Send a location and verify it appears

## Verification Checklist

- [ ] Docker containers running (`docker compose ps`)
- [ ] Health endpoint returns success
- [ ] User registration works
- [ ] New user can immediately authenticate to OwnTracks (ForwardAuth working)
- [ ] OwnTracks mobile app can connect

## Troubleshooting

### ForwardAuth returns 401

```bash
docker logs usermanagement-api

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
docker compose logs
docker compose logs usermanagement-api
docker compose logs reverse-proxy
```

### Database issues

```bash
ls -la ~/owntracks/usermanagement-data/
docker exec usermanagement-api ls -la /data/
```

## Updating

```bash
cd ~/owntracks/WhereHaveIBeen-API
git pull

cd ~/owntracks
docker compose up -d --build usermanagement-api
```

## Backup

```bash
cp ~/owntracks/usermanagement-data/users.db ~/owntracks/usermanagement-data/users.db.backup

# Automated backup (add to crontab)
0 2 * * * cp ~/owntracks/usermanagement-data/users.db ~/owntracks/usermanagement-data/users.db.$(date +\%Y\%m\%d)
```
