"""
Authentication utilities for password hashing and validation
"""

import bcrypt
import re


def hash_password(password):
    """
    Hash a password using bcrypt.

    Args:
        password (str): Plain text password

    Returns:
        str: Bcrypt hashed password
    """
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(password, password_hash):
    """
    Verify a password against its hash.

    Args:
        password (str): Plain text password
        password_hash (str): Bcrypt hash to verify against

    Returns:
        bool: True if password matches, False otherwise
    """
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except Exception:
        return False


def validate_password(password):
    """
    Validate password strength.

    Requirements:
    - At least 12 characters
    - Contains uppercase letter
    - Contains lowercase letter
    - Contains number

    Args:
        password (str): Password to validate

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    if not password:
        return False, "Password is required"

    if len(password) < 12:
        return False, "Password must be at least 12 characters"

    if len(password) > 128:
        return False, "Password must be less than 128 characters"

    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one number"

    return True, "Valid"
