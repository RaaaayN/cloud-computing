FROM python:3.10-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests prometheus_client
COPY dispatcher_redis.py .
EXPOSE 5001 8000
CMD ["python", "dispatcher_redis.py"]
