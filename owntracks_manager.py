"""
OwnTracks Recorder htpasswd file management

DEPRECATED: This module is no longer used.

With the switch to Traefik ForwardAuth middleware, authentication is now
handled directly against the SQLite database via the /auth/verify endpoint.
This eliminates the need for htpasswd file management.

This file is kept for reference but can be safely deleted.

Previous functionality:
- Adding/updating users in the OwnTracks htpasswd file
- Traefik usersfile did NOT support live reloading (contrary to prior belief)

See app.py:/auth/verify for the new authentication flow.
"""

import subprocess
import os


def add_user_to_owntracks(username, password):
    """
    Add or update a user in the OwnTracks htpasswd file.

    This function adds the user to the htpasswd file with bcrypt hashing.
    Traefik automatically picks up the changes without needing a reload.

    Args:
        username (str): Username to add
        password (str): Plain text password

    Raises:
        RuntimeError: If htpasswd command fails
    """
    htpasswd_path = os.getenv('OWNTRACKS_HTPASSWD_PATH')

    if not htpasswd_path:
        raise ValueError("OWNTRACKS_HTPASSWD_PATH environment variable not set")

    # Ensure directory exists
    htpasswd_dir = os.path.dirname(htpasswd_path)
    if htpasswd_dir and not os.path.exists(htpasswd_dir):
        os.makedirs(htpasswd_dir, mode=0o755, exist_ok=True)

    # Ensure htpasswd file exists
    if not os.path.exists(htpasswd_path):
        # Create empty file with appropriate permissions
        with open(htpasswd_path, 'a'):
            pass
        os.chmod(htpasswd_path, 0o644)

    # Add or update user in htpasswd file
    # -b: batch mode (password on command line)
    # -B: use bcrypt hashing (same as OwnTracks/Traefik uses)
    try:
        result = subprocess.run(
            ['htpasswd', '-bB', htpasswd_path, username, password],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to update htpasswd file: {e.stderr}")
    except FileNotFoundError:
        raise RuntimeError("htpasswd command not found. Please install apache2-utils (Debian/Ubuntu) or httpd-tools (RHEL/CentOS)")

    # Note: Traefik automatically watches the htpasswd file and picks up changes
    # No reload needed!


def update_user_password(username, new_password):
    """
    Update an existing user's password in the htpasswd file.

    This is an alias for add_user_to_owntracks, as htpasswd will
    update the entry if the user already exists.

    Args:
        username (str): Username to update
        new_password (str): New plain text password

    Raises:
        RuntimeError: If htpasswd command fails
    """
    add_user_to_owntracks(username, new_password)


def remove_user_from_owntracks(username):
    """
    Remove a user from the OwnTracks htpasswd file.

    Args:
        username (str): Username to remove

    Raises:
        RuntimeError: If htpasswd command fails
    """
    htpasswd_path = os.getenv('OWNTRACKS_HTPASSWD_PATH')

    if not htpasswd_path:
        raise ValueError("OWNTRACKS_HTPASSWD_PATH environment variable not set")

    if not os.path.exists(htpasswd_path):
        return  # Nothing to remove

    # Remove user from htpasswd file
    # -D: delete user
    try:
        subprocess.run(
            ['htpasswd', '-D', htpasswd_path, username],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        # User might not exist, which is fine
        if "not found" not in e.stderr.lower():
            raise RuntimeError(f"Failed to remove user from htpasswd file: {e.stderr}")

    # Note: Traefik automatically watches the htpasswd file and picks up changes
    # No reload needed!
