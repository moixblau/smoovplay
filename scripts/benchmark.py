"""GPU timing per stage for each RIFE model / resolution (synthetic frames, no decode).

  python scripts/benchmark.py
  python scripts/benchmark.py --models 4.25 4.22.lite --res 1080p 4k --scales 1.0 0.5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from framegen.interpolator import MODELS, RifeInterpolator  # noqa: E402

RES = {"1080p": (1920, 1080), "1440p": (2560, 1440), "4k": (3840, 2160)}


def gpu_ms(fn, iters: int) -> float:
    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    torch.cuda.synchronize()
    start.record()
    for _ in range(iters):
        fn()
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=list(MODELS))
    ap.add_argument("--res", nargs="+", default=["1080p", "4k"], choices=list(RES))
    ap.add_argument("--scales", nargs="+", type=float, default=[1.0, 0.5])
    ap.add_argument("--iters", type=int, default=50)
    a = ap.parse_args()

    print(f"{torch.cuda.get_device_name(0)}  torch {torch.__version__}")
    print(f"{'model':<10} {'res':<6} {'scale':>5} {'graph':>5} | {'prep ms':>7} {'interp ms':>9} "
          f"{'new-frame tick':>14} {'wall/pair':>9}")
    for res in a.res:
        w, h = RES[res]
        frame = torch.randint(0, 256, (h, w, 3), dtype=torch.uint8, device="cuda")
        for model in a.models:
            for scale in a.scales:
                for graph in (False, True):
                    it = RifeInterpolator(w, h, model, scale, cuda_graph=graph)
                    it.load(frame)
                    it.prepare(0)
                    for _ in range(3):  # MIOpen/cuDNN autotune happens on the first eager calls; keep it out of the timing
                        it.prepare(1)
                        it.interpolate(1)
                    prep = gpu_ms(lambda: it.prepare(1), a.iters)
                    interp = gpu_ms(lambda: it.interpolate(1), a.iters)
                    torch.cuda.synchronize()
                    t = time.perf_counter()
                    for _ in range(a.iters):
                        it.prepare(1)
                        it.interpolate(1)
                    torch.cuda.synchronize()
                    wall = (time.perf_counter() - t) * 1000 / a.iters
                    print(f"{model:<10} {res:<6} {scale:>5} {str(graph):>5} | {prep:7.2f} {interp:9.2f} "
                          f"{prep + interp:14.2f} {wall:9.2f}")
                    del it
                    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
