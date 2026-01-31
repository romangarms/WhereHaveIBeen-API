# WhereHaveIBeen API

User management and privacy-enforcing API proxy for the [WhereHaveIBeen](https://github.com/romangarms/wherehaveibeen) location tracker.

## Overview

This API server sits between the WhereHaveIBeen frontend and OwnTracks Recorder to provide:

1. **User Registration** - Allow new users to create accounts dynamically
2. **Authentication** - JWT-based authentication for secure API access
3. **Privacy Enforcement** - Ensure users can ONLY access their own location data
4. **Dynamic User Management** - Add users to OwnTracks htpasswd without service restart

## Architecture

```
WhereHaveIBeen (Fly.io) → UserManagementAPI (Linux Server) → OwnTracks Recorder (localhost)
```

The API validates JWT tokens and forces the `user` parameter to match the authenticated user, preventing users from querying other users' location data.

## Features

- ✅ User registration with password validation
- ✅ JWT-based authentication (30-day token expiry)
- ✅ Privacy-enforcing proxy to OwnTracks `/api/0/locations` endpoint
- ✅ Privacy-enforcing proxy to OwnTracks `/api/0/last` endpoint
- ✅ Dynamic OwnTracks htpasswd file management
- ✅ Rate limiting to prevent brute force attacks
- ✅ bcrypt password hashing
- ✅ SQLite database for user accounts

## Requirements

- Python 3.8+
- OwnTracks Recorder (running on same server)
- nginx (for OwnTracks authentication)
- htpasswd utility (apache2-utils or httpd-tools)

## Installation

### 1. Clone the Repository

```bash
cd /opt
sudo git clone https://github.com/romangarms/WhereHaveIBeen-API.git usermanagement
sudo chown -R $USER:$USER /opt/usermanagement
cd /opt/usermanagement
```

### 2. Install Dependencies

```bash
pip3 install -r requirements.txt
```

Or using a virtual environment (recommended):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure Environment Variables

```bash
cp .env.example .env
nano .env
```

Edit `.env` and set the following variables:

```bash
# Generate secret keys
JWT_SECRET_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")
SECRET_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")

# Configure OwnTracks
OWNTRACKS_URL=http://localhost:8083
OWNTRACKS_HTPASSWD_PATH=/etc/nginx/owntracks.htpasswd

# Create privileged OwnTracks user (see below)
OWNTRACKS_PRIVILEGED_USER=api
OWNTRACKS_PRIVILEGED_PASS=your-secure-password-here
```

### 4. Create Privileged OwnTracks User

The API needs a privileged OwnTracks account to proxy requests:

```bash
sudo htpasswd -bB /etc/nginx/owntracks.htpasswd api your-secure-password-here
sudo nginx -s reload
```

Use the same password in your `.env` file for `OWNTRACKS_PRIVILEGED_PASS`.

### 5. Create Database Directory

```bash
sudo mkdir -p /opt/usermanagement/database
sudo chown -R www-data:www-data /opt/usermanagement/database
```

### 6. Initialize Database

```bash
python3 -c "from app import init_db; init_db()"
```

### 7. Deploy as systemd Service

```bash
sudo cp systemd/usermanagement.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable usermanagement
sudo systemctl start usermanagement
```

### 8. Verify Service is Running

```bash
sudo systemctl status usermanagement

# Check logs
sudo journalctl -u usermanagement -f

# Test health endpoint
curl http://localhost:5002/health
```

Expected response:
```json
{"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"}
```

## API Endpoints

### Health Check

```bash
GET /health
```

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

All configuration is via environment variables (`.env` file):

| Variable | Description | Example |
|----------|-------------|---------|
| `JWT_SECRET_KEY` | Secret key for JWT signing (CRITICAL - must match frontend) | `abc123...` |
| `SECRET_KEY` | Flask secret key | `xyz789...` |
| `PORT` | Server port | `5002` |
| `DATABASE_PATH` | SQLite database path | `/opt/usermanagement/database/users.db` |
| `OWNTRACKS_URL` | OwnTracks Recorder URL | `http://localhost:8083` |
| `OWNTRACKS_HTPASSWD_PATH` | Path to htpasswd file | `/etc/nginx/owntracks.htpasswd` |
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

Example of privacy enforcement:

```python
# User Alice tries to access Bob's data
GET /api/locations?user=bob&device=phone
Authorization: Bearer <alice-jwt-token>

# API automatically changes this to:
GET /api/0/locations?user=alice&device=phone  # Force alice's data only
```

## Integration with WhereHaveIBeen Frontend

The frontend needs these environment variables:

```bash
# On Fly.io
fly secrets set WHIB_USER_API_URL=http://mini.romangarms.com:5002
fly secrets set WHIB_JWT_SECRET_KEY=<same-as-api-jwt-secret>
```

## Firewall Configuration

Allow access only from the WhereHaveIBeen frontend server:

```bash
# Ubuntu/Debian with ufw
sudo ufw allow from <whib-server-ip> to any port 5002

# Or open to all (less secure)
sudo ufw allow 5002
```

## Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u usermanagement -n 50

# Common issues:
# 1. Missing environment variables - check .env file
# 2. Database permission issues - check /opt/usermanagement/database ownership
# 3. htpasswd path incorrect - verify OWNTRACKS_HTPASSWD_PATH
```

### Users can't register

```bash
# Check htpasswd command is available
which htpasswd

# Install if missing (Ubuntu/Debian)
sudo apt install apache2-utils

# Check nginx can reload
sudo nginx -t
sudo nginx -s reload
```

### JWT token errors

```bash
# Ensure JWT_SECRET_KEY matches between API and frontend
# Generate a new one:
python3 -c "import os; print(os.urandom(32).hex())"

# Update both .env files and restart services
```

### Privacy not enforced

```bash
# Verify the API is being used (not direct OwnTracks access)
# Check WhereHaveIBeen frontend is calling USER_API_URL, not OwnTracks directly

# Test with curl
curl -H "Authorization: Bearer <jwt-token>" \
  "http://localhost:5002/api/locations?device=phone"
```

## Development

### Running in Development Mode

```bash
# Activate virtual environment
source venv/bin/activate

# Run with Flask development server (DO NOT use in production)
export FLASK_APP=app.py
export FLASK_ENV=development
flask run --port 5002
```

### Running Tests

```bash
# TODO: Add test suite
# python -m pytest tests/
```

## Updating

```bash
cd /opt/usermanagement
git pull
pip3 install -r requirements.txt
sudo systemctl restart usermanagement
```

## Backup

Backup the SQLite database regularly:

```bash
# Create backup
sudo cp /opt/usermanagement/database/users.db /opt/usermanagement/database/users.db.backup

# Or with timestamp
sudo cp /opt/usermanagement/database/users.db /opt/usermanagement/database/users.db.$(date +%Y%m%d)
```

## License

MIT License - See main WhereHaveIBeen project for details

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues or questions, please open an issue on GitHub.
