#!/usr/bin/env python3
"""
Pi Social AutoPost — random video from a queue folder, posted to TikTok +
Instagram simultaneously via the Zernio API.

Picks a random video from QUEUE_DIR, uploads it to Zernio, publishes it to
both platforms, then moves the file (and its caption, if any) to posted/.
On failure the file is moved to failed/ so it can be retried later.

Folder layout:
    QUEUE_DIR/
        clip1.mp4
        clip1.txt          <- optional per-clip caption (same basename)
        clip2.mov
        captions/          <- optional pool of fallback captions (*.txt)
        posted/            <- created automatically
        failed/            <- created automatically

Run manually:   python3 autopost.py
Scheduled:      via systemd timer (see systemd/autopost.timer)
"""

from __future__ import annotations  # keeps type hints working on Python 3.9 (Pi OS Bullseye)

import json
import logging
import os
import random
import sys
from pathlib import Path

import requests

# ---- Config (all overridable via environment variables) --------------------

def _env(*names, default=None):
    """Return the first set environment variable among names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


QUEUE_DIR = Path(_env("AUTOPOST_QUEUE_DIR", "KRECXX_QUEUE_DIR",
                      default="/mnt/ssd/social-queue"))
CAPTIONS_DIR = Path(_env("AUTOPOST_CAPTIONS_DIR", "KRECXX_CAPTIONS_DIR",
                         default=str(QUEUE_DIR / "captions")))
LOG_FILE = Path(_env("AUTOPOST_LOG_FILE", "KRECXX_LOG_FILE",
                     default=str(QUEUE_DIR / "autopost.log")))

POSTED_DIR = QUEUE_DIR / "posted"
FAILED_DIR = QUEUE_DIR / "failed"

ZERNIO_BASE = os.environ.get("ZERNIO_BASE", "https://zernio.com/api/v1")
ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY")

# Account IDs must come from the environment — find yours in your Zernio
# connected-accounts list (see README Step 2).
TIKTOK_ACCOUNT_ID = os.environ.get("TIKTOK_ACCOUNT_ID")
INSTAGRAM_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID")

VIDEO_EXTENSIONS = {".mp4", ".mov"}

# When the queue is empty, re-post clips from posted/ instead of going dark.
# Cycles fairly: every posted clip is replayed once (random order) before any
# clip repeats. Set AUTOPOST_RECYCLE=0 to disable.
RECYCLE = os.environ.get("AUTOPOST_RECYCLE", "1") == "1"
RECYCLE_STATE = QUEUE_DIR / ".recycle_state.json"

# ---- Logging ---------------------------------------------------------------

_handlers = [logging.StreamHandler(sys.stdout)]
try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(LOG_FILE))
except Exception:
    pass  # never let logging setup kill the poster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("autopost")


def require_config():
    missing = [
        name for name, val in (
            ("ZERNIO_API_KEY", ZERNIO_API_KEY),
            ("TIKTOK_ACCOUNT_ID", TIKTOK_ACCOUNT_ID),
            ("INSTAGRAM_ACCOUNT_ID", INSTAGRAM_ACCOUNT_ID),
        ) if not val
    ]
    if missing:
        log.error("Missing required env vars: %s — set them in your config "
                  "file (e.g. ~/.config/autopost.env, see README Step 3) and "
                  "make sure it is loaded.", ", ".join(missing))
        sys.exit(1)


def _load_recycle_state() -> list:
    try:
        return json.loads(RECYCLE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_recycle_state(done: list):
    try:
        RECYCLE_STATE.write_text(json.dumps(done), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save recycle state: %s", e)


def mark_recycled(video_name: str):
    """Record that this clip has been replayed in the current lap."""
    done = _load_recycle_state()
    if video_name not in done:
        done.append(video_name)
    _save_recycle_state(done)


def pick_random_video() -> tuple[Path, bool] | None:
    """Return (video, recycled) — a random fresh video from the queue, or if
    the queue is empty and recycling is on, the next clip in a fair replay
    cycle: every posted clip goes out once (random order) before any repeat."""
    if not QUEUE_DIR.exists():
        log.error("Queue dir %s does not exist.", QUEUE_DIR)
        return None

    candidates = [
        p for p in QUEUE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if candidates:
        return random.choice(candidates), False

    if RECYCLE and POSTED_DIR.exists():
        all_posted = [
            p for p in POSTED_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if all_posted:
            done = _load_recycle_state()
            remaining = [p for p in all_posted if p.name not in done]
            if not remaining:
                log.info("Recycle lap complete (%d clips) — starting a new lap.",
                         len(all_posted))
                _save_recycle_state([])
                remaining = all_posted
            log.info("Queue empty — recycling (%d of %d left this lap).",
                     len(remaining), len(all_posted))
            return random.choice(remaining), True

    log.info("No videos found in queue.")
    return None


def load_caption(video_path: Path) -> str:
    """Use a same-named .txt caption if present; otherwise pull a random one
    from the captions pool. Falls back to empty string if neither exists."""
    caption_path = video_path.with_suffix(".txt")
    if caption_path.exists():
        return caption_path.read_text(encoding="utf-8").strip()

    if CAPTIONS_DIR.exists():
        pool = [p for p in CAPTIONS_DIR.iterdir()
                if p.is_file() and p.suffix.lower() == ".txt"]
        if pool:
            chosen = random.choice(pool)
            log.info("No per-clip caption; using random caption %s", chosen.name)
            return chosen.read_text(encoding="utf-8").strip()

    log.warning("No caption for %s; posting with empty caption.", video_path.name)
    return ""


def get_presigned_url(file_name: str, file_type: str) -> tuple[str, str]:
    """Ask Zernio for a presigned upload URL + the resulting public URL."""
    resp = requests.post(
        f"{ZERNIO_BASE}/media/presign",
        headers={
            "Authorization": f"Bearer {ZERNIO_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"filename": file_name, "contentType": file_type},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["uploadUrl"], data["publicUrl"]


def upload_file(upload_url: str, video_path: Path, file_type: str):
    """PUT the raw file bytes to the presigned URL (no auth header needed)."""
    with open(video_path, "rb") as f:
        resp = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": file_type},
            timeout=300,
        )
    resp.raise_for_status()


def create_post(public_url: str, caption: str):
    """Publish the uploaded video to both platforms immediately."""
    payload = {
        "content": caption,
        "mediaItems": [
            {"type": "video", "url": public_url}
        ],
        "platforms": [
            {"platform": "tiktok", "accountId": TIKTOK_ACCOUNT_ID},
            {"platform": "instagram", "accountId": INSTAGRAM_ACCOUNT_ID},
        ],
        "publishNow": True,
    }
    resp = requests.post(
        f"{ZERNIO_BASE}/posts",
        headers={
            "Authorization": f"Bearer {ZERNIO_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def move_to(video_path: Path, caption_path: Path, dest_dir: Path):
    """Move the video (and its caption if per-clip) into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    video_path.rename(dest_dir / video_path.name)
    if caption_path.exists():
        caption_path.rename(dest_dir / caption_path.name)


def main():
    require_config()

    picked = pick_random_video()
    if picked is None:
        log.info("Nothing to post.")
        return
    video_path, recycled = picked

    caption_path = video_path.with_suffix(".txt")
    caption = load_caption(video_path)
    file_type = "video/quicktime" if video_path.suffix.lower() == ".mov" else "video/mp4"

    log.info("Selected video: %s%s", video_path.name, " (recycled)" if recycled else "")

    try:
        log.info("Requesting presigned upload URL...")
        upload_url, public_url = get_presigned_url(video_path.name, file_type)

        log.info("Uploading file to Zernio storage...")
        upload_file(upload_url, video_path, file_type)

        log.info("Creating post on TikTok + Instagram...")
        result = create_post(public_url, caption)
        log.info("Post created: %s", json.dumps(result)[:500])

        if recycled:
            mark_recycled(video_path.name)
            log.info("Recycled clip stays in posted/; marked done for this lap.")
        else:
            move_to(video_path, caption_path, POSTED_DIR)
            log.info("Moved %s to posted/", video_path.name)

    except requests.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        log.error("HTTP error posting %s: %s | %s", video_path.name, e, body)
        if not recycled:
            move_to(video_path, caption_path, FAILED_DIR)
    except Exception as e:
        log.exception("Unexpected error posting %s: %s", video_path.name, e)
        if not recycled:
            move_to(video_path, caption_path, FAILED_DIR)


if __name__ == "__main__":
    main()
