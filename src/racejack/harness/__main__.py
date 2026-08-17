"""Entry point for the concurrent load harness."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..config import HarnessConfig
from .engine import Harness
from .ledger import ReproductionMode, Variant
from .transcript import render_summary, render_transcript


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="racejack-harness",
        description=(
            "Drive the storefront under genuine concurrent load and reconcile the resulting "
            "ledger against its invariants. Measures correctness under concurrency only."
        ),
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help="where to write the run transcript (default: RACEJACK_TRANSCRIPT_PATH)",
    )
    parser.add_argument(
        "--no-transcript", action="store_true", help="do not write a transcript artifact"
    )
    args = parser.parse_args(argv)

    config = HarnessConfig.from_env()
    report = await Harness(config, variant=Variant.SECURE, mode=ReproductionMode.NATURAL).run()

    print(render_summary(report, config))

    if not args.no_transcript:
        path = args.transcript or config.transcript_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_transcript(report, config))
        except OSError as exc:
            print(f"could not write the transcript to {path}: {exc}", file=sys.stderr)
            return 1
        print(f"transcript written to {path}")

    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
