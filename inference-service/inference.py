"""
Image Classification Inference Service
---------------------------------------
Model  : ResNet-50 (ImageNet, 1000 classes) – CPU-only
Input  : base64-encoded JPEG/PNG  OR  a publicly reachable image URL
Output : top-k predictions with confidence scores + latency telemetry

Design goals
  * P99 latency < 0.5 s under moderate load
  * Prometheus metrics for Autoscaler and monitoring consumption
  * Thread-pool inference so the async event-loop never blocks
  * Request queuing depth exposed as a metric (custom autoscaler signal)
"""

from __future__ import annotations

import base64
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import numpy as np
import torch
import torch.nn.functional as F
import torchvision.models as models
import torchvision.transforms as T
from fastapi import FastAPI, HTTPException
from PIL import Image
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from starlette.responses import Response

# ─────────────────────────────────────────────
# Configuration (overridable via environment)
# ─────────────────────────────────────────────
TOP_K: int = int(os.environ.get("TOP_K", "5"))
INFERENCE_WORKERS: int = int(os.environ.get("INFERENCE_WORKERS", "2"))
# Hard-cap per-request processing time to surface latency problems early
REQUEST_TIMEOUT_S: float = float(os.environ.get("REQUEST_TIMEOUT_S", "2.0"))

# ─────────────────────────────────────────────
# Prometheus metrics
# ─────────────────────────────────────────────
INFERENCE_LATENCY = Histogram(
    "inference_latency_seconds",
    "End-to-end inference latency (pre-processing + forward pass + post-processing)",
    buckets=[0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.75, 1.0, 2.0],
)
INFERENCE_REQUESTS_TOTAL = Counter(
    "inference_requests_total",
    "Total inference requests received",
    ["status"],  # labels: success | error
)
ACTIVE_REQUESTS = Gauge(
    "inference_active_requests",
    "Number of requests currently being processed (in thread pool)",
)
QUEUE_DEPTH = Gauge(
    "inference_queue_depth",
    "Number of requests waiting for a thread-pool slot (custom autoscaler signal)",
)
MODEL_LOAD_TIME = Gauge(
    "inference_model_load_seconds",
    "Time taken to load and warm up the model at startup",
)

# ─────────────────────────────────────────────
# Model loading (single load at startup)
# ─────────────────────────────────────────────
_model: Optional[torch.nn.Module] = None
_transform: Optional[T.Compose] = None
_labels: Optional[List[str]] = None
_model_lock = threading.Lock()

# Standard ImageNet preprocessing
_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD  = [0.229, 0.224, 0.225]


def _load_imagenet_labels() -> List[str]:
    """Return the 1000 ImageNet class names in synset order."""
    try:
        from torchvision.models import ResNet50_Weights
        weights = ResNet50_Weights.IMAGENET1K_V2
        return weights.meta["categories"]
    except Exception:
        # Fallback: numbered labels
        return [f"class_{i}" for i in range(1000)]


def _build_model() -> torch.nn.Module:
    try:
        from torchvision.models import ResNet50_Weights
        m = models.resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)
    except Exception:
        # older torchvision API
        m = models.resnet50(pretrained=True)  # noqa: FBT003
    m.eval()
    # Freeze + use torch.inference_mode globally – no grad tracking needed
    for p in m.parameters():
        p.requires_grad_(False)
    return m


def load_model() -> None:
    global _model, _transform, _labels
    t0 = time.perf_counter()
    with _model_lock:
        if _model is None:
            _model = _build_model()
            _transform = T.Compose([
                T.Resize(256),
                T.CenterCrop(224),
                T.ToTensor(),
                T.Normalize(mean=_IMAGENET_MEAN, std=_IMAGENET_STD),
            ])
            _labels = _load_imagenet_labels()
    elapsed = time.perf_counter() - t0
    MODEL_LOAD_TIME.set(elapsed)


# ─────────────────────────────────────────────
# Inference thread pool
# ─────────────────────────────────────────────
_executor: Optional[ThreadPoolExecutor] = None


def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=INFERENCE_WORKERS,
            thread_name_prefix="inf-worker",
        )
    return _executor


# ─────────────────────────────────────────────
# Core inference logic (runs in thread pool)
# ─────────────────────────────────────────────
def _decode_image(image_b64: Optional[str], image_url: Optional[str]) -> Image.Image:
    """Decode an image from base64 string or URL into a PIL Image."""
    if image_b64:
        raw = base64.b64decode(image_b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    if image_url:
        import urllib.request
        with urllib.request.urlopen(image_url, timeout=5) as resp:
            data = resp.read()
        return Image.open(io.BytesIO(data)).convert("RGB")
    raise ValueError("Either image_b64 or image_url must be provided")


def _run_inference(image_b64: Optional[str], image_url: Optional[str], top_k: int) -> Dict[str, Any]:
    t_start = time.perf_counter()
    ACTIVE_REQUESTS.inc()
    try:
        # 1. Decode
        img = _decode_image(image_b64, image_url)

        # 2. Pre-process
        tensor = _transform(img).unsqueeze(0)  # (1, 3, 224, 224)

        # 3. Forward pass – inference_mode disables autograd for speed
        with torch.inference_mode():
            logits = _model(tensor)          # (1, 1000)
            probs  = F.softmax(logits, dim=1).squeeze(0)

        # 4. Top-k
        topk_probs, topk_indices = torch.topk(probs, k=min(top_k, 1000))
        predictions = [
            {
                "rank":       int(rank + 1),
                "class_id":   int(idx),
                "label":      _labels[int(idx)],
                "confidence": round(float(p), 6),
            }
            for rank, (p, idx) in enumerate(zip(topk_probs, topk_indices))
        ]

        latency = time.perf_counter() - t_start
        INFERENCE_LATENCY.observe(latency)
        INFERENCE_REQUESTS_TOTAL.labels(status="success").inc()

        return {
            "ok":             True,
            "predictions":    predictions,
            "latency_seconds": round(latency, 4),
            "image_size":     list(img.size),
        }
    except Exception as exc:
        latency = time.perf_counter() - t_start
        INFERENCE_LATENCY.observe(latency)
        INFERENCE_REQUESTS_TOTAL.labels(status="error").inc()
        raise exc
    finally:
        ACTIVE_REQUESTS.dec()


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────
app = FastAPI(
    title="Image Classification Inference Service",
    description="ResNet-50 / ImageNet-1k • CPU-only • Prometheus-instrumented",
    version="1.0.0",
)


class PredictRequest(BaseModel):
    # Exactly one of the two must be provided
    image_b64: Optional[str] = Field(
        default=None,
        description="Base64-encoded JPEG or PNG image bytes",
    )
    image_url: Optional[str] = Field(
        default=None,
        description="Publicly reachable URL of a JPEG or PNG image",
    )
    top_k: int = Field(
        default=TOP_K,
        ge=1,
        le=1000,
        description="Number of top predictions to return",
    )


class Prediction(BaseModel):
    rank: int
    class_id: int
    label: str
    confidence: float


class PredictResponse(BaseModel):
    ok: bool
    predictions: List[Prediction]
    latency_seconds: float
    image_size: List[int]


@app.on_event("startup")
def startup_event() -> None:
    load_model()


@app.post("/predict", response_model=PredictResponse, summary="Run image classification")
async def predict(request: PredictRequest) -> PredictResponse:
    """
    Classify an image using ResNet-50 pre-trained on ImageNet.

    Supply **either** `image_b64` (base64-encoded bytes) **or** `image_url`
    (a reachable JPEG/PNG URL). Returns the top-k class predictions with
    confidence scores and the server-side latency.
    """
    if not request.image_b64 and not request.image_url:
        raise HTTPException(
            status_code=422,
            detail="Provide either 'image_b64' or 'image_url'.",
        )

    QUEUE_DEPTH.inc()
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            get_executor(),
            _run_inference,
            request.image_b64,
            request.image_url,
            request.top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        QUEUE_DEPTH.dec()

    return PredictResponse(**result)


@app.get("/metrics", summary="Prometheus metrics endpoint")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz", summary="Liveness probe")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", summary="Readiness probe – fails until model is loaded")
def readyz() -> Dict[str, Any]:
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not yet loaded")
    return {"status": "ready", "model": "resnet50"}


@app.get("/", summary="Service info")
def root() -> Dict[str, Any]:
    return {
        "service":  "image-classification-inference",
        "model":    "ResNet-50 (ImageNet-1k, CPU)",
        "endpoints": ["/predict", "/metrics", "/healthz", "/readyz", "/docs"],
    }
