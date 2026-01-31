"""
WhereHaveIBeen User Management API

This API server provides user registration, authentication, and privacy-enforcing
proxy endpoints for OwnTracks Recorder. It ensures users can only access their
own location data by validating JWT tokens and forcing the 'user' parameter to
match the authenticated user.
"""

import os
import sys
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth

# Import our modules
from config import Config
from models import db, User
from auth import (
    hash_password,
    verify_password,
    generate_jwt,
    decode_jwt,
    validate_password
)
from owntracks_manager import add_user_to_owntracks, update_user_password

app = Flask(__name__)
app.config.from_object(Config)

# Initialize database
db.init_app(app)

# In-memory rate limiting store (use Redis in production)
rate_limit_store = {}


def rate_limit_check(ip, endpoint, max_attempts=5, window_minutes=15):
    """
    Simple in-memory rate limiting.
    Returns (allowed: bool, message: str)
    """
    from datetime import timedelta

    key = f"{ip}:{endpoint}"
    now = datetime.utcnow()

    if key in rate_limit_store:
        attempts, window_start = rate_limit_store[key]
        if now - window_start > timedelta(minutes=window_minutes):
            # Reset window
            rate_limit_store[key] = (1, now)
            return True, ""
        elif attempts >= max_attempts:
            return False, "Rate limit exceeded. Try again later."
        else:
            rate_limit_store[key] = (attempts + 1, window_start)
            return True, ""
    else:
        rate_limit_store[key] = (1, now)
        return True, ""


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"})


@app.route('/api/register', methods=['POST'])
def register():
    """
    Register a new user.

    Request body:
    {
        "username": "alice",
        "password": "SecurePass123!",
        "device": "phone"  // optional, defaults to "phone"
    }

    Returns:
    - 201: User created successfully
    - 400: Validation error
    - 429: Rate limit exceeded
    - 500: Server error
    """
    # Rate limiting
    allowed, msg = rate_limit_check(request.remote_addr, 'register', max_attempts=3, window_minutes=60)
    if not allowed:
        return jsonify({"error": msg}), 429

    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    device = data.get('device', 'phone').strip()

    # Validate username
    if not username or len(username) < 3 or len(username) > 20:
        return jsonify({"error": "Username must be 3-20 characters"}), 400

    if not username.replace('_', '').replace('-', '').isalnum():
        return jsonify({"error": "Username can only contain letters, numbers, hyphens, and underscores"}), 400

    # Validate password
    is_valid, msg = validate_password(password)
    if not is_valid:
        return jsonify({"error": msg}), 400

    # Validate device
    if not device or len(device) < 1 or len(device) > 50:
        return jsonify({"error": "Device name must be 1-50 characters"}), 400

    # Check if user already exists
    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        return jsonify({"error": "Username already exists"}), 400

    try:
        # Hash password
        password_hash = hash_password(password)

        # Create user in database
        new_user = User(
            username=username,
            password_hash=password_hash,
            owntracks_device=device
        )
        db.session.add(new_user)
        db.session.commit()

        # Add user to OwnTracks htpasswd file
        try:
            add_user_to_owntracks(username, password)
            app.logger.info(f"User registered successfully: {username}")
        except Exception as e:
            app.logger.error(f"Failed to add user to OwnTracks: {e}")
            # Rollback database if OwnTracks update fails
            db.session.delete(new_user)
            db.session.commit()
            return jsonify({"error": "Failed to configure OwnTracks access"}), 500

        return jsonify({
            "message": "User created successfully",
            "username": username
        }), 201

    except Exception as e:
        app.logger.error(f"Registration error: {e}")
        db.session.rollback()
        return jsonify({"error": "Registration failed"}), 500


@app.route('/api/login', methods=['POST'])
def login():
    """
    Authenticate user and return JWT token.

    Request body:
    {
        "username": "alice",
        "password": "SecurePass123!"
    }

    Returns:
    - 200: Login successful with JWT token
    - 401: Invalid credentials
    - 429: Rate limit exceeded
    """
    # Rate limiting
    allowed, msg = rate_limit_check(request.remote_addr, 'login', max_attempts=10, window_minutes=15)
    if not allowed:
        return jsonify({"error": msg}), 429

    data = request.get_json()

    if not data:
        return jsonify({"error": "No data provided"}), 401

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 401

    # Find user
    user = User.query.filter_by(username=username).first()

    if not user or not verify_password(password, user.password_hash):
        app.logger.warning(f"Failed login attempt for username: {username} from {request.remote_addr}")
        return jsonify({"error": "Invalid credentials"}), 401

    # Check if user is active
    if not user.is_active:
        return jsonify({"error": "Account is disabled"}), 401

    # Update last login time
    user.last_login = datetime.utcnow()
    db.session.commit()

    # Generate JWT token
    jwt_token = generate_jwt(username, user.owntracks_device)

    app.logger.info(f"User logged in: {username}")

    return jsonify({
        "jwt_token": jwt_token,
        "username": username,
        "device": user.owntracks_device
    }), 200


@app.route('/api/locations', methods=['GET'])
def proxy_locations():
    """
    Privacy-enforcing proxy to OwnTracks /api/0/locations endpoint.

    This endpoint FORCES the 'user' parameter to match the authenticated user
    from the JWT token, preventing users from accessing other users' data.

    Headers:
        Authorization: Bearer <jwt_token>

    Query params: (all passed through to OwnTracks)
        - from: Start timestamp (ISO 8601)
        - to: End timestamp (ISO 8601)
        - device: Device name
        - format: "geojson"

    Returns:
    - 200: Location data (GeoJSON)
    - 401: Invalid or missing JWT token
    - 500: OwnTracks server error
    """
    # Extract and validate JWT token
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.replace('Bearer ', '').strip()

    try:
        payload = decode_jwt(jwt_token)
        authenticated_user = payload['username']
    except Exception as e:
        app.logger.warning(f"Invalid JWT token: {e}")
        return jsonify({"error": "Invalid token"}), 401

    # Build request parameters - FORCE user to authenticated user
    params = dict(request.args)
    params['user'] = authenticated_user  # OVERRIDE any client-provided value

    # Ensure format is geojson
    if 'format' not in params:
        params['format'] = 'geojson'

    # Proxy to OwnTracks with privileged credentials
    try:
        response = requests.get(
            f"{Config.OWNTRACKS_URL}/api/0/locations",
            params=params,
            auth=HTTPBasicAuth(Config.OWNTRACKS_PRIVILEGED_USER, Config.OWNTRACKS_PRIVILEGED_PASS),
            timeout=30
        )
        response.raise_for_status()

        app.logger.info(f"User {authenticated_user} requested locations for device {params.get('device', 'unknown')}")

        return jsonify(response.json()), 200

    except requests.RequestException as e:
        app.logger.error(f"OwnTracks request failed: {e}")
        return jsonify({"error": "Failed to fetch location data"}), 500


@app.route('/api/last', methods=['GET'])
def proxy_last():
    """
    Privacy-enforcing proxy to OwnTracks /api/0/last endpoint.

    Returns only the authenticated user's devices, filtering out all other users.

    Headers:
        Authorization: Bearer <jwt_token>

    Returns:
    - 200: List of devices for authenticated user only
    - 401: Invalid or missing JWT token
    - 500: OwnTracks server error
    """
    # Extract and validate JWT token
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    jwt_token = auth_header.replace('Bearer ', '').strip()

    try:
        payload = decode_jwt(jwt_token)
        authenticated_user = payload['username']
    except Exception as e:
        app.logger.warning(f"Invalid JWT token: {e}")
        return jsonify({"error": "Invalid token"}), 401

    # Proxy to OwnTracks with privileged credentials
    try:
        response = requests.get(
            f"{Config.OWNTRACKS_URL}/api/0/last",
            auth=HTTPBasicAuth(Config.OWNTRACKS_PRIVILEGED_USER, Config.OWNTRACKS_PRIVILEGED_PASS),
            timeout=10
        )
        response.raise_for_status()

        data = response.json()

        # Filter to only return authenticated user's devices
        filtered_data = [
            entry for entry in data
            if entry.get('username') == authenticated_user
        ]

        app.logger.info(f"User {authenticated_user} requested device list")

        return jsonify(filtered_data), 200

    except requests.RequestException as e:
        app.logger.error(f"OwnTracks request failed: {e}")
        return jsonify({"error": "Failed to fetch device data"}), 500


def init_db():
    """Initialize database tables"""
    with app.app_context():
        db.create_all()
        print("Database initialized successfully")


if __name__ == '__main__':
    # Check required environment variables
    required_vars = [
        'JWT_SECRET_KEY',
        'OWNTRACKS_URL',
        'OWNTRACKS_PRIVILEGED_USER',
        'OWNTRACKS_PRIVILEGED_PASS',
        'OWNTRACKS_HTPASSWD_PATH'
    ]

    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
        print("Please create a .env file or set these environment variables.")
        sys.exit(1)

    # Initialize database if it doesn't exist
    if not os.path.exists(Config.SQLALCHEMY_DATABASE_PATH):
        print("Database not found. Initializing...")
        init_db()

    # Run the server
    print(f"Starting WhereHaveIBeen UserManagementAPI on port {Config.PORT}")
    print(f"OwnTracks URL: {Config.OWNTRACKS_URL}")

    from waitress import serve
    serve(app, host='0.0.0.0', port=Config.PORT, threads=10)
