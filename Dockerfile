FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    xvfb \
    pulseaudio \
    dbus \
    dbus-x11 \
    xdotool \
    ffmpeg \
    gnupg2 \
    libglib2.0-0 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libxkbcommon0 \
    && (curl -sS https://download.spotify.com/debian/pubkey_5384CE82BA52C83A.asc | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/spotify.gpg \
        || curl -sS https://download.spotify.com/debian/pubkey_C85668DF69375001.gpg | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/spotify.gpg \
        || curl -sS https://download.spotify.com/debian/pubkey_6224F9941A8AA6D1.gpg | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/spotify.gpg) \
    && echo "deb http://repository.spotify.com stable non-free" > /etc/apt/sources.list.d/spotify.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends spotify-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chrome
RUN playwright install-deps chrome

COPY . .

RUN mkdir -p /app/browser_data /app/data /app/local_files /root/.config/spotify

EXPOSE 5000

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
