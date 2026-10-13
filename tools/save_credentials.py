"""Validate captured Google credentials and store them in .credentials.json.

    .venv/bin/python tools/save_credentials.py            # reads capture.txt
    .venv/bin/python tools/save_credentials.py --check    # re-test the stored credentials

capture.txt holds Firefox's "Copy as cURL" of the iframerpc request (or the
issue token URL and cookie string on two lines). On success it is deleted.
Secret values are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

import credfile
from pynestproto.auth import AuthError, AuthUnavailable, GoogleCredentials, NestAuth, parse_capture

DEFAULT_CAPTURE = credfile.ROOT / "capture.txt"


async def validate(creds: GoogleCredentials) -> int:
    rotated_once = False

    def on_rotated(new: GoogleCredentials) -> None:
        nonlocal rotated_once
        rotated_once = True
        credfile.save(new)  # persist immediately; the old cookies are now superseded

    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        auth = NestAuth(session, creds, on_cookies_rotated=on_rotated)
        try:
            token = await auth.async_get_token()
        except AuthUnavailable as err:
            print(f"Temporary failure, try again shortly: {err}")
            if rotated_once:
                print("Google rotated cookies first; saved them. Retry with --check.")
            return 2
        except AuthError as err:
            print(f"Credentials rejected: {err}")
            print("Capture fresh credentials in a new Firefox private window.")
            return 1

    credfile.save(auth.credentials)
    credfile.save_token(token)
    minutes = (token.expires_at - datetime.now(UTC)).total_seconds() / 60
    print("Credentials work.")
    print(f"  Nest user id : {token.user_id}")
    print(f"  JWT expires  : {token.expires_at.isoformat()} ({minutes:.0f} min from now)")
    print(f"  Cookies rotated by Google on this call: {'yes' if rotated_once else 'no'}")
    print(f"  Saved to     : {credfile.DEFAULT_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("capture", nargs="?", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--check", action="store_true", help="re-test stored credentials")
    parser.add_argument("--keep", action="store_true", help="don't delete the capture file")
    args = parser.parse_args()

    if args.check:
        if not credfile.DEFAULT_PATH.exists():
            print(f"No stored credentials at {credfile.DEFAULT_PATH}")
            return 1
        return asyncio.run(validate(credfile.load()))

    if not args.capture.exists():
        print(f"No capture file at {args.capture}")
        return 1
    try:
        creds = parse_capture(args.capture.read_text())
    except ValueError as err:
        print(f"Couldn't read the capture: {err}")
        return 1

    result = asyncio.run(validate(creds))
    if result == 0 and not args.keep:
        args.capture.unlink()
        print(f"  Deleted      : {args.capture}")
    return result


if __name__ == "__main__":
    sys.exit(main())
