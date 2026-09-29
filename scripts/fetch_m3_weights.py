"""Fetch + convert the M3 translation-gloss weights (opus-mt-mul-en -> CTranslate2 int8).

Downloads the Helsinki-NLP/opus-mt-mul-en model files from Hugging Face at a
PINNED revision, verifies every file against a PINNED SHA-256, converts to
CTranslate2 int8 in an EPHEMERAL uvx environment (the conversion needs
transformers+torch; they never enter the project venv or the judged runtime),
and leaves the runtime model at models/opus_mt_mul_en_ct2/ (gitignored, like
every model artifact).

License basis (record of decision, 10 Jul 2026): the HF distribution is tagged
apache-2.0 by Helsinki-NLP (the model authors) - the license verified from
the distribution itself. The original Tatoeba-MT release zip of the same model carries CC-BY
4.0, so this script deliberately fetches from HF, not the Tatoeba bucket, to
stay on the verified Apache-2.0 grant. A CT2 conversion of apache-2.0 weights
is an apache-2.0 derivative; attribution lives in THIRD_PARTY_NOTICES.md.

Usage:  python scripts/fetch_m3_weights.py
Needs:  curl, uv (for uvx). Runs on the dev machine; judges never need it
        (M3 is a config-flagged side lane, default OFF).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "Helsinki-NLP/opus-mt-mul-en"
REVISION = "848eae0c1676cfce9bb791c200e8228e5a6396ff"  # pinned HF commit, apache-2.0 tag
OUT_DIR = Path(__file__).resolve().parents[1] / "models" / "opus_mt_mul_en_ct2"

# sha256 of every fetched file at REVISION (computed + verified 10 Jul 2026)
FILES = {
    "config.json": "7253aeecb4f20d42ed1c6cd1fc7cfd270e3e0f5a88919a4992e8001bd50f4695",
    "generation_config.json": "701433bb7e2a562cb86a58922c79e7299de226eb8935da94b5cce0c7ae3037dd",
    "pytorch_model.bin": "33ff438ec37160a105f0700819a5b78a07918e1913fc2f249184b1f46a248e4e",
    "source.spm": "c4a99ea3602b29fbf901ade8b93a45efa3d7c64eab8fc5fa812383efa327a87d",
    "target.spm": "c6dce5fa58fcd7dde9e81e279b8c075bf42ee558278f73d6fb48e342029d7f19",
    # public file checksum, not a credential (gitleaks entropy false positive)
    "tokenizer_config.json": "e3af06817cc9f5d7d47bb84dfbd1d547465e9e9ea53c375f05b0ef26bcdcd1cf",  # gitleaks:allow
    "vocab.json": "6f223b108c7ea7622561eb943a09ffd2329f493387bf12ab0e3aa341d355f837",
}

# conversion-environment pins (dev-machine build step only, never the judged runtime)
CT2_PIN = "ctranslate2==4.8.1"
CONVERT_WITH = ["sentencepiece==0.2.1", "transformers==4.57.3", "torch==2.9.1"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    if (OUT_DIR / "model.bin").exists():
        # NOTE: an existing model dir is NOT re-verified (the CT2 conversion
        # output is not bit-reproducible across platforms, so it cannot carry
        # an at-rest pin); delete the dir to force a verified re-fetch.
        print(f"already present (not re-verified): {OUT_DIR}")
        return 0
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        for name, digest in FILES.items():
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
            print(f"fetching {name} ...")
            subprocess.run(["curl", "-sSL", "-o", str(src / name), url], check=True)
            actual = sha256(src / name)
            if actual != digest:
                print(f"DIGEST MISMATCH for {name}:\n  pinned {digest}\n  actual {actual}")
                return 1
        print("all digests verified; converting to CTranslate2 int8 (ephemeral env) ...")
        cmd = ["uvx", "--from", CT2_PIN]
        for dep in CONVERT_WITH:
            cmd += ["--with", dep]
        cmd += [
            "ct2-transformers-converter",
            "--model", str(src),
            "--output_dir", str(OUT_DIR),
            "--quantization", "int8",
        ]
        subprocess.run(cmd, check=True)
        # inference needs the sentencepiece models next to the CT2 model
        for spm_name in ("source.spm", "target.spm"):
            (OUT_DIR / spm_name).write_bytes((src / spm_name).read_bytes())
    print(f"done: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
