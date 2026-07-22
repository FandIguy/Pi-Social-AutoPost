#!/usr/bin/env python3
"""
Pi Social AutoPost - Upload GUI + Dashboard

Dark, TikTok/Instagram-styled local web app:
  - Performance dashboard (followers, views, likes, comments, shares) via Zernio
  - Queue monitor with runway estimate
  - Queue browser with inline video previews + caption editing
  - Drag-and-drop uploads with per-file progress

LAN use only. No login. Do not expose to the internet.

Run:    python3 uploader.py
Open:   http://<pi-ip>:5000
"""

from __future__ import annotations  # keeps type hints working on Python 3.9 (Pi OS Bullseye)

import os
import re
import time
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string, send_file, abort

import requests as rq


def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


QUEUE_DIR = Path(_env("AUTOPOST_QUEUE_DIR", "KRECXX_QUEUE_DIR",
                      default="/mnt/ssd/social-queue"))
PORT = int(os.environ.get("UPLOADER_PORT", "5000"))
MAX_MB = int(os.environ.get("UPLOADER_MAX_MB", "500"))
POSTS_PER_DAY = int(os.environ.get("AUTOPOST_POSTS_PER_DAY", "2"))

ZERNIO_BASE = os.environ.get("ZERNIO_BASE", "https://zernio.com/api/v1")
ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY")
ANALYTICS_POSTS = int(os.environ.get("UPLOADER_ANALYTICS_POSTS", "6"))
ANALYTICS_CACHE_SECS = int(os.environ.get("UPLOADER_ANALYTICS_CACHE", "600"))

VIDEO_EXTENSIONS = {".mp4", ".mov"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


# ---------------------------------------------------------------- helpers

def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9 \-_().]", "_", name)
    return name.lstrip(".") or "upload"


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def find_in_queue(name: str) -> Path | None:
    """Resolve a client-supplied name against actual queue contents only."""
    if QUEUE_DIR.exists():
        for p in QUEUE_DIR.iterdir():
            if p.is_file() and p.name == name and p.suffix.lower() in VIDEO_EXTENSIONS:
                return p
    return None


def _zget(path, params=None):
    r = rq.get(
        f"{ZERNIO_BASE}{path}",
        headers={"Authorization": f"Bearer {ZERNIO_API_KEY}"},
        params=params or {},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _num(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return v
    return 0


_cache = {}


def cached(key, secs, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < secs:
        return hit[1]
    data = fn()
    _cache[key] = (now, data)
    return data


# ---------------------------------------------------------------- routes

@app.route("/status")
def status():
    def count(d: Path) -> int:
        if not d.exists():
            return 0
        return sum(1 for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)

    return jsonify(
        queued=count(QUEUE_DIR),
        posted=count(QUEUE_DIR / "posted"),
        failed=count(QUEUE_DIR / "failed"),
    )


@app.route("/accounts")
def accounts():
    """Connected account info (platform, handle, followers) via Zernio."""
    if not ZERNIO_API_KEY:
        return jsonify(ok=False, error="no api key configured")

    def fetch():
        data = _zget("/accounts")
        out = []
        for a in data.get("accounts", []):
            profile = ((a.get("metadata") or {}).get("profileData") or {})
            extra = profile.get("extraData") or {}
            out.append({
                "platform": a.get("platform", "?"),
                "username": a.get("username", ""),
                "displayName": a.get("displayName", ""),
                "followers": a.get("followersCount", 0) or profile.get("followersCount", 0),
                "posts": a.get("externalPostCount", 0) or extra.get("videoCount", 0) or extra.get("mediaCount", 0),
                "totalLikes": extra.get("likesCount", 0),
            })
        return {"ok": True, "accounts": out}

    try:
        return jsonify(cached("accounts", ANALYTICS_CACHE_SECS, fetch))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200])


VIEW_KEYS = ("views", "plays", "videoViews", "video_views", "impressions", "reach")
METRIC_KEYS = {
    "views": VIEW_KEYS,
    "likes": ("likes", "likeCount", "like_count", "diggCount"),
    "comments": ("comments", "commentCount", "comment_count"),
    "shares": ("shares", "shareCount", "share_count", "reposts"),
}


def _find_post_list(obj, depth=0):
    """Recursively locate the list of post objects in any response shape."""
    if depth > 4:
        return None
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and (
            "_id" in obj[0] or "id" in obj[0] or "postId" in obj[0]
        ):
            return obj
        return None
    if isinstance(obj, dict):
        # try likely keys first, then everything
        for k in ("posts", "data", "items", "results"):
            if k in obj:
                found = _find_post_list(obj[k], depth + 1)
                if found is not None:
                    return found
        for v in obj.values():
            found = _find_post_list(v, depth + 1)
            if found is not None:
                return found
    return None


def _metrics_from_dict(d):
    """Pull known metrics out of one flat-ish dict. Returns dict or None."""
    if not isinstance(d, dict):
        return None
    out = {}
    for metric, aliases in METRIC_KEYS.items():
        for a in aliases:
            v = d.get(a)
            if isinstance(v, (int, float)):
                out[metric] = out.get(metric, 0) or int(v)
                break
    return out if out else None


def _find_metrics(obj, depth=0):
    """Recursively find the first dict containing recognizable metrics."""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        direct = _metrics_from_dict(obj)
        if direct:
            return direct
        for k in ("analytics", "summary", "totals", "metrics", "stats", "data"):
            if k in obj:
                found = _find_metrics(obj[k], depth + 1)
                if found:
                    return found
        for v in obj.values():
            found = _find_metrics(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_metrics(v, depth + 1)
            if found:
                return found
    return None


def _find_platform_breakdown(obj, depth=0):
    """Find a list of per-platform analytics entries anywhere in the response."""
    if depth > 4:
        return []
    if isinstance(obj, dict):
        for k in ("platformAnalytics", "platforms", "byPlatform", "platform_analytics"):
            v = obj.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out = []
                for pa in v:
                    m = _find_metrics(pa) or {}
                    out.append({
                        "platform": pa.get("platform", "?"),
                        "views": m.get("views", 0),
                        "likes": m.get("likes", 0),
                        "_all": m,
                    })
                if out:
                    return out
        for v in obj.values():
            found = _find_platform_breakdown(v, depth + 1)
            if found:
                return found
    return []


def _post_id(p):
    return p.get("_id") or p.get("id") or p.get("postId")


# The documented /posts/{id}/analytics path 404s on the live API, so probe
# likely alternatives and remember whichever answers with metrics.
ANALYTICS_PATH_CANDIDATES = (
    "/posts/{pid}/analytics",
    "/analytics/posts/{pid}",
    "/posts/{pid}/insights",
    "/posts/{pid}/stats",
    "/posts/{pid}/metrics",
    "/analytics/post/{pid}",
)


def _post_analytics(pid):
    """Return (raw_response, metrics, platforms) for a post, trying candidate
    endpoint shapes. Caches the working path template. (None, None, []) if
    no endpoint answers usefully."""
    working = _cache.get("analytics_path")
    candidates = ([working[1]] if working else []) + [
        c for c in ANALYTICS_PATH_CANDIDATES if not working or c != working[1]
    ]
    for tmpl in candidates:
        try:
            resp = _zget(tmpl.format(pid=pid))
        except Exception:
            continue
        metrics = _find_metrics(resp)
        platforms = _find_platform_breakdown(resp)
        if metrics or platforms:
            _cache["analytics_path"] = (time.time(), tmpl)
            return resp, metrics, platforms
    return None, None, []


def _platform_status(p):
    """Extract per-platform publish status from a post object."""
    out = []
    for plat in p.get("platforms") or []:
        if not isinstance(plat, dict):
            continue
        name = plat.get("platform", "?")
        psd = plat.get("platformSpecificData") or {}
        status = (plat.get("status") or plat.get("publishStatus")
                  or psd.get("lastPublishStage") or "")
        s = str(status).lower()
        if "finaliz" in s or "await" in s or "progress" in s or "start" in s:
            label = "publishing"
        elif "fail" in s or "error" in s:
            label = "failed"
        elif s in ("", "none"):
            label = "posted"
        else:
            label = str(status)[:18]
        out.append({"platform": name, "status": label})
    return out


@app.route("/debug/analytics")
def debug_analytics():
    """Raw (truncated) Zernio responses so parsing can be verified against
    the real payload shape. LAN diagnostic only."""
    if not ZERNIO_API_KEY:
        return jsonify(ok=False, error="no api key configured")
    out = {}
    try:
        listing = _zget("/posts", params={"limit": 3})
        out["posts_raw"] = str(listing)[:1500]
        posts = _find_post_list(listing) or []
        out["posts_found"] = len(posts)
        if posts:
            pid = _post_id(posts[0])
            out["first_post_id"] = pid
            probes = {}
            for tmpl in ANALYTICS_PATH_CANDIDATES:
                try:
                    resp = _zget(tmpl.format(pid=pid))
                    m = _find_metrics(resp)
                    probes[tmpl] = "200, metrics=" + str(m)[:120]
                except Exception as e:
                    probes[tmpl] = str(e)[:80]
            out["endpoint_probes"] = probes
            working = _cache.get("analytics_path")
            out["working_path"] = working[1] if working else None
    except Exception as e:
        out["posts_error"] = str(e)[:300]
    return jsonify(out)


@app.route("/analytics")
def analytics():
    if not ZERNIO_API_KEY:
        return jsonify(ok=False, error="no api key configured")

    def fetch():
        listing = _zget("/posts", params={"limit": ANALYTICS_POSTS})
        posts = _find_post_list(listing) or []

        items = []
        totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
        analytics_available = False

        for p in posts[:ANALYTICS_POSTS]:
            pid = _post_id(p)
            if not pid:
                continue
            entry = {
                "id": pid,
                "content": (p.get("content") or "")[:90],
                "publishedAt": p.get("publishedAt") or p.get("createdAt") or "",
                "views": 0, "likes": 0, "comments": 0, "shares": 0,
                "platforms": [],
                "status": _platform_status(p),
            }

            _, metrics, platforms = _post_analytics(pid)
            if metrics or platforms:
                analytics_available = True
                entry["platforms"] = [
                    {"platform": x["platform"], "views": x["views"], "likes": x["likes"]}
                    for x in platforms
                ]
                if metrics:
                    for k in totals:
                        entry[k] = metrics.get(k, 0)
                if platforms and all(entry[k] == 0 for k in totals):
                    for x in platforms:
                        m = x.get("_all", {})
                        for k in totals:
                            entry[k] += m.get(k, 0)
            else:
                # metrics sometimes ride on the post object itself
                m = _find_metrics(p.get("analytics") or {})
                if m:
                    analytics_available = True
                    for k in totals:
                        entry[k] = m.get(k, 0)

            for k in totals:
                totals[k] += entry[k]
            items.append(entry)

        return {"ok": True, "totals": totals, "posts": items,
                "analyticsAvailable": analytics_available}

    try:
        fresh = request.args.get("fresh") == "1"
        if fresh:
            _cache.pop("analytics", None)
        return jsonify(cached("analytics", ANALYTICS_CACHE_SECS, fetch))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200])


@app.route("/queue")
def queue_list():
    items = []
    if QUEUE_DIR.exists():
        vids = sorted(
            (p for p in QUEUE_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )
        for p in vids:
            cap_path = p.with_suffix(".txt")
            caption = None
            if cap_path.exists():
                try:
                    caption = cap_path.read_text(encoding="utf-8").strip()
                except Exception:
                    caption = None
            items.append({
                "name": p.name,
                "sizeMb": round(p.stat().st_size / 1048576, 1),
                "caption": caption,
            })
    return jsonify(ok=True, items=items)


@app.route("/video/<path:name>")
def video(name):
    """Stream a queued video for inline preview. Names resolved against the
    queue listing only — no arbitrary path access."""
    target = find_in_queue(name)
    if target is None:
        abort(404)
    mime = "video/quicktime" if target.suffix.lower() == ".mov" else "video/mp4"
    # conditional=True enables Range requests so scrubbing works.
    return send_file(target, mimetype=mime, conditional=True)


@app.route("/caption", methods=["POST"])
def caption_save():
    data = request.get_json(silent=True) or {}
    name = data.get("name") or ""
    caption = (data.get("caption") or "").strip()

    target = find_in_queue(name)
    if target is None:
        return jsonify(ok=False, error="video not found in queue"), 404

    cap_path = target.with_suffix(".txt")
    try:
        if caption:
            cap_path.write_text(caption + "\n", encoding="utf-8")
            return jsonify(ok=True, caption=caption)
        if cap_path.exists():
            cap_path.unlink()
        return jsonify(ok=True, caption=None)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


@app.route("/upload", methods=["POST"])
def upload():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    f = request.files.get("video")
    if f is None or f.filename == "":
        return jsonify(ok=False, error="no file"), 400

    filename = safe_name(f.filename)
    if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
        return jsonify(ok=False, error="only .mp4 or .mov allowed"), 400

    dest = unique_path(QUEUE_DIR, filename)
    f.save(dest)

    caption = (request.form.get("caption") or "").strip()
    if caption:
        dest.with_suffix(".txt").write_text(caption + "\n", encoding="utf-8")

    return jsonify(ok=True, saved=dest.name)


@app.errorhandler(413)
def too_large(_e):
    # Fires when an upload exceeds MAX_CONTENT_LENGTH. Return JSON so the
    # dashboard shows a clear message instead of a generic "Bad response".
    return jsonify(ok=False, error=f"File too large (max {MAX_MB} MB)"), 413


@app.route("/")
def index():
    return render_template_string(PAGE, queue_dir=str(QUEUE_DIR),
                                  max_mb=MAX_MB, posts_per_day=POSTS_PER_DAY)


# ---------------------------------------------------------------- page

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoPost Studio</title>
<style>
  :root {
    --bg: #0b0b0f;
    --bg2: #101018;
    --card: #15151d;
    --card2: #1a1a24;
    --border: #262633;
    --text: #ececf1;
    --muted: #8a8a99;
    --cyan: #25f4ee;
    --pink: #fe2c55;
    --violet: #8b5cf6;
    --ok: #2dd4a7;
    --danger: #ff5c7a;
    --grad: linear-gradient(90deg, var(--cyan), var(--violet), var(--pink));
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background:
      radial-gradient(900px 500px at 85% -10%, rgba(139,92,246,.14), transparent 60%),
      radial-gradient(700px 420px at -10% 8%, rgba(37,244,238,.10), transparent 60%),
      radial-gradient(800px 500px at 60% 110%, rgba(254,44,85,.08), transparent 60%),
      var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
    padding: 44px 16px 80px;
    line-height: 1.5;
    perspective: 1200px;
  }
  .wrap { width: 100%; max-width: 680px; }

  /* ---------- scroll-in 3D animation ---------- */
  .rise {
    opacity: 0;
    transform: translateY(34px) rotateX(7deg) scale(.985);
    transform-origin: 50% 100%;
    transition: opacity .6s cubic-bezier(.2,.7,.2,1), transform .6s cubic-bezier(.2,.7,.2,1);
    will-change: opacity, transform;
  }
  .rise.in { opacity: 1; transform: none; }
  @media (prefers-reduced-motion: reduce) {
    .rise { opacity: 1; transform: none; transition: none; }
  }

  header { margin-bottom: 26px; }
  .brand { display:flex; align-items:center; gap:12px; }
  .logo {
    width: 40px; height: 40px; border-radius: 12px;
    background: var(--grad);
    display:flex; align-items:center; justify-content:center;
    font-weight: 800; color:#0b0b0f; font-size: 19px;
    box-shadow: 0 6px 24px rgba(139,92,246,.35);
  }
  h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }

  .section-title {
    font-size: 12px; font-weight: 700; letter-spacing: .12em;
    text-transform: uppercase; color: var(--muted);
    margin: 30px 0 12px;
  }
  .section-title:first-of-type { margin-top: 0; }

  .card {
    background: linear-gradient(180deg, var(--card2), var(--card));
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
    box-shadow: 0 10px 34px rgba(0,0,0,.35);
  }

  /* ---------- account chips ---------- */
  .chips { display:flex; gap:10px; flex-wrap:wrap; margin-bottom: 14px; }
  .chip {
    display:flex; align-items:center; gap:10px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 8px 14px 8px 8px;
    font-size: 13px;
  }
  .chip .dot { width: 26px; height: 26px; border-radius: 50%;
    display:flex; align-items:center; justify-content:center;
    font-size: 12px; font-weight: 800; color:#0b0b0f; }
  .chip.tiktok .dot { background: linear-gradient(135deg, var(--cyan), #7ef9f5); }
  .chip.instagram .dot { background: linear-gradient(135deg, #f9ce34, #ee2a7b 55%, #6228d7); color:#fff; }
  .chip b { font-weight: 700; }
  .chip span { color: var(--muted); }

  /* ---------- stat grid ---------- */
  .grid { display:grid; grid-template-columns: repeat(4,1fr); gap:10px; }
  .grid3 { display:grid; grid-template-columns: repeat(3,1fr); gap:10px; }
  .stat {
    background: linear-gradient(180deg, var(--card2), var(--card));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 12px;
    text-align: center;
    transition: transform .25s cubic-bezier(.2,.7,.2,1), border-color .25s;
    transform-style: preserve-3d;
  }
  .stat:hover { transform: translateY(-3px) rotateX(4deg); border-color:#34344a; }
  .stat .num {
    font-size: 26px; font-weight: 800; letter-spacing: -0.02em;
    background: var(--grad);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
  }
  .stat .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase;
    letter-spacing: .08em; margin-top: 3px; }
  .stat.plain .num { background: none; -webkit-text-fill-color: currentColor; color: var(--text); }
  .stat.q .num { color: var(--cyan); }
  .stat.p .num { color: var(--ok); }
  .stat.f .num { color: var(--muted); }
  .stat.f.hot .num { color: var(--danger); }
  .stat.clickable { cursor: pointer; }

  .note { font-size: 12px; color: var(--muted); text-align:center; margin: 10px 0 0; }
  .note.low { color: #f5b83d; font-weight: 600; }
  .note.empty { color: var(--danger); font-weight: 600; }

  /* ---------- lists ---------- */
  .rows { list-style:none; padding:0; margin: 14px 0 0; }
  .rows li {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    margin-bottom: 10px;
    font-size: 13px;
    transition: transform .25s cubic-bezier(.2,.7,.2,1), border-color .25s;
  }
  .rows li:hover { border-color:#34344a; }
  .row-line { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .fn { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }
  .meta { font-size:11px; color:var(--muted); margin-top:3px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pill {
    flex:none; font-size:11px; font-weight:700; padding: 4px 10px;
    border-radius: 999px; color: var(--muted); background: #20202c;
    border: 1px solid var(--border);
  }
  .pill.grad { background: var(--grad); color: #0b0b0f; border: none; }
  .pill.ok { color: var(--ok); background: rgba(45,212,167,.1); border-color: rgba(45,212,167,.25); }
  .pill.err { color: var(--danger); background: rgba(255,92,122,.1); border-color: rgba(255,92,122,.25); }

  /* ---------- queue browser ---------- */
  .qitem { cursor: pointer; }
  .qeditor { display:none; margin-top: 12px; }
  .qeditor video {
    width: 100%; max-height: 320px; border-radius: 10px;
    background: #000; border: 1px solid var(--border);
    display: block; margin-bottom: 10px;
  }
  textarea {
    width: 100%; background: var(--bg2); border: 1px solid var(--border);
    color: var(--text); font-family: inherit; font-size: 14px;
    border-radius: 10px; padding: 11px 12px; min-height: 92px; resize: vertical;
  }
  textarea:focus { outline:none; border-color: var(--violet);
    box-shadow: 0 0 0 3px rgba(139,92,246,.18); }
  .btnrow { display:flex; gap:8px; margin-top:10px; }
  button.btn {
    flex:1; border:none; padding: 11px; font-family:inherit; font-weight:700;
    font-size: 13px; border-radius: 10px; cursor:pointer;
    transition: filter .15s, transform .15s;
  }
  button.btn:hover { filter: brightness(1.12); transform: translateY(-1px); }
  button.btn:disabled { filter: grayscale(.6) brightness(.7); cursor: default; transform:none; }
  .btn.primary { background: var(--grad); color:#0b0b0f; }
  .btn.ghost { background:#20202c; color: var(--text); border:1px solid var(--border); }
  .capmsg { font-size:11px; color:var(--muted); margin-top:6px; }

  /* ---------- upload ---------- */
  .drop {
    border: 1.5px dashed #3a3a4d;
    background: var(--bg2);
    border-radius: 14px;
    padding: 40px 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s, transform .2s;
  }
  .drop:hover { border-color: var(--violet); }
  .drop.drag {
    border-color: var(--cyan); border-style: solid;
    background: linear-gradient(180deg, rgba(37,244,238,.06), rgba(139,92,246,.06));
    transform: scale(1.01);
  }
  .drop .big { font-size: 15px; font-weight: 700; }
  .drop .small { color: var(--muted); font-size: 12px; margin-top: 6px; }
  input[type=file] { display:none; }
  label.field { display:block; margin: 18px 0 6px; font-size: 13px; font-weight: 600; }
  .hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .bar { height: 4px; background:#20202c; border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > span { display:block; height:100%; width:0; background: var(--grad); transition: width .2s; }

  footer { margin-top: 44px; color: var(--muted); font-size: 12px; text-align:center; }
  footer code { color: var(--cyan); background: rgba(37,244,238,.07);
    padding: 1px 6px; border-radius: 5px; font-size: 11px; }
</style>
</head>
<body>
  <div class="wrap">
    <header class="rise in">
      <div class="brand">
        <div class="logo">A</div>
        <div>
          <h1>AutoPost Studio</h1>
          <p class="sub">TikTok + Instagram queue on your Pi</p>
        </div>
      </div>
    </header>

    <div class="section-title rise">Performance</div>
    <div class="chips rise" id="chips"></div>
    <div class="grid rise">
      <div class="stat"><div class="num" id="aViews">&middot;</div><div class="lbl" id="lblViews">Views</div></div>
      <div class="stat"><div class="num" id="aLikes">&middot;</div><div class="lbl" id="lblLikes">Likes</div></div>
      <div class="stat"><div class="num" id="aComments">&middot;</div><div class="lbl" id="lblComments">Comments</div></div>
      <div class="stat"><div class="num" id="aShares">&middot;</div><div class="lbl" id="lblShares">Shares</div></div>
    </div>
    <p class="note rise" id="perfNote">Loading performance&hellip;</p>
    <ul class="rows rise" id="recentPosts"></ul>

    <div class="section-title rise">Queue</div>
    <div class="grid3 rise">
      <div class="stat plain q clickable" id="statQueued"><div class="num" id="mQueued">&middot;</div><div class="lbl">In queue</div></div>
      <div class="stat plain p"><div class="num" id="mPosted">&middot;</div><div class="lbl">Posted</div></div>
      <div class="stat plain f" id="mFailedCard"><div class="num" id="mFailed">&middot;</div><div class="lbl">Failed</div></div>
    </div>
    <p class="note rise" id="runway">Checking queue&hellip;</p>

    <div class="card rise" id="queueCard" style="margin-top:14px;">
      <div class="row-line">
        <div style="font-size:14px;font-weight:700;">Waiting to post</div>
        <button class="btn ghost" style="flex:none;padding:7px 14px;" id="queueRefresh">Refresh</button>
      </div>
      <p class="hint" style="margin:4px 0 4px;">Tap a video to preview it and edit its caption before it goes out.</p>
      <ul class="rows" id="queueItems" style="margin-top:10px;"></ul>
    </div>

    <div class="section-title rise">Add videos</div>
    <div class="card rise">
      <div class="drop" id="drop">
        <div class="big" id="dropTitle">Drop videos or tap to browse</div>
        <div class="small">.mp4 / .mov &middot; up to {{max_mb}} MB each</div>
        <input type="file" id="fileInput" accept=".mp4,.mov,video/mp4,video/quicktime" multiple>
      </div>
      <label class="field" for="caption">Caption <span style="font-weight:400;color:var(--muted)">(optional, applies to this batch)</span></label>
      <textarea id="caption" placeholder="Leave blank to use the random caption pool&hellip;"></textarea>
      <p class="hint">You can also set per-video captions in the queue above after uploading.</p>
      <div class="btnrow"><button class="btn primary" id="go" disabled>Upload to queue</button></div>
      <ul class="rows" id="fileList"></ul>
    </div>

    <footer class="rise">
      Queue: <code>{{queue_dir}}</code> &middot; LAN only &middot; no login
    </footer>
  </div>

<script>
  // ---------- scroll-in animation ----------
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting){ e.target.classList.add('in'); io.unobserve(e.target); } });
  }, { threshold: 0.08 });
  document.querySelectorAll('.rise').forEach(el => io.observe(el));

  function fmt(n){
    if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n/1000).toFixed(1) + 'K';
    return String(n);
  }

  // ---------- accounts ----------
  let accountData = null;
  async function refreshAccounts(){
    try {
      const r = await fetch('/accounts');
      const a = await r.json();
      if (!a.ok) return;
      accountData = a.accounts;
      const chips = document.getElementById('chips');
      chips.innerHTML = '';
      a.accounts.forEach(acc => {
        const c = document.createElement('div');
        c.className = 'chip ' + acc.platform;
        const initial = acc.platform === 'tiktok' ? 'T' : acc.platform === 'instagram' ? 'I' : '?';
        const dot = document.createElement('div'); dot.className = 'dot'; dot.textContent = initial;
        const label = document.createElement('div');
        const handle = document.createElement('b'); handle.textContent = '@' + acc.username;
        const rest = document.createElement('span');
        rest.textContent = ' · ' + fmt(acc.followers) + ' followers · ' + fmt(acc.posts) + ' posts';
        label.appendChild(handle); label.appendChild(rest);
        c.appendChild(dot); c.appendChild(label);
        chips.appendChild(c);
      });
    } catch(_){}
  }

  function showAccountStats(){
    // Per-post analytics isn't exposed by the API — show real account totals.
    if (!accountData) return false;
    let followers = 0, likes = 0, posts = 0;
    accountData.forEach(a => { followers += a.followers||0; likes += a.totalLikes||0; posts += a.posts||0; });
    document.getElementById('lblViews').textContent = 'Followers';
    document.getElementById('aViews').textContent = fmt(followers);
    document.getElementById('lblLikes').textContent = 'All-time likes';
    document.getElementById('aLikes').textContent = likes ? fmt(likes) : '\u2014';
    document.getElementById('lblComments').textContent = 'Published posts';
    document.getElementById('aComments').textContent = fmt(posts);
    document.getElementById('lblShares').textContent = 'Accounts';
    document.getElementById('aShares').textContent = String(accountData.length);
    return true;
  }

  // ---------- analytics ----------
  async function refreshAnalytics(){
    const note = document.getElementById('perfNote');
    try {
      const r = await fetch('/analytics');
      const a = await r.json();
      if (!a.ok){
        note.textContent = a.error === 'no api key configured'
          ? 'Performance data unavailable — no API key configured for the uploader.'
          : 'Performance data unavailable right now.';
        showAccountStats();
        return;
      }

      if (a.analyticsAvailable){
        document.getElementById('aViews').textContent = fmt(a.totals.views);
        document.getElementById('aLikes').textContent = fmt(a.totals.likes);
        document.getElementById('aComments').textContent = fmt(a.totals.comments);
        document.getElementById('aShares').textContent = fmt(a.totals.shares);
        note.textContent = 'Across your ' + a.posts.length + ' most recent posts \u00b7 refreshes every 10 min';
      } else {
        if (!showAccountStats()){
          // accounts not loaded yet — retry shortly
          setTimeout(showAccountStats, 1500);
        }
        note.textContent = 'Account totals shown \u00b7 per-post analytics is not exposed by the posting API';
      }

      const list = document.getElementById('recentPosts');
      list.innerHTML = '';
      a.posts.forEach(p => {
        const li = document.createElement('li');
        const line = document.createElement('div'); line.className = 'row-line';
        const left = document.createElement('div'); left.style.minWidth=0; left.style.flex='1';
        const fn = document.createElement('div'); fn.className='fn';
        fn.textContent = p.content || '(no caption)';
        const meta = document.createElement('div'); meta.className='meta';
        if (a.analyticsAvailable){
          meta.textContent = (p.publishedAt||'').slice(0,10) +
            (p.platforms.length ? ' \u00b7 ' + p.platforms.map(x => x.platform + ' ' + fmt(x.views)).join(' \u00b7 ') : '') +
            ' \u00b7 \u2764 ' + fmt(p.likes) + ' \u00b7 \ud83d\udcac ' + fmt(p.comments);
        } else {
          meta.textContent = (p.publishedAt||'').slice(0,10) +
            (p.status && p.status.length ? ' \u00b7 ' + p.status.map(s => s.platform).join(' + ') : '');
        }
        left.appendChild(fn); left.appendChild(meta);
        const pill = document.createElement('div');
        if (a.analyticsAvailable){
          pill.className='pill grad';
          pill.textContent = fmt(p.views) + ' views';
        } else {
          const st = (p.status && p.status[0]) ? p.status[0].status : 'posted';
          const bad = p.status && p.status.some(s => s.status === 'failed');
          const pub = p.status && p.status.some(s => s.status === 'publishing');
          pill.className = 'pill' + (bad ? ' err' : pub ? '' : ' ok');
          pill.textContent = bad ? 'failed' : pub ? 'publishing' : 'posted';
        }
        line.appendChild(left); line.appendChild(pill);
        li.appendChild(line);
        list.appendChild(li);
      });
    } catch(_){ note.textContent = 'Performance data unavailable right now.'; showAccountStats(); }
  }

  // ---------- queue status ----------
  const POSTS_PER_DAY = {{posts_per_day}};
  async function refreshStatus(){
    try {
      const r = await fetch('/status');
      const s = await r.json();
      document.getElementById('mQueued').textContent = s.queued;
      document.getElementById('mPosted').textContent = s.posted;
      document.getElementById('mFailed').textContent = s.failed;
      document.getElementById('mFailedCard').classList.toggle('hot', s.failed > 0);

      const runway = document.getElementById('runway');
      if (s.queued === 0){
        runway.textContent = 'Queue is empty \u2014 recycling previously posted clips until you add more.';
        runway.className = 'note empty rise in';
      } else {
        const days = s.queued / POSTS_PER_DAY;
        const daysTxt = days >= 1 ? Math.floor(days) + (Math.floor(days)===1?' day':' days') : 'less than a day';
        runway.textContent = s.queued + (s.queued===1?' video':' videos') + ' waiting \u00b7 about ' +
          daysTxt + ' at ' + POSTS_PER_DAY + '/day';
        runway.className = 'note rise in' + (s.queued <= 3 ? ' low' : '');
      }
    } catch(_){ document.getElementById('runway').textContent = 'Could not read queue status.'; }
  }

  // ---------- queue browser ----------
  const queueItems = document.getElementById('queueItems');
  let openEditor = null;

  document.getElementById('queueRefresh').addEventListener('click', loadQueue);
  document.getElementById('statQueued').addEventListener('click', () => {
    document.getElementById('queueCard').scrollIntoView({behavior:'smooth', block:'start'});
  });

  async function loadQueue(){
    try {
      const r = await fetch('/queue');
      const q = await r.json();
      queueItems.innerHTML = '';
      openEditor = null;
      if (!q.ok || q.items.length === 0){
        queueItems.innerHTML = '<li><div class="fn" style="color:var(--muted)">Queue is empty \u2014 upload below.</div></li>';
        return;
      }
      q.items.forEach(item => queueItems.appendChild(buildQueueRow(item)));
    } catch(_){
      queueItems.innerHTML = '<li><div class="fn" style="color:var(--danger)">Could not load queue.</div></li>';
    }
  }

  function buildQueueRow(item){
    const li = document.createElement('li');
    li.className = 'qitem';

    const line = document.createElement('div'); line.className='row-line';
    const left = document.createElement('div'); left.style.minWidth=0; left.style.flex='1';
    const fn = document.createElement('div'); fn.className='fn';
    fn.textContent = item.name + '  (' + item.sizeMb + ' MB)';
    const meta = document.createElement('div'); meta.className='meta';
    meta.textContent = item.caption ? item.caption.split('\n')[0] : 'Random pool caption';
    left.appendChild(fn); left.appendChild(meta);
    const badge = document.createElement('div');
    badge.className = 'pill' + (item.caption ? ' grad' : '');
    badge.textContent = item.caption ? 'Custom' : 'Pool';
    line.appendChild(left); line.appendChild(badge);
    li.appendChild(line);

    const editor = document.createElement('div'); editor.className='qeditor';
    const vid = document.createElement('video');
    vid.controls = true; vid.muted = true; vid.playsInline = true; vid.preload = 'none';
    editor.appendChild(vid);
    const ta = document.createElement('textarea');
    ta.value = item.caption || '';
    ta.placeholder = 'Type the caption for this video\u2026';
    editor.appendChild(ta);
    const btnrow = document.createElement('div'); btnrow.className='btnrow';
    const saveBtn = document.createElement('button'); saveBtn.className='btn primary'; saveBtn.textContent='Save caption';
    const poolBtn = document.createElement('button'); poolBtn.className='btn ghost'; poolBtn.textContent='Use random pool';
    btnrow.appendChild(saveBtn); btnrow.appendChild(poolBtn);
    editor.appendChild(btnrow);
    const msg = document.createElement('div'); msg.className='capmsg';
    editor.appendChild(msg);
    li.appendChild(editor);

    line.addEventListener('click', () => {
      const opening = editor.style.display !== 'block';
      if (openEditor && openEditor !== editor){
        openEditor.style.display = 'none';
        const oldVid = openEditor.querySelector('video');
        if (oldVid){ oldVid.pause(); }
      }
      editor.style.display = opening ? 'block' : 'none';
      openEditor = opening ? editor : null;
      if (opening && !vid.src){
        vid.src = '/video/' + encodeURIComponent(item.name);
        vid.preload = 'metadata';
      }
      if (!opening){ vid.pause(); }
    });
    editor.addEventListener('click', e => e.stopPropagation());

    async function submit(captionValue){
      saveBtn.disabled = poolBtn.disabled = true;
      msg.textContent = 'Saving\u2026';
      try {
        const r = await fetch('/caption', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name: item.name, caption: captionValue}),
        });
        const res = await r.json();
        if (res.ok){
          item.caption = res.caption;
          ta.value = res.caption || '';
          meta.textContent = res.caption ? res.caption.split('\n')[0] : 'Random pool caption';
          badge.textContent = res.caption ? 'Custom' : 'Pool';
          badge.className = 'pill' + (res.caption ? ' grad' : '');
          msg.textContent = res.caption ? 'Saved.' : 'Reverted to random pool.';
        } else { msg.textContent = 'Error: ' + (res.error || 'failed'); }
      } catch(_){ msg.textContent = 'Network error.'; }
      saveBtn.disabled = poolBtn.disabled = false;
    }
    saveBtn.addEventListener('click', () => submit(ta.value));
    poolBtn.addEventListener('click', () => { ta.value=''; submit(''); });

    return li;
  }

  // ---------- upload ----------
  const drop = document.getElementById('drop');
  const input = document.getElementById('fileInput');
  const listEl = document.getElementById('fileList');
  const goBtn = document.getElementById('go');
  const captionEl = document.getElementById('caption');
  const dropTitle = document.getElementById('dropTitle');
  let staged = [];

  const OK_EXT = ['.mp4', '.mov'];
  function extOk(name){ name = name.toLowerCase(); return OK_EXT.some(e => name.endsWith(e)); }

  function renderStaged(){
    listEl.innerHTML = '';
    staged.forEach(item => {
      const li = document.createElement('li');
      const line = document.createElement('div'); line.className='row-line';
      const left = document.createElement('div'); left.style.minWidth=0; left.style.flex='1';
      const fn = document.createElement('div'); fn.className='fn';
      fn.textContent = item.file.name + '  (' + (item.file.size/1048576).toFixed(1) + ' MB)';
      left.appendChild(fn);
      if (item.status === 'uploading'){
        const bar = document.createElement('div'); bar.className='bar';
        const span = document.createElement('span'); span.style.width = item.pct + '%';
        bar.appendChild(span); left.appendChild(bar);
      }
      const st = document.createElement('div');
      st.className = 'pill' + (item.status==='done'?' ok':'') + (item.status==='error'?' err':'');
      st.textContent = item.status === 'done' ? 'Queued'
                     : item.status === 'error' ? (item.error || 'Failed')
                     : item.status === 'uploading' ? item.pct + '%'
                     : 'Ready';
      line.appendChild(left); line.appendChild(st);
      li.appendChild(line);
      listEl.appendChild(li);
    });
    goBtn.disabled = staged.filter(q => q.status==='ready').length === 0;
  }

  function addFiles(fileList){
    for (const f of fileList){
      if (!extOk(f.name)){ staged.push({file:f, status:'error', error:'Not .mp4/.mov'}); continue; }
      staged.push({file:f, status:'ready', pct:0});
    }
    renderStaged();
  }

  drop.addEventListener('click', () => input.click());
  input.addEventListener('change', e => { addFiles(e.target.files); input.value=''; });
  ['dragenter','dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); dropTitle.textContent='Release to add'; }));
  ['dragleave','drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); dropTitle.textContent='Drop videos or tap to browse'; }));
  drop.addEventListener('drop', e => { if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files); });

  function uploadOne(item){
    return new Promise(resolve => {
      const fd = new FormData();
      fd.append('video', item.file);
      fd.append('caption', captionEl.value || '');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload');
      xhr.upload.onprogress = e => { if (e.lengthComputable){ item.pct = Math.round(e.loaded/e.total*100); renderStaged(); } };
      xhr.onload = () => {
        try {
          const r = JSON.parse(xhr.responseText);
          if (xhr.status === 200 && r.ok){ item.status='done'; }
          else { item.status='error'; item.error = (r.error||'Failed'); }
        } catch(_){ item.status='error'; item.error='Bad response'; }
        renderStaged(); resolve();
      };
      xhr.onerror = () => { item.status='error'; item.error='Network'; renderStaged(); resolve(); };
      item.status='uploading'; item.pct=0; renderStaged();
      xhr.send(fd);
    });
  }

  goBtn.addEventListener('click', async () => {
    goBtn.disabled = true;
    for (const item of staged){ if (item.status !== 'ready') continue; await uploadOne(item); }
    renderStaged();
    refreshStatus();
    loadQueue();
  });

  // ---------- boot ----------
  refreshStatus();
  refreshAccounts().then(refreshAnalytics);
  loadQueue();
  setInterval(refreshStatus, 15000);
  setInterval(refreshAnalytics, 600000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=PORT)
