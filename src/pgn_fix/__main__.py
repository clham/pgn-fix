"""Allows ``python -m pgn_fix`` as an alternative to the ``pgn-fix`` console script."""

from .corrector import main

if __name__ == "__main__":
    raise SystemExit(main())
