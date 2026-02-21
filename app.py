"""
WhereHaveIBeen User Management API

Provides user registration and ForwardAuth for Traefik to validate
OwnTracks Basic Auth credentials against the SQLite database.
"""

import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

# Import our modules
from config import Config
from models import db, User
from auth import hash_password, verify_password, validate_password

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


@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "WhereHaveIBeen UserManagementAPI"})


@app.route('/auth/verify', methods=['GET'])
def auth_verify():
    """
    ForwardAuth endpoint for Traefik.

    This endpoint is called by Traefik's ForwardAuth middleware to validate
    HTTP Basic Auth credentials against the SQLite database.

    Headers:
        Authorization: Basic <base64(username:password)>

    Returns:
        - 200: Authentication successful (sets X-Forwarded-User header)
        - 401: Invalid or missing credentials
    """
    import base64

    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Basic '):
        return '', 401

    try:
        # Decode Base64 credentials
        encoded_credentials = auth_header[6:]  # Remove 'Basic ' prefix
        decoded = base64.b64decode(encoded_credentials).decode('utf-8')
        username, password = decoded.split(':', 1)
    except (ValueError, UnicodeDecodeError):
        return '', 401

    if not username or not password:
        return '', 401

    # Find user in database
    user = User.query.filter_by(username=username).first()

    if not user or not verify_password(password, user.password_hash):
        app.logger.warning(f"ForwardAuth: Failed auth for username: {username}")
        return '', 401

    # Check if user is active
    if not user.is_active:
        app.logger.warning(f"ForwardAuth: Inactive user attempted auth: {username}")
        return '', 401

    app.logger.debug(f"ForwardAuth: Successful auth for user: {username}")

    # Return 200 with X-Forwarded-User header for downstream services
    response = app.make_response('')
    response.headers['X-Forwarded-User'] = username
    return response, 200


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

        app.logger.info(f"User registered successfully: {username}")

        return jsonify({
            "message": "User created successfully",
            "username": username
        }), 201

    except Exception as e:
        app.logger.error(f"Registration error: {e}")
        db.session.rollback()
        return jsonify({"error": "Registration failed"}), 500


@app.route('/api/delete-account', methods=['POST'])
def delete_account():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not verify_password(password, user.password_hash):
        return jsonify({"error": "Invalid credentials"}), 401

    try:
        db.session.delete(user)
        db.session.commit()
        app.logger.info(f"User deleted their account: {username}")
        return jsonify({"message": "Account deleted successfully"}), 200
    except Exception as e:
        app.logger.error(f"Delete account error: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to delete account"}), 500


def init_db():
    """Initialize database tables"""
    with app.app_context():
        db.create_all()
        print("Database initialized successfully")


if __name__ == '__main__':
    # Initialize database if it doesn't exist
    if not os.path.exists(Config.SQLALCHEMY_DATABASE_PATH):
        print("Database not found. Initializing...")
        init_db()

    # Run the server
    print(f"Starting WhereHaveIBeen UserManagementAPI on port {Config.PORT}")

    from waitress import serve
    serve(app, host='0.0.0.0', port=Config.PORT, threads=10)
