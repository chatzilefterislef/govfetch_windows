#!/usr/bin/env python3
"""
Εκκίνηση της εφαρμογής σε Windows και macOS/Linux με μία εντολή:

    python3 run.py        (macOS / Linux)
    python run.py         (Windows)

Τι κάνει, με αυτή τη σειρά:
  1. Δημιουργεί virtual environment στο .venv, αν δεν υπάρχει
  2. Εγκαθιστά τα πακέτα του requirements.txt
  3. Εγκαθιστά τον Chromium της Playwright (μόνο την πρώτη φορά, ~150 MB)
  4. Ξαναεκτελείται ΜΕΣΑ στο venv και ανεβάζει τον server

Γιατί Python και όχι shell script: το run.sh είναι bash και δεν τρέχει σε
Windows. Έτσι υπάρχει ΕΝΑ σημείο εκκίνησης για όλη την ομάδα.

Σημαίες:
  --no-open    μη ανοίξεις browser (το Claude Code ανοίγει μόνο του preview)
  --port 8001  άλλη πόρτα (προεπιλογή 8000)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python() -> Path:
    """Ο interpreter του venv — διαφορετική διαδρομή σε Windows."""
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def inside_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def run(cmd: list[str], what: str) -> None:
    # flush=True: όταν η έξοδος πάει σε αρχείο ή σε παράθυρο του Claude Code
    # είναι block-buffered, οπότε ο χρήστης δεν έβλεπε τίποτα όσο κατέβαινε ο
    # Chromium (ένα-δύο λεπτά) και έμοιαζε κολλημένο.
    print(f"\n▶ {what}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"❌ Απέτυχε: {what}\n   Εντολή: {' '.join(map(str, cmd))}")


def bootstrap() -> None:
    """Φτιάχνει το venv και τα εξαρτήματα, και μετά ξαναμπαίνει μέσα σε αυτό."""
    if sys.version_info < (3, 9):
        sys.exit(f"❌ Χρειάζεται Python 3.9+ (τρέχει {sys.version.split()[0]})")
    # Άνω όριο, όχι υπερβολή: το Playwright 1.47 καρφώνει greenlet==3.0.3, που
    # έχει έτοιμα wheels μόνο μέχρι την 3.12. Σε 3.13/3.14 το pip προσπαθεί να
    # το ΜΕΤΑΓΛΩΤΤΙΣΕΙ και σκάει ζητώντας Visual C++ (Windows) ή Xcode CLT (mac).
    # Χωρίς αυτόν τον έλεγχο ο χρήστης έβλεπε 119 γραμμές σφάλματος από τον
    # compiler και καμία ένδειξη ότι το πρόβλημα είναι η έκδοση της Python.
    if sys.version_info >= (3, 13):
        sys.exit(
            f"❌ Η Python {sys.version.split()[0]} είναι πολύ νέα για τις "
            f"εκδόσεις που χρησιμοποιεί η εφαρμογή.\n"
            f"   Χρειάζεται Python 3.9 έως 3.12 — δες README.md.\n"
            f"   Τρέξε το run.py με τη 3.12, π.χ.:\n"
            f"   Windows: py -3.12 run.py\n"
            f"   macOS:   python3.12 run.py"
        )

    if not venv_python().exists():
        run([sys.executable, "-m", "venv", str(VENV)], "Δημιουργία virtual environment")
    else:
        print("✓ Το virtual environment υπάρχει", flush=True)

    py = str(venv_python())
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "Ενημέρωση pip")
    run([py, "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")],
        "Εγκατάσταση πακέτων")
    # Ο Chromium κατεβαίνει μόνο την πρώτη φορά· μετά το βρίσκει στην κρυφή μνήμη
    run([py, "-m", "playwright", "install", "chromium"],
        "Εγκατάσταση Chromium (μόνο την πρώτη φορά, ~150 MB)")

    print("\n✅ Όλα έτοιμα — εκκίνηση…", flush=True)
    # ΚΡΙΣΙΜΟ: το execv αντικαθιστά τη διεργασία ΧΩΡΙΣ να αδειάσει τα buffers,
    # οπότε χωρίς αυτό το flush χάνονταν όλα τα μηνύματα προόδου παραπάνω.
    sys.stdout.flush()
    sys.stderr.flush()

    script = str(Path(__file__).resolve())
    if os.name == "nt":
        # ΠΑΓΙΔΑ: το os.execv στα Windows ΔΕΝ βάζει quotes στα ορίσματα, οπότε
        # κάθε διαδρομή με κενό σπάει στο κενό. Με φάκελο «Δουλειά Ficon» ο
        # interpreter ζητούσε 'Ficon\gov-doc-fetcher\.venv\Scripts\python.exe'
        # και η εφαρμογή δεν ξεκινούσε καθόλου — και τα «C:\Users\Όνομα
        # Επώνυμο», «Program Files» και OneDrive έχουν όλα κενά.
        # Το subprocess περνά τα ορίσματα σωστά quoted.
        sys.exit(subprocess.run([py, script, *sys.argv[1:]], cwd=ROOT).returncode)
    os.execv(py, [py, script, *sys.argv[1:]])


def serve(port: int, open_browser: bool) -> None:
    import uvicorn   # διαθέσιμο μόνο μέσα στο venv

    url = f"http://localhost:{port}"
    if open_browser:
        def opener() -> None:
            time.sleep(1.5)   # όσο να ανεβεί ο server
            webbrowser.open(url)
        threading.Thread(target=opener, daemon=True).start()

    print(f"\n🌐 Η εφαρμογή τρέχει: {url}")
    print("   (Ctrl+C για διακοπή)\n")
    uvicorn.run("main:app", host="127.0.0.1", port=port, log_level="info")


def main() -> None:
    ap = argparse.ArgumentParser(description="Εκκίνηση του Gov Document Fetcher")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true",
                    help="μη ανοίξεις browser (για preview μέσα από Claude Code)")
    args = ap.parse_args()

    os.chdir(ROOT)          # τα templates/ βρίσκονται σχετικά με τη ρίζα
    if not inside_venv():
        bootstrap()         # δεν επιστρέφει — κάνει execv
    serve(args.port, not args.no_open)


if __name__ == "__main__":
    main()
