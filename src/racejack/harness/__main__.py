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
        "--variant",
        choices=[variant.value for variant in Variant],
        default=Variant.SECURE.value,
        help="which application to drive (default: secure)",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ReproductionMode],
        default=ReproductionMode.DETERMINISTIC.value,
        help=(
            "deterministic holds the time-of-check to time-of-use window open with an instrumented "
            "synchronization point, so the interleaving is identical everywhere; natural attaches "
            "nothing at all. The secure application is never instrumented either way."
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

    variant = Variant(args.variant)
    mode = ReproductionMode(args.mode)
    if variant is Variant.SECURE and mode is ReproductionMode.DETERMINISTIC:
        # There is no instrumented synchronization point in any secure code path, so there is
        # nothing for a deterministic run to hold open.
        mode = ReproductionMode.NATURAL

    config = HarnessConfig.from_env()
    report = await Harness(config, variant=variant, mode=mode).run()

    print(render_summary(report, config))

    if not args.no_transcript:
        path = args.transcript or _transcript_path(config, variant, mode)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_transcript(report, config))
        except OSError as exc:
            print(f"could not write the transcript to {path}: {exc}", file=sys.stderr)
            return 1
        print(f"transcript written to {path}")

    return 0 if report.passed else 1


def _transcript_path(config: HarnessConfig, variant: Variant, mode: ReproductionMode) -> Path:
    """One transcript per variant and mode, so a run never overwrites another run's evidence."""
    base = config.transcript_path
    return base.with_name(f"{base.stem}-{variant.value}-{mode.value}{base.suffix}")


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
