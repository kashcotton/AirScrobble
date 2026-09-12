"""
AirScrobble — Bridge Last.fm "Now Playing" to a headless Spotify Web Player.
All service configuration is managed through the web dashboard.
"""

from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    render_template,
    redirect,
    request,
    session,
    url_for,
)
from playwright.sync_api import sync_playwright, BrowserContext, Page

# ── Bootstrap ─────────────────────────────────────────────────────────────

load_dotenv()

DASHBOARD_USER = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASSWORD", "airscrobble")
FLASK_SECRET = os.getenv("FLASK_SECRET_KEY", "change-me-to-something-random")

BROWSER_DATA_DIR = Path("browser_data")
DB_PATH = Path("data/airscrobble.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("airscrobble")

# ── Database ──────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS status (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                state   TEXT NOT NULL DEFAULT 'idle',
                track   TEXT,
                artist  TEXT,
                album   TEXT,
                artwork TEXT,
                error   TEXT,
                updated TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO status (id, state, updated) VALUES (1, 'idle', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()


# ── Config helpers ────────────────────────────────────────────────────────
# DB values take priority. Env vars are a fallback for backward compat.

_ENV_MAP = {
    "lastfm_api_key": "LASTFM_API_KEY",
    "lastfm_username": "LASTFM_USERNAME",
    "spotify_email": "SPOTIFY_EMAIL",
    "spotify_password": "SPOTIFY_PASSWORD",
    "poll_interval": "POLL_INTERVAL_SECONDS",
}

CONFIG_KEYS = list(_ENV_MAP.keys())


def get_config(key: str, default: str = "") -> str:
    with _db() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"]:
            return row["value"]
    env_key = _ENV_MAP.get(key)
    if env_key:
        val = os.getenv(env_key, "")
        if val:
            return val
    return default


def set_config(key: str, value: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_all_config() -> dict:
    return {k: get_config(k, "10" if k == "poll_interval" else "") for k in CONFIG_KEYS}


def is_configured() -> bool:
    c = get_all_config()
    return all(
        c[k] for k in ("lastfm_api_key", "lastfm_username", "spotify_email", "spotify_password")
    )


# ── Status helpers ────────────────────────────────────────────────────────

def db_set_status(
    state: str,
    *,
    track: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    artwork: str | None = None,
    error: str | None = None,
):
    with _db() as conn:
        conn.execute(
            "UPDATE status SET state=?, track=?, artist=?, album=?, artwork=?, error=?, updated=? WHERE id=1",
            (state, track, artist, album, artwork, error, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def db_get_status() -> dict:
    with _db() as conn:
        row = conn.execute("SELECT * FROM status WHERE id = 1").fetchone()
        return dict(row) if row else {"state": "unknown"}


# ── Flask ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# ── Routes: auth ──────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authed"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if hmac.compare_digest(u, DASHBOARD_USER) and hmac.compare_digest(p, DASHBOARD_PASS):
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Invalid credentials."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Routes: pages ─────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/settings")
@login_required
def settings_page():
    config = get_all_config()
    has_password = bool(config.pop("spotify_password", ""))
    return render_template("settings.html", config=config, has_password=has_password)


# ── Routes: API ───────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    s = db_get_status()
    s["configured"] = is_configured()
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
@login_required
def api_save_settings():
    data = request.json or {}
    for key in ("lastfm_api_key", "lastfm_username", "spotify_email", "poll_interval"):
        if key in data:
            set_config(key, str(data[key]).strip())
    # Only overwrite password if a new one was actually provided
    if data.get("spotify_password"):
        set_config("spotify_password", data["spotify_password"])
    signal_restart()
    return jsonify({"ok": True, "configured": is_configured()})


@app.route("/api/test-lastfm", methods=["POST"])
@login_required
def api_test_lastfm():
    data = request.json or {}
    api_key = data.get("api_key", "").strip()
    username = data.get("username", "").strip()
    if not api_key or not username:
        return jsonify({"ok": False, "error": "API key and username required."})
    try:
        r = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            return jsonify({"ok": False, "error": body.get("message", "API error")})
        return jsonify({"ok": True})
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/health")
def health():
    return "ok", 200


@app.route("/debug")
def debug_view():
    return '<img src="/api/debug-image" style="max-width:100%;">'


@app.route("/api/debug-image")
def debug_image():
    import flask
    path = BROWSER_DATA_DIR / "debug.png"
    if not path.exists():
        return "No screenshot yet", 404
    return flask.send_file(str(path), mimetype='image/png')


# ── Last.fm ───────────────────────────────────────────────────────────────

@dataclass
class NowPlayingTrack:
    track: str
    artist: str
    album: str = ""
    artwork: str = ""


def lastfm_now_playing(api_key: str, username: str) -> Optional[NowPlayingTrack]:
    try:
        r = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        tracks = data.get("recenttracks", {}).get("track", [])
        if not tracks:
            return None
        latest = tracks[0] if isinstance(tracks, list) else tracks
        if latest.get("@attr", {}).get("nowplaying") != "true":
            return None
        images = latest.get("image", [])
        artwork = ""
        for img in reversed(images):
            if img.get("#text"):
                artwork = img["#text"]
                break
        return NowPlayingTrack(
            track=latest.get("name", ""),
            artist=latest.get("artist", {}).get("#text", ""),
            album=latest.get("album", {}).get("#text", ""),
            artwork=artwork,
        )
    except Exception as exc:
        log.warning("Last.fm poll failed: %s", exc)
        return None


# ── Spotify controller ────────────────────────────────────────────────────

class SpotifyController:
    URL = "https://open.spotify.com"

    def __init__(self) -> None:
        self._pw = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._current_query: Optional[str] = None
        self._access_token: Optional[str] = None

    def _intercept_request(self, request):
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            self._access_token = auth

    def launch(self) -> None:
        log.info("Launching browser …")
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # Clear left-over lock files from unclean docker shutdowns
        for lock_file in BROWSER_DATA_DIR.glob("Singleton*"):
            try:
                lock_file.unlink()
            except Exception:
                pass

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=[
                "--headless=new",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 720},
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._ctx.new_page()
        self._page.on("request", self._intercept_request)
        self._page.goto(self.URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(3)
        if not self._is_logged_in():
            self._login()
        log.info("Spotify ready.")

    def shutdown(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = self._page = None

    def _is_logged_in(self) -> bool:
        try:
            # If the "Log in" button is visible, we are definitively not logged in.
            if self._page.locator('[data-testid="login-button"]').first.is_visible(timeout=5_000):
                return False
            return True
        except Exception:
            return True

    def _login(self) -> None:
        email = get_config("spotify_email")
        password = get_config("spotify_password")
        if not email or not password:
            raise RuntimeError("Spotify credentials not configured")
        log.info("Logging in to Spotify …")
        page = self._page
        try:
            page.locator(
                'button[data-testid="login-button"], '
                'a[data-testid="login-button"], '
                'a[href*="login"]'
            ).first.click(timeout=10_000)
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            time.sleep(2)
        except Exception:
            page.goto(
                "https://accounts.spotify.com/login",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            time.sleep(2)
        page.locator("input#login-username").fill(email, timeout=10_000)
        page.locator("input#login-password").fill(password, timeout=10_000)
        page.locator("button#login-button").click(timeout=10_000, force=True)
        page.wait_for_url("**/open.spotify.com/**", timeout=30_000)
        time.sleep(3)
        try:
            page.screenshot(path=str(BROWSER_DATA_DIR / "debug.png"))
        except:
            pass
        log.info("Login successful.")

    def _api_search(self, query: str) -> Optional[str]:
        if not self._access_token:
            return None
        try:
            r = requests.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": self._access_token},
                params={"q": query, "type": "track", "limit": 1},
                timeout=5
            )
            if r.status_code == 200:
                items = r.json().get("tracks", {}).get("items", [])
                if items:
                    return items[0].get("uri")
        except Exception as e:
            log.debug("API search error: %s", e)
        return None

    def search_and_play(self, track: str, artist: str) -> bool:
        query = f"{track} {artist}"
        if self._current_query == query:
            return True
        log.info("Searching: %s", query)

        track_uri = None
        if self._access_token:
            # Fuzzy fallback search: Try exact first, then just track name
            track_uri = self._api_search(f"{track} {artist}")
            if not track_uri:
                track_uri = self._api_search(track)

        if track_uri and track_uri.startswith("spotify:track:"):
            track_id = track_uri.split(":")[-1]
            try:
                # Direct track page navigation completely bypasses all UI search issues!
                self._page.goto(f"{self.URL}/track/{track_id}", wait_until="domcontentloaded", timeout=30_000)
                time.sleep(3)
                
                # Accept cookie overlay if present
                try:
                    self._page.locator('#onetrust-accept-btn-handler').click(timeout=1000)
                except:
                    pass

                # Grab the main track play button
                btn = self._page.locator('button[data-testid="play-button"], button[data-testid="action-bar-play-button"]').first
                btn.scroll_into_view_if_needed(timeout=5_000)
                btn.evaluate("node => node.click()")
                
                try:
                    self._page.screenshot(path=str(BROWSER_DATA_DIR / "debug.png"))
                except:
                    pass
                
                self._current_query = query
                log.info("▶ Playing exact match via API lookup: %s — %s", track, artist)
                return True
            except Exception as exc:
                log.warning("Failed track page play: %s", exc)

        # -- Local File Stream Fallback Injection --
        if not track_uri and self._access_token:
            log.info("Song not found on Spotify. Faking a local file stream via API injection...")
            safe_artist = urllib.parse.quote(artist.replace(":", ""))
            safe_track = urllib.parse.quote(track.replace(":", ""))
            # Construct standard Spotify URI for local file stream representation
            local_uri = f"spotify:local:{safe_artist}:unknown:{safe_track}:180000"
            
            try:
                # Find the active Web Player session device
                dev_req = requests.get("https://api.spotify.com/v1/me/player/devices", headers={"Authorization": self._access_token}, timeout=5)
                devices = dev_req.json().get("devices", [])
                active_id = None
                for d in devices:
                    if d.get("is_active"):
                        active_id = d.get("id")
                        break
                if not active_id and devices:
                    active_id = devices[0].get("id")

                if active_id:
                    # Push the custom local track directly to the Spotify connect endpoint
                    requests.put(
                        "https://api.spotify.com/v1/me/player/play",
                        headers={"Authorization": self._access_token},
                        params={"device_id": active_id},
                        json={"uris": [local_uri]},
                        timeout=5
                    )
                    self._current_query = query
                    log.info("▶ Pushed local file stream: %s", local_uri)
                    return True
            except Exception as exc:
                log.warning("Local file API injection failed: %s", exc)

        # -- Raw UI Search Fallback --
        log.info("Fallback to raw UI search...")
        try:
            self._page.goto(
                f"{self.URL}/search/{urllib.parse.quote(query)}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            time.sleep(3)
            btn = self._page.locator(
                'button[data-testid="play-button"], '
                'div[data-testid="top-result-card"] button[aria-label*="Play"], '
                'div[data-testid="tracklist-row"] button, '
                'button[aria-label*="Play"]'
            ).first
            btn.scroll_into_view_if_needed(timeout=8_000)
            btn.evaluate("node => node.click()")
            
            try:
                self._page.screenshot(path=str(BROWSER_DATA_DIR / "debug.png"))
            except:
                pass

            self._current_query = query
            log.info("Playing (UI fallback): %s — %s", track, artist)
            return True
        except Exception as exc:
            log.warning("Playback failed: %s", exc)
            return False

    def pause(self) -> None:
        if not self._current_query:
            return
        try:
            btn = self._page.locator(
                'button[data-testid="control-button-pause"], button[aria-label="Pause"]'
            ).first
            if btn.is_visible(timeout=3_000):
                btn.click(timeout=5_000, force=True)
                log.info("Paused.")
        except Exception:
            pass
        self._current_query = None

    def alive(self) -> bool:
        try:
            self._page.title()
            return True
        except Exception:
            return False


# ── Relay loop ────────────────────────────────────────────────────────────

_restart_event = threading.Event()


def signal_restart():
    """Tell the relay loop to restart with fresh config."""
    _restart_event.set()


def relay_loop() -> None:
    backoff = 5

    while True:
        _restart_event.clear()

        # ── wait for config ──
        while not is_configured():
            db_set_status("not_configured")
            if _restart_event.wait(timeout=3):
                break
        if _restart_event.is_set():
            continue

        # ── snapshot config ──
        cfg = get_all_config()
        api_key = cfg["lastfm_api_key"]
        username = cfg["lastfm_username"]
        interval = max(5, int(cfg.get("poll_interval") or 10))

        spotify = SpotifyController()
        try:
            spotify.launch()
            db_set_status("idle")
            backoff = 5
            last_np: Optional[NowPlayingTrack] = None

            while not _restart_event.is_set():
                np = lastfm_now_playing(api_key, username)

                if np:
                    if (
                        not last_np
                        or np.track != last_np.track
                        or np.artist != last_np.artist
                    ):
                        db_set_status(
                            "syncing",
                            track=np.track,
                            artist=np.artist,
                            album=np.album,
                            artwork=np.artwork,
                        )
                        spotify.search_and_play(np.track, np.artist)
                        last_np = np
                else:
                    if last_np:
                        spotify.pause()
                        last_np = None
                        db_set_status("idle")

                if not spotify.alive():
                    log.warning("Browser died — restarting.")
                    db_set_status("error", error="Browser session lost")
                    break

                _restart_event.wait(timeout=interval)

        except Exception as exc:
            log.error("Relay error: %s", exc)
            db_set_status("error", error=str(exc))
        finally:
            spotify.shutdown()

        # If loop exited on its own (not a config restart), apply backoff
        if not _restart_event.is_set():
            _restart_event.wait(timeout=backoff)
            backoff = min(backoff * 2, 120)


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    db_init()
    threading.Thread(target=relay_loop, daemon=True, name="relay").start()
    log.info("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
