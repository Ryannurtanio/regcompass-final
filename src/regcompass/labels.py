"""Provision labels in each Economy's own drafting word.

The chunker labels every provision "s. N" whatever the legal tradition, so a
Chinese 第四十条 and an Indonesian Pasal 40 both arrive as "s. 40". A legal
reviewer reads "s." as a common-law section; civil-law statutes number
Articles. The label is rewritten at export time only: the stored label, the
duplicate check and the citation comparison (which already treat "s. 40" and
"Art. 40" as one provision) are unchanged.
"""

from __future__ import annotations

import re

# Economies whose statutes number Articles: 条 (CN), Pasal (ID), ມາດຕາ (LA),
# Điều (VN), Статья (KZ, RU), зүйл (MN). Thailand's มาตรา is "Section" in its
# official English translations and stays "s.".
ARTICLE_ECONOMIES = frozenset({"CN", "ID", "LA", "VN", "KZ", "RU", "MN"})

_SECTION_WORD_RE = re.compile(r"(?<![A-Za-z])s\.\s*(?=\S)")


def drafting_label(label: str, economy: str | None) -> str:
    """ "s. N" as "Art. N" for an Economy that drafts in Articles; any other
    label, or any other Economy, comes back unchanged."""
    if not label or (economy or "").upper() not in ARTICLE_ECONOMIES:
        return label
    return _SECTION_WORD_RE.sub("Art. ", label)
