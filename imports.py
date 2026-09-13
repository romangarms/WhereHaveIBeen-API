"""
Imported location history that is not in the OwnTracks recorder.

One file pair per (user, source) under IMPORT_DIR/<username>/: <source>.pkl
holds the Fix list, <source>.json the metadata that /api/me/imports lists.
Imports enter the track pipeline as pseudo-devices named "import:<source>"
(see track_cache.update); a fix is dropped for any UTC day on which the
recorder holds a fix, so OwnTracks wins wherever it was recording.
"""

import json
import logging
import os
import pickle
import re
import threading
import time

from config import Config

log = logging.getLogger("imports")

IMPORT_DIR = Config.IMPORT_DIR
SOURCES = ("google-timeline",)
DEVICE_PREFIX = "import:"
# Coverage granularity for "OwnTracks already has data here".
OVERLAP_BUCKET_S = 86400

_SAFE_USERNAME = re.compile(r'^[A-Za-z0-9-]{1,64}$')
_lock = threading.Lock()
_meta_cache = {}


def device_name(source):
    return DEVICE_PREFIX + source


def is_import_device(device):
    return device.startswith(DEVICE_PREFIX)


def source_of(device):
    return device[len(DEVICE_PREFIX):]


def _user_dir(username):
    if not _SAFE_USERNAME.match(username):
        raise ValueError("username is not a safe path segment")
    return os.path.join(IMPORT_DIR, username)


def _paths(username, source):
    if source not in SOURCES:
        raise ValueError("unknown import source")
    base = os.path.join(_user_dir(username), source)
    return base + ".pkl", base + ".json"


def _read_meta(username):
    out = {}
    try:
        names = os.listdir(_user_dir(username))
    except FileNotFoundError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        source = name[:-5]
        if source not in SOURCES:
            continue
        try:
            with open(os.path.join(_user_dir(username), name)) as fh:
                out[source] = json.load(fh)
        except Exception as e:
            log.warning("imports: unreadable meta %s/%s: %s", username, name, e)
    return out


def list_imports(username):
    """{source: meta} for every stored import; cached per user."""
    with _lock:
        meta = _meta_cache.get(username)
        if meta is None:
            meta = _meta_cache[username] = _read_meta(username)
        return dict(meta)


def fingerprint(username):
    """Changes whenever the user's imports change; part of every cache key."""
    meta = list_imports(username)
    return "|".join(f"{s}:{meta[s].get('imported_at', 0)}" for s in sorted(meta))


def save(username, source, fixes, meta):
    pkl, js = _paths(username, source)
    os.makedirs(os.path.dirname(pkl), exist_ok=True)
    row = dict(meta)
    row["source"] = source
    row["imported_at"] = int(time.time())
    tmp = pkl + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump([tuple(f) for f in fixes], fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, pkl)
    tmp = js + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(row, fh)
    os.replace(tmp, js)
    with _lock:
        _meta_cache.pop(username, None)
    return row


def delete(username, source):
    """True if an import was removed."""
    pkl, js = _paths(username, source)
    removed = False
    for path in (pkl, js):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    with _lock:
        _meta_cache.pop(username, None)
    return removed


def load_fixes(username, source):
    from track import Fix
    pkl, _ = _paths(username, source)
    try:
        with open(pkl, "rb") as fh:
            return [Fix(*f) for f in pickle.load(fh)]
    except FileNotFoundError:
        return []


def bucket(tst):
    return int(tst // OVERLAP_BUCKET_S)


def covered_buckets(fixes, into=None):
    """Add the overlap buckets the recorder fixes fall in. Mutates and returns into."""
    if into is None:
        into = set()
    for f in fixes:
        into.add(bucket(f.tst))
    return into


def select(fixes, lower, lower_inclusive, upper, covered):
    """Imported fixes inside the window whose bucket the recorder does not
    cover. Returns (kept, skipped_count)."""
    kept, skipped = [], 0
    for f in fixes:
        if f.tst > upper:
            break
        if lower is not None and (f.tst < lower or (f.tst == lower and not lower_inclusive)):
            continue
        if bucket(f.tst) in covered:
            skipped += 1
            continue
        kept.append(f)
    return kept, skipped
