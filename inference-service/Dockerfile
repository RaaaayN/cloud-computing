# Base image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    apt-get install redis-server \
    redis-server

# Set working directory
WORKDIR /app

# Copy all files
COPY . .

# Upgrade pip and install dependencies in venv
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Expose Flask port
EXPOSE 6001 8001


# Run the Flask app
CMD ["python3", "app.py"]