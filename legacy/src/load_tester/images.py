import base64
import io
import os
from pathlib import Path

import httpx

SAMPLE_URL = "https://raw.githubusercontent.com/EliSchwartz/imagenet-sample-images/master/"
SAMPLES = [
    "n01440764_tench.JPEG",
    "n01484850_great_white_shark.JPEG",
    "n02085620_Chihuahua.JPEG",
    "n02123045_tabby.JPEG",
    "n02690373_airliner.JPEG",
]
SYNTHETIC_COUNT = 5

# Real ImageNet query images shipped inside the package (from
# github.com/EliSchwartz/imagenet-sample-images, slide 19). These are baked
# into the loadtester image, so the in-cluster Job uses real images out of the
# box instead of falling back to synthetic noise.
BUNDLED_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


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


def load_local_samples(samples_dir: Path) -> list[str]:
    """Encode any image files already present in samples_dir (jpg/jpeg/png)."""
    if not samples_dir.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted(p for p in samples_dir.iterdir() if p.suffix.lower() in exts)
    return [encode_image_bytes(p.read_bytes()) for p in files]


async def fetch_samples(samples_dir: Path | None = None) -> list[str]:
    """Return base64-encoded sample images.

    Resolution order:
      1. Use any images already in `samples/` (drop your own ImageNet samples
         from github.com/EliSchwartz/imagenet-sample-images here to override).
      2. Use the real ImageNet samples bundled in the package (shipped in the
         loadtester image) — the default in-cluster path.
      3. Otherwise try to download the configured ImageNet sample set.
      4. Otherwise generate synthetic JPEGs (offline, in-cluster safe).
    """
    out = samples_dir or Path("samples")
    out.mkdir(exist_ok=True)

    encoded = load_local_samples(out)
    if encoded:
        print(f"  using {len(encoded)} local sample image(s) from {out}")
        return encoded

    encoded = load_local_samples(BUNDLED_SAMPLES_DIR)
    if encoded:
        print(f"  using {len(encoded)} bundled ImageNet sample image(s)")
        return encoded

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
