import argparse
import asyncio
import csv
import random
import time
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


async def fetch_samples():
    out = Path("samples")
    out.mkdir(exist_ok=True)
    imgs = []
    async with httpx.AsyncClient(timeout=30) as c:
        for n in SAMPLES:
            p = out / n
            if not p.exists():
                print(f"  downloading {n}")
                r = await c.get(SAMPLE_URL + n)
                r.raise_for_status()
                p.write_bytes(r.content)
            imgs.append(p.read_bytes())
    return imgs


def target_rps(t, dur, base, peak):
    half = dur / 2
    return base + (peak - base) * (t / half if t <= half else (dur - t) / half)


async def send(c, target, img, writer):
    files = {"file": ("img.jpg", img, "image/jpeg")}
    t0 = time.perf_counter()
    status = 0
    try:
        r = await c.post(f"{target}/predict", files=files, timeout=15)
        status = r.status_code
    except Exception:
        status = -1
    writer.writerow([round(time.time(), 3), status, round(time.perf_counter() - t0, 4)])


async def run(target, dur, base, peak, out):
    imgs = await fetch_samples()
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = p.open("w", newline="")
    w = csv.writer(f)
    w.writerow(["timestamp", "status", "latency_seconds"])

    async with httpx.AsyncClient() as c:
        start = time.perf_counter()
        tasks = []
        while True:
            now = time.perf_counter() - start
            if now >= dur:
                break
            rps = target_rps(now, dur, base, peak)
            interval = 1.0 / rps if rps > 0 else 1.0
            tasks.append(asyncio.create_task(send(c, target, random.choice(imgs), w)))
            await asyncio.sleep(interval * random.uniform(0.8, 1.2))
            if len(tasks) > 500:
                tasks = [t for t in tasks if not t.done()]
            if int(now) % 10 == 0 and now - int(now) < 0.1:
                print(f"[t={int(now):4d}s] rps={rps:.1f} inflight={len(tasks)}")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    f.close()
    print(f"done -> {out}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--base", type=float, default=1.0)
    p.add_argument("--peak", type=float, default=20.0)
    p.add_argument("--out", default="results.csv")
    a = p.parse_args()
    asyncio.run(run(a.target, a.duration, a.base, a.peak, a.out))


if __name__ == "__main__":
    main()