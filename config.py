"""
Configuration for WhereHaveIBeen UserManagement API

All configuration is loaded from environment variables.
Create a .env file or set these in your deployment environment.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()


class Config:
    """Application configuration"""

    # Flask settings
    SECRET_KEY = os.getenv('SECRET_KEY', os.urandom(32).hex())
    PORT = int(os.getenv('PORT', 5002))

    # Database settings
    SQLALCHEMY_DATABASE_PATH = os.getenv('DATABASE_PATH', '/opt/usermanagement/database/users.db')
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{SQLALCHEMY_DATABASE_PATH}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # JWT settings
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY')
    JWT_EXPIRY_DAYS = int(os.getenv('JWT_EXPIRY_DAYS', 30))

    # OwnTracks Recorder settings
    OWNTRACKS_URL = os.getenv('OWNTRACKS_URL', 'http://localhost:8083')
    OWNTRACKS_HTPASSWD_PATH = os.getenv('OWNTRACKS_HTPASSWD_PATH', '/etc/nginx/owntracks.htpasswd')

    # Privileged OwnTracks credentials (for proxy access)
    # This account should have access to all users' data
    OWNTRACKS_PRIVILEGED_USER = os.getenv('OWNTRACKS_PRIVILEGED_USER')
    OWNTRACKS_PRIVILEGED_PASS = os.getenv('OWNTRACKS_PRIVILEGED_PASS')

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
