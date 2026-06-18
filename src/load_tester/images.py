import base64
import io
import os
from pathlib import Path

import httpx

SAMPLE_URL = "https://raw.githubusercontent.com/EliSchwartz/imagenet-sample-images/master/"
SAMPLES = [
    "n02085620_60.JPEG",
    "n02123045_50.JPEG",
    "n03445777_42.JPEG",
    "n07873807_8.JPEG",
    "n02690373_1.JPEG",
]
SYNTHETIC_COUNT = 5


def encode_image_bytes(raw: bytes) -> str:
    """Encode raw JPEG bytes as base64 for the dispatcher /submit API."""
    return base64.b64encode(raw).decode("utf-8")


def build_submit_payload(image_b64: str) -> dict[str, str]:
    """Build JSON payload expected by POST /submit."""
    return {"data": image_b64}


def generate_synthetic_samples(count: int = SYNTHETIC_COUNT) -> list[str]:
    """Generate random RGB JPEGs in-process (no network, no bundled binary).

    The model accuracy is irrelevant for load testing; we only need valid,
    differently-sized JPEGs to exercise the inference path. Requires Pillow.
    """
    from PIL import Image

    encoded: list[str] = []
    for _ in range(count):
        raw = os.urandom(224 * 224 * 3)
        image = Image.frombytes("RGB", (224, 224), raw)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        encoded.append(encode_image_bytes(buffer.getvalue()))
    return encoded


async def fetch_samples(samples_dir: Path | None = None) -> list[str]:
    """Return base64-encoded sample images.

    Tries to download the ImageNet sample set first (cached on disk). If the
    remote is unavailable (e.g. the upstream repo moved/404s), falls back to
    locally generated synthetic JPEGs so the load tester always runs offline
    and inside the cluster.
    """
    out = samples_dir or Path("samples")
    out.mkdir(exist_ok=True)
    encoded: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for name in SAMPLES:
                path = out / name
                if not path.exists():
                    print(f"  downloading {name}")
                    response = await client.get(SAMPLE_URL + name)
                    response.raise_for_status()
                    path.write_bytes(response.content)
                encoded.append(encode_image_bytes(path.read_bytes()))
    except Exception as exc:  # noqa: BLE001 - network is best-effort
        print(f"  sample download failed ({exc}); using synthetic images")
        encoded = []

    if not encoded:
        encoded = generate_synthetic_samples()
    return encoded
