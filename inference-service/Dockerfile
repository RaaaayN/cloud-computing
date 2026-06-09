FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python - <<'PYEOF'
import torchvision.models as models
try:
    from torchvision.models import ResNet50_Weights
    models.resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)
    print("ResNet-50 weights cached.")
except Exception as e:
    print(f"Failed: {e}")
PYEOF

COPY inference.py .

EXPOSE 8000

ENV INFERENCE_WORKERS=2 TOP_K=5

CMD ["uvicorn", "inference:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
