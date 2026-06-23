FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir flask redis prometheus_client requests
EXPOSE 5001 8000
CMD ["python3", "-u", "dispatcher_redis.py"]