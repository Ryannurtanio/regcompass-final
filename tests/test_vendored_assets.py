"""The vendored tesseract language data is what the README says it is.

The traineddata files are redistributed third-party bytes under Apache-2.0.
THIRD_PARTY_NOTICES.md names them and the README's artifact table pins each
one's size and SHA-256; this test reads that table and hashes the files, so a
silently swapped or truncated download fails the suite instead of quietly
changing what a Run reads.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

ROW = re.compile(
    r"^\|\s*`(?P<path>vendor/tessdata/[a-z_]+\.traineddata)`\s*\|"
    r"[^|]*\|\s*(?P<size>[\d,]+)\s*\|\s*`(?P<sha>[0-9a-f]{64})`\s*\|",
    re.M,
)


def table_rows() -> dict[str, tuple[int, str]]:
    """Pins from the README's artifact table and from the notices' own table
    (the language data added for the live test is pinned there first); a file
    pinned in both must carry the same pin."""
    rows: dict[str, tuple[int, str]] = {}
    for doc in (README, NOTICES):
        for m in ROW.finditer(doc.read_text(encoding="utf-8")):
            pin = (int(m.group("size").replace(",", "")), m.group("sha"))
            assert rows.setdefault(m.group("path"), pin) == pin, f"{m.group('path')}: two pins disagree"
    return rows


def test_the_table_covers_every_vendored_file():
    rows = table_rows()
    on_disk = {
        str(p.relative_to(ROOT)) for p in (ROOT / "vendor/tessdata").glob("*.traineddata")
    }
    assert on_disk, "no traineddata is vendored"
    assert set(rows) == on_disk, (
        "the README artifact table and vendor/tessdata disagree:"
        f" only in table {sorted(set(rows) - on_disk)},"
        f" only on disk {sorted(on_disk - set(rows))}"
    )


def test_every_file_matches_its_pin():
    for rel, (size, sha) in sorted(table_rows().items()):
        raw = (ROOT / rel).read_bytes()
        assert len(raw) == size, f"{rel}: {len(raw)} bytes, table says {size}"
        assert hashlib.sha256(raw).hexdigest() == sha, f"{rel}: sha256 does not match the table"


CODES = ("eng", "msa", "lao", "ind", "tha", "rus", "chi_sim", "vie", "kaz", "mon", "hin", "por")


def test_the_languages_this_round_needs_are_present():
    """The Prepared Economies that are not English (Indonesia, Thailand, Lao
    PDR), the Russian Federation, and every other national script of the
    live-test pool: Chinese, Vietnamese, Kazakh, Mongolian, Hindi, and
    Timor-Leste's Portuguese."""
    rows = table_rows()
    for code in CODES:
        assert f"vendor/tessdata/{code}.traineddata" in rows


def test_the_notices_name_them_and_their_licence():
    text = NOTICES.read_text(encoding="utf-8")
    assert "Apache-2.0" in text
    for code in CODES:
        assert f"`{code}`" in text, f"{code} is not listed in THIRD_PARTY_NOTICES.md"
    assert "tessdata_best" in text


def test_every_organizer_language_with_a_script_of_its_own_is_routed_to_its_data():
    from regcompass.contracts import ORGANIZER_LANGUAGES
    from regcompass.languages import tesseract_languages

    rows = table_rows()
    for language in ORGANIZER_LANGUAGES:
        for code in tesseract_languages(language, "XX").split("+"):
            assert f"vendor/tessdata/{code}.traineddata" in rows, (language, code)
