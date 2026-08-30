"""End-to-end tests: run the full `GameCorrector` pipeline over the fixture
games in tests/data and check the corrections it lands on.

`sample_broken.pgn` is a small, hand-crafted demo covering four distinct
error types (a substantive piece-swap, two mis-cased castling moves, and a
move accidentally split across two tokens by a stray space).

`anonymized_game.pgn` is a real club game (headers scrubbed; the moves
themselves are untouched, since the transcription errors are the point) and
exercises the harder cases: missing rank digits, a genuinely wrong move that
only fuzzy/lookahead scoring can catch, and a retroactive rewrite of an
earlier, individually-legal-but-not-actually-played move.
"""

import chess

from pgn_fix.corrector import GameCorrector, split_headers_and_movetext, tokenize_movetext


def _correct(path):
    with open(path) as f:
        raw = f.read()
    headers, movetext = split_headers_and_movetext(raw)
    tokens, labels = tokenize_movetext(movetext)
    engine = GameCorrector()
    return engine.correct(tokens, labels)


def _corrections_by_original(corrections):
    return {c.original: c for c in corrections}


def test_sample_broken_corrections(data_dir):
    board, history, corrections = _correct(data_dir / "sample_broken.pgn")
    by_original = _corrections_by_original(corrections)

    assert len(corrections) == 4

    # A genuinely different move had to be identified (piece-swap typo).
    assert by_original["Nf6"].corrected == "Nf3"
    assert by_original["Nf6"].category == "substantive"

    # Mis-cased/no-hyphen castling, both sides -- deterministic formatting fixes.
    oo_corrections = [c for c in corrections if c.original == "OO"]
    assert len(oo_corrections) == 2
    assert all(c.corrected == "O-O" and c.category == "formatting" for c in oo_corrections)

    # A move accidentally split across two tokens by a stray space.
    assert by_original["Nx d5"].corrected == "Nxd5"
    assert by_original["Nx d5"].category == "formatting"

    assert board.is_valid()


def test_sample_broken_produces_a_fully_legal_game(data_dir):
    board, history, corrections = _correct(data_dir / "sample_broken.pgn")
    # Replaying every corrected SAN from scratch must reach the same
    # position with no illegal moves anywhere in the history.
    replay = chess.Board()
    for ply in history:
        replay.push(replay.parse_san(ply.san))
    assert replay.fen() == board.fen()


def test_anonymized_game_resolves_every_ply_and_stays_legal(data_dir):
    board, history, corrections = _correct(data_dir / "anonymized_game.pgn")

    assert len(history) == 96
    assert not any(c.category == "unresolved" for c in corrections)

    replay = chess.Board()
    for ply in history:
        replay.push(replay.parse_san(ply.san))
    assert replay.fen() == board.fen()


def test_anonymized_game_completes_missing_rank_digits(data_dir):
    board, history, corrections = _correct(data_dir / "anonymized_game.pgn")
    by_original = _corrections_by_original(corrections)

    assert by_original["cxd"].corrected == "cxd4"
    assert by_original["dxc"].corrected == "dxc3"
    assert by_original["gxf"].corrected == "gxf4"
    for original in ("cxd", "dxc", "gxf"):
        assert by_original[original].category == "formatting"


def test_anonymized_game_retroactively_revises_an_earlier_move(data_dir):
    # White's 34th move parses fine as written ("b4+", a legal check) --
    # but it turns out not to be the move actually played: recognizing it
    # as "a4" instead (with Black's reply as "Kb4", not "Kb6") lets far more
    # of the rest of the game parse cleanly, and the retroactive/window
    # search should find and adopt that rewrite even though the original
    # ply was never individually "wrong".
    board, history, corrections = _correct(data_dir / "anonymized_game.pgn")
    retro = [c for c in corrections if c.confidence == "retro"]
    assert len(retro) == 2
    by_original = _corrections_by_original(retro)
    assert by_original["b4+"].corrected == "a4"
    assert by_original["Kb6"].corrected == "Kb4"
