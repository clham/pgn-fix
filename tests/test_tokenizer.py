"""Tests for `pgn_fix.corrector.split_headers_and_movetext`/`tokenize_movetext`."""

from pgn_fix.corrector import split_headers_and_movetext, tokenize_movetext


def test_split_headers_and_movetext():
    pgn = '[Event "Test"]\n[Site "?"]\n\n1. e4 e5 2. Nf3 *\n'
    headers, movetext = split_headers_and_movetext(pgn)
    assert '[Event "Test"]' in headers
    assert '[Site "?"]' in headers
    assert "1. e4 e5 2. Nf3 *" in movetext


def test_tokenize_strips_comments_variations_and_nags():
    movetext = "1. e4 {a comment} e5 $1 2. Nf3 (2. Nc3 Nc6) Nc6 *"
    tokens, labels = tokenize_movetext(movetext)
    assert tokens == ["e4", "e5", "Nf3", "Nc6"]


def test_tokenize_drops_bare_move_number_with_no_period():
    # A move number missing its trailing period ("36" instead of "36.") is
    # common from hand transcription -- it must never be mistaken for a move.
    tokens, labels = tokenize_movetext("34. b4 Kb6 36 Rd7 Nc2")
    assert tokens == ["b4", "Kb6", "Rd7", "Nc2"]


def test_tokenize_recovers_recorded_move_number_labels():
    tokens, labels = tokenize_movetext("34. b4 Kb6 36 Rd7 Nc2 37. Ke2 Nxa3")
    # Both plies under the same "N. move move" label share that label,
    # regardless of whether the period was present.
    assert labels == ["34", "34", "36", "36", "37", "37"]


def test_tokenize_handles_fused_move_number():
    tokens, labels = tokenize_movetext("12.Nf3 Nc6")
    assert tokens == ["Nf3", "Nc6"]
    assert labels == ["12", "12"]


def test_tokenize_black_only_label_with_ellipsis():
    # "12..." conventionally re-establishes context for a Black move alone
    # (e.g. right after a variation or comment), unlike "12." which covers
    # both the White move that follows and the Black move after it.
    tokens, labels = tokenize_movetext("12. Nf3 Nc6 13... Bb4")
    assert tokens == ["Nf3", "Nc6", "Bb4"]
    assert labels == ["12", "12", "13"]


def test_tokenize_strips_result_markers():
    tokens, labels = tokenize_movetext("1. e4 e5 1-0")
    assert tokens == ["e4", "e5"]
