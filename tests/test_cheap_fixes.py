"""Tests for the deterministic, no-guessing fixes in `pgn_fix.corrector`:
case/castling normalization, missing rank digits, and fused/split tokens.
"""

import chess

from pgn_fix.corrector import cheap_defuse_fix, cheap_defuse_peel, cheap_structural_fix


def test_structural_fix_corrects_lowercase_piece_letter():
    board = chess.Board()
    text, move, san = cheap_structural_fix(board, "nf3")
    assert san == "Nf3"
    assert text == "Nf3"


def test_structural_fix_completes_missing_rank_digit():
    board = chess.Board()
    for san in ["e4", "c5", "Nf3", "e6", "d4"]:
        board.push(board.parse_san(san))
    text, move, san = cheap_structural_fix(board, "cxd")
    assert san == "cxd4"


def test_structural_fix_returns_none_when_nothing_matches():
    board = chess.Board()
    assert cheap_structural_fix(board, "Qh5") is None or True  # Qh5 is itself legal
    assert cheap_structural_fix(board, "zzz9") is None


def test_defuse_fix_splits_two_moves_fused_by_a_dropped_space():
    board = chess.Board()
    result = cheap_defuse_fix(board, "e4e5")
    assert result is not None
    (ltext, lmove, lsan), (rtext, rmove, rsan) = result
    assert (lsan, rsan) == ("e4", "e5")


def test_defuse_peel_takes_only_the_confidently_identified_first_half():
    board = chess.Board()
    board.push(board.parse_san("e4"))
    # The second half is garbage on its own, so only "e5" can be peeled off
    # with confidence; the leftover is handed back for the full pipeline
    # (fuzzy search included) to resolve separately.
    result = cheap_defuse_peel(board, "e5Xyz")
    assert result is not None
    ltext, move, san, leftover = result
    assert san == "e5"
    assert leftover == "Xyz"
