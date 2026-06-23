FROM python:3.11-slim
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
EXPOSE 6001 8001
CMD ["python3", "-u", "app.py"]