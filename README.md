# PDFmesh Backend

Node.js + Express API that converts PDF files to DOCX. The heavy conversion is
performed by **LibreOffice in headless mode**, invoked from
`python/converter.py`.

## Conversion engine (two-engine strategy)

- Endpoint: `POST /api/pdf-to-docx` (multipart form field: `file`).
- The Node layer writes the upload to a temp file and runs:
  `python converter.py <input_pdf> <output_docx>`

`converter.py` chooses the best engine per file:

- **Primary — `pdf2docx-plus`**: reconstructs tables, images and block layout,
  so the DOCX closely matches the source PDF. Used for the common case
  (mostly-English / mixed-content documents). Best layout fidelity.
- **Fallback — LibreOffice headless**: used when the PDF contains complex Indic
  scripts (Telugu, Hindi/Marathi, Kannada, Tamil, ...), where `pdf2docx` garbles
  characters. LibreOffice shapes those scripts correctly. Also used if
  `pdf2docx` fails for any reason.

Script detection uses PyMuPDF (`fitz`), which ships with `pdf2docx-plus`.

Requirements:

- The Python interpreter is resolved from `backend/python/venv` (or the
  `PYTHON_BIN` env override) — keep the venv in place.
- Install `pdf2docx-plus` (and PyMuPDF) into the venv (pip).
- Keep **LibreOffice** installed as a system package for the fallback path.

### Reinstall the pip engine (in the venv)

```bash
# Linux
backend/python/venv/bin/pip install \
  "pdf2docx-plus @ git+https://github.com/mithunvoe/pdf2docx-plus.git" PyMuPDF
```

## Server requirements

These are **system** installs on the VPS (not pip):

1. **LibreOffice** — provides the `soffice` binary used by `converter.py`.
2. **Fonts for every script you need** — without the right fonts, text can
   render as boxes even when the engine is correct. Noto and Lohit cover the
   major Indic scripts.

### Ubuntu / Debian

```bash
sudo apt-get update
# LibreOffice (Writer core is enough for PDF -> DOCX)
sudo apt-get install -y libreoffice-core libreoffice-writer

# Fonts: broad Indic + Latin coverage
sudo apt-get install -y \
  fonts-noto \
  fonts-noto-core \
  fonts-noto-cjk \
  fonts-indic \
  fonts-lohit-telu fonts-lohit-deva fonts-lohit-knda \
  fonts-lohit-taml fonts-lohit-guru fonts-lohit-gujr fonts-lohit-beng

# Refresh the font cache so LibreOffice sees the new fonts
sudo fc-cache -f -v
```

Verify LibreOffice is on PATH:

```bash
soffice --version
```

If `soffice` is not on PATH, set an override in `backend/.env`:

```
SOFFICE_BIN=/usr/bin/soffice
```

### Optional environment variables

- `SOFFICE_BIN` — absolute path to the LibreOffice binary if it is not on PATH.
- `PYTHON_BIN` — absolute path to a Python interpreter if you are not using the
  `backend/python/venv` virtual environment.

## Exit codes (converter.py -> Node mapping)

| Exit | Meaning                | Client message                       |
| ---- | ---------------------- | ------------------------------------ |
| 0    | success                | (returns the DOCX)                   |
| 1    | usage/unexpected error | generic conversion failure           |
| 2    | invalid/unreadable PDF | "could not be read as a valid PDF"   |
| 3    | password protected     | "PDF is password protected"          |

## Notes / limitations

- Text correctness is reliable for PDFs that contain real Unicode text. If a PDF
  was made with a non-Unicode custom font (copy-paste from it yields garbage),
  no text converter can recover it — that needs OCR (e.g. Tesseract with the
  relevant language packs).
- Layout is preserved closely but not pixel-perfect: PDF is fixed-layout and
  DOCX is reflowable, so minor spacing/positioning differences are expected.
