import sys, re

with open('app.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Add urllib.parse
if 'import urllib.parse' not in code:
    code = code.replace('import time', 'import time\nimport urllib.parse')

# Add access_token and intercept request
code = code.replace(
    'self._current_query: Optional[str] = None',
    'self._current_query: Optional[str] = None\n        self._access_token: Optional[str] = None\n\n    def _intercept_request(self, request):\n        auth = request.headers.get("authorization", "")\n        if auth.startswith("Bearer "):\n            self._access_token = auth'
)

# Add autoplay-policy and attach interceptor
code = code.replace(
    '"--mute-audio",',
    '"--mute-audio",\n                "--autoplay-policy=no-user-gesture-required",'
)
code = code.replace(
    'self._page = self._ctx.new_page()',
    'self._page = self._ctx.new_page()\n        self._page.on("request", self._intercept_request)'
)

# Add _api_search method
api_search = '''
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
            log.debug(f"API search error: {e}")
        return None
'''
code = code.replace('def search_and_play(self, track: str, artist: str) -> bool:', api_search.lstrip() + '\n    def search_and_play(self, track: str, artist: str) -> bool:')

# Rewrite search_and_play
old_search_and_play = re.search(r'def search_and_play.*?def pause', code, flags=re.DOTALL).group(0)
new_search_and_play = '''def search_and_play(self, track: str, artist: str) -> bool:
        query = f"{track} {artist}"
        if self._current_query == query:
            return True
        log.info("Searching: %s", query)

        track_uri = None
        if self._access_token:
            track_uri = self._api_search(f"{track} {artist}")
            if not track_uri:
                track_uri = self._api_search(track)

        if track_uri and track_uri.startswith("spotify:track:"):
            track_id = track_uri.split(":")[-1]
            try:
                self._page.goto(f"{self.URL}/track/{track_id}", wait_until="domcontentloaded", timeout=30_000)
                time.sleep(3)
                
                try:
                    self._page.locator('#onetrust-accept-btn-handler').click(timeout=1000)
                except:
                    pass

                btn = self._page.locator('button[data-testid="play-button"], button[data-testid="action-bar-play-button"]').first
                btn.scroll_into_view_if_needed(timeout=5_000)
                btn.click(timeout=5_000, force=True)
                self._current_query = query
                log.info("▶ Playing exact match via API lookup: %s — %s", track, artist)
                return True
            except Exception as exc:
                log.warning("Failed track page play: %s", exc)

        if not track_uri and self._access_token:
            log.info("Song not found on Spotify. Attempting local file stream injection...")
            safe_artist = urllib.parse.quote(artist.replace(":", ""))
            safe_track = urllib.parse.quote(track.replace(":", ""))
            local_uri = f"spotify:local:{safe_artist}:unknown:{safe_track}:180000"
            
            try:
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
                log.warning("Local file injection failed: %s", exc)

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
            btn.click(timeout=8_000, force=True)
            self._current_query = query
            log.info("Playing (UI fallback): %s — %s", track, artist)
            return True
        except Exception as exc:
            log.warning("Playback failed: %s", exc)
            return False

    def pause'''

code = code.replace(old_search_and_play, new_search_and_play)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(code)
print('Patch successful')
