FROM python:3.11-slim

# Install system dependencies for headless browsers
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libnss3 \
    libgconf-2-4 \
    libatk1.0-0 \
    libcups2 \
    libxss1 \
    libasound2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    wget curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy project files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Install Playwright browsers
RUN playwright install --with-deps

# Expose port
ENV PORT=8000
CMD ["python", "app.py"]
