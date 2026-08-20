from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from pathlib import Path
from typing import List, Optional, Tuple
import asyncio
import os
import re
import tempfile
import unicodedata
from datetime import date


def debug_dir() -> Path:
    """
    Φάκελος για logs και screenshots διάγνωσης, κοινός σε macOS και Windows.

    Προτιμά το /tmp όπου υπάρχει (macOS/Linux), γιατί είναι προβλέψιμο και
    εύκολο να το ανοίξεις — το tempfile.gettempdir() στο macOS δίνει κάτι σαν
    /var/folders/7s/…/T που δεν το βρίσκει κανείς. Στα Windows, όπου /tmp δεν
    υπάρχει, πέφτει στον κανονικό προσωρινό φάκελο.
    """
    posix_tmp = Path("/tmp")
    if posix_tmp.is_dir() and os.access(posix_tmp, os.W_OK):
        return posix_tmp
    return Path(tempfile.gettempdir())


def gr_norm(text: str) -> str:
    """
    Κεφαλαία ΧΩΡΙΣ τόνους — απαραίτητο για συγκρίσεις ελληνικού κειμένου.

    ΠΑΓΙΔΑ: το str.upper() ΚΡΑΤΑΕΙ τους τόνους, οπότε
        'ΥΠΟΒΛΗΘΕΙ' in 'Δεν έχει υποβληθεί'.upper()   ->  False
    γιατί γίνεται 'ΔΕΝ ΈΧΕΙ ΥΠΟΒΛΗΘΕΊ'. Αποτυγχάνει σιωπηλά.
    """
    decomposed = unicodedata.normalize("NFD", text.upper())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


# Λατινικά κεφαλαία που μοιάζουν οπτικά με ελληνικά. Το portal γράφει άλλοτε
# "Ε3" (ελληνικό Έψιλον) και άλλοτε "E3" (λατινικό E) — οπτικά ίδια, αλλά
# διαφορετικοί χαρακτήρες, οπότε η σύγκριση αποτυγχάνει σιωπηλά.
_LOOKALIKES = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
    "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ",
})


def label_norm(text: str) -> str:
    """
    Μορφή σύγκρισης για labels του portal: κεφαλαία, χωρίς τόνους, με τα
    λατινικά ομοιογράμματα να γίνονται ελληνικά και χωρίς διπλά κενά.

    Χρησιμοποιείται για ΟΛΕΣ τις συγκρίσεις labels (_click_labeled), ώστε το
    "Ε3 Υπόχρεου", "E3 ΥΠΟΧΡΕΟΥ" και "Ε3 ΥΠΌΧΡΕΟΥ" να ταιριάζουν όλα.
    """
    return " ".join(gr_norm(text).translate(_LOOKALIKES).split())


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # Ο Chromium παγώνει χρονομετρητές σε παράθυρα που δεν φαίνονται· χωρίς
    # αυτά οι Angular σελίδες θα αργούσαν.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]

# Πρώτα ο πακεταρισμένος Chromium — είναι ο δοκιμασμένος. Τα υπόλοιπα είναι
# δίχτυ ασφαλείας, όχι προτίμηση.
LAUNCH_CHANNELS = [
    (None,     "πακεταρισμένος Chromium"),
    ("msedge", "Microsoft Edge του συστήματος"),
    ("chrome", "Google Chrome του συστήματος"),
]


async def launch_browser(playwright, headless: bool, log=None) -> Browser:
    """
    Ξεκινά browser, με fallback στον Edge/Chrome του συστήματος.

    ΓΙΑΤΙ: ο Chromium 129 που καρφώνει το Playwright 1.47 (Σεπτ. 2024) ΔΕΝ
    ξεκινά σε Windows 11 build 26200 — πετά «spawn UNKNOWN», και το Event Log
    δείχνει σφάλμα side-by-side στο manifest του chrome.exe. Το κατέβασμα είναι
    ακέραιο (δοκιμάστηκε `playwright install --force`) και το VC++ runtime
    εγκατεστημένο· είναι ο ίδιος ο build που δεν τρέχει πια. Χωρίς fallback η
    εφαρμογή ήταν εντελώς άχρηστη σε τέτοιο μηχάνημα, ενώ ο Edge —που υπάρχει
    σε ΚΑΘΕ Windows— δουλεύει μια χαρά.

    Ο Edge και ο Chrome είναι κι αυτοί Chromium, οπότε η λογική πλοήγησης δεν
    αλλάζει σε τίποτα.
    """
    errors: List[str] = []
    for channel, label in LAUNCH_CHANNELS:
        try:
            browser = await playwright.chromium.launch(
                headless=headless, channel=channel, args=LAUNCH_ARGS)
        except Exception as e:
            errors.append(f"{label}: {str(e).splitlines()[0]}")
            continue
        if channel is not None and log:
            log(f"  ℹ️ Ο πακεταρισμένος Chromium δεν ξεκίνησε — "
                f"χρησιμοποιείται ο {label}")
        return browser

    raise RuntimeError(
        "Δεν ξεκίνησε κανένας browser.\n   " + "\n   ".join(errors) +
        "\n   Δοκίμασε να εγκαταστήσεις τον Microsoft Edge ή τον Chrome."
    )


class BaseAutomation:
    def __init__(self, log_callback):
        self.log = log_callback
        self._playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        # Κάθε PDF που πιάνεται το κρατάμε στη μνήμη — δες _capture_download
        self._pdf_captures: List[Tuple[str, bytes]] = []
        self._pdf_tasks: List[asyncio.Future] = []

    async def setup(self, headless: bool = True):
        """
        `headless=True` (προεπιλογή): ο browser τρέχει χωρίς παράθυρο, ώστε να
        μη διακόπτει τη δουλειά σου.

        ΔΟΚΙΜΑΣΤΗΚΑΝ ΚΑΙ ΑΠΟΡΡΙΦΘΗΚΑΝ δύο εναλλακτικές για «παράθυρο από πίσω»:
          • --window-position=-32000,-32000 → το macOS επαναφέρει τα παράθυρα
            μέσα στην ορατή περιοχή (μετρήθηκε: κατέληγε στο x=0, y=30).
          • ελαχιστοποίηση μέσω CDP Browser.setWindowBounds → το παράθυρο όντως
            κρύβεται, ΑΛΛΑ το page.screenshot() κάνει timeout. Όλα τα
            διαγνωστικά μας στηρίζονται σε screenshots, οπότε ήταν αδιέξοδο.
        Σε headless και τα screenshots και οι χρονομετρητές δουλεύουν κανονικά.
        """
        self._headless = headless
        self._playwright = await async_playwright().start()
        self.browser = await launch_browser(self._playwright, headless, self.log)
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 900},
            accept_downloads=True,
            locale="el-GR",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
        )
        # Νέα tabs/popups: να παρακολουθούνται κι αυτά για downloads
        self.context.on("page", self._attach_page_listeners)
        self.page = await self.context.new_page()
        self._attach_page_listeners(self.page)

    # ------------------------------------------------------------------
    # Σύλληψη PDF
    #
    # ΣΗΜΕΙΩΣΗ (επαληθευμένο πειραματικά): το response.body() ΔΕΝ δουλεύει για
    # PDF — σε headless πετά "No resource with given identifier found", και σε
    # headful επιστρέφει το HTML του PDF viewer αντί για το αρχείο.
    #
    # Οι μέθοδοι που δουλεύουν, με σειρά αξιοπιστίας:
    #   • request interception (route.fetch)  → δουλεύει ΚΑΙ με endpoints μιας χρήσης,
    #                                            όπως τα ...menuPrint.do του TaxisNet
    #                                            που σε δεύτερο GET δεν δίνουν το PDF
    #   • download event                      → όταν το portal στέλνει attachment
    #   • context.request.get(page.url)       → όταν το PDF ξαναζητείται με GET
    # ------------------------------------------------------------------
    async def start_pdf_interception(self):
        """
        Ενεργοποιεί interception ΜΟΝΟ για document navigations: κάνουμε εμείς το
        request (route.fetch) ώστε να κρατήσουμε τα bytes, και μετά σερβίρουμε το
        ίδιο response στον browser (route.fulfill) για να φαίνεται κανονικά.
        Έτσι πιάνουμε PDF από URL μιας χρήσης, που δεν μπορεί να ξαναζητηθεί.
        """
        await self.context.route("**/*", self._route_handler)

    async def stop_pdf_interception(self):
        try:
            await self.context.unroute("**/*", self._route_handler)
        except Exception:
            pass

    async def _route_handler(self, route):
        # Μόνο πλοηγήσεις σελίδων — τα υπόλοιπα (css/js/img) περνούν ανέπαφα
        if route.request.resource_type != "document":
            await route.fallback()
            return
        try:
            # max_redirects=0: τα redirects τα ακολουθεί ΜΟΝΟΣ ο browser, ώστε το
            # URL της σελίδας να παραμείνει σωστό (αλλιώς σπάνε τα relative links).
            resp = await route.fetch(max_redirects=0)
            body = await resp.body()
        except Exception:
            await route.fallback()
            return
        if body[:4] == b"%PDF":
            self._pdf_captures.append((route.request.url, body))
            self.log(f"  📥 Πιάστηκε PDF από το δίκτυο ({len(body)//1024} KB)")
        try:
            await route.fulfill(response=resp, body=body)
        except Exception:
            await route.fallback()

    def _attach_page_listeners(self, page: Page):
        def on_download(d):
            self._pdf_tasks.append(asyncio.ensure_future(self._capture_download(d)))
        page.on("download", on_download)

    async def _await_pending_captures(self, timeout: float = 30):
        """Περιμένει να ολοκληρωθούν τα downloads που ξεκίνησαν, χωρίς να κολλήσει."""
        pending = [t for t in self._pdf_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        self._pdf_tasks = [t for t in self._pdf_tasks if not t.done()]

    async def _capture_download(self, download):
        try:
            path = await download.path()
            body = Path(path).read_bytes()
        except Exception:
            return
        if body[:4] == b"%PDF":
            self._pdf_captures.append((download.url, body))
            self.log(f"  📥 Εντοπίστηκε PDF ({len(body)//1024} KB)")

    async def fetch_pdf_from_url(self, url: str) -> Optional[bytes]:
        """Ξαναζητά το URL με τα cookies του browser. Επιστρέφει bytes αν είναι PDF."""
        try:
            resp = await asyncio.wait_for(self.context.request.get(url), timeout=45)
            if resp.ok:
                body = await resp.body()
                if body[:4] == b"%PDF":
                    return body
        except Exception:
            pass
        return None

    def reset_pdf_captures(self):
        self._pdf_captures.clear()
        self._pdf_tasks.clear()

    async def save_real_pdf(self, filepath: Path) -> Optional[str]:
        """
        Αποθηκεύει το πραγματικό PDF, δοκιμάζοντας με τη σειρά:
          1. PDF που έχει ήδη πιαστεί ως download
          2. Re-fetch του URL της τρέχουσας σελίδας (PDF viewer)
        Επιστρέφει το URL από όπου προήλθε, ή None αν δεν βρέθηκε PDF.
        """
        # ΠΕΡΙΜΕΝΕΙ να φτάσει το PDF, δεν κοιτάζει μία φορά. Το interception το
        # πιάνει ασύγχρονα, όταν ολοκληρωθεί η απόκριση του δικτύου.
        # ΓΙΑΤΙ: στο «Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ» δεν ανοίγει νέο tab, οπότε ο έλεγχος
        # γινόταν ΠΡΙΝ φτάσει το αρχείο — το log έδειχνε «Πιάστηκε PDF 837 KB»
        # και αμέσως μετά «δεν εντοπίστηκε πραγματικό PDF». Στα Ε1/Εκκαθαριστικό
        # δεν φαινόταν, γιατί το άνοιγμα νέου tab έδινε τον χρόνο που έλειπε.
        for _ in range(16):                   # ~8 δευτερόλεπτα
            await self._await_pending_captures()
            if self._pdf_captures:
                url, body = self._pdf_captures[-1]
                filepath.write_bytes(body)
                return url
            await asyncio.sleep(0.5)

        body = await self.fetch_pdf_from_url(self.page.url)
        if body:
            filepath.write_bytes(body)
            return self.page.url

        # Το PDF μπορεί να είναι μέσα σε iframe (embed/object) της σελίδας
        for frame in self.page.frames:
            if frame is self.page.main_frame:
                continue
            body = await self.fetch_pdf_from_url(frame.url)
            if body:
                filepath.write_bytes(body)
                return frame.url
        return None

    async def cleanup(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def save_as_pdf(self, filepath: Path):
        """
        print-to-PDF. ΠΡΟΣΟΧΗ: το page.pdf() υποστηρίζεται μόνο σε headless
        Chromium. Σε headed (που τρέχουμε, για να βλέπει ο χρήστης το login)
        ΣΚΟΤΩΝΕΙ ολόκληρο τον browser: μετά την κλήση κάθε επόμενη ενέργεια
        έσκαγε με «Target page, context or browser has been closed», χάνοντας
        όλα τα υπόλοιπα έγγραφα του τρεξίματος.
        """
        if not getattr(self, "_headless", False):
            raise RuntimeError(
                "print-to-PDF δεν είναι διαθέσιμο σε ορατό browser (headed) — "
                "θα έκλεινε ο browser και θα χάνονταν τα υπόλοιπα έγγραφα"
            )
        await self.page.pdf(
            path=str(filepath),
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "10mm", "right": "10mm"},
        )

    async def screenshot(self, filepath: Path):
        await self.page.screenshot(path=str(filepath), full_page=True)

    def merge_pdfs(self, parts: List[Path], target: Path) -> bool:
        """
        Ενώνει τα `parts` σε ένα PDF στο `target` και σβήνει τα επιμέρους.

        Επιστρέφει True μόνο αν το ενωμένο γράφτηκε ΚΑΙ επαληθεύτηκε. Σε κάθε
        αποτυχία τα επιμέρους μένουν ανέπαφα: καλύτερα τέσσερα αρχεία παρά
        κανένα, όταν πρόκειται για φορολογικά στοιχεία πελάτη.
        """
        existing = [p for p in parts if p.exists()]
        if not existing:
            return False
        try:
            from pypdf import PdfWriter
            writer = PdfWriter()
            for p in existing:
                writer.append(str(p))
            tmp = target.with_suffix(".merging.pdf")
            with tmp.open("wb") as fh:
                writer.write(fh)
            writer.close()

            # Επαλήθευση πριν σβήσουμε ΟΤΙΔΗΠΟΤΕ: το ενωμένο πρέπει να έχει
            # τουλάχιστον όσες σελίδες τα επιμέρους μαζί.
            from pypdf import PdfReader
            merged_pages = len(PdfReader(str(tmp)).pages)
            part_pages = sum(len(PdfReader(str(p)).pages) for p in existing)
            if merged_pages < part_pages:
                tmp.unlink(missing_ok=True)
                self.log(f"  ⚠️ Η ένωση έδωσε {merged_pages} σελίδες αντί για "
                         f"{part_pages} — κρατούνται τα επιμέρους", "error")
                return False

            tmp.replace(target)
            for p in existing:
                if p != target:
                    p.unlink(missing_ok=True)
            self.log(f"  🔗 Ενώθηκαν {len(existing)} αρχεία σε ένα "
                     f"({merged_pages} σελίδες)")
            return True
        except Exception as e:
            self.log(f"  ⚠️ Δεν έγινε ένωση ({e}) — κρατούνται τα επιμέρους",
                     "error")
            return False

    @staticmethod
    def dated_filename(client_name: str, doc_type: str) -> str:
        """
        Όνομα με ΗΜΕΡΟΜΗΝΙΑ αντί για έτος, για έγγραφα που δεν αφορούν
        φορολογικό έτος (μητρώο, ενημερότητες): είναι στιγμιότυπα.
        """
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name.strip()).replace(" ", "_")
        return f"{date.today().isoformat()}_{safe}_{doc_type}.pdf"

    @staticmethod
    def registry_filename(client_name: str) -> str:
        """
        Όνομα για τη Βεβαίωση Μητρώου.

        Φέρει ΗΜΕΡΟΜΗΝΙΑ και όχι έτος: το μητρώο δεν αφορά φορολογικό έτος,
        είναι η τρέχουσα εικόνα της επιχείρησης. Με έτος στο όνομα θα έμοιαζε
        με έγγραφο συγκεκριμένης χρήσης, που δεν είναι.
        """
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name.strip()).replace(" ", "_")
        return f"{date.today().isoformat()}_{safe}_Μητρώο.pdf"

    @staticmethod
    def safe_filename(client_name: str, year: str, doc_type: str,
                      shift_year: bool = True) -> str:
        """
        Το όνομα του αρχείου φέρει το ΦΟΡΟΛΟΓΙΚΟ έτος, δηλαδή year-1.

        Στο portal το «ΔΗΛΩΣΕΙΣ ΕΤΟΥΣ 2025» είναι το έτος υποβολής — η δήλωση
        που περιέχει αφορά το φορολογικό έτος 2024 (φαίνεται και μέσα στο PDF:
        "ΔΗΛΩΣΗ ΦΟΡΟΛΟΓΙΑΣ ΕΙΣΟΔΗΜΑΤΟΣ ΦΟΡΟΛΟΓΙΚΟΥ ΕΤΟΥΣ 2024"). Στα αρχεία μας
        μας ενδιαφέρει το φορολογικό έτος, οπότε αφαιρούμε 1.

        `shift_year=False` για το ΦΠΑ: εκεί το έτος ΔΕΝ μετατοπίζεται, γιατί οι
        περίοδοι είναι μέσα στο ίδιο έτος («1ο Τρίμηνο 2025» → 2025). Με τη
        μετατόπιση τα αρχεία ΦΠΑ του 2025 σώζονταν λανθασμένα ως 2024.
        """
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name.strip())
        safe = safe.replace(" ", "_")
        if not shift_year:
            return f"{year}_{safe}_{doc_type}.pdf"
        try:
            fiscal_year = str(int(year) - 1)
        except (TypeError, ValueError):
            fiscal_year = str(year)  # μη αριθμητικό — το αφήνουμε ως έχει
        return f"{fiscal_year}_{safe}_{doc_type}.pdf"
