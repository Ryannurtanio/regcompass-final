"""A SCANNED law renders in the audit view, with its highlight on the page.

The final round is built around non-English scans, and a government scan is a
page of compressed image data rather than text. The viewer decodes that image
in WebAssembly and fetches those modules at runtime; with nowhere to fetch them
from it renders a blank white page and the quote highlight floats on nothing,
while the browser console reports a missing wasm location and a decoder that
failed to start. The bytes are fine either way, which is what makes the failure
so easy to miss: the same file renders correctly server-side.

So this drives the real thing. It uploads the two-page Lao PDR scan, Runs the
offline fake Engine over it, opens the audit view, and READS THE PIXELS the
browser painted: a page that is entirely white has not rendered. It then does
the same for a born-digital act, because the fix must not cost the lane that
already worked. The console is watched throughout and must stay clean.

Self-contained and offline: committed fixture bytes, no key, no network, no
model call. It drives the COMMITTED interface bundle, so run `npm run build` in
`ui/` first when the React sources have moved ahead of `src/regcompass/ui_dist`.
Dev machine only (needs playwright and its browser); never on the judge path.

Run: uv run python scripts/pdf_render_check.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"

# The two-page extract of the Lao PDR electronic transactions law: a scan,
# compressed the way government scanners compress, which is exactly the lane
# that used to render blank.
SCANNED = ROOT / "tests/fixtures/derived/lao_electronic_transactions_p05_p06.pdf"
SCANNED_URL = "https://laoofficialgazette.gov.la/konan/ans_doc/edoc/31_ET.pdf"
SCANNED_TITLE = "Law on Electronic Transactions (Amended) No. 31/NA"

# The lane that already worked, kept honest.
BORN_DIGITAL = ROOT / "tests/fixtures/sample_legislation/born_digital/Telecommunications Act 1999.pdf"
BORN_DIGITAL_URL = "https://sso.agc.gov.sg/Act/TA1999?ViewType=Pdf"
BORN_DIGITAL_TITLE = "Telecommunications Act 1999"

# Anything the viewer says when it cannot find or start a decoder.
FORBIDDEN = ("wasmUrl", "JBig2Error", "JpxError", "Cannot load wasm")


def seed(db: Path, data: Path) -> None:
    """Both Documents into their Corpora, then one fake-Engine Run each."""
    from regcompass.corpus import add_document
    from regcompass.engines import resolve_engine
    from regcompass.pipeline import run_economy
    from regcompass.storage import Storage

    storage = Storage(db)
    storage.apply_schema()
    add_document(
        storage, data, "LA", SCANNED.read_bytes(),
        source_url=SCANNED_URL, language="Lao", title=SCANNED_TITLE,
        filename_hint=SCANNED.name,
    )
    add_document(
        storage, data, "SG", BORN_DIGITAL.read_bytes(),
        source_url=BORN_DIGITAL_URL, language="English", title=BORN_DIGITAL_TITLE,
        filename_hint=BORN_DIGITAL.name,
    )
    engine = resolve_engine("fake")
    for economy in ("LA", "SG"):
        run_economy(storage, economy, pillars=(7,), engine=engine, data_dir=data)
    storage.close()


def wait_ready(timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/documents", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server never became ready")


def ink(page: Page) -> float:
    """The share of the rendered page canvas that is not blank white.

    This is the whole assertion. A canvas the viewer could not decode is a
    uniform white rectangle, and no amount of DOM structure around it tells
    you that; the pixels do. Sampled on a grid, because reading a full page
    bitmap back through the bridge is slow and unnecessary."""
    return page.evaluate(
        """() => {
            const canvas = document.querySelector('.pdf-page canvas');
            if (!canvas || !canvas.width || !canvas.height) return -1;
            const ctx = canvas.getContext('2d');
            const step = 4;
            let seen = 0, marked = 0;
            for (let y = 0; y < canvas.height; y += step) {
                const row = ctx.getImageData(0, y, canvas.width, 1).data;
                for (let x = 0; x < canvas.width; x += step) {
                    const i = x * 4;
                    seen += 1;
                    // Off-white counts: a scan's paper is never pure #FFFFFF.
                    if (row[i] < 240 || row[i + 1] < 240 || row[i + 2] < 240) {
                        marked += 1;
                    }
                }
            }
            return seen === 0 ? -1 : marked / seen;
        }"""
    )


def open_first_document(page: Page, economy: str, shots: Path, label: str) -> None:
    """Open this Economy's newest Run in the audit view and prove the page was
    really painted, with the quote highlight drawn on top of it."""
    page.goto(BASE)
    page.click("button.nav-btn:has-text('Runs')")
    row = page.locator(f"table.runs tr.row:has(td:text-is('{economy}'))").first
    expect(row).to_be_visible(timeout=20000)
    row.click()

    expect(page.locator("table.docs tr.row")).to_have_count(1, timeout=20000)
    page.keyboard.press("Enter")
    expect(page.locator(".record-nav")).to_contain_text("record 1 /")

    page.wait_for_selector(".pdf-page canvas", timeout=30000)
    page.wait_for_selector(".hl", timeout=30000)
    assert page.locator(".hl").count() > 0, f"{label}: no highlight rects"

    # Let the render settle: the canvas is sized before it is painted.
    page.wait_for_timeout(1500)
    share = ink(page)
    page.screenshot(path=str(shots / f"render_{economy.lower()}.png"))
    assert share > 0.005, (
        f"{label}: the page canvas is blank ({share:.4%} of sampled pixels"
        " are not white). The viewer rendered nothing."
    )
    print(f"{label}: {share:.2%} of sampled pixels painted")


def run_flow(shots: Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        open_first_document(page, "LA", shots, "scanned Lao act")
        open_first_document(page, "SG", shots, "born-digital act")

        # Nothing went wrong quietly. A missing decoder announces itself here
        # and nowhere else a reviewer would ever look.
        bad = [line for line in console if any(t in line for t in FORBIDDEN)]
        assert not bad, "the viewer complained about its decoders:\n" + "\n".join(bad)
        errors = [line for line in console if line.startswith(("error", "pageerror"))]
        assert not errors, "console errors:\n" + "\n".join(errors)

        browser.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "regcompass.db"
        data = tmp_path / "data"
        started = time.monotonic()
        seed(db, data)
        print(f"seeded both Runs in {time.monotonic() - started:.0f}s")
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "regcompass.cli",
                "serve",
                "--db",
                str(db),
                "--data-dir",
                str(data),
                "--out",
                str(tmp_path / "out"),
                "--port",
                str(PORT),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_ready()
            shots = tmp_path / "shots"
            shots.mkdir()
            run_flow(shots)
        finally:
            server.terminate()
            server.wait(timeout=10)
    print("PDF RENDER CHECK OK")


if __name__ == "__main__":
    main()
