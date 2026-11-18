"""Abonnements et positions, sans requêtes vers les éditeurs ni fichiers audio."""
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

# Compatibilité de migration pour les installations du service déjà configurées.
ROOT = Path(os.environ.get("ONDIER_DATA")
            or os.environ.get("QUIRE_PODCAST_DATA")
            or os.environ.get("SCHOLIA_PODCAST_DATA")
            or Path.home() / ".local/share/ondier")
SEED = Path(__file__).with_name("podcasts-seed.json")
LOCK = threading.Lock()
ID = re.compile(r"[0-9a-f]{64}")
DURATION_OVERRIDE = "minimumEpisodeMinutesOverride"


def public_url(raw):
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("Invalid feed address.")
    url = urlsplit(raw)
    if url.scheme not in ("https", "http") or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("Invalid feed address.")
    return raw


def string(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid podcast field.")
    return value


def subscription(item):
    key = item.get("id")
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise ValueError("Invalid subscription ID.")
    raw_image = item.get("artwork")
    notify = item.get("notify", False)
    if not isinstance(notify, bool):
        raise ValueError("Invalid notification setting.")
    normalized = {
        "id": key, "url": public_url(item.get("url")),
        "title": string(item.get("title"), 400), "publisher": string(item.get("publisher"), 400),
        "artwork": public_url(raw_image) if raw_image is not None else None, "notify": notify,
    }
    if DURATION_OVERRIDE in item:
        minutes = item[DURATION_OVERRIDE]
        if type(minutes) is not int or not 0 <= minutes < 2**63:
            raise ValueError("Invalid minimum episode duration.")
        normalized[DURATION_OVERRIDE] = minutes
    return normalized


def initial():
    subscriptions = [] if os.environ.get("ONDIER_EMPTY_LIBRARY") == "1" else json.loads(SEED.read_text(encoding="utf-8"))
    return {"subscriptions": subscriptions, "progress": {}}


def load():
    try:
        with (ROOT / "state.json").open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return initial()


def save(state):
    if not ROOT.parent.is_dir():
        raise FileNotFoundError("Podcast storage is unavailable.")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".podcasts-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(json.dumps(state, ensure_ascii=False, allow_nan=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
            temp.replace(ROOT / "state.json")
        finally:
            temp.unlink(missing_ok=True)


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 9_000_000_000_000:
        raise ValueError("Invalid timestamp.")
    return value


def change_subscription(state, mutation):
    operation = mutation.get("operation")
    key = mutation.get("id")
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise ValueError("Invalid subscription ID.")
    stamp = timestamp(mutation.get("updatedAt"))
    versions = state.setdefault("subscriptionVersions", {})
    if stamp < versions.get(key, -1):
        return
    current = {item["id"]: item for item in state["subscriptions"]}
    if operation == "upsert":
        item = mutation.get("subscription")
        if not isinstance(item, dict) or item.get("id") != key:
            raise ValueError("Invalid subscription.")
        normalized = subscription(item)
        previous = current.get(key, {})
        if DURATION_OVERRIDE in previous:
            # Older Roan versions can change notifications without sending this preference.
            normalized.setdefault(DURATION_OVERRIDE, previous[DURATION_OVERRIDE])
        current[key] = normalized
    elif operation == "remove":
        current.pop(key, None)
    else:
        raise ValueError("Unknown subscription operation.")
    if len(current) > 500 or len(versions) >= 10_000 and key not in versions:
        raise ValueError("Too many subscriptions.")
    versions[key] = stamp
    state["subscriptions"] = list(current.values())


def change_progress(state, mutation):
    key = mutation.get("id")
    value = mutation.get("progress")
    if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(value, dict):
        raise ValueError("Invalid episode progress.")
    position = value.get("position")
    if type(position) not in (int, float) or not math.isfinite(position) or not 0 <= position <= 30 * 86400:
        raise ValueError("Invalid playback position.")
    updated = timestamp(value.get("updatedAt"))
    finished = value.get("finished")
    if not isinstance(finished, bool):
        raise ValueError("Invalid played status.")
    current = state["progress"]
    if len(current) >= 100_000 and key not in current:
        raise ValueError("Too many episode positions.")
    if updated >= current.get(key, {}).get("updatedAt", -1):
        current[key] = {"position": position, "finished": finished, "updatedAt": updated}


def endpoint(route, mutation=None):
    try:
        with LOCK:
            state = load()
            if mutation is not None:
                if not isinstance(mutation, dict):
                    raise ValueError("Invalid request.")
                if route == "subscriptions":
                    change_subscription(state, mutation)
                else:
                    change_progress(state, mutation)
                save(state)
                return 200, {"ok": True}
            return 200, {route: state[route]}
    except (OSError, KeyError, json.JSONDecodeError):
        return 503, {"error": "Podcast storage is unavailable."}
    except (ValueError, OverflowError):
        return 400, {"error": "Invalid podcast request."}
