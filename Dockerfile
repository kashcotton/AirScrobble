FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Google Chrome for Widevine DRM support (required by Spotify)
RUN playwright install chrome
RUN playwright install-deps chrome

# Copy application code
COPY . .

# Create directories for persistent data
RUN mkdir -p /app/browser_data /app/data

EXPOSE 5000

CMD ["python", "app.py"]

