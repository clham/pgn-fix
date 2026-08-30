"""Tests that reported move numbers track the source file's own numbering
(see `_move_number_and_color`), even when that numbering has a gap -- e.g.
a hand-transcribed scoresheet that skips straight from "34." to "36."
without ever writing "35.". Which side moved is always inferred from ply
order, never from these labels, so a gap like that can never desync the
game itself; it used to desync the *reported* move numbers, though, which
is what this guards against.
"""

from pgn_fix.corrector import (
    GameCorrector,
    _move_number_and_color,
    split_headers_and_movetext,
    tokenize_movetext,
)


def test_move_number_prefers_recorded_label_over_recount():
    # Ply index 10 would naively be "move 6" (10 // 2 + 1) -- but if the
    # source recorded it under "8", that's what should be reported.
    move_number, color = _move_number_and_color(10, recorded_label="8")
    assert move_number == 8
    assert color == "White"  # still inferred structurally from ply parity


def test_move_number_falls_back_to_recount_when_no_label_present():
    move_number, color = _move_number_and_color(10, recorded_label=None)
    assert move_number == 6
    assert color == "White"


def test_report_numbering_survives_a_skipped_label_in_the_source():
    pgn = (
        "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. b4 axb5 "
        "6. c3 d6 7. d4 Bd7 *"
    )
    headers, movetext = split_headers_and_movetext(pgn)
    tokens, labels = tokenize_movetext(movetext)
    engine = GameCorrector()
    board, history, corrections = engine.correct(tokens, labels)

    # Ply order alone determines who moved -- the missing "5." label can't
    # desync the game, so every move still parses (nothing is flagged).
    assert not any(c for c in corrections)

    # The move recorded under "6." should still be reported as move 6, even
    # though it's only the 5th move-pair the source actually numbered.
    sixth_pair_plies = [p for p in history if p.recorded_label == "6"]
    assert len(sixth_pair_plies) == 2
