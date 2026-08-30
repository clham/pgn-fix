"""
pgn_fix -- find and correct mis-recorded chess moves in PGN movetext.

Public API:
    GameCorrector          -- the correction engine (see corrector.correct)
    Ply, Correction        -- result data types
    tokenize_movetext       -- split PGN movetext into per-ply tokens (+ their
                               recorded move-number labels)
    split_headers_and_movetext
    render_movetext         -- reflow a corrected history back into PGN text
    render_report           -- render a table of corrections for humans
    similarity_score         -- the token/candidate scoring heuristic

The CLI entry point (``pgn-fix``) is ``pgn_fix.corrector:main``.
"""

from .corrector import (
    Correction,
    GameCorrector,
    Ply,
    render_movetext,
    render_report,
    similarity_score,
    split_headers_and_movetext,
    tokenize_movetext,
)

__version__ = "0.1.0"

__all__ = [
    "Correction",
    "GameCorrector",
    "Ply",
    "render_movetext",
    "render_report",
    "similarity_score",
    "split_headers_and_movetext",
    "tokenize_movetext",
    "__version__",
]
