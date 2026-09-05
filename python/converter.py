#!/usr/bin/env python3
"""PDFmesh PDF -> DOCX converter.

Invoked by the Node.js backend as:

    python converter.py <input_pdf_path> <output_docx_path>

Engine strategy (chosen for the common case: mostly-English PDFs with tables,
images and rich layout, plus occasional Indic-language documents):

  * PRIMARY:  pdf2docx-plus — reconstructs tables, images and block layout, so
              the DOCX matches the source PDF far more closely. Best for the
              typical English/mixed-content uploads.
  * FALLBACK: LibreOffice headless — used when the PDF contains complex Indic
              scripts (Telugu, Hindi/Marathi, Kannada, Tamil, ...), where
              pdf2docx garbles the characters. LibreOffice shapes those scripts
              correctly. Also used if pdf2docx fails for any reason.

The exit-code contract is UNCHANGED so the Node layer keeps working as-is:

    0  success
    1  usage / unexpected error
    2  invalid or unreadable PDF
    3  password protected PDF
"""

import os
import shutil
import subprocess
import sys
import tempfile


CONVERT_TIMEOUT_S = 110
PDF_MAGIC = b"%PDF-"

# Unicode blocks for the Indic scripts we route to LibreOffice. pdf2docx tends
# to scramble these; LibreOffice shapes them correctly.
INDIC_RANGES = (
    (0x0900, 0x097F),  # Devanagari (Hindi, Marathi, Sanskrit)
    (0x0980, 0x09FF),  # Bengali/Assamese
    (0x0A00, 0x0A7F),  # Gurmukhi (Punjabi)
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B00, 0x0B7F),  # Oriya
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
)


def _fail(code: int, message: str) -> None:
    # Diagnostics go to stderr only; Node captures and logs them internally.
    print(message, file=sys.stderr)
    sys.exit(code)


def _looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == PDF_MAGIC
    except OSError as exc:
        _fail(2, f"could not read input file: {exc}")


def _is_indic_char(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in INDIC_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _pdf_has_indic_text(input_pdf: str) -> bool:
    """Sample the PDF text and decide whether it contains Indic script.

    Uses PyMuPDF (fitz), which ships with pdf2docx. If PyMuPDF is unavailable or
    text extraction fails, we conservatively return False (use pdf2docx), since
    that engine is the better default for the common English/mixed content.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        return False

    try:
        doc = fitz.open(input_pdf)
    except Exception:
        # Let the actual converters produce a precise error/exit code later.
        return False

    try:
        indic = 0
        pages_to_scan = min(len(doc), 5)
        for i in range(pages_to_scan):
            try:
                text = doc[i].get_text("text")
            except Exception:
                continue
            for ch in text:
                if _is_indic_char(ch):
                    indic += 1
                    if indic >= 5:  # a few Indic chars is enough signal
                        return True
        return False
    finally:
        try:
            doc.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Engine: pdf2docx-plus (primary)
# --------------------------------------------------------------------------- #
def _convert_with_pdf2docx(input_pdf: str, output_docx: str) -> None:
    """Convert using pdf2docx-plus. Raises on failure so the caller can fall
    back to LibreOffice. Maps password errors to the reserved exit code 3."""
    from pdf2docx_plus import convert

    try:
        convert(input_pdf, output_docx, timeout_s=CONVERT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        text = (type(exc).__name__ + " " + str(exc)).lower()
        if "password" in text or "encrypted" in text:
            _fail(3, f"password protected: {exc}")
        # Re-raise so main() can attempt the LibreOffice fallback.
        raise

    if not os.path.isfile(output_docx) or os.path.getsize(output_docx) == 0:
        raise RuntimeError("pdf2docx produced no output")


# --------------------------------------------------------------------------- #
# Engine: LibreOffice headless (fallback / Indic scripts)
# --------------------------------------------------------------------------- #
def _find_soffice() -> str:
    override = os.environ.get("SOFFICE_BIN")
    if override and os.path.isfile(override):
        return override
    for name in ("soffice", "libreoffice", "soffice.bin"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/opt/libreoffice/program/soffice",
        "/snap/bin/libreoffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if os.path.isfile(path):
            return path
    raise RuntimeError("LibreOffice (soffice) is not installed or not on PATH")


def _looks_password_protected(*texts: str) -> bool:
    blob = "\n".join(texts).lower()
    return "password" in blob or "encrypted" in blob


def _convert_with_libreoffice(input_pdf: str, output_docx: str) -> None:
    """Convert using LibreOffice headless. Raises on failure; maps password
    detection to exit code 3."""
    soffice = _find_soffice()

    with tempfile.TemporaryDirectory(prefix="pdfmesh_lo_") as work_dir:
        profile_dir = os.path.join(work_dir, "profile")
        out_dir = os.path.join(work_dir, "out")
        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)

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
            "docx:MS Word 2007 XML",
            "--outdir",
            out_dir,
            input_pdf,
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONVERT_TIMEOUT_S,
            check=False,
        )
        stdout_text = (proc.stdout or b"").decode("utf-8", "replace")
        stderr_text = (proc.stderr or b"").decode("utf-8", "replace")

        if proc.returncode != 0:
            if _looks_password_protected(stderr_text, stdout_text):
                _fail(3, f"password protected: {stderr_text.strip()}")
            raise RuntimeError(
                f"LibreOffice exited with code {proc.returncode}: {stderr_text.strip()}"
            )

        produced = None
        for name in os.listdir(out_dir):
            if name.lower().endswith(".docx"):
                produced = os.path.join(out_dir, name)
                break

        if produced is None or not os.path.isfile(produced) or os.path.getsize(produced) == 0:
            if _looks_password_protected(stderr_text, stdout_text):
                _fail(3, "password protected PDF")
            raise RuntimeError("LibreOffice produced no output")

        os.makedirs(os.path.dirname(os.path.abspath(output_docx)), exist_ok=True)
        try:
            shutil.move(produced, output_docx)
        except OSError:
            shutil.copyfile(produced, output_docx)


def main() -> None:
    if len(sys.argv) != 3:
        _fail(1, "usage: converter.py <input_pdf_path> <output_docx_path>")

    input_pdf = sys.argv[1]
    output_docx = sys.argv[2]

    if not os.path.isfile(input_pdf):
        _fail(2, f"input file does not exist: {input_pdf}")
    if not _looks_like_pdf(input_pdf):
        _fail(2, "the uploaded file is not a valid PDF")

    use_libreoffice_first = _pdf_has_indic_text(input_pdf)

    if use_libreoffice_first:
        # Indic content: LibreOffice preserves the script; pdf2docx would garble it.
        try:
            _convert_with_libreoffice(input_pdf, output_docx)
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"libreoffice failed, trying pdf2docx: {exc}", file=sys.stderr)
            try:
                _convert_with_pdf2docx(input_pdf, output_docx)
                sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc2:  # noqa: BLE001
                _fail(1, f"conversion failed (both engines): {exc2}")
    else:
        # Common case: English / mixed content. pdf2docx gives the best layout.
        try:
            _convert_with_pdf2docx(input_pdf, output_docx)
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"pdf2docx failed, trying libreoffice: {exc}", file=sys.stderr)
            try:
                _convert_with_libreoffice(input_pdf, output_docx)
                sys.exit(0)
            except SystemExit:
                raise
            except Exception as exc2:  # noqa: BLE001
                _fail(2, f"the PDF could not be converted: {exc2}")


if __name__ == "__main__":
    main()
