import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="whib-tests-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "users.db")
os.environ["TRACK_CACHE_DIR"] = os.path.join(_TMP, "tracks")
os.environ["IMPORT_DIR"] = os.path.join(_TMP, "imports")
os.environ["AGGREGATE_CACHE_PATH"] = os.path.join(_TMP, "aggregate.json")
os.environ["RECORDER_URL"] = "http://127.0.0.1:9"
os.environ["RECORDER_TIMEOUT"] = "1"

import base64  # noqa: E402

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from auth import hash_password  # noqa: E402
from models import User, db  # noqa: E402

PASSWORD = "CorrectHorse123"


@pytest.fixture(scope="session")
def flask_app():
    a = app_module.app
    a.config["TESTING"] = True
    with a.app_context():
        db.create_all()
        db.session.add(User(username="alice", password_hash=hash_password(PASSWORD),
                            owntracks_device="phone"))
        db.session.add(User(username="bob", password_hash=hash_password(PASSWORD),
                            owntracks_device="phone", is_active=False))
        db.session.commit()
    yield a


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def basic(username, password=PASSWORD):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}
