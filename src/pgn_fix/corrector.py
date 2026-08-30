#!/usr/bin/env python3
"""
corrector.py -- pgn_fix core engine
====================================

Reads a game recorded in algebraic notation (a PGN movetext, with or
without headers), finds moves that could not actually have been played
in the position they appear in, and works out what was probably really
played -- then writes a corrected PGN to a new file plus a plain-text
report of every change it made.

The hard part of this problem is that a mis-recorded move usually still
*looks* like a legal move in isolation ("Nf3" is a perfectly fine looking
move token) -- the only way to know it is wrong is that it does not fit
the actual position, or that accepting it makes the rest of the game
impossible to parse. So the strategy used here is:

  1. Play through the game one ply at a time with `python-chess`, which
     will refuse (raise ValueError / IllegalMoveError / AmbiguousMoveError
     / InvalidMoveError) the moment a recorded token cannot be played in
     the current position.
  2. When that happens, generate every legal move available in the
     current position and rank them by how closely their SAN text
     resembles the garbled token (piece letter, destination square,
     capture marker, edit distance, ...).
  3. For the top few candidates, tentatively play each one and try to
     parse the *next several* recorded moves against the resulting
     position. The candidate that lets the game continue matching the
     rest of the transcript the furthest is almost certainly the move
     that was actually played -- this is the "look ahead to see where
     the piece actually landed" heuristic.
  4. If even the best candidate only explains a couple of future moves
     (low confidence), the error may actually be a ply or two further
     back (a move that was legal, but not the move that was really
     played, only becomes visible once a later move stops making
     sense). In that case the tool re-opens the last couple of plies and
     does a small joint search over that window plus the current token,
     again scored by how far the transcript keeps parsing afterwards.

None of this requires knowing the "real" game in advance -- it only uses
internal consistency of the rest of the transcript, exactly as a human
annotator would when they say "wait, that can't be right, look at what
happens two moves later."

Usage:
    pgn-fix input.pgn -o corrected.pgn -r report.txt

Requires the `python-chess` package (``pip install chess``).
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:
    import chess
except ImportError:  # pragma: no cover - convenience for the end user
    sys.exit(
        "This tool requires the 'python-chess' package.\n"
        "Install it with:  pip install chess"
    )


# --------------------------------------------------------------------------
# 1. Splitting a PGN file into headers + a clean list of move tokens
# --------------------------------------------------------------------------

RESULT_TOKENS = {"1-0", "0-1", "1/2-1/2", "*"}
_MOVE_NUM_PARTS_RE = re.compile(r"^(\d+)(\.+)(.*)$")
_TRAILING_GLYPHS_RE = re.compile(r"[!?]+$")
_NAG_RE = re.compile(r"\$\d+")


def split_headers_and_movetext(pgn_text: str) -> Tuple[str, str]:
    """Separate the ``[Tag "value"]`` header block from the movetext."""
    header_lines = []
    body_lines = []
    for line in pgn_text.splitlines():
        if line.strip().startswith("[") and line.strip().endswith("]"):
            header_lines.append(line)
        else:
            body_lines.append(line)
    return "\n".join(header_lines), "\n".join(body_lines)


def _strip_braced_comments(text: str) -> str:
    return re.sub(r"\{[^}]*\}", " ", text)


def _strip_line_comments(text: str) -> str:
    return re.sub(r";[^\n]*", " ", text)


def _strip_variations(text: str) -> str:
    """Drop any ``( ... )`` side-variation, including nested ones.

    PGN variations can nest, so a simple regex can't safely remove them;
    walk the string tracking depth instead. Only the mainline (depth 0)
    text is kept.
    """
    out = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def tokenize_movetext(movetext: str) -> Tuple[List[str], List[Optional[str]]]:
    """Turn PGN movetext into an ordered list of raw move strings, plus a
    parallel list of the move-number label (as written, e.g. "36") each
    token appeared under in the source -- or None if none preceded it.

    Move numbers ("12.", "12...", "12. ", or fused like "12.Nf3"), result
    markers, comments, NAGs and side variations are all stripped out.
    What's left is exactly one token per ply, in play order.

    Whose move it actually is never depends on these labels -- that's
    always determined by ply order, strictly alternating -- so a mistake
    or gap in the source's own numbering (a skipped "35.", say) can never
    desync the game itself. The labels are captured purely so a reported
    correction can cite the *same* number the user's own file shows next
    to that move, rather than a silently recomputed one that stops
    matching the page as soon as the source's numbering has a gap.
    """
    text = _strip_braced_comments(movetext)
    text = _strip_line_comments(text)
    text = _strip_variations(text)
    text = _NAG_RE.sub(" ", text)

    tokens: List[str] = []
    labels: List[Optional[str]] = []
    pending_label: Optional[str] = None
    slots_remaining = 0
    for raw in text.split():
        tok = raw
        # A bare move number missing its period(s) -- "36" instead of "36."
        # -- is common from scanners/OCR and hand transcription. No legal
        # move is ever purely digits, so this is always safe to drop --
        # but it still sets the label for the next couple of tokens.
        if tok.isdigit():
            pending_label = tok
            slots_remaining = 2
            continue
        m = _MOVE_NUM_PARTS_RE.match(tok)
        if m:
            pending_label = m.group(1)
            dots = m.group(2)
            # "12." precedes a White move (and, conventionally, Black's
            # right after it); "12..." (3+ dots) explicitly precedes just
            # a Black move on its own.
            slots_remaining = 1 if len(dots) >= 3 else 2
            tok = m.group(3)
        if not tok:
            continue
        if tok in RESULT_TOKENS:
            continue
        tok = _TRAILING_GLYPHS_RE.sub("", tok)  # drop trailing !, ?, !!, ?! ...
        if not tok or tok in RESULT_TOKENS:
            continue
        tokens.append(tok)
        if slots_remaining > 0:
            labels.append(pending_label)
            slots_remaining -= 1
        else:
            labels.append(None)
    return tokens, labels


# --------------------------------------------------------------------------
# 2. Scoring how well a legal move matches a (possibly garbled) token
# --------------------------------------------------------------------------

_PIECE_LETTERS = set("NBRQK")
_PROMO_SUFFIX_RE = re.compile(r"=([QRBNqrbn])$")
_PAWN_SHAPE_RE = re.compile(r"^[a-h](x[a-h])?[1-8]$")
_SQUARE_RE = re.compile(r"[a-h][1-8]")
# Castling written every which way: with hyphens (standard), with zeros
# instead of the letter O, or with the hyphens simply dropped ("OO"/"000"
# -- a common shorthand). Long-castle patterns are checked before short
# ones wherever both are tried, since "OOO"/"000" would otherwise also
# need excluding from the short-castle pattern.
_LONG_CASTLE_RE = re.compile(r"^(o-o-o|0-0-0|ooo|000)$", re.IGNORECASE)
_SHORT_CASTLE_RE = re.compile(r"^(o-o|0-0|oo|00)$", re.IGNORECASE)


def _core(san: str) -> str:
    """Strip trailing check/mate marks so we compare the meaningful part."""
    return san.rstrip("+#")


def _piece_letter(core: str) -> str:
    """Which piece a SAN-ish string refers to: N/B/R/Q/K, or 'P' for a pawn.

    Handwritten or OCR'd/scanned transcripts often lose the capitalization
    that normally distinguishes a piece letter from a file letter --
    "bc4" (meant as Bishop to c4) versus "b4" (pawn to b4). The only
    reliable signal left is *shape*: a real pawn move is always exactly
    "<file>[x<file>]<rank>" (e.g. "b4", "bxc4"). Anything that doesn't
    match that shape but starts with one of N/B/R/Q/K, in either case, is
    almost certainly that piece with its letter case simply lost --
    lowercase "bc4" cannot be a legal pawn move (missing the required
    "x"), so it is read as Bishop.
    """
    if _PAWN_SHAPE_RE.match(core):
        return "P"
    first = core[:1].upper()
    return first if first in _PIECE_LETTERS else "P"


def _extract_dest_square(core: str) -> Optional[Tuple[int, int]]:
    """(file 0-7, rank 0-7) of the last square-shaped substring in `core`,
    e.g. the destination square of a move -- or None if there isn't one."""
    matches = _SQUARE_RE.findall(core.lower())
    if not matches:
        return None
    last = matches[-1]
    return ord(last[0]) - ord("a"), int(last[1]) - 1


def similarity_score(token: str, candidate_san: str) -> float:
    """Heuristic score for how plausible it is that ``token`` was meant to
    be ``candidate_san``. Weighted, deliberately, to favor the way people
    and scanners actually mis-record moves: a slip to a neighbouring
    square (Kd2 for Kd3, a file swapped for an adjacent one) is far more
    likely than the piece changing entirely -- so matching piece identity
    and destination-square proximity dominate the score, and a piece
    mismatch is actively penalized rather than just failing to help.
    """
    a = _core(token)
    b = _core(candidate_san)

    # Castling is special-cased since "0-0"/"OO"/etc. typos are common and
    # the text otherwise looks nothing like a normal move.
    if b == "O-O-O" and _LONG_CASTLE_RE.match(a):
        return 2.0
    if b == "O-O" and _SHORT_CASTLE_RE.match(a):
        return 2.0

    score = difflib.SequenceMatcher(None, a, b).ratio()

    a_sq = _PROMO_SUFFIX_RE.sub("", a)
    b_sq = _PROMO_SUFFIX_RE.sub("", b)

    a_dest = _extract_dest_square(a_sq)
    b_dest = _extract_dest_square(b_sq)
    if a_dest is not None and b_dest is not None:
        dist = max(abs(a_dest[0] - b_dest[0]), abs(a_dest[1] - b_dest[1]))
        if dist == 0:
            score += 0.5  # exact destination square
        else:
            score += max(0.0, 0.5 - 0.15 * dist)  # one square off is still a strong signal

    if _piece_letter(a_sq) == _piece_letter(b_sq):
        score += 0.5  # same piece: the single strongest, most reliable signal
    else:
        score -= 0.35  # a genuinely different piece is a much bigger, rarer error

    if ("x" in a_sq) == ("x" in b_sq):
        score += 0.05

    # Whether the token was recorded *with* a check/mate mark is treated as
    # close to authoritative: noticing and writing down "+" (or "#") takes
    # real attention over the board, so a transcriber essentially never
    # adds one that wasn't there or leaves one off that was -- unlike a
    # square or piece letter, which are trivially easy to misread or
    # mistype. So a candidate whose actual, engine-verified check/mate
    # status disagrees with what was recorded is heavily disfavored, even
    # if it otherwise looks textually close. The reward is only given for
    # actually *confirming* a recorded mark, though, not for both simply
    # lacking one -- most moves don't give check, so "neither has a +"
    # is the uninformative default and must never inflate the score the
    # way a real, rarely-wrong confirmation should.
    token_gives_check = token.rstrip("!?").endswith(("+", "#"))
    candidate_gives_check = candidate_san.endswith(("+", "#"))
    if token_gives_check and candidate_gives_check:
        score += 0.5
    elif token_gives_check != candidate_gives_check:
        score -= 0.5
    return score


def _case_fix_variants(token: str) -> List[str]:
    """Just the capitalization half of the mechanical fixes: a piece
    letter (or castling) that lost its capitalization -- "bc4" -> "Bc4",
    "ne7" -> "Ne7", "o-o" -> "O-O". Always includes the token itself
    first. Deliberately does *not* also guess a missing rank digit the
    way `_normalization_variants` does: that guess is only meaningful
    for a *complete* token that's missing its last character, not for an
    arbitrary short fragment -- letting it apply to fragments (as when
    scanning split points in `cheap_defuse_fix`) would manufacture
    spurious extra matches (e.g. "rd" "expanding" into "Rd6", "Rd7", ...)
    purely because it happens to be a short prefix, which would make a
    perfectly unambiguous split look artificially ambiguous.
    """
    core = _core(token)
    suffix = token[len(core) :]
    if _LONG_CASTLE_RE.match(core):
        return ["O-O-O"]
    if _SHORT_CASTLE_RE.match(core):
        return ["O-O"]
    if core[:1] in "nbrqk":
        return [token, core[0].upper() + core[1:] + suffix]
    return [token]


def _normalization_variants(token: str) -> List[str]:
    """Cheap, purely mechanical rewrites of a *complete* token worth
    trying as a direct parse before any fuzzy search: everything
    `_case_fix_variants` covers, plus a capture or move missing its
    destination rank digit -- "cxd" -> "cxd4". Used by
    `cheap_structural_fix`, which only ever sees whole tokens.
    """
    variants = list(_case_fix_variants(token))
    seen = set(variants)

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            variants.append(text)

    for text in list(variants):
        core = _core(text)
        suffix = text[len(core) :]
        if core and core[-1] in "abcdefgh":
            for rank in "12345678":
                add(core + rank + suffix)

    return variants


def cheap_structural_fix(
    board: "chess.Board", token: str
) -> Optional[Tuple[str, "chess.Move", str]]:
    """Deterministically fix a small set of extremely common, purely
    mechanical transcription errors, without any fuzzy search -- see
    `_normalization_variants`. Returns (corrected_text, move, san) only
    when the fix is unambiguous (exactly one legal move results);
    otherwise returns None and leaves the token to the fuzzy search,
    which is better equipped to weigh genuinely ambiguous cases.
    """
    hits: List[Tuple[str, "chess.Move"]] = []
    for text in _normalization_variants(token):
        try:
            hits.append((text, board.parse_san(text)))
        except ValueError:
            pass

    if len(hits) == 1:
        text, mv = hits[0]
        san = board.san(mv)
        return text, mv, san
    return None


def cheap_defuse_fix(
    board: "chess.Board", token: str
) -> Optional[Tuple[Tuple[str, "chess.Move", str], Tuple[str, "chess.Move", str]]]:
    """Mirror image of the split-token merge fix: sometimes *two* moves get
    fused into a single token because the writer dropped the space between
    them, e.g. "Rd6+Kd7" instead of "Rd6+ Kd7" -- and each half may *also*
    have lost its capitalization, e.g. "rd6+kd7". Each half is tried both
    as written and case-fixed (see `_case_fix_variants` for why not also
    rank-completed here). If there is exactly one way to split the token
    into two pieces that both parse as legal moves, one right after the
    other, treat it as that pair of moves. Returns
    ((left_text, move, san), (right_text, move, san)) or None.
    """
    hits = []
    for split in range(2, len(token) - 1):
        left_raw, right_raw = token[:split], token[split:]
        for left in _case_fix_variants(left_raw):
            try:
                mv1 = board.parse_san(left)
            except ValueError:
                continue
            b2 = board.copy(stack=False)
            b2.push(mv1)
            for right in _case_fix_variants(right_raw):
                try:
                    mv2 = b2.parse_san(right)
                except ValueError:
                    continue
                hits.append((left, mv1, right, mv2))

    if len(hits) == 1:
        left, mv1, right, mv2 = hits[0]
        san1 = board.san(mv1)
        b2 = board.copy(stack=False)
        b2.push(mv1)
        san2 = b2.san(mv2)
        return (left, mv1, san1), (right, mv2, san2)
    return None


def cheap_defuse_peel(
    board: "chess.Board", token: str
) -> Optional[Tuple[str, "chess.Move", str, str]]:
    """A softer fallback for a fused token when `cheap_defuse_fix` can't
    confirm *both* halves: sometimes only the first move survived the
    fusion intact and the second is *also* garbled or genuinely wrong
    (e.g. "Rd6+kd7" where the real reply wasn't Kd7 at all -- Kd7 doesn't
    even escape the check from Rd6). If there is exactly one legal move
    that can be peeled off the front, it's still worth committing to that
    one confidently-identified move and handing the leftover text back
    to the caller as a fresh token -- to go through the *entire* normal
    pipeline (structural fixes, then fuzzy search) on its own, rather
    than being forced through a same-token, case-only check.

    Distinct split points that happen to name the *same* move (e.g. "rd6"
    and "rd6+" both meaning Rd6) aren't treated as ambiguous; among those
    the longest is preferred, since it cleanly absorbs a trailing "+"/"#"
    into the identified move instead of leaking it into the leftover.
    Returns (left_text, move, san, leftover_text) or None.
    """
    by_move: dict = {}
    for split in range(2, len(token)):
        left_raw, leftover = token[:split], token[split:]
        if not leftover:
            continue
        for left in _case_fix_variants(left_raw):
            try:
                mv = board.parse_san(left)
            except ValueError:
                continue
            by_move.setdefault(mv.uci(), []).append((split, left, mv, leftover))

    if len(by_move) != 1:
        return None
    (candidates,) = by_move.values()
    candidates.sort(key=lambda c: -c[0])  # longest split first
    _split, left, mv, leftover = candidates[0]
    san = board.san(mv)
    return left, mv, san, leftover


def ranked_candidates(
    board: "chess.Board", token: str, top_k: Optional[int] = None
) -> List[Tuple[float, "chess.Move", str]]:
    """All legal moves in ``board``, ranked by similarity to ``token``.

    ``top_k`` of ``None`` (or <= 0) means "no cap" -- every legal move in
    the position is returned, most-plausible first. Real chess positions
    rarely have more than a few dozen legal moves, so this is cheap and,
    unlike a small fixed cap, can never accidentally exclude the move that
    was actually played.
    """
    scored = []
    for mv in board.legal_moves:
        san = board.san(mv)
        scored.append((similarity_score(token, san), mv, san))
    scored.sort(key=lambda t: -t[0])
    if top_k is not None and top_k > 0:
        return scored[:top_k]
    return scored


def _parse_san_tolerant(board: "chess.Board", token: str) -> Optional["chess.Move"]:
    """Parse ``token`` against ``board``, tolerating the same cheap,
    purely mechanical typos (`_normalization_variants`) that
    `cheap_structural_fix` already fixes deterministically elsewhere.

    `lookahead_matches` needs this: it's the tool's main way of judging
    whether a *candidate* correction is right, by checking whether the
    rest of the transcript goes on making sense afterwards -- but in a
    transcript with a systematic error (every piece letter lowercased,
    say), a literal, un-normalized parse of those future tokens would
    fail on the very first one regardless of position, making lookahead
    uninformative for the entire rest of a badly-transcribed game. Since
    these particular fixes are only ever taken when they're unambiguous
    (exactly one legal move matches), applying them here first is no
    less trustworthy than applying them at the point the ply is actually
    corrected -- it just lets that same tolerance inform *scoring*, not
    only the final fix.
    """
    for variant in _normalization_variants(token):
        try:
            return board.parse_san(variant)
        except ValueError:
            continue
    return None


def lookahead_matches(
    board: "chess.Board", future_tokens: List[str], depth: Optional[int] = None
) -> int:
    """How many of the next ``future_tokens`` parse cleanly, one after
    another, starting from ``board``. ``depth`` of ``None`` (or <= 0)
    means "check all the way to the end of the transcript" -- the most
    thorough (and most reliable) way to score a candidate correction,
    since a candidate that only happens to fit the next move or two can
    still be wrong. A genuinely bad candidate almost always fails to
    parse the very next token anyway, so this rarely costs more in
    practice than a short lookahead would.
    """
    b = board.copy(stack=False)
    matched = 0
    limit = len(future_tokens) if depth is None or depth <= 0 else min(depth, len(future_tokens))
    for tok in future_tokens[:limit]:
        mv = _parse_san_tolerant(b, tok)
        if mv is None:
            break
        b.push(mv)
        matched += 1
    return matched


def best_single_correction(
    board: "chess.Board",
    token: str,
    future_tokens: List[str],
    lookahead_depth: Optional[int],
    top_k: Optional[int],
) -> Optional[Tuple[int, float, "chess.Move", str]]:
    """Pick the best legal move to substitute for ``token``.

    Every legal move in the position is tried (by default); each is
    scored primarily by how many future moves it lets the transcript go
    on matching, with text similarity as a tie-breaker. Returns
    (lookahead_matches, similarity, move, san, la_margin, sim_margin), or
    None if the position has no legal moves at all.

    Two different moves can easily "explain" exactly the same number of
    future moves -- e.g. two different quiet developing moves rarely
    interact with anything said about later, unrelated pieces -- so a
    lookahead tie is common and doesn't by itself mean the choice is
    uncertain. ``la_margin`` is how far this candidate's lookahead beat
    the next-best *distinct* lookahead score; ``sim_margin`` is, among
    the candidates tied on lookahead, how far this one's text similarity
    beat the next-best of those. A large margin of either kind is strong,
    independent evidence -- lookahead margin says "no other move fits
    what happens later nearly this well"; similarity margin says "of the
    several moves that fit equally well, the text overwhelmingly points
    to this one" (e.g. a one-character typo).
    """
    candidates = ranked_candidates(board, token, top_k=top_k)
    if not candidates:
        return None
    scored = []
    for sim, mv, san in candidates:
        b2 = board.copy(stack=False)
        b2.push(mv)
        la = lookahead_matches(b2, future_tokens, lookahead_depth)
        scored.append((la, sim, mv, san))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    best_la, best_sim, best_mv, best_san = scored[0]

    tie_group_sims = sorted((s[1] for s in scored if s[0] == best_la), reverse=True)
    if len(tie_group_sims) > 1:
        la_margin = 0
        sim_margin = tie_group_sims[0] - tie_group_sims[1]
    else:
        distinct_las = sorted({s[0] for s in scored}, reverse=True)
        la_margin = best_la - distinct_las[1] if len(distinct_las) > 1 else best_la
        sim_margin = None

    return best_la, best_sim, best_mv, best_san, la_margin, sim_margin


def classify_confidence(
    la_matches: int,
    sim: float,
    remaining_future: int,
    la_margin: Optional[int] = None,
    sim_margin: Optional[float] = None,
) -> str:
    """How much to trust a correction.

    Lookahead now typically runs to the end of the whole transcript, so
    `remaining_future` can be large and often includes *other*, unrelated
    errors further down the game -- those truncate every earlier
    candidate's lookahead count at the same point, regardless of how
    right that earlier candidate is. So confidence leans on whichever
    signal actually discriminates here: either this candidate explains
    clearly more of the future than any other (`la_margin`), or -- when
    several candidates are tied on lookahead because they don't interact
    with anything checkable later -- this one's text is a clearly better
    match than the other equally-plausible candidates (`sim_margin`).
    """
    if remaining_future == 0:
        # Nothing left to check against (end of game) -- go by text similarity.
        return "medium" if sim >= 0.8 else "low"
    if la_matches >= remaining_future:
        return "high"  # explains literally everything that follows
    if la_margin is not None and la_margin >= 3 and la_matches >= 3:
        return "high"  # far and away the best-fitting candidate
    if sim_margin is not None and sim_margin >= 0.3 and la_matches >= 1:
        return "high"  # clearly the best textual match among equally-consistent moves
    if (
        la_matches >= 6
        or (la_margin is not None and la_margin >= 2 and la_matches >= 2)
        or (sim_margin is not None and sim_margin >= 0.15)
        or sim >= 1.0
    ):
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# 3. The correction engine
# --------------------------------------------------------------------------


@dataclass
class Ply:
    token: str            # original text as it appeared in the input
    move: "chess.Move"
    san: str              # correct SAN once `move` is known
    flagged: bool          # True if this ply differs from the raw input
    confidence: str        # "exact" | "high" | "medium" | "low" | "retro"
    note: str = ""
    # True for a ply that resulted from splitting/merging raw tokens (a
    # fused or space-split move). `token` here is already just this ply's
    # own half, which can coincidentally equal `san` exactly (e.g. a fused
    # move's cleanly-identified first half needed no further correction of
    # its own) -- but the ply is still worth reporting, since a *raw
    # recorded token* was restructured to produce it. Sidesteps
    # `_build_report`'s usual "token vs san differ only by punctuation"
    # skip, which is meant for a single untouched token, not this case.
    always_report: bool = False
    # "formatting": the recorded text and the actual move are the same
    #   move -- just miscased, missing a digit, or split/fused across
    #   token boundaries. Found deterministically (exactly one legal move
    #   matches), never by fuzzy guessing.
    # "substantive": the recorded text didn't identify a legal move at
    #   all (or was legal but demonstrably not what was played), and a
    #   different move had to be picked via similarity/lookahead scoring
    #   or retroactive re-search. This is where a human review matters
    #   most, since it reflects real uncertainty rather than a mechanical
    #   fix.
    # "unresolved": no candidate could be found at all.
    category: str = "substantive"
    # A few words for a table's Note column -- only set when there's
    # something worth flagging beyond what the Original/Corrected columns
    # already show (e.g. "fused tokens"); blank otherwise.
    short_note: str = ""
    # The move-number label this ply was recorded under in the *source*
    # file (e.g. "36"), if any -- kept so a reported correction can be
    # found by searching the original file for that same number, even if
    # the source's own numbering has a gap or a typo somewhere earlier.
    recorded_label: Optional[str] = None


@dataclass
class Correction:
    ply_number: int        # 1-based ply index in the game
    move_number: int
    color: str             # "White" | "Black"
    original: str
    corrected: str
    confidence: str
    note: str
    category: str          # "formatting" | "substantive" | "unresolved"
    short_note: str = ""


def _move_number_and_color(ply_index: int, recorded_label: Optional[str] = None) -> Tuple[int, str]:
    """ply_index is 0-based. Which side moved is always determined by ply
    order (strictly alternating) and never by the source's own numbering.
    The move *number* shown, though, prefers the label actually recorded
    in the source over a silent recount, so a correction can be found by
    searching the user's own file for that same number -- a gap or typo
    earlier in the source's numbering (a skipped "35.", say) then simply
    carries forward, matching the page, rather than drifting out of sync
    with it.
    """
    color = "White" if ply_index % 2 == 0 else "Black"
    if recorded_label is not None:
        try:
            return int(recorded_label), color
        except ValueError:
            pass
    return ply_index // 2 + 1, color


class GameCorrector:
    """Robust-by-default settings: every legal move is considered at the
    point of the error itself (``top_k=None``), scored against the *entire
    rest of the transcript* (``lookahead_depth=None``), and up to
    ``backtrack_window`` prior plies are always re-examined jointly with
    the failing move whenever the single-move fix doesn't fully explain
    what follows. This is deliberately thorough rather than fast -- for
    a typical game (well under a thousand plies) it still finishes in a
    few seconds, because an implausible candidate almost always fails to
    parse the very next move and the lookahead scan aborts immediately;
    the expensive, exhaustive search is only ever spent on the plies that
    actually contain an error.
    """

    def __init__(
        self,
        lookahead_depth: Optional[int] = None,
        top_k: Optional[int] = None,
        window_top_k: int = 12,
        backtrack_window: int = 3,
        window_time_budget: float = 60.0,
    ):
        self.lookahead_depth = lookahead_depth
        self.top_k = top_k
        self.window_top_k = window_top_k
        self.backtrack_window = backtrack_window
        # Safety valve: the retroactive/window search is meant for the rare
        # earlier-move-was-legal-but-wrong case. A transcript with very
        # frequent errors (e.g. every piece letter lowercased throughout --
        # though `cheap_structural_fix` should catch that particular case
        # before this ever runs) could otherwise make it re-run over and
        # over on low-quality history and take an unreasonable amount of
        # time. Once this many seconds have been spent inside
        # `_window_search` in total, it is skipped for the rest of the
        # game; the fast forward/merge/cheap fixes are never affected.
        self.window_time_budget = window_time_budget
        self._window_time_used = 0.0
        self._window_budget_exhausted_noted = False

    # -- window (retroactive) search -------------------------------------
    def _window_search(
        self,
        board_before: "chess.Board",
        window_tokens: List[str],
        trailing_tokens: List[str],
    ) -> Optional[Tuple[int, float, List[Tuple["chess.Move", str, float]]]]:
        """Try combinations of moves for `window_tokens` (a short run of
        consecutive plies, replayed from `board_before`), scored first by
        how far `trailing_tokens` keep parsing afterwards, and by average
        text similarity as a tie-breaker. Branches on the top
        `window_top_k` candidates at each ply -- bounded so that widening
        `backtrack_window` stays tractable even though the primary
        single-move search below considers every legal move.
        """
        best: dict = {"la": -1, "sim": -1.0, "assign": None}

        def dfs(pos: int, board: "chess.Board", assign: list, sim_acc: float):
            if pos == len(window_tokens):
                la = lookahead_matches(board, trailing_tokens, self.lookahead_depth)
                sim_avg = sim_acc / len(window_tokens)
                if (la, sim_avg) > (best["la"], best["sim"]):
                    best["la"] = la
                    best["sim"] = sim_avg
                    best["assign"] = list(assign)
                return
            token = window_tokens[pos]
            for sim, mv, san in ranked_candidates(board, token, top_k=self.window_top_k):
                b2 = board.copy(stack=False)
                b2.push(mv)
                assign.append((mv, san, sim))
                dfs(pos + 1, b2, assign, sim_acc + sim)
                assign.pop()

        dfs(0, board_before.copy(stack=False), [], 0.0)
        if best["assign"] is None:
            return None
        return best["la"], best["sim"], best["assign"]

    # -- main entry point --------------------------------------------------
    def correct(
        self, tokens: List[str], labels: Optional[List[Optional[str]]] = None
    ) -> Tuple["chess.Board", List[Ply], List[Correction]]:
        board = chess.Board()
        history: List[Ply] = []
        tokens = list(tokens)  # local, mutable copy -- `cheap_defuse_peel` can insert into it
        # Parallel to `tokens` -- the move-number label (if any) each token
        # was recorded under in the source, kept in lockstep with every
        # insert/consume below so a reported correction can still cite the
        # number as it appears on the user's own page.
        labels = list(labels) if labels is not None else [None] * len(tokens)
        i = 0

        while i < len(tokens):
            token = tokens[i]
            label = labels[i]

            # 1. Try the token exactly as recorded.
            try:
                mv = board.parse_san(token)
                san = board.san(mv)
                board.push(mv)
                history.append(Ply(token, mv, san, flagged=False, confidence="exact",
                                    recorded_label=label))
                i += 1
                continue
            except ValueError:
                pass

            # 1b. Deterministic, structural fixes (piece-letter/castling case,
            #     missing destination-rank digit) -- cheap, unambiguous, and
            #     handled before any fuzzy search. This is what keeps a
            #     transcript with systematic errors (e.g. every piece letter
            #     lowercased by a scanner) fast and correct, instead of
            #     making every single ply fall through to the expensive path.
            structural = cheap_structural_fix(board, token)
            if structural is not None:
                text, mv, san = structural
                board.push(mv)
                history.append(
                    Ply(token, mv, san, flagged=True, confidence="high",
                        note=f"formatting fix: recorded as '{token}', actually '{san}'",
                        category="formatting", recorded_label=label)
                )
                i += 1
                continue

            # 1c. Mirror image of the split-token merge below: two moves
            #     fused into one token because a space was dropped, e.g.
            #     "Rd6+Kd7" instead of "Rd6+ Kd7". Only taken when there is
            #     exactly one way to split the token into two legal moves.
            defused = cheap_defuse_fix(board, token)
            if defused is not None:
                (ltext, lmv, lsan), (rtext, rmv, rsan) = defused
                board.push(lmv)
                history.append(
                    Ply(ltext, lmv, lsan, flagged=True, confidence="high",
                        note=f"one token appears to be two moves fused together (split from '{token}')",
                        always_report=True, category="formatting", short_note="fused 1/2",
                        recorded_label=label)
                )
                board.push(rmv)
                history.append(
                    Ply(rtext, rmv, rsan, flagged=True, confidence="high",
                        note="second half of a fused token", always_report=True,
                        category="formatting", short_note="fused 2/2", recorded_label=label)
                )
                i += 1
                continue

            # 1d. Softer fallback for a fused token: even if the *second*
            #     half doesn't check out (it may be independently garbled,
            #     or genuinely wrong, not just fused), peel off the one
            #     confidently-identified move at the front and feed the
            #     remainder back in as its own token, to go through this
            #     entire pipeline -- including the fuzzy search below --
            #     on its own rather than being forced into a same-token,
            #     case-only check.
            peeled = cheap_defuse_peel(board, token)
            if peeled is not None:
                ltext, lmv, lsan, leftover = peeled
                board.push(lmv)
                history.append(
                    Ply(ltext, lmv, lsan, flagged=True, confidence="high",
                        note=f"one confidently-identified move peeled off the front of '{token}'",
                        always_report=True, category="formatting", short_note="peeled from fusion",
                        recorded_label=label)
                )
                tokens.insert(i + 1, leftover)
                labels.insert(i + 1, label)  # leftover was recorded under the same label
                i += 1
                continue

            future = tokens[i + 1 :]
            options: List[dict] = []

            # 2. The direct fix: substitute the best legal move for this
            #    one token, considering every legal move in the position.
            forward = best_single_correction(board, token, future, self.lookahead_depth, self.top_k)
            if forward is not None:
                la, sim, mv, san, la_margin, sim_margin = forward
                options.append({"kind": "forward", "la": la, "sim": sim, "mv": mv, "san": san,
                                 "la_margin": la_margin, "sim_margin": sim_margin})

            # 3. Cheap fix for a move split across two tokens by a stray
            #    space (e.g. "N" "f3" instead of "Nf3"). This consumes the
            #    *next* token too, so it is checked against the correctly
            #    reduced trailing tokens (tokens[i+2:]), not `future`.
            has_merge = False
            if i + 1 < len(tokens):
                merged = token + tokens[i + 1]
                try:
                    mv = board.parse_san(merged)
                    san = board.san(mv)
                    b2 = board.copy(stack=False)
                    b2.push(mv)
                    la = lookahead_matches(b2, tokens[i + 2 :], self.lookahead_depth)
                    options.append({"kind": "merge", "la": la, "sim": 1.5, "mv": mv, "san": san})
                    has_merge = True
                except ValueError:
                    pass

            # 4. Retroactive fix: maybe an earlier, individually-legal move
            #    wasn't actually the move played, and only now does that
            #    stop making sense. Re-open the last 1..backtrack_window
            #    plies together with this one and jointly re-search them,
            #    always scored against the same "rest of the transcript".
            #
            #    Skipped when a merge is available: a token that cleanly
            #    merges with its neighbour into a valid move is a strong,
            #    structural explanation, and `future` here still contains
            #    that neighbour token uninterpreted -- searching it as if
            #    it were a genuine next ply can otherwise make some
            #    unrelated rewrite of earlier, correct moves look falsely
            #    "consistent" purely by coincidence.
            if not has_merge and self._window_time_used < self.window_time_budget:
                max_w = min(self.backtrack_window, len(history))
                for w in range(1, max_w + 1):
                    if self._window_time_used >= self.window_time_budget:
                        break
                    saved = history[-w:]
                    for _ in range(w):
                        board.pop()
                    window_tokens = [p.token for p in saved] + [token]
                    window_labels = [p.recorded_label for p in saved] + [label]
                    _t0 = time.monotonic()
                    result = self._window_search(board, window_tokens, future)
                    self._window_time_used += time.monotonic() - _t0
                    for p in saved:  # restore -- we only commit the winner below
                        board.push(p.move)
                    if result is not None:
                        la, sim_avg, assign = result
                        options.append(
                            {"kind": "window", "w": w, "la": la, "sim": sim_avg,
                             "assign": assign, "window_tokens": window_tokens,
                             "window_labels": window_labels}
                        )

            if not options:
                history.append(
                    Ply(token, chess.Move.null(), token, flagged=True, confidence="low",
                        note="no legal move available in this position; left unresolved",
                        category="unresolved", short_note="no legal move", recorded_label=label)
                )
                break

            # Pick the best move that leaves history untouched (forward or
            # merge). A window/retroactive option is only ever allowed to
            # win if it *strictly* explains more of the rest of the
            # transcript than that -- its similarity score is an average
            # over several plies and not on the same scale as a single
            # token's, so it must never be used to break a tie against a
            # simpler fix. This is what stops the tool from "fixing"
            # moves that were already correct.
            direct = [o for o in options if o["kind"] in ("forward", "merge")]
            direct.sort(key=lambda o: (-o["la"], -o["sim"]))
            window = [o for o in options if o["kind"] == "window"]

            best_direct_la = direct[0]["la"] if direct else -1
            better_windows = [o for o in window if o["la"] > best_direct_la]
            if better_windows:
                # Prefer the smallest window that achieves the best la, then
                # the highest average similarity.
                better_windows.sort(key=lambda o: (-o["la"], o["w"], -o["sim"]))
                chosen = better_windows[0]
            elif direct:
                chosen = direct[0]
            else:
                # Nothing but window options exist (e.g. no legal moves at
                # all for a direct fix) -- take the best of those.
                window.sort(key=lambda o: (-o["la"], o["w"], -o["sim"]))
                chosen = window[0]
            remaining = len(future)

            if chosen["kind"] == "forward":
                confidence = classify_confidence(
                    chosen["la"], chosen["sim"], remaining,
                    chosen.get("la_margin"), chosen.get("sim_margin"),
                )
                board.push(chosen["mv"])
                history.append(Ply(token, chosen["mv"], chosen["san"], flagged=True,
                                    confidence=confidence, note=self._explain(token, chosen["san"]),
                                    category="substantive", recorded_label=label))
                i += 1
            elif chosen["kind"] == "merge":
                confidence = "high" if chosen["la"] else "medium"
                board.push(chosen["mv"])
                history.append(Ply(token + " " + tokens[i + 1], chosen["mv"], chosen["san"],
                                    flagged=True, confidence=confidence,
                                    note="two tokens appear to be one move split by a stray space",
                                    always_report=True, category="formatting", short_note="split tokens",
                                    recorded_label=label))
                i += 2
            else:  # "window": revise the last `w` plies plus this one
                w = chosen["w"]
                for _ in range(w):
                    board.pop()
                del history[-w:]
                assign = chosen["assign"]
                window_tokens = chosen["window_tokens"]
                window_labels = chosen["window_labels"]
                for idx, (mv, san, sim) in enumerate(assign):
                    board.push(mv)
                    original_token = window_tokens[idx]
                    changed = _core(san) != _core(original_token)
                    history.append(
                        Ply(original_token, mv, san,
                            flagged=changed or idx == len(assign) - 1,
                            confidence="retro" if changed else "exact",
                            note="revised after looking ahead in the transcript" if changed else "",
                            category="substantive",
                            short_note="revised earlier move" if changed else "",
                            recorded_label=window_labels[idx])
                    )
                i += 1

        corrections = self._build_report(history)
        return board, history, corrections

    @staticmethod
    def _explain(token: str, san: str) -> str:
        if _core(token) == _core(san):
            return "formatting only (check/mate marker)"
        return f"recorded as '{token}', actually '{san}'"

    @staticmethod
    def _build_report(history: List[Ply]) -> List[Correction]:
        report = []
        for idx, ply in enumerate(history):
            if not ply.flagged:
                continue
            if not ply.always_report and _core(ply.token) == _core(ply.san) and " " not in ply.token:
                # Only a check/mate marker differed -- not worth reporting.
                continue
            move_number, color = _move_number_and_color(idx, ply.recorded_label)
            report.append(
                Correction(
                    ply_number=idx + 1,
                    move_number=move_number,
                    color=color,
                    original=ply.token,
                    corrected=ply.san,
                    confidence=ply.confidence,
                    note=ply.note,
                    category=ply.category,
                    short_note=ply.short_note,
                )
            )
        return report


# --------------------------------------------------------------------------
# 4. Writing the corrected PGN + report back out
# --------------------------------------------------------------------------


def render_movetext(history: List[Ply], line_width: int = 80) -> str:
    parts = []
    for idx, ply in enumerate(history):
        if idx % 2 == 0:
            parts.append(f"{idx // 2 + 1}.")
        parts.append(ply.san)

    lines, cur = [], ""
    for part in parts:
        candidate = f"{cur} {part}".strip()
        if len(candidate) > line_width and cur:
            lines.append(cur)
            cur = part
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return "\n".join(lines)


_CATEGORY_LABELS = {
    "formatting": "formatting",       # same move, just miscased/incomplete/split notation
    "substantive": "substantive",     # a different move had to be identified
    "unresolved": "unresolved",       # no candidate could be found at all
}


def render_report(corrections: List[Correction], total_plies: int) -> str:
    if not corrections:
        return "No errors found -- every recorded move was legal as written.\n"

    formatting = [c for c in corrections if c.category == "formatting"]
    substantive = [c for c in corrections if c.category == "substantive"]
    unresolved = [c for c in corrections if c.category == "unresolved"]

    summary_bits = []
    if formatting:
        summary_bits.append(f"{len(formatting)} formatting-only")
    if substantive:
        summary_bits.append(f"{len(substantive)} substantive")
    if unresolved:
        summary_bits.append(f"{len(unresolved)} unresolved")

    lines = [
        f"Found {len(corrections)} move(s) that needed correction "
        f"(out of {total_plies} plies total): " + ", ".join(summary_bits) + ".",
        "Type: formatting = same move, notation fixed | substantive = a different move "
        "was identified | unresolved = no candidate found.",
        "Move numbers match the source file's own numbering, even where it has a gap.",
        "",
    ]

    # Move number shown the way PGN itself would write it ("12." for White,
    # "12..." for Black) -- doubles as the color column, so the table stays
    # narrow without losing information.
    headers = ["Move", "Type", "Conf", "Original", "Corrected", "Note"]
    rows = []
    for c in corrections:
        move_ref = f"{c.move_number}{'.' if c.color == 'White' else '...'}"
        label = _CATEGORY_LABELS.get(c.category, c.category)
        rows.append([move_ref, label, c.confidence, c.original, c.corrected, c.short_note])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: List[str]) -> str:
        # Move number reads better right-aligned; everything else left-aligned.
        parts = [cells[0].rjust(widths[0])]
        parts += [cells[i].ljust(widths[i]) for i in range(1, len(cells))]
        return "  ".join(parts).rstrip()

    lines.append(fmt_row(headers))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(fmt_row(row))
    lines.append("")

    if substantive:
        lines.append(
            f"Note: the {len(substantive)} substantive row(s) above are where a different "
            "move had to be identified (not just a notation fix) -- worth a closer look by hand."
        )
    low_conf = [c for c in corrections if c.confidence == "low"]
    if low_conf:
        lines.append(
            f"Note: {len(low_conf)} row(s) above are low-confidence -- "
            "please double check them by hand."
        )
    if unresolved:
        lines.append(
            f"Note: {len(unresolved)} move(s) could not be resolved at all -- "
            "no legal move was found to substitute."
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0])
    parser.add_argument("input", help="Path to the PGN (or plain movetext) file to correct")
    parser.add_argument("-o", "--output", default=None, help="Where to write the corrected PGN")
    parser.add_argument("-r", "--report", default=None, help="Where to write the correction report")
    parser.add_argument(
        "--lookahead", type=int, default=0,
        help="How many future plies to check when scoring a correction. "
             "0 (default) means unlimited: check all the way to the end of the transcript, "
             "which is the most reliable way to identify the right fix. Set a smaller "
             "number only to speed up correction of very long, heavily-corrupted games.",
    )
    parser.add_argument(
        "--top-k", type=int, default=0,
        help="How many candidate moves to consider at the exact point of an error. "
             "0 (default) means unlimited: every legal move in the position is considered, "
             "so the right fix can never be excluded by this cutoff.",
    )
    parser.add_argument(
        "--window-top-k", type=int, default=12,
        help="How many candidate moves to consider per ply when re-examining earlier, "
             "individually-legal moves that turn out to be wrong (default: 12). This search "
             "branches per ply reconsidered, so it stays capped even though --top-k does not.",
    )
    parser.add_argument(
        "--backtrack", type=int, default=3,
        help="How many prior plies to jointly reconsider, together with the current one, "
             "when a move doesn't fully explain the rest of the game (default: 3). Higher "
             "values catch more subtle, earlier mistakes at the cost of more search time.",
    )
    parser.add_argument(
        "--window-time-budget", type=float, default=60.0,
        help="Total seconds the retroactive/backtracking search (--backtrack) is allowed to "
             "spend across the whole game (default: 60). Once used up, it's skipped for any "
             "remaining errors -- the fast direct-fix and formatting-fix paths are unaffected. "
             "This only guards against pathological inputs; a normal, sparsely-erroneous game "
             "won't come close to it.",
    )
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as f:
        raw = f.read()

    headers, movetext = split_headers_and_movetext(raw)
    tokens, labels = tokenize_movetext(movetext)
    if not tokens:
        print("No moves found in input file.", file=sys.stderr)
        return 1

    engine = GameCorrector(
        lookahead_depth=args.lookahead or None,
        top_k=args.top_k or None,
        window_top_k=args.window_top_k,
        backtrack_window=args.backtrack,
        window_time_budget=args.window_time_budget,
    )
    board, history, corrections = engine.correct(tokens, labels)

    corrected_movetext = render_movetext(history)
    result = board.result() if board.is_game_over() else "*"
    output_pgn = ""
    if headers.strip():
        output_pgn += headers.strip() + "\n\n"
    output_pgn += corrected_movetext + f" {result}\n"

    out_path = args.output or (args.input + ".corrected.pgn")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output_pgn)

    report_text = render_report(corrections, total_plies=len(history))
    report_path = args.report or (args.input + ".report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print(f"Corrected PGN written to: {out_path}")
    print(f"Report written to:        {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
