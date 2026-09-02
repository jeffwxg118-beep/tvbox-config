from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]

SOURCES_FILE = ROOT / "sources.json"
OUTPUT_FILE = ROOT / "tvbox.json"
STATUS_FILE = ROOT / "active-source.json"

LKG_DIR = ROOT / "last-known-good"
LKG_FILE = LKG_DIR / "tvbox.json"

MIN_BYTES = 3000
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 25
RETRIES = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_sources() -> list[dict]:
    with SOURCES_FILE.open("r", encoding="utf-8") as f:
        config = json.load(f)

    sources = [
        item
        for item in config.get("sources", [])
        if item.get("enabled", True)
    ]

    sources.sort(key=lambda x: x.get("priority", 9999))

    if not sources:
        raise RuntimeError("No enabled TVBox sources found.")

    return sources


def download(url: str) -> bytes:
    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 TVBox-Config-Updater/1.0",
                    "Accept": "*/*",
                },
            )

            with urlopen(req, timeout=TIMEOUT) as response:
                status = getattr(response, "status", 200)

                if status != 200:
                    raise RuntimeError(f"HTTP status {status}")

                data = response.read(MAX_BYTES + 1)

                if len(data) > MAX_BYTES:
                    raise RuntimeError("File exceeds maximum size")

                return data

        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            print(f"[download] attempt {attempt}/{RETRIES} failed: {exc}")

            if attempt < RETRIES:
                time.sleep(3)

    raise RuntimeError(
        f"Download failed after {RETRIES} attempts: {last_error}"
    )


def looks_like_html(text: str) -> bool:
    sample = text[:2000].lower()
    return (
        "<html" in sample
        or "<!doctype html" in sample
        or "<body" in sample
    )


def validate_tvbox(data: bytes) -> tuple[bool, str]:
    size = len(data)

    if size < MIN_BYTES:
        return False, f"too small: {size} bytes"

    if size > MAX_BYTES:
        return False, f"too large: {size} bytes"

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False, "not valid UTF-8 text"

    if looks_like_html(text):
        return False, "HTML/error page detected"

    if '"sites"' not in text and "'sites'" not in text:
        return False, "missing sites section"

    sites_match = re.search(
        r'["\']?sites["\']?\s*:\s*\[',
        text,
        flags=re.IGNORECASE,
    )

    if not sites_match:
        return False, "sites array not detected"

    site_key_count = len(
        re.findall(
            r'["\']?key["\']?\s*:',
            text,
            flags=re.IGNORECASE,
        )
    )

    if site_key_count < 2:
        return False, f"too few site entries: {site_key_count}"

    suspicious_markers = [
        "404: not found",
        "repository not found",
        "access denied",
        "bad gateway",
        "service unavailable",
    ]

    lower = text[:5000].lower()

    for marker in suspicious_markers:
        if marker in lower:
            return False, f"error marker detected: {marker}"

    if not text.rstrip().endswith("}"):
        return False, "file appears truncated"

    return True, f"healthy; {size} bytes; site_keys={site_key_count}"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_name, path)

    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_status(
    source: dict,
    data: bytes,
    message: str,
    changed: bool,
) -> None:
    status = {
        "status": "healthy",
        "source": source["id"],
        "name": source["name"],
        "url": source["url"],
        "priority": source.get("priority"),
        "updated_at": utc_now(),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "changed": changed,
        "message": message,
    }

    STATUS_FILE.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def save_failed_status(errors: list[dict]) -> None:
    status = {
        "status": "all_sources_failed",
        "updated_at": utc_now(),
        "using_last_known_good": LKG_FILE.exists() or OUTPUT_FILE.exists(),
        "errors": errors,
    }

    STATUS_FILE.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    print("TVBox production updater")
    print(f"Started: {utc_now()}")

    sources = load_sources()
    errors = []

    for source in sources:
        sid = source["id"]
        name = source["name"]
        url = source["url"]

        print()
        print(f"Checking source {sid}: {name}")
        print(url)

        try:
            data = download(url)

            healthy, message = validate_tvbox(data)

            print(f"Validation: {message}")

            if not healthy:
                raise RuntimeError(message)

            digest = sha256_bytes(data)

            if OUTPUT_FILE.exists():
                current = OUTPUT_FILE.read_bytes()
                current_digest = sha256_bytes(current)

                if current_digest == digest:
                    print("Config unchanged.")
                    save_status(
                        source,
                        data,
                        "healthy / unchanged",
                        changed=False,
                    )
                    return 0

                current_ok, current_msg = validate_tvbox(current)

                if current_ok:
                    LKG_DIR.mkdir(parents=True, exist_ok=True)
                    print(
                        "Backing up current config as last-known-good."
                    )
                    atomic_write(LKG_FILE, current)
                else:
                    print(
                        "Current config not healthy; "
                        f"not promoted to LKG: {current_msg}"
                    )

            print(f"Activating source {sid}.")
            atomic_write(OUTPUT_FILE, data)

            final_ok, final_msg = validate_tvbox(
                OUTPUT_FILE.read_bytes()
            )

            if not final_ok:
                raise RuntimeError(
                    f"post-write validation failed: {final_msg}"
                )

            save_status(
                source,
                data,
                final_msg,
                changed=True,
            )

            print("Update completed successfully.")
            return 0

        except Exception as exc:
            error = {
                "source": sid,
                "name": name,
                "url": url,
                "error": str(exc),
            }

            errors.append(error)

            print(f"Source {sid} rejected: {exc}")

    print()
    print("All external sources failed.")

    if LKG_FILE.exists():
        print("Restoring last-known-good configuration.")

        lkg_data = LKG_FILE.read_bytes()

        valid, message = validate_tvbox(lkg_data)

        if valid:
            atomic_write(OUTPUT_FILE, lkg_data)
            print(f"LKG restored: {message}")
        else:
            print(f"LKG invalid: {message}")

    elif OUTPUT_FILE.exists():
        print("Keeping existing tvbox.json unchanged.")

    else:
        print("No usable config exists.")

    save_failed_status(errors)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise
