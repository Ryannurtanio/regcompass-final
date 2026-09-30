"""One Unicode form for the canonical stream: NFC, with the offsets that follow.

The same letter can be stored two ways. "ề" is one code point in most files
and three in some (e, a circumflex, a grave), and a PDF built with the second
spelling reads as "Điều" on screen while matching no heading pattern, no quote
typed from the page and no Verbatim Quote copied from the composed form. The
stream is therefore composed once, where it is built, so words, chunks, quotes
and the word-for-word check all see one spelling.

Composing shortens the text, so every character offset that points into it
(page ranges, word boxes) has to move with it. The text is cut into pieces
that compose independently (a base character with the marks that follow it),
each piece is composed on its own, and an offset on a piece boundary maps
exactly. An offset that falls inside a piece, which a word box never does in
practice, maps outward to the piece's edge.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_left, bisect_right


def is_nfc(text: str) -> bool:
    return unicodedata.is_normalized("NFC", text)


class ComposedText:
    """The NFC form of one text and the offset map from the original."""

    def __init__(self, text: str):
        old: list[int] = [0]
        new: list[int] = [0]
        parts: list[str] = []
        piece_start = 0
        for i in range(1, len(text) + 1):
            if i < len(text) and not self._starts_a_piece(text, piece_start, i):
                continue
            composed = unicodedata.normalize("NFC", text[piece_start:i])
            parts.append(composed)
            old.append(i)
            new.append(new[-1] + len(composed))
            piece_start = i
        self.text = "".join(parts)
        self._old = old
        self._new = new

    @staticmethod
    def _starts_a_piece(text: str, piece_start: int, i: int) -> bool:
        """Whether position i opens a new piece: a character that combines with
        nothing before it. A combining mark never does; a base character does
        unless composition would fuse it with the piece before (Hangul jamo,
        a few Indic vowel pairs), which the check below catches without a
        table."""
        ch = text[i]
        if unicodedata.combining(ch):
            return False
        prev = text[piece_start:i]
        if ch.isascii() and prev.isascii():
            return True
        joined = unicodedata.normalize("NFC", prev + ch)
        return joined == unicodedata.normalize("NFC", prev) + unicodedata.normalize("NFC", ch)

    def start(self, pos: int) -> int:
        """Where an offset that opens a range lands in the composed text."""
        return self._new[bisect_right(self._old, pos) - 1]

    def end(self, pos: int) -> int:
        """Where an offset that closes a range lands in the composed text."""
        return self._new[min(bisect_left(self._old, pos), len(self._new) - 1)]
