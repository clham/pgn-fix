# pgn-fix

Finds moves in a recorded chess game (PGN-style algebraic notation) that
could not actually have been played, and figures out what was probably
really played, by checking each move against the actual board position
and, when a move doesn't fit, looking ahead at how the rest of the
transcript plays out.

## Install

```
pip install -e .
```

This pulls in its only dependency, [`python-chess`](https://python-chess.readthedocs.io/)
(pure Python, no engine needed). Requires Python 3.9+.

## Usage

```
pgn-fix input.pgn -o corrected.pgn -r report.txt
```

(equivalently: `python -m pgn_fix input.pgn -o corrected.pgn -r report.txt`)

Or use it as a library:

```python
from pgn_fix import GameCorrector, split_headers_and_movetext, tokenize_movetext

headers, movetext = split_headers_and_movetext(pgn_text)
tokens, labels = tokenize_movetext(movetext)
board, history, corrections = GameCorrector().correct(tokens, labels)
```

- `input.pgn` — a file with PGN headers (optional) and movetext, e.g.
  `1. e4 e5 2. Nf3 Nc6 ...`. Comments, NAGs, and side variations are
  stripped automatically; only the mainline is corrected.
- `-o/--output` — where to write the corrected PGN (default:
  `input.pgn.corrected.pgn`)
- `-r/--report` — where to write a table listing every correction made,
  with the move number, a **type** (see below), a confidence level,
  the original text, the corrected text, and a short note (default:
  `input.pgn.report.txt`)

The report is a plain-text table, e.g.:

```
 Move  Type         Conf    Original  Corrected  Note
-----  -----------  ------  --------  ---------  ------------------
   6.  formatting   high    o-o       O-O
 6...  formatting   high    bf6       Bf6
  41.  formatting   high    kf2       Kf2
41...  substantive  low     nb2       Re2+
  47.  formatting   high    Rd6+      Rd6+       peeled from fusion
47...  substantive  medium  kd7       Kc7
```

Every **substantive** (and unresolved) row also gets a "Why" entry after
the table, spelling out the concrete reason, e.g.:

```
Why:
  34. 'b4+' -> 'a4': 'b4+/Kb6' is legal here, but only lets 11 of the next 28
      recorded move(s) that follow make sense afterwards -- keeping it, the
      next move that stops making sense is recorded move 12 of what follows
      ('Nb2'): no black knight can reach b2 from here. Reading this instead
      as 'a4/Kb4' lets 23 of them parse -- strong evidence this is what was
      actually played.
```

Formatting fixes don't get one, since they're deterministic (exactly one
legal move matched some cheap, mechanical rewrite) and don't need
justifying. For a substantive fix, it says why the recorded text doesn't
play here at all (no piece of that type can reach the square, more than one
could and the notation doesn't say which, or a missing digit that no
completion of it fixes). For a revised-earlier-move ("retro") correction, it
names the *specific* later recorded move that exposes the original reading
as wrong, and how much more of the game the revision explains.

The move number is written the way PGN itself would ("12." for White,
"12..." for Black), so it also serves as the color column. It always
matches the *source file's own* numbering — including any gap or typo
in that numbering — rather than a silently recomputed count, so you can
find a row by searching your original file for that same number, even
several moves after a numbering mistake earlier in the game (only which
*side* moved is ever inferred structurally, never from the printed
number, so a gap in the source's own labels can't desync the game
itself).

Every correction is labeled with a type, so you can tell at a glance
which ones are worth a closer look:

- **formatting** — the recorded text and the actual move are the *same
  move*: it was just miscased ("bc4" for "Bc4"), missing a digit ("cxd"
  for "cxd4"), or split/fused across token boundaries ("N" "f3" for
  "Nf3"). These are found deterministically — exactly one legal move
  matches — never by guessing, so they're effectively certain.
- **substantive** — the recorded text didn't identify a legal move at
  all, or was legal but demonstrably not the move actually played, so a
  *different* move had to be identified by scoring candidates against
  the rest of the game. This is where the tool's judgment call actually
  lives, and where a human review matters most.
- **unresolved** — no candidate could be found at all; left as originally
  recorded.

The report's summary line and per-move confidence levels both still
apply within each type — a formatting fix can still be "medium"
confidence if, say, a split-token merge only partially confirmed itself
against a short remaining transcript, and a substantive fix can still be
"high" confidence if it clearly explains everything that follows.

The defaults are tuned for correctness over speed:

- `--lookahead N` — how many future plies to check when scoring a
  possible correction. Default is **0, meaning unlimited**: every
  candidate is checked all the way to the end of the transcript, which
  is the most reliable way to identify the right fix. Only lower this if
  you need to speed up correcting a very long, heavily-corrupted game.
- `--top-k N` — how many candidate moves to consider at the point of an
  error. Default is **0, meaning unlimited**: every legal move in the
  position is considered, so the right fix can never be silently
  excluded by an arbitrary cutoff.
- `--backtrack N` — how many prior plies to jointly reconsider, together
  with the current one, whenever a fix doesn't fully explain what
  follows (default **3**, up from an earlier default of 2). This is what
  catches a move that was legal and unremarkable at the time but turns
  out not to be the move actually played.
- `--window-top-k N` — how many candidates to branch on per ply *inside*
  that retroactive search (default 12). This one stays capped (unlike
  `--top-k`) because the retroactive search's cost multiplies with every
  additional prior ply it reconsiders; 12 is generous without letting a
  `--backtrack 3` search take an unreasonable amount of time.

Because of these defaults, a single error can take a few seconds to
resolve on a full-length game — the tool always fully checks a
correction against the *entire rest of the transcript*, and (when
needed) reconsiders the last few moves jointly, rather than stopping
after a plausible-looking local match. Clean games with no errors are
unaffected and finish instantly, since none of this search ever runs
unless a move actually fails to parse.

## How it decides what a garbled move "really" was

1. Every move is first tried exactly as written. Most transcription
   quirks (missing "x", a stray "+", "0-0" instead of "O-O", a move
   number with no trailing period, etc.) are already tolerated and need
   no fixing.
2. A handful of extremely common, purely mechanical errors are fixed
   deterministically, with no fuzzy guessing, whenever the fix is
   unambiguous (exactly one legal move results):
   - a piece letter (or castling) that lost its capitalization -- "bc4"
     -> "Bc4", "ne7" -> "Ne7", "o-o" -> "O-O". This is extremely common
     out of OCR/scanner transcriptions, which often don't preserve case,
     and previously was a real source of *bad* corrections: the tool
     couldn't tell "b" meant Bishop rather than a pawn move, so it never
     gave the right candidate credit for matching.
   - a capture or move missing its destination rank digit -- "cxd" ->
     "cxd4", "fxf" -> "fxf3".
   - two moves fused into one token because a space was dropped --
     "Rd6+Kd7" -> "Rd6+" and "Kd7" as separate plies -- and, the mirror
     image, one move accidentally split into two tokens by a stray space
     -- "N" "f3" -> "Nf3". If only the *first* half of a fused token can
     be confidently identified (the second half may be independently
     garbled, or simply wrong), that one move is still peeled off and
     the leftover text is fed back in as its own token, to go through
     this entire process -- fuzzy search included -- on its own.
3. Only once those are ruled out does the tool fall back to fuzzy
   matching: every legal move available is tried (not just a handful),
   each one scored by how many of the *remaining* recorded moves keep
   parsing correctly afterwards — checked all the way to the end of the
   transcript by default. The candidate that keeps the rest of the game
   consistent the longest is taken to be the move that was actually
   played — this is the "look ahead to see where the piece landed"
   strategy. This lookahead check tolerates the same cheap, unambiguous
   fixes as step 2 (case, missing rank digit) when checking *future*
   tokens too, not just the one currently being corrected — otherwise, in
   a transcript with a systematic error running through the rest of the
   game (every piece letter lowercased, say), the lookahead would fail on
   the very next token regardless of position and never provide any real
   signal at all. The scoring itself is weighted toward how these errors
   actually happen: a slip to a *neighbouring* square (Kd2 for Kd3, one
   file or rank off) scores far higher than a candidate that also
   requires changing the piece, since swapping the piece entirely is a
   much bigger, rarer kind of mistake than misreading a digit or a
   letter. A recorded check or mate mark ("+"/"#") is treated as close to
   authoritative in this scoring too -- noticing and writing one down
   takes real attention over the board, so it's rarely wrong the way a
   square or letter is -- so a candidate that actually delivers check
   when one was recorded is favored, and one that contradicts a recorded
   check or mate mark is heavily penalized. This is a strong signal, not
   an absolute one: it can still be outweighed if a candidate that
   disagrees with it explains dramatically more of the rest of the
   transcript, since that lookahead evidence is stronger still. When
   several different candidates are tied on lookahead (common for quiet
   moves that don't interact with anything checkable later), the one
   whose text most closely resembles what was recorded wins the tie.
4. Whenever the single-move fix above doesn't fully explain what
   follows, the tool separately checks whether the true error is
   actually a ply or two earlier — a move that was legal at the time,
   just not the move actually played, only becoming visible once a
   later move stops making sense. It reopens the last few plies (up to
   `--backtrack`, default 3) together with the current one and jointly
   re-searches that whole window, but only adopts a rewritten history if
   it demonstrably explains *more* of the rest of the transcript than
   leaving the earlier moves alone — never merely as good, so it won't
   rewrite a move that was already right. This search is capped by
   `--window-time-budget` (default 60 seconds total) so that a
   transcript with very many simultaneous errors can't run unbounded --
   though step 2 above is what should keep systematic errors (like an
   entire game in lowercase) fast and accurate in the first place,
   rather than relying on this fallback at all.

## Known limitations

- A move that is **both legal and looks fine on its own**, but wasn't
  actually the move played (e.g. the wrong one of two possible knights
  moved to the same square), is only detectable if it eventually causes
  a real contradiction later in the transcript. If the game is never
  played out far enough to expose the inconsistency, this class of error
  is invisible from the notation alone — this is a fundamental limit,
  not a bug.
- Whole missing or extra moves (not just garbled ones) aren't realigned
  automatically — the tool assumes one token per ply, with the single
  exception of a move accidentally split across two tokens.
- "High confidence" means *this correction keeps the rest of the
  transcript consistent*, not that it is provably the only possible
  original move — in very open positions, several legal moves can look
  equally consistent.

## Development

```
pip install -e ".[dev]"
pytest
```

Layout:

```
src/pgn_fix/
    corrector.py   -- tokenizer, scoring, the correction engine, and the CLI
    __init__.py    -- public API re-exports
    __main__.py    -- `python -m pgn_fix`
tests/
    data/
        sample_broken.pgn    -- small, hand-crafted demo with four seeded errors
        anonymized_game.pgn  -- a real club game (headers scrubbed; the
                                 garbled moves are untouched, since the
                                 transcription errors are the point)
    test_*.py
```
