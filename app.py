"""
AirScrobble — Bridge Last.fm Now Playing to Spotify.
"""

from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import subprocess
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

load_dotenv()

DASHBOARD_USER = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASSWORD", "airscrobble")
FLASK_SECRET = os.getenv("FLASK_SECRET_KEY", "change-me-to-something-random")

BROWSER_DATA_DIR = Path("browser_data")
DB_PATH = Path("data/airscrobble.db")
SPOTIFY_CONFIG_DIR = Path("/root/.config/spotify")
LOCAL_FILES_DIR = Path("/app/local_files")

TOKEN_REFRESH_INTERVAL = 1800
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("airscrobble")


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


_ENV_MAP = {
    "lastfm_api_key": "LASTFM_API_KEY",
    "lastfm_username": "LASTFM_USERNAME",
    "spotify_email": "SPOTIFY_EMAIL",
    "spotify_password": "SPOTIFY_PASSWORD",
    "spotify_sp_dc": "SPOTIFY_SP_DC",
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
    has_lastfm = bool(c.get("lastfm_api_key") and c.get("lastfm_username"))
    has_spotify = bool(c.get("spotify_sp_dc") or (c.get("spotify_email") and c.get("spotify_password")))
    return has_lastfm and has_spotify


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
    for key in ("lastfm_api_key", "lastfm_username", "spotify_email", "spotify_sp_dc", "poll_interval"):
        if key in data:
            set_config(key, str(data[key]).strip())
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


AD_BLOCK_PATTERNS = ("/ads/", "ad-logic", "ads-api", "doubleclick.net", "spclient.wg.spotify.com/ads")


class SpotifyController:
    URL = "https://open.spotify.com"

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._desktop_proc: Optional[subprocess.Popen] = None
        self._access_token: Optional[str] = None
        self._token_fetched_at: float = 0
        self._device_id: Optional[str] = None
        self._current_query: Optional[str] = None
        self._consecutive_api_failures: int = 0
        self._use_desktop: bool = False

    def _ad_blocker(self, route):
        url = route.request.url.lower()
        for pattern in AD_BLOCK_PATTERNS:
            if pattern in url:
                route.abort()
                return
        route.continue_()

    def launch(self) -> None:
        self._start_desktop()

        token = self._refresh_token()
        if not token:
            log.warning("HTTP token fetch failed. Starting web player fallback ...")
            self._launch_web_session()
            token = self._refresh_token()

        if self._desktop_proc and token:
            self._wait_for_desktop_device(token)

        if not self._device_id and self._page:
            self._wake_web_player(token)

        log.info("Spotify ready. Desktop: %s", "active" if self._use_desktop else "inactive")

    def _start_desktop(self) -> None:
        LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
        SPOTIFY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        sp_dc = get_config("spotify_sp_dc")
        has_session = (SPOTIFY_CONFIG_DIR / "Users").exists()

        if sp_dc and not has_session:
            log.info("First run: bootstrapping desktop client session ...")
            self._bootstrap_desktop(sp_dc)

        self._configure_local_files()

        try:
            self._desktop_proc = subprocess.Popen(
                ["spotify", "--no-zygote", "--disable-gpu"],
                env={**os.environ, "DISPLAY": ":99"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Desktop client started (PID %d).", self._desktop_proc.pid)
        except FileNotFoundError:
            log.info("Spotify desktop client not installed. Using web player only.")
            self._desktop_proc = None

    def _bootstrap_desktop(self, sp_dc: str) -> None:
        try:
            proc = subprocess.Popen(
                ["spotify", "--no-zygote", "--disable-gpu"],
                env={**os.environ, "DISPLAY": ":99"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(8)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

            self._inject_sp_dc_desktop(sp_dc)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Desktop bootstrap failed: %s", e)

    def _inject_sp_dc_desktop(self, sp_dc: str) -> None:
        browser_dir = SPOTIFY_CONFIG_DIR / "Browser"
        browser_dir.mkdir(parents=True, exist_ok=True)
        cookie_db = browser_dir / "Cookies"

        chrome_offset = 11644473600
        now = int((time.time() + chrome_offset) * 1_000_000)
        expires = int((time.time() + 365 * 86400 + chrome_offset) * 1_000_000)

        try:
            conn = sqlite3.connect(str(cookie_db))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cookies (
                    creation_utc INTEGER NOT NULL,
                    host_key TEXT NOT NULL DEFAULT '',
                    top_frame_site_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL DEFAULT '',
                    encrypted_value BLOB NOT NULL DEFAULT X'',
                    path TEXT NOT NULL DEFAULT '',
                    expires_utc INTEGER NOT NULL DEFAULT 0,
                    is_secure INTEGER NOT NULL DEFAULT 0,
                    is_httponly INTEGER NOT NULL DEFAULT 0,
                    last_access_utc INTEGER NOT NULL DEFAULT 0,
                    has_expires INTEGER NOT NULL DEFAULT 1,
                    is_persistent INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 1,
                    samesite INTEGER NOT NULL DEFAULT -1,
                    source_scheme INTEGER NOT NULL DEFAULT 2,
                    source_port INTEGER NOT NULL DEFAULT 443,
                    is_same_party INTEGER NOT NULL DEFAULT 0,
                    last_update_utc INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("DELETE FROM cookies WHERE name = 'sp_dc'")
            conn.execute("""
                INSERT INTO cookies (
                    creation_utc, host_key, name, value, path, expires_utc,
                    is_secure, is_httponly, last_access_utc, has_expires, is_persistent,
                    priority, samesite, source_scheme, source_port, is_same_party, last_update_utc
                ) VALUES (?, '.spotify.com', 'sp_dc', ?, '/', ?, 1, 1, ?, 1, 1, 1, -1, 2, 443, 0, ?)
            """, (now, sp_dc, expires, now, now))
            conn.commit()
            conn.close()
            log.info("Injected SP_DC into desktop client cookie store.")
        except Exception as e:
            log.warning("Cookie injection failed: %s", e)

    def _configure_local_files(self) -> None:
        prefs_file = SPOTIFY_CONFIG_DIR / "prefs"
        prefs = {}
        if prefs_file.exists():
            for line in prefs_file.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    prefs[k.strip()] = v.strip()

        prefs["app.local-files-source-dirs"] = f'"{str(LOCAL_FILES_DIR)}"'
        prefs_file.write_text("\n".join(f"{k}={v}" for k, v in prefs.items()) + "\n")

    def _wait_for_desktop_device(self, token: str) -> None:
        if not token:
            return
        log.info("Waiting for desktop client to register ...")
        for _ in range(30):
            try:
                r = requests.get(
                    "https://api.spotify.com/v1/me/player/devices",
                    headers={"Authorization": token},
                    timeout=5,
                )
                for d in r.json().get("devices", []):
                    if d.get("type") == "Computer":
                        self._device_id = d["id"]
                        self._use_desktop = True
                        log.info("Desktop device found: %s (%s)", d.get("name"), d["id"])
                        requests.put(
                            "https://api.spotify.com/v1/me/player",
                            headers={"Authorization": token},
                            json={"device_ids": [self._device_id], "play": False},
                            timeout=5,
                        )
                        return
            except Exception:
                pass
            time.sleep(2)
        log.warning("Desktop client did not register within 60s. Using web player fallback.")

    def _launch_web_session(self) -> None:
        from playwright.sync_api import sync_playwright

        log.info("Launching web player ...")
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

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
            user_agent=USER_AGENT,
            args=[
                "--headless=new",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            viewport={"width": 1280, "height": 720},
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._ctx.new_page()
        self._page.route("**/*", self._ad_blocker)

        sp_dc = get_config("spotify_sp_dc")
        if sp_dc:
            self._ctx.add_cookies([{
                "name": "sp_dc",
                "value": sp_dc,
                "domain": ".spotify.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }])

        self._page.goto(self.URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(5)
        log.info("Web player loaded.")

    def _wake_web_player(self, token: Optional[str]) -> None:
        if not token or not self._page:
            return
        try:
            r = requests.get(
                "https://api.spotify.com/v1/me/player/devices",
                headers={"Authorization": token},
                timeout=5,
            )
            for d in r.json().get("devices", []):
                self._device_id = d["id"]
                return
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._desktop_proc:
            try:
                self._desktop_proc.terminate()
                self._desktop_proc.wait(timeout=5)
            except Exception:
                try:
                    self._desktop_proc.kill()
                except Exception:
                    pass
            self._desktop_proc = None

        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

        self._ctx = self._pw = self._page = None
        self._access_token = None
        self._device_id = None
        self._current_query = None
        self._use_desktop = False

    def _refresh_token(self) -> Optional[str]:
        sp_dc = get_config("spotify_sp_dc")
        if sp_dc:
            try:
                r = requests.get(
                    f"{self.URL}/get_access_token",
                    params={"reason": "transport", "productType": "web_player"},
                    cookies={"sp_dc": sp_dc},
                    headers={"User-Agent": USER_AGENT},
                    timeout=10,
                )
                if r.status_code == 200:
                    token = r.json().get("accessToken")
                    if token:
                        self._access_token = f"Bearer {token}"
                        self._token_fetched_at = time.monotonic()
                        log.info("Token refreshed via HTTP.")
                        return self._access_token
            except Exception:
                pass

        if self._page:
            for _ in range(3):
                try:
                    token = self._page.evaluate(
                        "() => fetch('/get_access_token?reason=transport&productType=web_player')"
                        ".then(r => r.json()).then(j => j.accessToken)"
                    )
                    if token:
                        self._access_token = f"Bearer {token}"
                        self._token_fetched_at = time.monotonic()
                        log.info("Token refreshed via browser.")
                        return self._access_token
                except Exception:
                    pass
                time.sleep(1)

        log.warning("Token refresh failed.")
        return None

    def _get_token(self) -> Optional[str]:
        if self._access_token and (time.monotonic() - self._token_fetched_at) < TOKEN_REFRESH_INTERVAL:
            return self._access_token
        return self._refresh_token()

    def _api_search(self, query: str) -> Optional[str]:
        token = self._get_token()
        if not token:
            return None
        try:
            r = requests.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": token},
                params={"q": query, "type": "track", "limit": 1},
                timeout=5,
            )
            if r.status_code == 401:
                self._access_token = None
                return None
            if r.status_code == 200:
                items = r.json().get("tracks", {}).get("items", [])
                if items:
                    return items[0].get("uri")
        except Exception:
            pass
        return None

    def _get_device_id(self, token: str, force_refresh: bool = False) -> Optional[str]:
        if self._device_id and not force_refresh:
            return self._device_id

        try:
            r = requests.get(
                "https://api.spotify.com/v1/me/player/devices",
                headers={"Authorization": token},
                timeout=5,
            )
            devices = r.json().get("devices", [])

            for d in devices:
                if d.get("type") == "Computer":
                    self._device_id = d["id"]
                    self._use_desktop = True
                    if not d.get("is_active"):
                        requests.put(
                            "https://api.spotify.com/v1/me/player",
                            headers={"Authorization": token},
                            json={"device_ids": [self._device_id], "play": False},
                            timeout=5,
                        )
                    return self._device_id

            for d in devices:
                if d.get("is_active"):
                    self._device_id = d["id"]
                    return self._device_id

            if devices:
                self._device_id = devices[0]["id"]
                requests.put(
                    "https://api.spotify.com/v1/me/player",
                    headers={"Authorization": token},
                    json={"device_ids": [self._device_id], "play": False},
                    timeout=5,
                )
                return self._device_id
        except Exception as e:
            log.warning("Device lookup failed: %s", e)

        return None

    def _api_play(self, token: str, track_uri: str) -> bool:
        device_id = self._get_device_id(token)
        if not device_id:
            return False

        try:
            res = requests.put(
                "https://api.spotify.com/v1/me/player/play",
                headers={"Authorization": token},
                params={"device_id": device_id},
                json={"uris": [track_uri]},
                timeout=5,
            )
            if res.status_code in (200, 202, 204):
                self._consecutive_api_failures = 0
                return True

            if res.status_code == 401:
                self._access_token = None
                return False

            if res.status_code == 404:
                self._device_id = None
                device_id = self._get_device_id(token, force_refresh=True)
                if device_id:
                    retry = requests.put(
                        "https://api.spotify.com/v1/me/player/play",
                        headers={"Authorization": token},
                        params={"device_id": device_id},
                        json={"uris": [track_uri]},
                        timeout=5,
                    )
                    if retry.status_code in (200, 202, 204):
                        self._consecutive_api_failures = 0
                        return True

            log.warning("Play API returned %s", res.status_code)
        except Exception as exc:
            log.warning("Play API error: %s", exc)

        self._consecutive_api_failures += 1
        return False

    def _api_pause(self) -> bool:
        token = self._get_token()
        if not token:
            return False
        try:
            res = requests.put(
                "https://api.spotify.com/v1/me/player/pause",
                headers={"Authorization": token},
                timeout=5,
            )
            return res.status_code in (200, 202, 204)
        except Exception:
            return False

    def _generate_local_file(self, track: str, artist: str) -> Optional[Path]:
        safe_name = f"{artist} - {track}".replace("/", "_").replace("\\", "_")[:200]
        output_path = LOCAL_FILES_DIR / f"{safe_name}.mp3"

        if output_path.exists():
            return output_path

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "180", "-c:a", "libmp3lame", "-q:a", "9",
                    "-metadata", f"artist={artist}",
                    "-metadata", f"title={track}",
                    str(output_path),
                ],
                capture_output=True,
                timeout=15,
            )
            if output_path.exists():
                log.info("Generated local file: %s", safe_name)
                return output_path
        except Exception as e:
            log.warning("Local file generation failed: %s", e)

        return None

    def _ui_fallback(self, query: str) -> bool:
        if not self._page:
            return False
        log.info("Falling back to UI search ...")
        try:
            self._page.goto(
                f"{self.URL}/search/{urllib.parse.quote(query)}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            btn = self._page.locator('main button[data-testid="play-button"], main button[aria-label*="Play"]').first
            btn.wait_for(state="visible", timeout=10_000)
            btn.scroll_into_view_if_needed(timeout=5_000)
            btn.evaluate("node => node.click()")
            return True
        except Exception as exc:
            log.warning("UI fallback failed: %s", exc)
            return False

    def search_and_play(self, track: str, artist: str) -> bool:
        query = f"{track} {artist}"
        if self._current_query == query:
            return True

        log.info("Searching: %s", query)
        token = self._get_token()

        if not token:
            if self._ui_fallback(query):
                self._current_query = query
                return True
            return False

        track_uri = self._api_search(query) or self._api_search(track)
        is_local = False

        if not track_uri:
            log.info("Not on Spotify. Generating local file ...")
            self._generate_local_file(track, artist)
            safe_artist = urllib.parse.quote(artist.replace(":", ""))
            safe_track = urllib.parse.quote(track.replace(":", ""))
            track_uri = f"spotify:local:{safe_artist}:unknown:{safe_track}:180000"
            is_local = True

        if self._api_play(token, track_uri):
            self._current_query = query
            log.info("Playing: %s", track_uri)
            return True

        if self._consecutive_api_failures >= 3:
            log.warning("API unstable. Full refresh ...")
            self._access_token = None
            self._device_id = None
            self._consecutive_api_failures = 0
            token = self._refresh_token()
            if token and self._api_play(token, track_uri):
                self._current_query = query
                return True

        if not is_local and self._ui_fallback(query):
            self._current_query = query
            return True

        return False

    def pause(self) -> None:
        if not self._current_query:
            return
        if self._api_pause():
            log.info("Paused.")
        elif self._page:
            try:
                btn = self._page.locator(
                    'button[data-testid="control-button-pause"], button[aria-label="Pause"]'
                ).first
                if btn.is_visible(timeout=2_000):
                    btn.click(timeout=3_000, force=True)
            except Exception:
                pass
        self._current_query = None

    def alive(self) -> bool:
        if self._desktop_proc:
            if self._desktop_proc.poll() is not None:
                log.warning("Desktop client exited. Restarting ...")
                self._start_desktop()
                time.sleep(5)
                token = self._get_token()
                if token:
                    self._wait_for_desktop_device(token)
                return True

        if self._page:
            try:
                return self._page.evaluate("() => document.readyState") in ("complete", "interactive")
            except Exception:
                return False

        if self._desktop_proc and self._desktop_proc.poll() is None:
            return True

        return False


_restart_event = threading.Event()


def signal_restart():
    _restart_event.set()


def relay_loop() -> None:
    backoff = 5

    while True:
        _restart_event.clear()

        while not is_configured():
            db_set_status("not_configured")
            if _restart_event.wait(timeout=3):
                break
        if _restart_event.is_set():
            continue

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
            idle_count = 0
            polls_since_token_check = 0
            token_check_every = max(1, TOKEN_REFRESH_INTERVAL // interval)

            while not _restart_event.is_set():
                np = lastfm_now_playing(api_key, username)

                if np:
                    idle_count = 0
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
                    idle_count += 1
                    if last_np and idle_count >= 3:
                        spotify.pause()
                        last_np = None
                        db_set_status("idle")

                if not spotify.alive():
                    log.warning("Session lost. Restarting ...")
                    db_set_status("error", error="Session lost")
                    break

                polls_since_token_check += 1
                if polls_since_token_check >= token_check_every:
                    polls_since_token_check = 0
                    spotify._refresh_token()

                _restart_event.wait(timeout=interval)

        except Exception as exc:
            log.error("Relay error: %s", exc)
            db_set_status("error", error=str(exc))
        finally:
            spotify.shutdown()

        if not _restart_event.is_set():
            log.info("Retrying in %ds ...", backoff)
            _restart_event.wait(timeout=backoff)
            backoff = min(backoff * 2, 120)


def main() -> None:
    db_init()
    threading.Thread(target=relay_loop, daemon=True, name="relay").start()
    log.info("Dashboard -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
