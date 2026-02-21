"""
Configuration for WhereHaveIBeen UserManagement API

All configuration is loaded from environment variables.
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
    SQLALCHEMY_DATABASE_PATH = os.getenv('DATABASE_PATH', '/data/users.db')
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{SQLALCHEMY_DATABASE_PATH}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
