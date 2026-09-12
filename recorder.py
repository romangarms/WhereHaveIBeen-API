"""
Read-only client for the OwnTracks Recorder HTTP API on the internal docker
network. Every caller must pass a `from`: the recorder defaults an omitted
`from` to "now minus 6 hours", not to the start of history.
"""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from config import Config

RECORDER_URL = Config.RECORDER_URL.rstrip('/')
RECORDER_TIMEOUT = Config.RECORDER_TIMEOUT
# "user:password" for reaching the recorder through the public ForwardAuth
# route (local testing against production); unset on the docker network.
RECORDER_AUTH = Config.RECORDER_AUTH

_REC_MONTH = re.compile(r'(\d{4})-(\d{2})\.rec$')
_TIME_FMT = "%Y-%m-%dT%H:%M:%S"


class RecorderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def get(path, **params):
    url = RECORDER_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if RECORDER_AUTH:
        token = base64.b64encode(RECORDER_AUTH.encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=RECORDER_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RecorderError(f"recorder returned HTTP {e.code} for {path}", status=e.code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RecorderError(f"recorder request failed for {path}: {e}") from e


def list_users():
    return get("/api/0/list").get("results", [])


def list_devices(user):
    try:
        return get("/api/0/list", user=user).get("results", [])
    except RecorderError as e:
        if e.status == 404:
            return []
        raise


def list_rec_months(user, device):
    """Months with stored data for a device, as (year, month) tuples, ascending."""
    try:
        files = get("/api/0/list", user=user, device=device).get("results", [])
    except RecorderError as e:
        if e.status == 404:
            return []
        raise
    months = set()
    for name in files:
        m = _REC_MONTH.search(str(name))
        if m:
            months.add((int(m.group(1)), int(m.group(2))))
    return sorted(months)


def fetch_points(user, device, from_dt, to_dt):
    """Raw points for one window. format=json is the only format carrying acc/vel/alt/tst."""
    data = get(
        "/api/0/locations",
        user=user, device=device,
        **{"from": from_dt.strftime(_TIME_FMT), "to": to_dt.strftime(_TIME_FMT)},
        format="json",
    )
    return data.get("data", [])


def windows(start, end, step_days):
    """Consecutive [start, end] datetime windows of at most step_days covering the range."""
    step = timedelta(days=step_days)
    while start < end:
        stop = min(start + step, end)
        yield start, stop
        start = stop


def month_start(year, month):
    return datetime(year, month, 1, tzinfo=timezone.utc)
