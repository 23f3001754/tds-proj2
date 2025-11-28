# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your project
COPY . .

# Install Playwright browsers
RUN python -m playwright install

# Expose the port (Render uses 10000 by default)
ENV PORT=10000
EXPOSE 10000

# Start the app
CMD ["python", "app.py"]
