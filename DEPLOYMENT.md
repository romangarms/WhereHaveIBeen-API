# Server Deployment Guide: OwnTracks + UserManagementAPI

This guide walks through configuring your OwnTracks server to work with the UserManagementAPI for dynamic user registration.

## Prerequisites

- OwnTracks Recorder running in Docker with Traefik
- Server: mini.romangarms.com (Debian/Ubuntu)
- SSH access with sudo privileges
- apache2-utils installed (`sudo apt install apache2-utils`)

## Overview

We'll be making these changes:
1. **Backup** existing OwnTracks configuration
2. **Modify** docker-compose.yml to use htpasswd file instead of inline users
3. **Create** htpasswd file with existing users
4. **Restart** Docker containers to apply changes
5. **Deploy** UserManagementAPI
6. **Test** the complete setup

## Phase 1: Backup Existing Configuration

### Step 1: Create Backup Directory

```bash
# Create backup directory with timestamp
BACKUP_DIR=~/owntracks/backups/$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# Backup docker-compose.yml
cp ~/owntracks/docker-compose.yml "$BACKUP_DIR/"

# Backup entire config directory
cp -r ~/owntracks/owntracks-recorder/config "$BACKUP_DIR/"

# Verify backup
ls -la "$BACKUP_DIR"
echo "Backup created at: $BACKUP_DIR"
```

## Phase 2: Configure OwnTracks for htpasswd File

### Step 2: Modify docker-compose.yml

**File:** `~/owntracks/docker-compose.yml`

**Find the owntracks-recorder service labels section and change:**

```yaml
# BEFORE (inline users - hard to update):
- "traefik.http.middlewares.owntracks-auth.basicauth.users=roman:$$apr1$$Ct/T36Id$$6.Dir0BSnkwi3Ym0iZX1i.,test:$$apr1$$Ct/T36Id$$6.Dir0BSnkwi3Ym0iZX1i."
```

**TO (htpasswd file - dynamic updates):**

```yaml
# AFTER (htpasswd file):
- "traefik.http.middlewares.owntracks-auth.basicauth.usersfile=/htpasswd/owntracks.htpasswd"
```

**Then add volume mount to reverse-proxy service:**

```yaml
reverse-proxy:
  image: traefik:v3.1
  # ... existing configuration ...
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - ./letsencrypt:/letsencrypt
    - ./cloudflare:/cloudflare
    - ./owntracks-recorder/config/htpasswd:/htpasswd:ro  # ADD THIS LINE
```

### Step 3: Create htpasswd File with Existing Users

```bash
# Create htpasswd directory
mkdir -p ~/owntracks/owntracks-recorder/config/htpasswd

# Add existing users (REPLACE with your actual passwords!)
# User: roman
htpasswd -bBC 10 ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd roman YOUR_PASSWORD_HERE

# User: test
htpasswd -bB ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd test YOUR_PASSWORD_HERE

# Set correct permissions
chmod 644 ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd

# Verify file was created
cat ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd
```

**Expected output:** Two lines with bcrypt hashes:
```
roman:$2y$10$...
test:$2y$10$...
```

### Step 4: Restart Docker Containers

```bash
cd ~/owntracks

# Stop containers
docker-compose down

# Start containers with new configuration
docker-compose up -d

# Check logs for errors
docker-compose logs -f
# Press Ctrl+C to exit logs
```

### Step 5: Test Authentication Still Works

```bash
# Test with existing credentials (REPLACE with your actual password)
curl -u roman:YOUR_PASSWORD https://mini.romangarms.com/api/0/last

# Expected: JSON response with OwnTracks data
# If you get 401 Unauthorized, check the htpasswd file and password
```

## Phase 3: Deploy UserManagementAPI

### Step 6: Clone Repository

```bash
cd /opt
sudo git clone https://github.com/romangarms/WhereHaveIBeen-API.git usermanagement
sudo chown -R $USER:$USER /opt/usermanagement
cd /opt/usermanagement
```

### Step 7: Install Dependencies

```bash
# Install Python dependencies
pip3 install -r requirements.txt

# Or use virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 8: Configure Environment Variables

```bash
# Copy example config
cp .env.example .env

# Edit configuration
nano .env
```

**Configuration template:**

```bash
# Flask settings
SECRET_KEY=<GENERATE_RANDOM_KEY>
PORT=5002

# Database path
DATABASE_PATH=/opt/usermanagement/database/users.db

# JWT Secret Key (CRITICAL: Must match frontend!)
# Generate with: python3 -c "import os; print(os.urandom(32).hex())"
JWT_SECRET_KEY=<GENERATE_AND_SAVE_THIS>
JWT_EXPIRY_DAYS=30

# OwnTracks Configuration
# Use Docker network name (no auth needed on internal network)
OWNTRACKS_URL=http://owntracks-recorder:8083

# htpasswd file path (on host filesystem)
OWNTRACKS_HTPASSWD_PATH=/home/romangarms/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd

# Privileged OwnTracks credentials (use your actual credentials)
OWNTRACKS_PRIVILEGED_USER=roman
OWNTRACKS_PRIVILEGED_PASS=<YOUR_ACTUAL_PASSWORD>

# Logging
LOG_LEVEL=INFO
```

**Generate secret keys:**

```bash
# Generate JWT secret (save this - you'll need it for the frontend!)
python3 -c "import os; print(os.urandom(32).hex())"

# Generate Flask secret
python3 -c "import os; print(os.urandom(32).hex())"
```

### Step 9: Create Database Directory

```bash
# Create database directory
sudo mkdir -p /opt/usermanagement/database
sudo chown -R www-data:www-data /opt/usermanagement/database

# Or if running as your user:
mkdir -p /opt/usermanagement/database
```

### Step 10: Initialize Database

```bash
cd /opt/usermanagement
python3 -c "from app import init_db; init_db()"

# Verify database was created
ls -la database/users.db
```

### Step 11: Test API Manually (Optional)

```bash
# Run in foreground for testing
python3 app.py

# In another terminal, test health endpoint
curl http://localhost:5002/health

# Expected: {"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"}

# Press Ctrl+C to stop
```

### Step 12: Deploy as systemd Service

```bash
# Copy service file
sudo cp systemd/usermanagement.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service (start on boot)
sudo systemctl enable usermanagement

# Start service
sudo systemctl start usermanagement

# Check status
sudo systemctl status usermanagement

# View logs
sudo journalctl -u usermanagement -f
# Press Ctrl+C to exit
```

## Phase 4: Testing

### Step 13: Test User Registration

```bash
# Register a new test user
curl -X POST http://localhost:5002/api/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "password": "SecurePass123!",
    "device": "phone"
  }'

# Expected: {"message": "User created successfully", "username": "testuser"}
```

### Step 14: Verify User Added to htpasswd

```bash
# Check htpasswd file
cat ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd

# Should now contain 3 users: roman, test, testuser
```

### Step 15: Test Login

```bash
# Login with new user
curl -X POST http://localhost:5002/api/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "testuser",
    "password": "SecurePass123!"
  }'

# Expected: {"jwt_token": "eyJ...", "username": "testuser", "device": "phone"}
```

### Step 16: Test OwnTracks Mobile App Access

1. Open OwnTracks mobile app
2. Configure connection:
   - Mode: HTTP
   - Host: `mini.romangarms.com`
   - Username: `testuser`
   - Password: `SecurePass123!`
3. Connect and verify authentication works

### Step 17: Test Privacy Enforcement

```bash
# Get JWT token from login (from Step 15)
JWT_TOKEN="<token_from_login>"

# Try to access locations (should work for authenticated user)
curl -H "Authorization: Bearer $JWT_TOKEN" \
  "http://localhost:5002/api/locations?device=phone"

# Should return location data for testuser only
```

## Phase 5: Firewall Configuration

### Step 18: Configure Firewall (if needed)

```bash
# Allow UserManagementAPI from WhereHaveIBeen frontend only
# Get Fly.io IP address first, then:
sudo ufw allow from <fly-io-ip> to any port 5002

# Or allow from anywhere (less secure):
sudo ufw allow 5002
```

## Verification Checklist

- [ ] Backup created successfully
- [ ] docker-compose.yml updated with htpasswd file reference
- [ ] htpasswd file created with existing users
- [ ] Docker containers restarted without errors
- [ ] Existing authentication still works (curl test passed)
- [ ] UserManagementAPI repository cloned
- [ ] Dependencies installed
- [ ] .env file configured with all required values
- [ ] Database initialized
- [ ] systemd service running
- [ ] Health check endpoint returns success
- [ ] New user registration works
- [ ] New user appears in htpasswd file
- [ ] Login returns JWT token
- [ ] OwnTracks mobile app can connect with new user
- [ ] Location API enforces privacy

## Rollback Plan (If Something Goes Wrong)

```bash
# Stop UserManagementAPI
sudo systemctl stop usermanagement
sudo systemctl disable usermanagement

# Stop Docker containers
cd ~/owntracks
docker-compose down

# Restore backup
BACKUP_DIR=~/owntracks/backups/<your_backup_timestamp>
cp "$BACKUP_DIR/docker-compose.yml" ~/owntracks/
cp -r "$BACKUP_DIR/config/"* ~/owntracks/owntracks-recorder/config/

# Restart with old config
docker-compose up -d

# Verify everything works
curl -u roman:YOUR_PASSWORD https://mini.romangarms.com/api/0/last
```

## Troubleshooting

### Issue: Containers won't start

```bash
# Check logs
cd ~/owntracks
docker-compose logs

# Check Traefik specifically
docker-compose logs reverse-proxy

# Verify htpasswd file exists
ls -la ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd
```

### Issue: Authentication fails

```bash
# Verify htpasswd file format
cat ~/owntracks/owntracks-recorder/config/htpasswd/owntracks.htpasswd

# Test authentication directly
curl -u roman:YOUR_PASSWORD https://mini.romangarms.com/api/0/last
```

### Issue: UserManagementAPI won't start

```bash
# Check logs
sudo journalctl -u usermanagement -n 50

# Common issues:
# 1. Missing environment variables - check .env file
# 2. Database permission issues
# 3. Port 5002 already in use
```

### Issue: New users not added to htpasswd

```bash
# Check UserManagementAPI logs
sudo journalctl -u usermanagement -f

# Verify OWNTRACKS_HTPASSWD_PATH is correct in .env
cat /opt/usermanagement/.env | grep HTPASSWD

# Check file permissions
ls -la ~/owntracks/owntracks-recorder/config/htpasswd/
```

## Next Steps

Once this is deployed and tested:

1. **Save the JWT_SECRET_KEY** - you'll need it for the frontend configuration
2. **Update WhereHaveIBeen frontend** with:
   ```bash
   fly secrets set WHIB_USER_API_URL=http://mini.romangarms.com:5002
   fly secrets set WHIB_JWT_SECRET_KEY=<your_jwt_secret_from_env>
   ```
3. **Implement frontend changes** (registration UI, login modifications, etc.)

## Support

If you encounter issues:
1. Check the logs: `sudo journalctl -u usermanagement -f`
2. Review this deployment guide
3. Check the main README: https://github.com/romangarms/WhereHaveIBeen-API
