#!/usr/bin/env python3
"""PDFmesh PDF -> DOCX converter.

Invoked by the Node.js backend as:

    python converter.py <input_pdf_path> <output_docx_path>

Uses LibreOffice in headless mode as the conversion engine. LibreOffice uses a
full text-layout/shaping engine (HarfBuzz), so it correctly preserves complex
Indic scripts (Telugu, Hindi, Kannada, Tamil, Marathi, ...) and other Unicode
text, unlike glyph-extraction converters.

Exit codes are meaningful so the Node layer can map them to safe, client-facing
messages (this contract is UNCHANGED from the previous engine):

    0  success
    1  usage / unexpected error
    2  invalid or unreadable PDF
    3  password protected PDF
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile


# Per-call soft timeout (seconds). Node enforces the hard process-level timeout;
# this is a secondary guard so a stuck LibreOffice call cannot hang forever.
CONVERT_TIMEOUT_S = 110

# PDF magic bytes for a lightweight validity pre-check.
PDF_MAGIC = b"%PDF-"


def _fail(code: int, message: str) -> None:
    # Diagnostics go to stderr only; Node captures and logs them internally.
    print(message, file=sys.stderr)
    sys.exit(code)


def _find_soffice() -> str:
    """Locate the LibreOffice binary.

    Honors an optional SOFFICE_BIN override, then falls back to the common
    executable names on PATH ('soffice' on Linux/macOS, 'soffice.exe'/'soffice'
    handled by shutil.which on Windows).
    """
    override = os.environ.get("SOFFICE_BIN")
    if override and os.path.isfile(override):
        return override

    for name in ("soffice", "libreoffice", "soffice.bin"):
        found = shutil.which(name)
        if found:
            return found

    # Common fixed locations as a last resort.
    candidates = [
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/opt/libreoffice/program/soffice",
        "/snap/bin/libreoffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    _fail(1, "LibreOffice (soffice) is not installed or not found on PATH.")


def _looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
        return head == PDF_MAGIC
    except OSError as exc:
        _fail(2, f"could not read input file: {exc}")


def _looks_password_protected(stderr_text: str, stdout_text: str) -> bool:
    blob = (stderr_text + "\n" + stdout_text).lower()
    return "password" in blob or "encrypted" in blob


def main() -> None:
    if len(sys.argv) != 3:
        _fail(1, "usage: converter.py <input_pdf_path> <output_docx_path>")

    input_pdf = sys.argv[1]
    output_docx = sys.argv[2]

    if not os.path.isfile(input_pdf):
        _fail(2, f"input file does not exist: {input_pdf}")

    if not _looks_like_pdf(input_pdf):
        _fail(2, "the uploaded file is not a valid PDF")

    soffice = _find_soffice()

    # LibreOffice writes <basename>.docx into an output directory; it cannot
    # target an arbitrary output filename directly. So we convert into an
    # isolated temp dir, then move the single result to the exact path Node
    # expects. A private per-call user-profile dir avoids clashes when multiple
    # conversions run concurrently.
    with tempfile.TemporaryDirectory(prefix="pdfmesh_lo_") as work_dir:
        profile_dir = os.path.join(work_dir, "profile")
        out_dir = os.path.join(work_dir, "out")
        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

        # file:// URI for the user profile keeps concurrent runs isolated.
        profile_uri = "file://" + profile_dir.replace(os.sep, "/")
        if not profile_uri.startswith("file:///"):
            profile_uri = "file:///" + profile_dir.lstrip("/").replace(os.sep, "/")

        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--nologo",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            # Force the MS Word 2007+ .docx filter explicitly.
            "docx:MS Word 2007 XML",
            "--outdir",
            out_dir,
            input_pdf,
        ]

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=CONVERT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _fail(1, "conversion timed out")
            return
        except OSError as exc:
            _fail(1, f"failed to start LibreOffice: {exc}")
            return

        stdout_text = (proc.stdout or b"").decode("utf-8", "replace")
        stderr_text = (proc.stderr or b"").decode("utf-8", "replace")

        if proc.returncode != 0:
            if _looks_password_protected(stderr_text, stdout_text):
                _fail(3, f"password protected: {stderr_text.strip()}")
            _fail(1, f"LibreOffice exited with code {proc.returncode}: {stderr_text.strip()}")

        # Find the produced .docx in the output directory.
        produced = None
        for name in os.listdir(out_dir):
            if name.lower().endswith(".docx"):
                produced = os.path.join(out_dir, name)
                break

        if produced is None or not os.path.isfile(produced) or os.path.getsize(produced) == 0:
            # LibreOffice sometimes returns 0 but produces nothing for unreadable
            # or password-protected input.
            if _looks_password_protected(stderr_text, stdout_text):
                _fail(3, "password protected PDF")
            _fail(2, "conversion produced no output; the PDF may be invalid or protected")
            return

        # Move the result to the exact destination Node expects. Use copy+replace
        # so it works across filesystems/mounts, then let the temp dir clean up.
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_docx)), exist_ok=True)
            shutil.move(produced, output_docx)
        except OSError:
            try:
                shutil.copyfile(produced, output_docx)
            except OSError as exc:
                _fail(1, f"could not write output file: {exc}")
                return

    if not os.path.isfile(output_docx) or os.path.getsize(output_docx) == 0:
        _fail(1, "conversion produced no output file")

    sys.exit(0)


if __name__ == "__main__":
    main()
