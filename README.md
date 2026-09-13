# AirScrobble

If you listen to music on Tidal, Deezer, YouTube Music, Plex, or local files, you’re usually locked out of social features built exclusively for Spotify. AirScrobble bridges that gap.

It keeps an eye on your Last.fm scrobbles in real time and mirrors whatever you're playing onto a muted, headless Spotify Web Player. To Discord, Airbuds, and Instagram Notes, it looks like regular Spotify activity—so your friends can finally see what you're actually listening to.

---

## What It Does

* **Instant Sync via Connect API:** Instead of clicking around the web UI, it controls playback directly through the Spotify Connect API so tracks update immediately.
* **Smart Local File Fallbacks:** Listening to unreleased demos or bandcamp tracks missing from Spotify? AirScrobble automatically generates `spotify:local` URIs so your presence still updates.
* **Headless Session Keep-Alive:** Authenticates using your `sp_dc` cookie, bypassing bot checks, CAPTCHAs, and datacenter IP blocks.
* **Silent & Ad-Free:** Drops Spotify audio ads on the wire so they never interrupt your tracking loop.
* **Simple Dashboard:** Includes an Airbuds-inspired web UI to manage settings, check sync status, and update credentials.

---

## Supported Social Integrations

Because AirScrobble updates your real Spotify account presence, anything reading Spotify playback will pick it up automatically:

* **Airbuds**
* **Discord** (Rich Presence)
* **Instagram Notes** (Spotify status)
* Any third-party widget or bot connected to your Spotify profile

---

## Getting Started

### Prerequisites

1. A Spotify account (Free or Premium).
2. A [Last.fm](https://www.last.fm/api/account/create) account and API key.
3. Your media player configured to scrobble tracks to Last.fm.
4. Docker installed on your server or local machine.

### 1. Run with Docker Compose

Save this to a `docker-compose.yml` file:

```yaml
services:
  airscrobble:
    image: ghcr.io/kashcotton/airscrobble:latest
    container_name: airscrobble
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - airscrobble_data:/app/data
    environment:
      - ADMIN_USER=admin
      - ADMIN_PASS=airscrobble

volumes:
  airscrobble_data:

```

Spin it up:

```bash
docker compose up -d

```

---

### 2. Grab Your Spotify Cookie (`sp_dc`)

Spotify blocks standard login requests coming from server IPs, so AirScrobble uses your session cookie to connect:

1. Open an Incognito window in Chrome and sign in to [open.spotify.com](https://open.spotify.com).
2. Press `F12` (or right-click and choose **Inspect**) to open Developer Tools.
3. Go to the **Application** tab, expand **Cookies**, and select `[https://open.spotify.com](https://open.spotify.com)`.
4. Find the cookie named **`sp_dc`** and copy its value.
5. Open your AirScrobble dashboard at `http://<your-server-ip>:5000`, head to **Settings**, and paste the cookie in.

---

## How It Works Under the Hood

```text
Tidal / Deezer / YouTube Music / Plex
                 │
           (Scrobbles)
                 ▼
            Last.fm API
                 │
          (Polls status)
                 ▼
       AirScrobble Container
  ┌───────────────────────────────┐
  │ • Flask Web Dashboard (:5000) │
  │ • Background Sync Worker      │
  │ • Headless Chromium Instance  │
  └──────────────┬────────────────┘
                 │
     Spotify Connect API (PUT /play)
                 ▼
    Muted Spotify Web Player SDK
                 │
                 ▼
   Discord / Airbuds / Instagram Notes

```

Inside the container, a background Python worker regularly polls the Last.fm API for your active track. When a new song starts, it issues an API call to a headless Chromium instance (driven by Playwright) that runs a persistent, muted Spotify Web Player session. Spotify registers the playback as active, and your linked social apps update in tandem.

---

## Contributing & Development

Images are automatically built and published to GitHub Container Registry (`ghcr.io/kashcotton/airscrobble:latest`) on every push to `main`. Issues, pull requests, and feedback are always welcome!