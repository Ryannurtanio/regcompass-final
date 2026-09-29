"""The Evidence Export as the organizer's own workbook (M9 / final round).

Lanes: the vendored template is a byte-identical copy of the organizers' file;
the writer fills Output Data from row 9 and never past row 109, leaving the
Pillar formula, the four validations, the autofilter and the other six sheets
exactly as the organizers wrote them; Indicator IDs are TEXT cells (4.01 and
12.4.1 must survive); Language of Source is one of the organizer's eleven
values, with "Other" plus a Notes disclosure when nothing honest is known; over
the 101-row cap, accepted rows are picked round-robin over Economies by
Confidence so every Economy appears, and the cut is reported.
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

import pytest
from openpyxl import load_workbook

from regcompass.config import load_portals
from regcompass.contracts import (
    ORGANIZER_LANGUAGES,
    CorpusDoc,
    MappingRecord,
    organizer_language,
)
from regcompass.export import (
    COLUMNS,
    EXTRA_COLUMNS,
    LANGUAGE_COLUMN,
    LANGUAGE_UNKNOWN_NOTE,
    SyntheticDoc,
    export_all,
    select_rows,
)
from regcompass.workbook import (
    EXAMPLE_ROWS,
    FIRST_ROW,
    LAST_ROW,
    ROW_CAP,
    SHEET,
    WORKBOOK_COLUMNS,
    template_path,
    write_workbook,
)

ROOT = Path(__file__).resolve().parents[1]
ORGANIZER_DIR = ROOT / "config/organizer"


# ---------------------------------------------------------------------------
# the vendored template
# ---------------------------------------------------------------------------


class TestVendoredTemplate:
    def test_sha256_matches_the_recorded_value(self):
        """config/organizer/README.md records the SHA-256 of the organizers'
        file. The copy must still be byte-identical: a template that drifted
        silently would ship a workbook the secretariat never wrote."""
        readme = (ORGANIZER_DIR / "README.md").read_text(encoding="utf-8")
        m = re.search(r"\b([0-9a-f]{64})\b", readme)
        assert m, "README.md must record the template SHA-256"
        actual = hashlib.sha256(template_path().read_bytes()).hexdigest()
        assert actual == m.group(1)

    def test_template_path_resolves_under_the_config_dir(self, tmp_path):
        assert template_path().parent == ORGANIZER_DIR
        assert template_path(tmp_path) == tmp_path / "organizer" / template_path().name

    def test_workbook_columns_are_the_csv_contract_plus_language(self):
        assert WORKBOOK_COLUMNS == COLUMNS + (LANGUAGE_COLUMN,)
        assert len(WORKBOOK_COLUMNS) == 14  # A..N; O is the organizers' formula

    def test_workbook_columns_are_the_templates_own_headers(self):
        """The organizers' header row is the contract. If their A..N ever stop
        matching our column order, the writer must fail here, not in a file the
        secretariat validates."""
        ws = load_workbook(template_path())[SHEET]
        headers = tuple(
            ws.cell(row=4, column=c).value for c in range(1, len(WORKBOOK_COLUMNS) + 1)
        )
        assert headers == WORKBOOK_COLUMNS


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def wb_row(**over) -> dict:
    row = {
        "Economy": "Indonesia",
        "Law Name": "Personal Data Protection Law 2022",
        "Law Number / Ref": "Law No. 27/2022",
        "Last Amended": "2022",
        "Indicator ID": "6.1",
        "Article / Section": "Art. 56(1)",
        "Discovery Tag": "NEW",
        "Location Reference": "PDF: page 12",
        "Verbatim Snippet": "Personal data must be processed within the territory.",
        "Mapping Rationale": "This Art. 56(1) requires local processing.",
        "Source URL": "https://peraturan.go.id/id/uu-no-27-tahun-2022",
        "Confidence": "0.81",
        "Notes": "",
        LANGUAGE_COLUMN: "Bahasa Indonesia",
    }
    row.update(over)
    return row


def written(tmp_path, rows, **kw):
    out = tmp_path / "submission.xlsx"
    result = write_workbook(template_path(), out, rows, **kw)
    return result, load_workbook(out)


def pillar_from_formula(value: str) -> int | str:
    """The organizers' O-column formula, evaluated in Python: INT(E) when E is
    numeric, else the text before the first dot, else '?'. openpyxl stores
    formulas, it does not evaluate them, so the test computes the same thing."""
    if value == "":
        return ""
    try:
        return int(float(value))
    except ValueError:
        pass
    head = value.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return "?"


class TestWriteWorkbook:
    def test_rows_land_from_row_9_in_the_organizers_column_order(self, tmp_path):
        rows = [wb_row(), wb_row(**{"Indicator ID": "7.3", "Article / Section": "Art. 60"})]
        result, wb = written(tmp_path, rows)
        ws = wb[SHEET]
        assert result.n_rows == 2 and result.rows_cut == 0
        assert ws["A9"].value == "Indonesia"
        assert ws["F10"].value == "Art. 60"
        assert ws["N9"].value == "Bahasa Indonesia"
        for col, header in enumerate(WORKBOOK_COLUMNS, start=1):
            value = ws.cell(row=FIRST_ROW, column=col).value
            if header == "Confidence":
                assert value == pytest.approx(float(rows[0][header]))
            else:
                assert (value if value is not None else "") == rows[0][header]

    def test_indicator_ids_are_text_cells(self, tmp_path):
        """4.01 and 12.4.1 are different indicators from 4.1 and 12.4: entered
        as numbers they collapse. The template formats E as text; the writer
        must also write a STRING, not a float."""
        rows = [
            wb_row(**{"Indicator ID": "4.01", "Article / Section": "s. 1"}),
            wb_row(**{"Indicator ID": "12.4.1", "Article / Section": "s. 2"}),
            wb_row(**{"Indicator ID": "6.1", "Article / Section": "s. 3"}),
        ]
        _, wb = written(tmp_path, rows)
        ws = wb[SHEET]
        assert [ws.cell(row=r, column=5).value for r in (9, 10, 11)] == ["4.01", "12.4.1", "6.1"]
        for r in (9, 10, 11):
            assert ws.cell(row=r, column=5).data_type == "s"
            assert ws.cell(row=r, column=5).number_format == "@"

    def test_pillar_formula_survives_and_derives_the_pillar(self, tmp_path):
        rows = [wb_row(**{"Indicator ID": "12.4.1", "Article / Section": "s. 2"})]
        _, wb = written(tmp_path, rows)
        ws = wb[SHEET]
        assert ws["O9"].value == (
            '=IF($E9="","",IFERROR(INT($E9),IFERROR(VALUE(LEFT($E9,FIND(".",$E9)-1)),"?")))'
        )
        assert ws["O109"].value.startswith("=IF($E109=")
        assert pillar_from_formula(ws["E9"].value) == 12

    def test_example_rows_are_cleared_not_deleted(self, tmp_path):
        """Deleting rows 7 and 8 would shift every O-column formula, the
        autofilter range and the Coverage Matrix COUNTIFS bounds, which openpyxl
        does not translate. The values go; the rows stay."""
        result, wb = written(tmp_path, [wb_row()])
        ws = wb[SHEET]
        for row in EXAMPLE_ROWS:
            for col in range(1, len(WORKBOOK_COLUMNS) + 2):
                assert ws.cell(row=row, column=col).value is None
        assert result.example_rows_cleared is True
        assert ws["A6"].value.startswith("▸ EXAMPLE ROWS")  # the banner stays

    def test_template_structure_is_left_alone(self, tmp_path):
        _, wb = written(tmp_path, [wb_row()])
        assert wb.sheetnames == [
            "Output Data",
            "Indicator Reference",
            "Coverage Matrix",
            "Engine Comparison",
            "Run Record",
            "Submission Checklist",
            "Instructions",
        ]
        ws = wb[SHEET]
        assert ws.auto_filter.ref == "A4:M109"
        assert ws.freeze_panes == "A9"
        validations = {
            str(dv.sqref): (dv.type, dv.formula1) for dv in ws.data_validations.dataValidation
        }
        assert validations["G9:G109"] == ("list", '"NEW,KNOWN"')
        assert validations["N9:N109"] == (
            "list",
            '"English,Thai,Vietnamese,Bahasa Indonesia,Chinese,Hindi,Kazakh,'
            'Russian,Lao,Mongolian,Other"',
        )
        assert validations["L9:L109"][0] == "decimal"
        assert validations["J9:J109"] == ("custom", "lte(LEN(J9),300)")
        assert wb["Coverage Matrix"]["B4"].value == (
            "=COUNTIFS('Output Data'!$O$9:$O$109,1,'Output Data'!$A$9:$A$109,$A4)"
        )
        assert wb["Instructions"]["A1"].value.startswith("How to complete")

    def test_confidence_is_written_as_a_number_and_blanks_stay_blank(self, tmp_path):
        rows = [wb_row(), wb_row(**{"Confidence": "", "Article / Section": "s. 9"})]
        _, wb = written(tmp_path, rows)
        ws = wb[SHEET]
        assert ws["L9"].value == pytest.approx(0.81)
        assert ws["L10"].value is None

    def test_never_writes_past_the_last_entry_row(self, tmp_path):
        rows = [wb_row(**{"Article / Section": f"s. {i}"}) for i in range(150)]
        result, wb = written(tmp_path, rows)
        ws = wb[SHEET]
        assert result.n_rows == ROW_CAP == 101
        assert result.rows_cut == 49
        assert ws.cell(row=LAST_ROW, column=1).value == "Indonesia"
        assert ws.cell(row=LAST_ROW + 1, column=1).value is None


# ---------------------------------------------------------------------------
# Language of Source
# ---------------------------------------------------------------------------


class TestOrganizerLanguage:
    @pytest.mark.parametrize("value", ORGANIZER_LANGUAGES)
    def test_every_organizer_value_maps_to_itself(self, value):
        assert organizer_language(value) == value

    @pytest.mark.parametrize("value", ["Malay", "Bahasa Melayu", "Portuguese", "Tetum", "Khmer"])
    def test_languages_outside_the_list_become_other(self, value):
        assert organizer_language(value) == "Other"

    def test_no_language_at_all_becomes_other(self):
        assert organizer_language(None) == "Other"
        assert organizer_language("") == "Other"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("english", "English"),
            ("Indonesian", "Bahasa Indonesia"),
            ("bahasa indonesia", "Bahasa Indonesia"),
            ("Mandarin", "Chinese"),
            ("Lao", "Lao"),
            ("Thai", "Thai"),
        ],
    )
    def test_known_spellings_normalise(self, raw, expected):
        assert organizer_language(raw) == expected


# ---------------------------------------------------------------------------
# the whole export: CSV column, cap, gate, workbook
# ---------------------------------------------------------------------------


HOST = {eco: (p.hosts[0] if p.hosts else f"{eco.lower()}.example.gov") for eco, p in load_portals().items()}


def synth(economy: str, i: int, indicator: str = "12.3", cosine: float = 0.6):
    """One passed MappingRecord on the mappings-only Pillar 12 lane (no absence
    rows), with a unique section so no two rows collide in the battery."""
    doc_id = f"doc_{economy.lower()}_synthetic_act"
    quote = f"A service provider shall register with the authority before offering service {i}."
    rec = MappingRecord(
        mapping_id=f"{doc_id}:c{i:04d}::{indicator}",
        document_id=doc_id,
        chunk_id=f"{doc_id}:c{i:04d}",
        economy=economy,  # type: ignore[arg-type]
        indicator_id=indicator,  # type: ignore[arg-type]
        indicator_name="Licensing scheme for e-commerce providers",
        section=f"s. {i}",
        subsection="(1)",
        verbatim_quote=quote,
        page_number=i + 1,
        impact="Requires registration before offering the service.",
        verification_status="passed",
        controlling_evidence=False,
        extraction_attempts=1,
    )
    return rec, quote, cosine


def synth_dataset(economies, per_economy: int, indicator: str = "12.3"):
    records, texts, cosines, docs = [], {}, {}, {}
    for eco in economies:
        doc_id = f"doc_{eco.lower()}_synthetic_act"
        docs[doc_id] = SyntheticDoc(
            corpus_doc=CorpusDoc(
                economy=eco,
                law_name=f"{eco} Electronic Transactions Act 2021",
                law_number_ref="Act 1 of 2021",
                last_amended="2024",
                source_url=f"https://{HOST[eco]}/act/{eco.lower()}-eta-2021",
            ),
            allow_any_host=True,
        )
        for i in range(per_economy):
            # a spread of cosines wide enough that no two rows of one Economy
            # tie on the two-decimal Confidence, so the rank is unambiguous
            rec, quote, cosine = synth(eco, i, indicator, cosine=0.40 + 0.012 * i)
            records.append(rec)
            texts[rec.chunk_id] = f"PREFIX {quote} SUFFIX"
            cosines[(rec.chunk_id, rec.indicator_id)] = cosine
    return records, texts, cosines, docs


def run_export(tmp_path, records, texts, cosines, docs, **kw):
    return export_all(
        tmp_path,
        records,
        chunk_text_lookup=texts,
        gate_cosine_lookup=cosines,
        coverage_stats={},
        liveness_fn=lambda url: True,
        synthetic_docs=docs,
        **kw,
    )


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class TestCsvLanguageColumn:
    def test_language_is_column_14_ahead_of_the_enrichment_columns(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG"], 2)
        result = run_export(tmp_path, records, texts, cosines, docs)
        with result.csv_path.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f))
        assert tuple(header[:13]) == COLUMNS
        assert header[13] == LANGUAGE_COLUMN == "Language of Source"
        assert tuple(header[13:]) == EXTRA_COLUMNS

    def test_a_single_language_portal_names_its_language(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG"], 2)
        result = run_export(tmp_path, records, texts, cosines, docs)
        rows = csv_rows(result.csv_path)
        assert {r[LANGUAGE_COLUMN] for r in rows} == {"English"}
        assert all(LANGUAGE_UNKNOWN_NOTE not in r["Notes"] for r in rows)

    def test_a_recorded_document_language_wins_and_maps_to_the_list(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["MY"], 2)
        meta = {"doc_my_synthetic_act": {"language": "Malay"}}
        result = run_export(tmp_path, records, texts, cosines, docs, document_meta=meta)
        rows = csv_rows(result.csv_path)
        assert {r[LANGUAGE_COLUMN] for r in rows} == {"Other"}
        assert all(LANGUAGE_UNKNOWN_NOTE not in r["Notes"] for r in rows)

    def test_portuguese_and_thai_documents(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["TH"], 2)
        pt = run_export(
            tmp_path / "pt",
            records,
            texts,
            cosines,
            docs,
            document_meta={"doc_th_synthetic_act": {"language": "Portuguese"}},
        )
        assert {r[LANGUAGE_COLUMN] for r in csv_rows(pt.csv_path)} == {"Other"}
        th = run_export(
            tmp_path / "th",
            records,
            texts,
            cosines,
            docs,
            document_meta={"doc_th_synthetic_act": {"language": "Thai"}},
        )
        assert {r[LANGUAGE_COLUMN] for r in csv_rows(th.csv_path)} == {"Thai"}

    def test_unknown_language_is_other_and_says_so_in_notes(self, tmp_path):
        """Thailand's Portal expects Thai OR English, so an unrecorded
        Language cannot be inferred. The row ships "Other" and discloses it
        rather than guessing at a value criterion C1c is scored on."""
        records, texts, cosines, docs = synth_dataset(["TH"], 2)
        result = run_export(
            tmp_path, records, texts, cosines, docs,
            document_meta={"doc_th_synthetic_act": {"language": None}},
        )
        rows = csv_rows(result.csv_path)
        assert {r[LANGUAGE_COLUMN] for r in rows} == {"Other"}
        assert all(LANGUAGE_UNKNOWN_NOTE in r["Notes"] for r in rows)

    def test_every_shipped_value_is_on_the_organizers_list(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG", "TH", "ID"], 3)
        result = run_export(tmp_path, records, texts, cosines, docs)
        for row in csv_rows(result.csv_path):
            assert row[LANGUAGE_COLUMN] in ORGANIZER_LANGUAGES

    def test_two_exports_of_the_same_records_are_byte_identical(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG", "TH"], 4)
        a = run_export(tmp_path / "a", records, texts, cosines, docs)
        b = run_export(tmp_path / "b", records, texts, cosines, docs)
        assert a.csv_path.read_bytes() == b.csv_path.read_bytes()


class TestRowCap:
    ECONOMIES = ["SG", "MY", "ID", "TH", "LA", "VN"]

    def test_over_cap_ships_101_rows_with_every_economy_present(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(self.ECONOMIES, 25)  # 150
        reviews = {r.mapping_id: "accepted" for r in records}
        result = run_export(tmp_path, records, texts, cosines, docs, reviews=reviews)
        rows = csv_rows(result.csv_path)
        assert len(rows) == ROW_CAP == 101
        assert result.rows_cut == 49
        official = {load_portals()[e].official_name for e in self.ECONOMIES}
        assert {r["Economy"] for r in rows} == official
        # round-robin: no Economy is starved while another takes everything
        per_economy = {e: sum(1 for r in rows if r["Economy"] == e) for e in official}
        assert max(per_economy.values()) - min(per_economy.values()) <= 1

    def test_the_cut_keeps_the_most_confident_rows_of_each_economy(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(self.ECONOMIES, 25)
        reviews = {r.mapping_id: "accepted" for r in records}
        result = run_export(tmp_path, records, texts, cosines, docs, reviews=reviews)
        rows = csv_rows(result.csv_path)
        # synth_dataset raises the cosine with i, so the highest i of an
        # Economy are its most confident rows; the cut must take the tail.
        for economy in {r["Economy"] for r in rows}:
            kept = sorted(
                int(re.match(r"s\. (\d+)", r["Article / Section"]).group(1))
                for r in rows
                if r["Economy"] == economy
            )
            assert kept == list(range(25 - len(kept), 25))

    def test_unaccepted_mappings_never_ship_even_under_the_cap(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(self.ECONOMIES, 25)
        reviews = {}
        for i, r in enumerate(records):
            reviews[r.mapping_id] = ("accepted", "rejected", "flagged")[i % 3]
        result = run_export(tmp_path, records, texts, cosines, docs, reviews=reviews)
        rows = csv_rows(result.csv_path)
        accepted = {
            r.verbatim_quote for r in records if reviews[r.mapping_id] == "accepted"
        }
        assert len(rows) == 50 and result.rows_cut == 0
        assert {r["Verbatim Snippet"] for r in rows} == accepted

    def test_select_rows_is_a_no_op_under_the_cap(self):
        rows = [
            {"Economy": "Singapore", "Indicator ID": "6.1", "Article / Section": "s. 1",
             "Confidence": "0.90"},
            {"Economy": "Thailand", "Indicator ID": "6.1", "Article / Section": "s. 2",
             "Confidence": "0.10"},
        ]
        kept, cut = select_rows(rows)
        assert cut == 0 and kept == rows


class TestWorkbookFromExport:
    def test_export_writes_the_workbook_beside_the_csv(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG", "TH"], 3)
        result = run_export(tmp_path, records, texts, cosines, docs)
        assert result.xlsx_path == tmp_path / "submission.xlsx"
        assert result.xlsx_path.exists()

    def test_every_output_data_row_equals_the_csv_row(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG", "TH"], 3)
        result = run_export(tmp_path, records, texts, cosines, docs)
        rows = csv_rows(result.csv_path)
        ws = load_workbook(result.xlsx_path)[SHEET]
        for offset, row in enumerate(rows):
            for col, header in enumerate(WORKBOOK_COLUMNS, start=1):
                cell = ws.cell(row=FIRST_ROW + offset, column=col).value
                expected = row[header]
                if header == "Confidence" and expected:
                    assert cell == pytest.approx(float(expected))
                else:
                    assert (cell if cell is not None else "") == expected
        assert ws.cell(row=FIRST_ROW + len(rows), column=1).value is None

    def test_the_workbook_ships_the_same_rows_as_the_csv_under_the_cap(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(TestRowCap.ECONOMIES, 25)
        reviews = {r.mapping_id: "accepted" for r in records}
        result = run_export(tmp_path, records, texts, cosines, docs, reviews=reviews)
        rows = csv_rows(result.csv_path)
        ws = load_workbook(result.xlsx_path)[SHEET]
        written_ids = [
            ws.cell(row=r, column=5).value for r in range(FIRST_ROW, FIRST_ROW + len(rows))
        ]
        assert len(written_ids) == ROW_CAP
        assert written_ids == [r["Indicator ID"] for r in rows]
        assert ws.cell(row=LAST_ROW + 1, column=1).value is None

    def test_absence_rows_are_csv_only(self, tmp_path):
        """A "No provision found" row is our earned zero and belongs in the
        CSV. In Output Data it would spend one of the 101 entry slots and, via
        the Pillar formula, be counted by the organizers' Coverage Matrix as a
        provision we cited, so the workbook lists provisions only."""
        records, texts, cosines, docs = synth_dataset(["SG"], 2, indicator="6.1")
        result = run_export(tmp_path, records, texts, cosines, docs)
        rows = csv_rows(result.csv_path)
        absence = [r for r in rows if r["Article / Section"] == "No provision found"]
        assert len(absence) == 3  # the Run covered Pillar 6: 6.2, 6.3, 6.4 earned zeros
        ws = load_workbook(result.xlsx_path)[SHEET]
        written = []
        for offset in range(len(rows)):
            value = ws.cell(row=FIRST_ROW + offset, column=6).value
            if value is None:
                break
            written.append(value)
        assert "No provision found" not in written
        assert written == [
            r["Article / Section"] for r in rows if r["Article / Section"] != "No provision found"
        ]

    def test_the_cap_is_budgeted_over_provisions_not_absence_rows(self, tmp_path):
        """Absence rows never reach Output Data, so they must not spend its
        101 slots: counting them would leave entry rows empty while
        battery-green provisions were cut."""
        records, texts, cosines, docs = synth_dataset(["SG"], 120, indicator="6.1")
        result = run_export(tmp_path, records, texts, cosines, docs)
        rows = csv_rows(result.csv_path)
        absence = [r for r in rows if r["Article / Section"] == "No provision found"]
        assert len(absence) == 3  # 6.2, 6.3 and 6.4 earned their zeros
        assert result.rows_cut == 19  # 120 provisions - 101 slots
        assert len(rows) == ROW_CAP + len(absence)
        ws = load_workbook(result.xlsx_path)[SHEET]
        assert ws.cell(row=LAST_ROW, column=1).value == "Singapore"  # the sheet is full
        assert ws.cell(row=LAST_ROW + 1, column=1).value is None
        written = [ws.cell(row=r, column=6).value for r in range(FIRST_ROW, LAST_ROW + 1)]
        assert "No provision found" not in written
        assert written == [
            r["Article / Section"] for r in rows if r["Article / Section"] != "No provision found"
        ]

    def test_the_workbook_can_be_switched_off_for_the_golden_lane(self, tmp_path):
        records, texts, cosines, docs = synth_dataset(["SG"], 2)
        result = run_export(tmp_path, records, texts, cosines, docs, write_xlsx=False)
        assert result.xlsx_path is None
        assert not (tmp_path / "submission.xlsx").exists()


class TestFromTheDatabase:
    """The judge path: a real working database, through export_from_db and the
    CLI, writing both files into the chosen output directory."""

    def _seeded(self, tmp_path):
        from test_pipeline import seed_db_from_goldens

        return seed_db_from_goldens(tmp_path / "db.sqlite")

    def test_export_from_db_writes_both_files(self, tmp_path):
        from regcompass.pipeline import export_from_db

        storage = self._seeded(tmp_path)
        result = export_from_db(storage, tmp_path / "out")
        assert result.csv_path.exists()
        assert result.xlsx_path == tmp_path / "out" / "submission.xlsx"
        assert result.xlsx_path.exists()
        ws = load_workbook(result.xlsx_path)[SHEET]
        assert ws["A9"].value == "Singapore"

    def test_a_documents_recorded_language_reaches_the_column(self, tmp_path):
        """The Document's own Language beats the Portal default: Singapore's
        Portal expects English, but a document recorded as Thai ships Thai."""
        from regcompass.pipeline import export_from_db

        storage = self._seeded(tmp_path)
        storage.conn.execute("UPDATE documents SET language = 'Thai'")
        storage.conn.commit()
        assert all(m["language"] == "Thai" for m in storage.document_meta().values())
        result = export_from_db(storage, tmp_path / "out")
        rows = csv_rows(result.csv_path)
        assert rows and {r[LANGUAGE_COLUMN] for r in rows} == {"Thai"}
        ws = load_workbook(result.xlsx_path)[SHEET]
        assert ws["N9"].value == "Thai"

    def test_cli_export_writes_both_files_to_the_chosen_directory(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from regcompass.cli import app

        monkeypatch.chdir(ROOT)  # config/ resolution
        db = tmp_path / "db.sqlite"
        self._seeded(tmp_path)
        out = tmp_path / "out"
        result = CliRunner().invoke(app, ["export", "--db", str(db), "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert (out / "submission.csv").exists()
        assert (out / "submission.xlsx").exists()
        assert "submission.xlsx" in result.output
