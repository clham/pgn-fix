"""Tests for `pgn_fix.corrector.render_report`."""

from pgn_fix.corrector import Correction, render_report


def test_no_corrections_message():
    assert "No errors found" in render_report([], total_plies=10)


def test_table_has_header_and_one_row_per_correction():
    corrections = [
        Correction(
            ply_number=1, move_number=1, color="White",
            original="nf3", corrected="Nf3", confidence="high",
            note="formatting fix", category="formatting", short_note="",
        ),
        Correction(
            ply_number=2, move_number=1, color="Black",
            original="e6", corrected="e5", confidence="medium",
            note="recorded as 'e6', actually 'e5'", category="substantive",
            short_note="",
        ),
    ]
    report = render_report(corrections, total_plies=2)
    lines = report.splitlines()
    header_line = next(
        line for line in lines if "Type" in line and "Conf" in line and "Note" in line
    )
    assert header_line.strip().startswith("Move")
    assert any("nf3" in line and "Nf3" in line for line in lines)
    assert any("e6" in line and "e5" in line for line in lines)
    assert "Found 2 move(s)" in report


def test_short_note_appears_in_note_column():
    corrections = [
        Correction(
            ply_number=1, move_number=12, color="White",
            original="Rd6+Kd7", corrected="Rd6+", confidence="high",
            note="peeled off the front of a fused token", category="formatting",
            short_note="peeled from fusion",
        ),
    ]
    report = render_report(corrections, total_plies=1)
    assert "peeled from fusion" in report
