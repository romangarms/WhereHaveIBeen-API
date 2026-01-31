#!/usr/bin/env python3
"""
Migration script to copy users from htpasswd file to SQLite database.

Bcrypt hashes from htpasswd are compatible with the database format.
This script reads the htpasswd file and inserts users into SQLite.

Usage:
    python migrate_htpasswd_users.py /path/to/htpasswd/file [/path/to/database.db]
"""

import sys
import os

# Add the current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from models import db, User
from datetime import datetime


def migrate_users(htpasswd_path, db_path=None):
    """
    Migrate users from htpasswd file to SQLite database.

    Args:
        htpasswd_path: Path to the htpasswd file
        db_path: Optional path to database file (defaults to ./data/users.db)
    """
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), 'data', 'users.db')

    # Ensure database directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, mode=0o755, exist_ok=True)

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        # Create tables if they don't exist
        db.create_all()

        # Read htpasswd file
        if not os.path.exists(htpasswd_path):
            print(f"Error: htpasswd file not found: {htpasswd_path}")
            sys.exit(1)

        with open(htpasswd_path, 'r') as f:
            lines = f.readlines()

        migrated = 0
        skipped = 0

        for line in lines:
            line = line.strip()
            if not line or ':' not in line:
                continue

            username, password_hash = line.split(':', 1)

            # Check if user already exists
            existing = User.query.filter_by(username=username).first()
            if existing:
                print(f"  Skipping '{username}' - already exists in database")
                skipped += 1
                continue

            # Create new user with the existing bcrypt hash
            new_user = User(
                username=username,
                password_hash=password_hash,
                owntracks_device='phone',  # Default device
                created_at=datetime.utcnow(),
                is_active=True
            )

            db.session.add(new_user)
            print(f"  Migrated '{username}'")
            migrated += 1

        db.session.commit()

        print(f"\nMigration complete: {migrated} users migrated, {skipped} skipped")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python migrate_htpasswd_users.py /path/to/htpasswd/file [/path/to/database.db]")
        sys.exit(1)

    htpasswd_path = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Migrating users from: {htpasswd_path}")
    if db_path:
        print(f"Database path: {db_path}")
    migrate_users(htpasswd_path, db_path)
