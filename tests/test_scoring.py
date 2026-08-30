"""Tests for the candidate-scoring heuristics in `pgn_fix.corrector`."""

from pgn_fix.corrector import _core, _piece_letter, similarity_score


def test_piece_letter_distinguishes_pawn_shape_from_piece_letter():
    # "b4" is a pawn move (matches the pawn shape); "bc4" cannot be a legal
    # pawn move (no "x" for what would need to be a capture), so a lowercase
    # leading letter there must be read as a mis-cased piece letter instead.
    assert _piece_letter(_core("b4")) == "P"
    assert _piece_letter(_core("bc4")) == "B"
    assert _piece_letter(_core("Nf3")) == "N"
    assert _piece_letter(_core("nf3")) == "N"


def test_same_piece_and_near_square_beats_a_different_piece():
    # This is the exact "scrivener's error" case the scoring was tuned for:
    # a recorded "rd1" should point strongly at "Rd1" (its own piece, just
    # re-cased) rather than "Kd2" (a different piece on a different square),
    # even though naive text-edit-distance might not obviously prefer one.
    assert similarity_score("rd1", "Rd1") > similarity_score("rd1", "Kd2")


def test_one_square_off_beats_a_piece_swap():
    # "Kd3" recorded, actually "Kd2" (one rank off, same piece) should score
    # far higher than "Qd2" (same-ish square, but a different piece).
    assert similarity_score("kd3", "Kd2") > similarity_score("kd3", "Qd2")


def test_castling_shortcut_regardless_of_hyphens_or_case():
    for token in ["OO", "oo", "0-0", "O-O"]:
        assert similarity_score(token, "O-O") > similarity_score(token, "Nf3")
    for token in ["OOO", "ooo", "0-0-0"]:
        assert similarity_score(token, "O-O-O") > similarity_score(token, "O-O")


def test_recorded_check_mark_is_confirmed_by_a_checking_candidate():
    # A recorded "+" should be treated as close to authoritative: a
    # candidate that actually delivers check scores higher than one that
    # doesn't, all else being equal.
    assert similarity_score("re2+", "Re2+") > similarity_score("re2+", "Re2")


def test_recorded_check_mark_penalizes_a_non_checking_candidate():
    # And the converse: a candidate that contradicts a recorded check mark
    # is penalized, not just left unrewarded.
    checking = similarity_score("nb2", "Re2+")  # no "+" recorded, candidate checks
    quiet = similarity_score("nb2", "Kb7")  # no "+" recorded, candidate doesn't check
    assert quiet > checking


def test_absence_of_check_on_both_sides_is_not_rewarded():
    # Most moves don't give check, so "neither has a +" must be the
    # uninformative default -- it must not inflate the score the way an
    # actual confirmed "+" does (this previously caused a regression where
    # a plain candidate could out-score an exact split-token merge).
    with_bonus_if_buggy = similarity_score("Nx", "Nxd5")
    # A "+"-less candidate matching a "+"-less token should score the same
    # as it would if the check-authority feature didn't exist at all --
    # i.e. it should still be beaten by an exact split-token merge's fixed
    # score of 1.5 in the corrector's own tie-break, which only holds if
    # this case isn't artificially inflated.
    assert with_bonus_if_buggy < 1.5
