"""Entry point for the sequential demonstration."""

from __future__ import annotations

import asyncio
import sys

from ..config import RunnerConfig
from .sequential import run


def main(argv: list[str] | None = None) -> int:
    if argv:
        print(f"racejack-demo takes no arguments; ignoring {argv}", file=sys.stderr)
    return asyncio.run(run(RunnerConfig.from_env()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
