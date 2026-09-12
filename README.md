# AirScrobble

AirScrobble bridges music platforms that don't support Airbuds (Tidal, Deezer, YouTube Music, etc.) to Airbuds by way of Spotify. It works by watching what you're currently playing via Last.fm scrobbles and mirroring that playback onto a muted, headless Spotify Web Player running inside a Docker container. Airbuds sees the Spotify activity, and your friends see what you're actually listening to.

## How it works

The core of AirScrobble is a polling loop that runs alongside a headless Chromium browser.

Every few seconds, it hits the Last.fm API and checks if your account has a track with the `nowplaying` flag set. Most music players that support scrobbling will set this flag in real time, so it doesn't matter where the scrobble originates from — Tidal, Deezer, Plex, whatever. If Last.fm says you're playing something, AirScrobble knows about it.

When a new track shows up, AirScrobble takes that track name and artist, navigates the headless Spotify Web Player to the search page, and clicks play on the first result. Spotify is muted at the browser level (`--mute-audio` flag on Chromium), so nothing actually plays out of your speakers. But as far as Spotify's servers are concerned, you're listening to that track. Airbuds picks up the Spotify activity and shows it on your feed.

When you stop playing music and the `nowplaying` flag drops off, AirScrobble pauses Spotify.

The browser session is persistent. Login cookies are cached in a Docker volume, so you authenticate with Spotify once and it sticks across container restarts. No repeated logins, no CAPTCHAs on every boot.

## Setup

### Prerequisites

- A Spotify account (free or premium)
- A Last.fm account with an [API key](https://www.last.fm/api/account/create)
- Your music player configured to scrobble to Last.fm
- Docker, on whatever machine you want to run this on

### Deploy with Portainer

Copy the contents of `portainer-stack.yml` into Portainer under Stacks > Add Stack > Web Editor. Hit deploy. That's it.

The default dashboard login is `admin` / `airscrobble`. You can change this by editing the environment variables in the stack before deploying.

### Deploy with Docker Compose

```
git clone https://github.com/kashcotton/airscrobble.git
cd airscrobble
docker compose up -d --build
```

No `.env` file is required. The defaults work out of the box.

### First run

Once the container is up, open `http://<your-host>:5000` in a browser. Log in with the dashboard credentials (`admin` / `airscrobble` by default), then go to Settings and enter:

- Your Last.fm API key and username
- Your Spotify email and password

Hit save. The relay loop starts automatically. Go back to the dashboard to see the current status.

You can test your Last.fm credentials from the settings page before saving. There's a "Test connection" link under the Last.fm fields.

### Spotify authentication

On the first run, AirScrobble will automate the Spotify login flow using the credentials you provide in settings. The session cookies get cached in a Docker volume, so subsequent restarts skip the login entirely.

If Spotify presents a CAPTCHA during the first automated login, you'll need to log in manually once. To do this, temporarily change `headless=True` to `headless=False` in `app.py` (in the `launch` method), run the container with a display attached, complete the CAPTCHA, then switch back to headless. The session persists.

## Architecture

```
Tidal / Deezer / etc.
        |
    [scrobbles]
        |
     Last.fm API
        |
    [polls every N seconds]
        |
   AirScrobble container
        |
    [headless Chromium]
        |
   Spotify Web Player (muted)
        |
    [detects playback]
        |
      Airbuds
```

The container runs two things:

1. A Flask web server on port 5000 that serves the dashboard and settings UI
2. A background thread running the relay loop (Last.fm polling + Playwright browser automation)

They communicate through a SQLite database. The relay loop writes the current status (idle, syncing, error, track info) to a single-row table. The dashboard reads it via an API endpoint that the frontend polls every 3 seconds.

All configuration (Last.fm credentials, Spotify credentials, polling interval) is stored in the same SQLite database and managed through the web UI. Environment variables are only used for dashboard login credentials and the Flask session secret.

## Files

```
.
├── app.py                          # Flask server + relay loop
├── templates/
│   ├── base.html                   # Shared layout
│   ├── login.html                  # Login page
│   ├── index.html                  # Dashboard
│   └── settings.html               # Settings page
├── Dockerfile
├── docker-compose.yml              # For local dev / direct Docker Compose
├── portainer-stack.yml             # For Portainer (references GHCR image)
├── .github/workflows/
│   └── docker-publish.yml          # CI: builds + pushes image to GHCR
├── requirements.txt
├── .env.example
└── .gitignore
```

## CI

Every push to `main` triggers a GitHub Actions workflow that builds the Docker image and pushes it to `ghcr.io/kashcotton/airscrobble:latest`. The workflow uses `GITHUB_TOKEN` for authentication, so there's nothing to configure.

After the first successful workflow run, go to the repo's Packages tab, click the package, go to Package Settings, and set the visibility to Public. Otherwise Portainer won't be able to pull the image without authentication.

To update a running Portainer deployment: click Recreate on the stack with "Re-pull image" enabled.

## Notes

- The polling interval defaults to 10 seconds. You can change it in settings. Minimum is 5 seconds.
- The relay loop has exponential backoff for errors (5 seconds up to 2 minutes). If the browser crashes or the network drops, it recovers on its own.
- The container is set to `restart: unless-stopped`, so it comes back after reboots.
- The dashboard is dark mode. There is no light mode.
