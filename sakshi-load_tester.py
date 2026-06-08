import argparse  #This lets you run the script from the terminal with arguments.
import asyncio  #This simulates multiple users better.
import csv   
import random
import time
from pathlib import Path

import httpx

SAMPLE_URL = "https://raw.githubusercontent.com/EliSchwartz/imagenet-sample-images/master/"

SAMPLES = [
    "n02085620_Chihuahua.JPEG",
    "n02123045_tabby.JPEG",
    "n03445777_golf_ball.JPEG",
    "n07873807_pizza.JPEG",
    "n02690373_airliner.JPEG",
]


async def fetch_samples():     #This function downloads the sample images.
    out = Path("samples")        #This means the function is asynchronous. It can wait for downloads without blocking everything.
    out.mkdir(exist_ok=True)
    imgs = []
    async with httpx.AsyncClient(timeout=30) as c:
        for n in SAMPLES:
            p = out / n
            if not p.exists():    #This checks whether the image is already downloaded.
                print(f"  downloading {n}")
                r = await c.get(SAMPLE_URL + n)  #This downloads the image.
                r.raise_for_status()  #This checks if download was successful.
                p.write_bytes(r.content)  #This saves the downloaded image to your computer.
            imgs.append(p.read_bytes())    #This reads the image from the file and stores it in the list as bytes.
    return imgs


def target_rps(t, dur, base, peak):  #This function decides the current request rate.
    half = dur / 2
    return base + (peak - base) * (t / half if t <= half else (dur - t) / half)


async def send(c, target, img, writer):  #This function sends one image request to the ML service.
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
    imgs = await fetch_samples() #First, it downloads/loads sample images.
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
            tasks.append(asyncio.create_task(send(c, target, random.choice(imgs), w)))   #This randomly chooses one image from the sample images.
            await asyncio.sleep(interval * random.uniform(0.8, 1.2))  #This adds small randomness to the request timing.
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