from __future__ import annotations

from agent.cli import main as _main


def cli() -> int:
    return _main()


if __name__ == "__main__":
    raise SystemExit(cli())
