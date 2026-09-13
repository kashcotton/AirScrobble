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

    def _ad_blocker(self, route):
        url = route.request.url.lower()
        if "/ads/" in url or "ad-logic" in url or "ads-api" in url or "doubleclick.net" in url:
            route.abort()
        else:
            route.continue_()

    def launch(self) -> None:
        log.info("Launching browser …")
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
        self._page.route("**/*", self._ad_blocker)
        
        sp_dc = get_config("spotify_sp_dc")
        if sp_dc:
            self._ctx.add_cookies([{
                "name": "sp_dc",
                "value": sp_dc,
                "domain": ".spotify.com",
                "path": "/",
                "secure": True,
                "httpOnly": True
            }])
            log.info("Injected SP_DC authentication cookie.")

        self._page.on("request", self._intercept_request)
        self._page.goto(self.URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(5)
        
                                                                                            
                                                                    
        if sp_dc:
            log.info("Bypassing login check entirely due to SP_DC cookie.")
        elif not self._is_logged_in():
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
        time.sleep(2)
        try:
                                                                    
            user_node = self._page.locator('[data-testid="user-widget-link"], [data-testid="user-widget-avatar"]').first
            if user_node.is_visible(timeout=3_000):
                return True
                
                                                                    
            login_node = self._page.locator('button[data-testid*="login"], a[data-testid*="login"], button:has-text("Log in")').first
            if login_node.is_visible(timeout=3_000):
                return False
        except:
            pass
            
                                                                                                            
        return False

    def _login(self) -> None:
        email = get_config("spotify_email")
        password = get_config("spotify_password")
        if not email or not password:
            raise RuntimeError("Spotify credentials not configured")
            
        log.info("Logging in to Spotify …")
        page = self._page
        
                                                                                          
        page.goto("https://accounts.spotify.com/login", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)
        
                                                                                                      
        if "open.spotify.com" in page.url:
            log.info("Already logged in (redirected).")
            return
            
                                                   
        try:
            page.locator('#onetrust-accept-btn-handler').click(timeout=2000)
        except:
            pass
            
        try:
            email_loc = page.locator('input#login-username, input#username, input[data-testid="login-username"], input[type="email"], input[type="text"]').first
            email_loc.fill(email, timeout=10_000)
            time.sleep(1)
            
                                                              
            pwd_loc = page.locator('input[type="password"], input#login-password, input#password').first
            if not pwd_loc.is_visible():
                log.info("Two-step login flow detected. Proceeding to password step...")
                btn = page.locator('button[data-testid="login-button"], button[type="submit"], button:has-text("Continue"), button:has-text("Next")').first
                btn.click(timeout=5_000, force=True)
                pwd_loc.wait_for(state="visible", timeout=10_000)
                time.sleep(1)
                
            pwd_loc.fill(password, timeout=10_000)
            time.sleep(1)
            login_btn = page.locator('button[data-testid="login-button"], button[type="submit"], button:has-text("Log In"), button:has-text("Log in")').first
            login_btn.click(timeout=10_000, force=True)
            
            page.wait_for_url("**/open.spotify.com/**", timeout=30_000)
            time.sleep(3)
        except Exception as exc:
            try:
                page.screenshot(path=str(BROWSER_DATA_DIR / "debug.png"))
            except:
                pass
            raise RuntimeError(f"Login failed (Check credentials or /debug for captcha): {exc}")
            
        log.info("Login successful.")

    def _get_token(self) -> Optional[str]:
        if self._access_token:
            return self._access_token
                                                               
        for _ in range(3):
            try:
                token = self._page.evaluate("() => fetch('https://open.spotify.com/get_access_token?reason=transport&productType=web_player').then(r => r.json()).then(j => j.accessToken)")
                if token:
                    self._access_token = f"Bearer {token}"
                    return self._access_token
            except Exception:
                pass
            time.sleep(1)
        return None

    def _api_search(self, query: str) -> Optional[str]:
        token = self._get_token()
        if not token:
            return None
        try:
            r = requests.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": token},
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

    def _ensure_device_active(self, token: str) -> Optional[str]:
        try:
            r = requests.get("https://api.spotify.com/v1/me/player/devices", headers={"Authorization": token}, timeout=5)
            devices = r.json().get("devices", [])
            
                                                    
            active_id = next((d.get("id") for d in devices if d.get("is_active")), None)
            if active_id:
                return active_id
                
            device_id = devices[0].get("id") if devices else None
            
                                                                
            if not device_id:
                log.info("Waking up Web Player device...")
                try:
                    self._page.locator('[data-testid="control-button-playpause"]').first.evaluate("node => node.click()")
                    time.sleep(1)
                    r = requests.get("https://api.spotify.com/v1/me/player/devices", headers={"Authorization": token}, timeout=5)
                    devices = r.json().get("devices", [])
                    device_id = devices[0].get("id") if devices else None
                except Exception:
                    pass
            
                                                                           
            if device_id:
                requests.put(
                    "https://api.spotify.com/v1/me/player",
                    headers={"Authorization": token},
                    json={"device_ids": [device_id], "play": False},
                    timeout=5
                )
                return device_id
        except Exception as e:
            log.debug("Device activation error: %s", e)
        return None

    def _ui_fallback(self, query: str) -> bool:
        log.info("Fallback to raw UI search...")
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
            self._current_query = query
            return True
        except Exception as exc:
            log.warning("Playback UI fallback failed: %s", exc)
            return False

    def search_and_play(self, track: str, artist: str) -> bool:
        query = f"{track} {artist}"
        if self._current_query == query:
            return True
            
        log.info("Searching: %s", query)
        token = self._get_token()
        
                                                             
        if not token:
            return self._ui_fallback(query)

                                    
        track_uri = self._api_search(query) or self._api_search(track)
        
                                          
        if not track_uri:
            log.info("Song not found on Spotify. Faking a local file stream...")
            safe_artist = urllib.parse.quote(artist.replace(":", ""))
            safe_track = urllib.parse.quote(track.replace(":", ""))
            track_uri = f"spotify:local:{safe_artist}:unknown:{safe_track}:180000"

                                                                    
        active_id = self._ensure_device_active(token)
        if active_id:
            try:
                res = requests.put(
                    "https://api.spotify.com/v1/me/player/play",
                    headers={"Authorization": token},
                    params={"device_id": active_id},
                    json={"uris": [track_uri]},
                    timeout=5
                )
                if res.status_code in (200, 202, 204):
                    self._current_query = query
                    log.info("⚡ Instant Play API Success: %s", track_uri)
                    return True
                else:
                    log.warning("Instant Play API rejected the request (Status %s).", res.status_code)
            except Exception as exc:
                log.warning("Instant Play API failed: %s", exc)

                                                                                              
        return self._ui_fallback(query)

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


                                                                            

_restart_event = threading.Event()


def signal_restart():
    """Tell the relay loop to restart with fresh config."""
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
                    log.warning("Browser died — restarting.")
                    db_set_status("error", error="Browser session lost")
                    break

                _restart_event.wait(timeout=interval)

        except Exception as exc:
            log.error("Relay error: %s", exc)
            db_set_status("error", error=str(exc))
        finally:
            spotify.shutdown()

                                                                                 
        if not _restart_event.is_set():
            _restart_event.wait(timeout=backoff)
            backoff = min(backoff * 2, 120)


                                                                            

def main() -> None:
    db_init()
    threading.Thread(target=relay_loop, daemon=True, name="relay").start()
    log.info("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
