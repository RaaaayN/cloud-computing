from torchvision.models import resnet18, ResNet18_Weights
import torch
import base64
from PIL import Image
import io
import asyncio
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from aiohttp import web
import time
from prometheus_client import Counter, Histogram, generate_latest


preprocessor = ResNet18_Weights.IMAGENET1K_V1.transforms()

# These two lines are important, as your pods will have CPU request and CPU limit of "1" (for memory also use "1G" for both request and limit)
torch.set_num_interop_threads(1)
torch.set_num_threads(1)


resnet_model = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
resnet_model.eval()

# Inference runs in a single-worker thread so it does NOT block the aiohttp event
# loop (otherwise /healthz stalls under load and the liveness probe kills the pod).
# max_workers=1 keeps the spec guarantee: one inference at a time per replica.
_inference_executor = ThreadPoolExecutor(max_workers=1)

# Pod only becomes Ready after the model is warmed up (first torch pass is slow),
# so cold-start latency never hits real traffic.
model_ready = False

INFERENCE_REQUESTS_TOTAL = Counter(
    "inference_requests_total",
    "Total number of inference requests",
)
INFERENCE_DURATION_SECONDS = Histogram(
    "inference_duration_seconds",
    "Server-side inference latency in seconds",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0, 30.0],
)



def infer(d):
    t = time.perf_counter()
    decoded = base64.b64decode(d["data"])
    inp = Image.open(io.BytesIO(decoded)).convert("RGB")
    inp = np.array(preprocessor(inp))
    inp = torch.from_numpy(np.array([inp]))

    with torch.no_grad():
        preds = resnet_model(inp)
    labels = []
    for idx in list(preds[0].sort()[1])[-1:-6:-1]:
        labels.append(ResNet18_Weights.IMAGENET1K_V1.meta["categories"][idx])
    print("Server-side processing took:", round(time.perf_counter() - t, 3))
    return labels


def _warmup() -> None:
    """Run one forward pass so the first real request is not paying lazy init."""
    with torch.no_grad():
        resnet_model(torch.zeros(1, 3, 224, 224))


app = web.Application()


async def infer_handler(request):
    req = await request.json()
    INFERENCE_REQUESTS_TOTAL.inc()
    loop = asyncio.get_running_loop()
    with INFERENCE_DURATION_SECONDS.time():
        # off-load to the single inference thread; event loop stays responsive
        labels = await loop.run_in_executor(_inference_executor, infer, req)
    return web.json_response(labels)


async def on_startup(_: web.Application) -> None:
    global model_ready
    await asyncio.get_running_loop().run_in_executor(_inference_executor, _warmup)
    model_ready = True
    print("model warmed up; ready")


async def healthz_handler(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def readyz_handler(_: web.Request) -> web.Response:
    status = 200 if model_ready else 503
    return web.json_response({"ready": model_ready}, status=status)


async def metrics_handler(_: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), content_type="text/plain", charset="utf-8")
    

app.on_startup.append(on_startup)
app.add_routes(
    [
        web.post("/infer", infer_handler),
        web.get("/healthz", healthz_handler),
        web.get("/readyz", readyz_handler),
        web.get("/metrics", metrics_handler),
    ]
)

if __name__ == '__main__':
    web.run_app(app, host="0.0.0.0", port=8001, access_log=None)