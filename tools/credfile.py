"""Credential storage for the dev tools: a gitignored .credentials.json.

Holds the Google credentials and the current Nest token, so several dev tools
can run at once: whichever mints a token saves it and the others reuse it,
and cookie rotations are saved immediately so no tool works from a stale copy.
Written atomically with mode 0600. (The HA integration stores credentials in
its config entry instead.)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "protonest"))

from pynestproto.auth import GoogleCredentials, NestAuth, NestToken  # noqa: E402

DEFAULT_PATH = ROOT / ".credentials.json"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def _write(data: dict[str, Any], path: Path) -> None:
    data["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path):
    """Hold an exclusive lock across a read-modify-write, so two tools saving at
    once don't write back each other's stale fields."""
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(creds: GoogleCredentials, path: Path = DEFAULT_PATH) -> None:
    """Save credentials (e.g. after a cookie rotation), keeping other fields."""
    with _locked(path):
        data = _read(path)
        data.update(issue_token=creds.issue_token, cookies=creds.cookies)
        _write(data, path)


def save_token(token: NestToken, path: Path = DEFAULT_PATH) -> None:
    with _locked(path):
        data = _read(path)
        data.update(
            user_id=token.user_id,
            token={"jwt": token.jwt, "user_id": token.user_id, "expires_at": token.expires_at.isoformat()},
        )
        _write(data, path)


def _save_quietly(save_fn, value, path: Path) -> None:
    """For auth hooks: a failed save shouldn't stop a running tool."""
    try:
        save_fn(value, path)
    except OSError as err:
        print(f"warning: couldn't save to {path.name}: {err}", file=sys.stderr)


def load(path: Path = DEFAULT_PATH) -> GoogleCredentials:
    return load_shared(path)[0]


def load_shared(path: Path = DEFAULT_PATH) -> tuple[GoogleCredentials, NestToken | None]:
    data = _read(path)
    creds = GoogleCredentials(issue_token=data["issue_token"], cookies=data["cookies"])
    tok = data.get("token")
    token = (
        NestToken(jwt=tok["jwt"], user_id=tok["user_id"], expires_at=datetime.fromisoformat(tok["expires_at"]))
        if tok
        else None
    )
    return creds, token


def make_auth(session, path: Path = DEFAULT_PATH, on_rotated=None) -> NestAuth:
    """NestAuth wired to share credentials and tokens through the file."""
    creds, token = load_shared(path)

    def rotated(new: GoogleCredentials) -> None:
        _save_quietly(save, new, path)
        if on_rotated:
            on_rotated(new)

    return NestAuth(
        session,
        creds,
        on_cookies_rotated=rotated,
        token=token,
        on_token=lambda t: _save_quietly(save_token, t, path),
        reload=lambda: load_shared(path),
    )
