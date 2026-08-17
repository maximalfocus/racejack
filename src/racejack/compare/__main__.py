"""Entry point for the comparison CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..artifacts import persist
from ..config import HarnessConfig
from .engine import build_comparison
from .table import render


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="racejack-compare",
        description=(
            "Run every scenario once and print them side by side: both secure strategies, all "
            "four vulnerable shapes, both reproduction modes, both replica counts, and both "
            "controls. Correctness under concurrency only."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the per-request records and the interleaving timelines behind each row",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="also write the rendered comparison to a file"
    )
    args = parser.parse_args(argv)

    comparison = await build_comparison(HarnessConfig.from_env())
    rendered = render(comparison, verbose=args.verbose)
    print(rendered)

    if args.output is not None:
        failure = persist(args.output, rendered + "\n")
        if failure is None:
            print(f"comparison written to {args.output}")
        else:
            # Loud, and deliberately not fatal: the comparison is already on the page above.
            print(f"\nWARNING: {failure}", file=sys.stderr)

    if not comparison.rows:
        print("nothing could be compared", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
