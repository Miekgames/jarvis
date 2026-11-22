"""Build a wallpaper pack through JARVIS's own workshop service."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.business.service import BusinessService  # noqa: E402
from backend.config import settings  # noqa: E402


async def main() -> int:
    palette = sys.argv[1] if len(sys.argv) > 1 else "midnight-harbour"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 12

    service = BusinessService(settings)
    plan = service.plan(palette, count=count)
    estimate = service.estimate(plan)
    print(f"{plan.name}: {plan.count} designs, {estimate['files']} files, "
          f"~{estimate['megabytes']:.0f} MB, ${plan.price_usd:.2f}")
    if not estimate.get("fits", True):
        print("! " + estimate.get("warning", "not enough room"))
        return 1

    def show(done, total, name):
        if done % 6 == 0 or done == total:
            print(f"  [{done:3}/{total}] {name[:64]}", flush=True)

    summary = await service.build(plan, overwrite=True, on_progress=None)
    print()
    print(f"built: {summary['name']}")
    print(f"  {summary['wallpapers']} designs, {summary['files']} files, "
          f"{summary['megabytes']} MB")
    print(f"  {summary['directory']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
