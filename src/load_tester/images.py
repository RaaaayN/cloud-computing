import base64
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


def encode_image_bytes(raw: bytes) -> str:
    """Encode raw JPEG bytes as base64 for the dispatcher /submit API."""
    return base64.b64encode(raw).decode("utf-8")


def build_submit_payload(image_b64: str) -> dict[str, str]:
    """Build JSON payload expected by POST /submit."""
    return {"data": image_b64}


async def fetch_samples(samples_dir: Path | None = None) -> list[str]:
    """Download sample images and return base64-encoded strings."""
    out = samples_dir or Path("samples")
    out.mkdir(exist_ok=True)
    encoded: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for name in SAMPLES:
            path = out / name
            if not path.exists():
                print(f"  downloading {name}")
                response = await client.get(SAMPLE_URL + name)
                response.raise_for_status()
                path.write_bytes(response.content)
            encoded.append(encode_image_bytes(path.read_bytes()))
    return encoded
