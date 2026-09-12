FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium only to save space)
RUN playwright install chromium

# Copy application code
COPY . .

# Create directories for persistent data
RUN mkdir -p /app/browser_data /app/data

EXPOSE 5000

CMD ["python", "app.py"]

