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

    # --- Aggregate "visited roads" endpoint (/api/aggregate-roads) ---
    # Recorder is reachable on the internal docker network without auth.
    RECORDER_URL = os.getenv('RECORDER_URL', 'http://owntracks-recorder:8083')
    RECORDER_TIMEOUT = int(os.getenv('RECORDER_TIMEOUT', 60))
    # Time range + windowing (recorder has no offset pagination; we page by time).
    AGGREGATE_FROM = os.getenv('AGGREGATE_FROM', '2024-08-01')
    AGGREGATE_WINDOW_DAYS = int(os.getenv('AGGREGATE_WINDOW_DAYS', 30))
    # Geometry (metres). Buffer radius mirrors the frontend's ~0.5km corridor.
    AGGREGATE_BUFFER_M = float(os.getenv('AGGREGATE_BUFFER_M', 500))
    AGGREGATE_SIMPLIFY_M = float(os.getenv('AGGREGATE_SIMPLIFY_M', 15))       # pre-buffer
    AGGREGATE_OUT_SIMPLIFY_M = float(os.getenv('AGGREGATE_OUT_SIMPLIFY_M', 30))  # post-union
    # Point filtering (mirrors the frontend).
    AGGREGATE_ACC_MAX_M = float(os.getenv('AGGREGATE_ACC_MAX_M', 100))
    AGGREGATE_MIN_DIST_M = float(os.getenv('AGGREGATE_MIN_DIST_M', 20))
    AGGREGATE_FLIGHT_SPEED_KMH = float(os.getenv('AGGREGATE_FLIGHT_SPEED_KMH', 200))
    AGGREGATE_FLIGHT_JUMP_KM = float(os.getenv('AGGREGATE_FLIGHT_JUMP_KM', 100))
    # Caching.
    AGGREGATE_TTL_SECONDS = int(os.getenv('AGGREGATE_TTL_SECONDS', 86400))
    AGGREGATE_CACHE_PATH = os.getenv('AGGREGATE_CACHE_PATH', '/data/aggregate_roads.json')
