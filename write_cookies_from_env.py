#!/usr/bin/env python3
"""
Heroku startup helper:
- Supports YTDL_COOKIES_CONTENT (raw cookies text) OR
- YTDL_COOKIES_B64 (base64 encoded cookies.txt)
Writes to OUT_PATH (default /tmp/cookies.txt) and optionally execs a command so
the child process inherits the env var set here.

Usage examples:
1) Using Heroku config vars (recommended):
   - Set YTDL_COOKIES_B64 and YTDL_COOKIE_FILE in Heroku config.
   - Procfile: web: python write_cookies_from_env.py && python -m ShrutiMusic

2) Exec mode (script will replace itself with given command, inheriting env):
   web: python write_cookies_from_env.py python -m ShrutiMusic

3) Local eval mode (prints export line):
   eval "$(python write_cookies_from_env.py --export)"
"""
import os
import sys
import stat
import base64
import argparse

DEFAULT_OUT = "/tmp/cookies.txt"
RAW_ENV = "YTDL_COOKIES_CONTENT"
B64_ENV = "YTDL_COOKIES_B64"
OUT_ENV = "YTDL_COOKIE_FILE"


def write_cookie_file(content_bytes: bytes, out_path: str) -> bool:
    try:
        dirpath = os.path.dirname(out_path)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(content_bytes)
    except Exception as e:
        print(f"Failed to write cookies to {out_path}: {e}", file=sys.stderr)
        return False

    try:
        os.chmod(out_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return True


def looks_like_netscape(buf: bytes) -> bool:
    try:
        text = buf.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return text.lstrip().startswith("# Netscape")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true", help="Print 'export' line for shell eval and exit.")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="Optional command to exec after writing cookies.")
    args = parser.parse_args()

    out_path = os.environ.get(OUT_ENV, DEFAULT_OUT)
    raw = os.environ.get(RAW_ENV)
    b64 = os.environ.get(B64_ENV)

    if not raw and not b64:
        print(f"No {RAW_ENV} or {B64_ENV} set; skipping cookie write.", file=sys.stderr)
        if args.export:
            # still print export so caller can use same env
            print(f'export {OUT_ENV}="{out_path}"')
        return 0

    if b64:
        try:
            b64_clean = "".join(b64.split())
            content = base64.b64decode(b64_clean)
        except Exception as e:
            print(f"Failed to decode {B64_ENV}: {e}", file=sys.stderr)
            return 2
    else:
        content = raw.encode("utf-8")

    if not write_cookie_file(content, out_path):
        return 3

    # set env var in THIS process (useful if we exec a child below)
    os.environ[OUT_ENV] = out_path

    # log summary (do not print cookie payload)
    print(f"Wrote cookies to {out_path} (len={len(content)} bytes).")

    # Extra check for format
    if not looks_like_netscape(content):
        print("Warning: written cookie file does not appear to start with Netscape header.", file=sys.stderr)

    # If --export requested, print shell export line for caller to eval
    if args.export:
        print(f'export {OUT_ENV}="{out_path}"')
        return 0

    # If a command was provided, exec it so it inherits os.environ
    if args.cmd:
        # args.cmd is a list like ['python', '-m', 'ShrutiMusic']
        cmd = args.cmd
        # remove leading '--' if present (argparse REMAINDER may include it)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if cmd:
            # Replace current process with the command so env persists
            print(f"Executing: {' '.join(cmd)}")
            os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
