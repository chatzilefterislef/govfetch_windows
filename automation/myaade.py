"""
Automation για TaxisNet / AADE portal.

Login:
  income portal → redirect → login.gsis.gr → back to aade.gr

Portals:
  income (Ν):                     www1.aade.gr/taxisnet/income
  webtax (Ε1, Ε3, Εκκαθαριστικό): www1.aade.gr/webtax/incomefp/
  vat (ΦΠΑ):                      www1.aade.gr/taxisnet/vat

Τα income/vat portals παρεμβάλλουν σελίδα «Επιλογή Νομικού Προσώπου» — δες
_select_taxpayer(). Τα labels των κουμπιών αλλάζουν ανά φορολογούμενο
("Ε3 ΥΠΟΧΡΕΟΥ" vs "Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ"), γι' αυτό η επιλογή γίνεται διαβάζοντας
τα πραγματικά labels της σελίδας — δες _click_labeled().
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Callable, List, Optional

from playwright.async_api import TimeoutError as PwTimeout

from .base import BaseAutomation, debug_dir, gr_norm, label_norm

# ------------------------------------------------------------------
# URLs
# ------------------------------------------------------------------
INCOME_ENTRY    = "https://www1.aade.gr/taxisnet/income"
WEBTAX_ENTRY    = "https://www1.aade.gr/webtax/incomefp/"
VAT_ENTRY       = "https://www1.aade.gr/taxisnet/vat"
# «Μητρώο & Επικοινωνία» — ΝΕΟ myAADE portal, Angular εφαρμογή με #! routing.
# Δεν είναι server-rendered σαν τα υπόλοιπα, οπότε το περιεχόμενο εμφανίζεται
# ΜΕΤΑ την εκτέλεση JavaScript· χρειάζεται αναμονή για το στοιχείο, όχι απλό
# networkidle.
REGISTRY_ENTRY  = "https://www1.aade.gr/saadeapps3/comregistry/?#!/arxiki"
# Αποδεικτικό Φορολογικής Ενημερότητας — επίσης νέο myAADE (Angular).
CLEARANCE_ENTRY = "https://www1.aade.gr/saadeapps3/ApodeiktikoEnimerotitas/#!/arxiki"

# Οι λόγοι έκδοσης, όπως ακριβώς εμφανίζονται στο portal. Ο χρήστης διαλέγει
# έναν από αυτούς στη φόρμα — ΔΕΝ επιλέγεται αυτόματα, γιατί το αποδεικτικό
# εκδίδεται δεσμευτικά για τον σκοπό που δηλώνεται.
CLEARANCE_REASONS = {
    "nomimi":    "Για κάθε νόμιμη χρήση (εκτός είσπραξης χρημάτων και "
                 "μεταβίβασης ακινήτων)",
    "eispraxi":  "Είσπραξη χρημάτων από φορείς του Δημοσίου Τομέα (πλην "
                 "Κεντρικής Διοίκησης)",
    "akinito":   "Μεταβίβαση Ακινήτου",
    "kentriki":  "Είσπραξη χρημάτων από φορείς της Κεντρικής Διοίκησης",
}

# ── Ασφαλιστική ενημερότητα (e-ΕΦΚΑ) ─────────────────────────────────────
# Άλλο portal, όχι ΑΑΔΕ: μπαίνει όμως με τους ΙΔΙΟΥΣ κωδικούς TaxisNet μέσω του
# SSO του gsis, γι' αυτό ζει στην ίδια συνεδρία με τα υπόλοιπα.
EFKA_ENTRY = ("https://apps.e-efka.gov.gr/eClearanceCertTaxis/faces/"
              "secureUser/insuranceRequestCommonOper.xhtml")

# Οι αιτίες χορήγησης, όπως ακριβώς εμφανίζονται στη σελίδα.
#   κλειδί -> (πλήρες κείμενο για το UI και τα ονόματα αρχείων,
#              διακριτό απόσπασμα για την αντιστοίχιση στη σελίδα)
# ΓΙΑΤΙ ξεχωριστό απόσπασμα: τα κείμενα είναι τεράστια και σπάνε σε γραμμές,
# οπότε ταίριασμα ολόκληρου κειμένου είναι εύθραυστο. Τα αποσπάσματα είναι
# επιλεγμένα ώστε να μην ταιριάζουν σε δεύτερη αιτία — πρόσεξε ότι το
# «Μεταβίβαση μεταχειρισμένων επαγγελματικών αυτοκινήτων» περιέχει και αυτό τη
# φράση «αυτοκινήτων Δ.Χ.», γι' αυτό το «Μεταβίβαση αυτοκινήτου ΔΧ» ταιριάζει
# μόνο ολόκληρο.
INSURANCE_REASONS = {
    "eispraxi": (
        "Είσπραξη Εκκαθαρισμένων Απαιτήσεων ποσού άνω των 3.000€ ανά "
        "εκκαθαρισμένη απαίτηση",
        "Είσπραξη Εκκαθαρισμένων Απαιτήσεων"),
    "athlitis": (
        "Απόκτηση Αθλητή",
        "Απόκτηση Αθλητή"),
    "daneio": (
        "Σύναψη ή ανανέωση συμβάσεων δανείων άνω των 6.000€",
        "Σύναψη ή ανανέωση συμβάσεων δανείων"),
    "akinito": (
        "Μεταβίβαση ακινήτων λόγω πώλησης, γονικής παροχής ή δωρεάς",
        "Μεταβίβαση ακινήτων λόγω πώλησης"),
    "empragmato": (
        "Σύσταση εμπράγματου δικαιώματος επί ακινήτου",
        "Σύσταση εμπράγματου δικαιώματος"),
    "autokinito_dx": (
        "Μεταβίβαση αυτοκινήτου ΔΧ",
        "Μεταβίβαση αυτοκινήτου ΔΧ"),
    "epaggelmatika": (
        "Μεταβίβαση μεταχειρισμένων επαγγελματικών αυτοκινήτων (πλην "
        "αυτοκινήτων Δ.Χ.), μηχανοκινήτων θαλασσίων σκαφών άνω των πέντε (5) "
        "μέτρων, ελικοπτέρων, ανεμοπτέρων, αεροσκαφών και επαγγελματικών "
        "σκαφών αλιείας",
        "Μεταβίβαση μεταχειρισμένων επαγγελματικών"),
    "ergoliptis": (
        "Συμμετοχή εργολήπτη σε δημοπρασία οποιουδήποτε τεχνικού έργου",
        "Συμμετοχή εργολήπτη σε δημοπρασία"),
    "diagonismoi": (
        "Συμμετοχή σε διαγωνισμούς ανάληψης δημοσίων έργων ή προμηθειών του "
        "Δημοσίου και των ΝΠΔΔ",
        "Συμμετοχή σε διαγωνισμούς ανάληψης"),
    "koinopraxia": (
        "Συμμετοχή ως μέλος σε Κοινοπραξία ή ως εταίρος σε Ο.Ε, Ε.Ε, Ε.Π.Ε.",
        "Συμμετοχή ως μέλος σε Κοινοπραξία"),
    "prosymfono": (
        "Σύνταξη συμβολαιογραφικού προσυμφώνου με τον εργολάβο",
        "Σύνταξη συμβολαιογραφικού προσυμφώνου"),
    "nomimi": (
        "Κάθε νόμιμη χρήση, προβλεπόμενη από ειδικές διατάξεις, πέραν αυτών "
        "του Ν. 4611/2019",
        "Κάθε νόμιμη χρήση"),
}

# Το πεδίο «Είδος Ασφαλ. Ενημερότητας». Δύο ΤΕΛΕΙΩΣ διαφορετικά πράγματα:
# το 01 εκδίδει αποδεικτικό, το 00 καταχωρεί υπεύθυνη δήλωση εξαίρεσης.
# Επιλέγεται ρητά από τον χρήστη — δεν μαντεύεται.
INSURANCE_KINDS = {
    "01": "01 Αποδεικτικό Ασφαλιστικής Ενημερότητας",
    "00": "00 Καταχώρηση Υπεύθυνης Δήλωσης Εξαίρεσης",
}

# Λίστα υποχρεώσεων ανά έντυπο/έτος. ΙΔΙΟ μοτίβο για ΦΠΑ και εισόδημα — αλλάζει
# μόνο το declarationType (vatF2 / incomeN), οπότε η ροή «υποχρεώσεις →
# Επεξεργασία Δηλώσεων → Προβολή» είναι κοινή.
LIABILITIES_URL = ("https://www1.aade.gr/taxisnet/{portal}/protected/"
                   "displayLiabilitiesForYear.htm?declarationType={dtype}&year={year}")

# login.gsis.gr selectors
SEL_USER = "input[name='username'], #username"
SEL_PASS = "input[name='password'], #password"
SEL_SUB  = (
    "a[onclick*='submit' i], a[onclick*='login' i], "
    "input[type='submit'], button[type='submit'], "
    "a:has-text('Είσοδος'), a:has-text('Σύνδεση'), button:has-text('Είσοδος')"
)

DOCUMENT_LABELS = {
    "e1":             "Ε1",
    "e3":             "Ε3",
    "n":              "Ν",
    "ekkatharistiko": "Εκκαθαριστικό",
    "fpa":            "ΦΠΑ",
    "mitroo":         "Μητρώο",
    "forologiki":     "Φορολογική Ενημερότητα",
    "asfalistiki":    "Ασφαλιστική Ενημερότητα",
}

# Έγγραφα που ΔΕΝ εξαρτώνται από έτος: κατεβαίνουν μία φορά ανά τρέξιμο, όσα
# έτη κι αν επιλεγούν. Το μητρώο είναι η τρέχουσα εικόνα της επιχείρησης —
# χωρίς αυτό, επιλογή τριών ετών θα κατέβαζε τρεις φορές το ίδιο έγγραφο.
YEAR_INDEPENDENT_DOCS = {"mitroo", "forologiki", "asfalistiki"}

# debug_dir(): στα Windows δεν υπάρχει /tmp, οπότε τα screenshots διάγνωσης δεν
# γράφονταν καθόλου (οι κλήσεις είναι σε try/except, άρα σιωπηλά).
DEBUG_SHOT = debug_dir() / "gov_debug.png"


class DocumentNotAvailable(Exception):
    """
    Το έγγραφο δεν ΥΠΑΡΧΕΙ για αυτόν τον φορολογούμενο/έτος — δεν είναι σφάλμα.

    Ξεχωρίζει από τα πραγματικά σφάλματα ώστε στο τέλος να φαίνεται καθαρά τι
    δεν υπήρχε (π.χ. νομικό πρόσωπο δεν έχει Ε1) και τι όντως χάλασε. Χωρίς τον
    διαχωρισμό, κάθε λείπον έγγραφο έμοιαζε με βλάβη.
    """


class MyAADEAutomation(BaseAutomation):

    def __init__(self, log_callback: Callable, ready_event: Optional[asyncio.Event] = None):
        super().__init__(log_callback)
        self._ready = ready_event
        # Ατομική = φυσικό πρόσωπο· δες _select_taxpayer(). Ορίζεται στο run().
        self.is_atomiki = True

    # ------------------------------------------------------------------
    # Login μέσω login.gsis.gr
    # ------------------------------------------------------------------
    async def login(self, username: str, password: str):
        self.log("↗ Σύνδεση στο TaxisNet (μέσω login.gsis.gr)…")
        await self.page.goto(INCOME_ENTRY, wait_until="domcontentloaded", timeout=30_000)
        await self.page.wait_for_load_state("networkidle", timeout=20_000)

        try:
            await self.page.wait_for_selector(SEL_USER, timeout=15_000)
        except PwTimeout:
            await self.page.screenshot(path=str(DEBUG_SHOT))
            raise RuntimeError(
                f"Δεν βρέθηκε φόρμα login. URL: {self.page.url}\n"
                f"Screenshot: {DEBUG_SHOT}"
            )

        self.log(f"  Φόρμα στο: {self.page.url}")
        self.log("🔑 Εισαγωγή κωδικών…")
        await self.page.fill(SEL_USER, username)
        await self.page.fill(SEL_PASS, password)

        # Στο gsis.gr το submit μπορεί να είναι anchor με onclick
        submitted = False
        try:
            sub = await self.page.wait_for_selector(SEL_SUB, timeout=5_000)
            await sub.click()
            submitted = True
        except PwTimeout:
            pass

        if not submitted:
            await self.page.evaluate(
                "() => { const f = document.querySelector('form'); if(f) f.submit(); }"
            )

        await self.page.wait_for_load_state("networkidle", timeout=30_000)

        # ΕΛΕΓΧΟΣ ΣΤΟ HOST, όχι σε ολόκληρο το URL.
        # ΠΑΓΙΔΑ: το URL της ΑΠΟΤΥΧΗΜΕΝΗΣ σύνδεσης περιέχει παράμετρο
        # resource_url=…www1.aade.gr…, οπότε ένα «'aade.gr' in url» περνούσε
        # και δηλωνόταν «Σύνδεση επιτυχής» ενώ ήμασταν ακόμη στη φόρμα του
        # login.gsis.gr. Μετά, ΚΑΘΕ έγγραφο αποτύγχανε με παραπλανητικά
        # μηνύματα τύπου «δεν βρέθηκε το έντυπο», αντί για «λάθος κωδικοί».
        host = urlparse(self.page.url).netloc.lower()
        if not host.endswith("aade.gr"):
            # Το Oracle Access Manager επιστρέφει τον λόγο στο URL
            params = parse_qs(urlparse(self.page.url).query)
            code = (params.get("p_error_code") or [""])[0]
            if code:
                reason = {
                    "OAM-2": "λάθος όνομα χρήστη ή κωδικός",
                    "OAM-3": "ο λογαριασμός είναι κλειδωμένος",
                    "OAM-4": "ο κωδικός έχει λήξει",
                }.get(code, f"κωδικός σφάλματος {code}")
                raise RuntimeError(
                    f"Η σύνδεση στο TaxisNet απέτυχε: {reason}. "
                    f"Έλεγξε τους κωδικούς του πελάτη."
                )
            err = await self.page.query_selector(
                ".error, #errorDiv, span[class*='error' i]")
            if err:
                text = (await err.inner_text()).strip()
                if text:
                    raise RuntimeError(f"Η σύνδεση στο TaxisNet απέτυχε: {text}")
            await self.page.screenshot(path=str(DEBUG_SHOT))
            raise RuntimeError(
                f"Η σύνδεση στο TaxisNet δεν ολοκληρώθηκε — παραμένουμε στο "
                f"{host}. Screenshot: {DEBUG_SHOT}"
            )

        self.log(f"✅ Σύνδεση επιτυχής! ({self.page.url})", "success")

    # ------------------------------------------------------------------
    # Βοηθητικές
    # ------------------------------------------------------------------
    async def _goto(self, url: str):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        await self.page.wait_for_load_state("networkidle", timeout=15_000)

    async def _click_and_follow(self, el):
        """
        Κάνει κλικ και ακολουθεί είτε πλοήγηση στην ίδια σελίδα είτε άνοιγμα σε νέο
        tab/popup (συχνό στα gov portals για viewPdf-type links) — μεταθέτει το
        self.page στο νέο tab όταν χρειάζεται, ώστε τα επόμενα βήματα να δουλέψουν
        στη σωστή σελίδα.
        """
        ctx = self.page.context
        popup_page = None

        def on_page(p):
            nonlocal popup_page
            popup_page = p

        ctx.on("page", on_page)
        try:
            await el.click()
            for _ in range(15):
                if popup_page is not None:
                    break
                await self.page.wait_for_timeout(200)
            if popup_page is not None:
                try:
                    await popup_page.wait_for_load_state("domcontentloaded",
                                                         timeout=8_000)
                    self.page = popup_page
                    self.log(f"  ↗ Άνοιξε νέο tab: {self.page.url}")
                except Exception:
                    # Σε HEADLESS το PDF δεν ανοίγει σε viewer αλλά κατεβαίνει:
                    # το portal ανοίγει popup που ΔΕΝ φορτώνει ποτέ σελίδα.
                    # Πριν, η αναμονή έσκαγε στα 15 δευτερόλεπτα και έριχνε όλη
                    # τη λήψη — παρότι το αρχείο είχε ήδη πιαστεί από το δίκτυο.
                    self.log("  ↗ Νέο tab χωρίς σελίδα (λήψη αρχείου) — συνεχίζω")
                    # ΜΗΝ το κλείσεις. Το page.close() σε popup που δεν έκανε
                    # ΠΟΤΕ commit πλοήγηση ΔΕΝ ΕΠΙΣΤΡΕΦΕΙ ΠΟΤΕ — μετρήθηκε με
                    # απομονωμένη αναπαραγωγή: το await κρεμόταν και στα 10
                    # δευτερόλεπτα ορίου. Δεν πετάει εξαίρεση, οπότε το try/
                    # except από κάτω δεν βοηθούσε καθόλου: όλη η λήψη πάγωνε
                    # σιωπηλά στο Μητρώο και δεν έφτανε ποτέ στην ενημερότητα.
                    # Χωρίς close() η ροή συνεχίζει κανονικά (επαληθεύτηκε), και
                    # το άδειο tab το καθαρίζει το cleanup() με τον browser.
            else:
                # Δεν είναι σφάλμα αν δεν ησυχάσει το δίκτυο: πολλές σελίδες του
                # portal κρατούν ανοιχτά αιτήματα (χρονόμετρο συνεδρίας κ.λπ.).
                try:
                    await self.page.wait_for_load_state("networkidle",
                                                        timeout=20_000)
                except Exception:
                    pass
        finally:
            ctx.remove_listener("page", on_page)

    # Όλα τα clickable στοιχεία (a / button / input) της σελίδας, μαζί με το
    # πραγματικό τους label. Το χρησιμοποιούμε γιατί τα labels του portal
    # αλλάζουν ανά φορολογούμενο ("Ε3 ΥΠΟΧΡΕΟΥ" vs "Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ"), οπότε
    # τα hardcoded selectors σπάνε — καλύτερα να διαβάζουμε τι υπάρχει όντως.
    CLICKABLE_CSS = "a, button, input[type='button'], input[type='submit']"

    async def _settle(self):
        """Περιμένει να ησυχάσει η σελίδα μετά από (πιθανή) πλοήγηση."""
        for state in ("domcontentloaded", "networkidle"):
            try:
                await self.page.wait_for_load_state(state, timeout=15_000)
            except Exception:
                pass

    LABELS_JS = """(css) => [...document.querySelectorAll(css)].map((el, i) => ({
                       i,
                       label: (el.value || el.innerText || el.textContent || '')
                                .trim().replace(/\\s+/g, ' '),
                       disabled: !!el.disabled,
                   })).filter(o => o.label)"""

    LABELS_IN_JS = """(el, css) => [...el.querySelectorAll(css)].map((e, i) => ({
                          i,
                          label: (e.value || e.innerText || e.textContent || '')
                                   .trim().replace(/\\s+/g, ' '),
                          disabled: !!e.disabled,
                      })).filter(o => o.label)"""

    async def _clickables(self, scope=None) -> List[dict]:
        """
        Τα clickable στοιχεία της σελίδας — ή, αν δοθεί `scope` (locator),
        μόνο όσα βρίσκονται ΜΕΣΑ σε αυτό (π.χ. σε μια συγκεκριμένη γραμμή).
        """
        # Η επιλογή έτους πυροδοτεί πλοήγηση· αν το evaluate πέσει πάνω σε
        # πλοήγηση εν εξελίξει, το context καταστρέφεται — ξαναδοκιμάζουμε.
        last_err = None
        for _ in range(3):
            await self._settle()
            try:
                if scope is None:
                    return await self.page.evaluate(self.LABELS_JS, self.CLICKABLE_CSS)
                return await scope.evaluate(self.LABELS_IN_JS, self.CLICKABLE_CSS)
            except Exception as e:
                last_err = e
                if "Execution context was destroyed" not in str(e):
                    raise
                await self.page.wait_for_timeout(800)
        raise last_err

    async def _click_labeled(self, preferences: List[str], what: str,
                             avoid: Optional[List[str]] = None,
                             scope=None) -> Optional[str]:
        """
        Κάνει κλικ στο πρώτο στοιχείο που ταιριάζει με τη σειρά προτίμησης και
        επιστρέφει το label που πατήθηκε (ή None). Ο caller χρειάζεται να ξέρει
        ΠΟΙΟ πατήθηκε, γιατί π.χ. το Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ σώζεται με άλλο όνομα.

        Κάθε προτίμηση δοκιμάζεται πρώτα ως ακριβές label, μετά ως υποσύνολο.
        Το `avoid` αποκλείει labels που δεν είναι ποτέ το ζητούμενο έγγραφο
        (π.χ. "Ε3 - myDATA", "ΣΥΝΟΨΗ ..."), για να μη κατέβει λάθος αρχείο.
        Το `scope` περιορίζει την αναζήτηση μέσα σε ένα στοιχείο (π.χ. γραμμή Φ2).
        """
        # ΟΛΕΣ οι συγκρίσεις γίνονται σε label_norm() μορφή: το portal γράφει
        # άλλοτε "ΥΠΟΧΡΕΟΥ", άλλοτε "Υπόχρεου" και άλλοτε με λατινικό "E" στο
        # "E3" — χωρίς κανονικοποίηση η σύγκριση αποτυγχάνει σιωπηλά.
        avoid = [label_norm(a) for a in (avoid or [])]
        items = await self._clickables(scope=scope)
        base = scope if scope is not None else self.page
        for pref in preferences:
            pref_n = label_norm(pref)
            for exact in (True, False):
                for it in items:
                    label_n = label_norm(it["label"])
                    hit = label_n == pref_n if exact else pref_n in label_n
                    if not hit:
                        continue
                    if any(a in label_n for a in avoid):
                        continue
                    if it["disabled"]:
                        self.log(f"  ⚠️ Το '{it['label']}' είναι ανενεργό — παρακάμπτεται", "error")
                        continue
                    self.log(f"  → Κλικ στο '{it['label']}'")
                    el = base.locator(self.CLICKABLE_CSS).nth(it["i"])
                    await self._click_and_follow(el)
                    return it["label"]
        self.log(
            f"  ⚠️ Δεν βρέθηκε κουμπί για {what}. Ζητήθηκαν: {preferences}. "
            f"Διαθέσιμα στη σελίδα: {[i['label'] for i in items]}",
            "error",
        )
        return None

    async def _row_locator(self, code: str):
        """
        Locator της γραμμής της οποίας το ΠΡΩΤΟ κελί είναι ακριβώς `code`.

        Το `tr:has-text('Φ2')` έπιανε γραμμή-περιτύλιγμα (φωλιασμένοι πίνακες),
        οπότε το dropdown/κουμπί που έβρισκε ανήκε στο Φ1 — γι' αυτό κατέληγε
        στο vatF1&year=2010. Το ακριβές πρώτο κελί λύνει το πρόβλημα.
        """
        await self._settle()
        idx = await self.page.evaluate(
            """(code) => {
                   const rows = [...document.querySelectorAll('tr')];
                   for (let i = 0; i < rows.length; i++) {
                       const cell = rows[i].querySelector('td, th');
                       if (cell && cell.innerText.trim() === code) return i;
                   }
                   return -1;
               }""",
            code,
        )
        if idx < 0:
            return None
        return self.page.locator("tr").nth(idx)

    async def _select_year_in(self, scope, year: str) -> bool:
        """Επιλέγει έτος στο dropdown που βρίσκεται ΜΕΣΑ στο `scope` (π.χ. γραμμή Φ2)."""
        sel = scope.locator("select").first
        try:
            await sel.wait_for(timeout=5_000)
        except Exception:
            self.log("  ⚠️ Δεν βρέθηκε dropdown έτους στη γραμμή", "error")
            return False
        options = await sel.evaluate(
            "(el) => [...el.options].map(o => ({value: o.value, text: o.text.trim()}))"
        )
        await sel.click()
        for opt in [{"value": year}, {"label": year}]:
            try:
                await sel.select_option(**opt)
                self.log(f"  📅 Επιλέχθηκε έτος {year} στο dropdown της γραμμής")
                return True
            except Exception:
                continue
        self.log(
            f"  ⚠️ Το έτος {year} δεν υπάρχει στο dropdown. "
            f"Διαθέσιμα: {[o['text'] for o in options]}",
            "error",
        )
        return False

    # Γραμμές που ανήκουν στο "σκελετό" του TaxisNet και υπάρχουν σε ΚΑΘΕ σελίδα.
    # Δεν είναι ποτέ γραμμές δεδομένων, αλλά περιέχουν clickables με τα ίδια
    # labels που ψάχνουμε — π.χ. η κίτρινη μπάρα «Έχετε N νέα μηνύματα. Πατήστε
    # προβολή …» έχει link «προβολή» και, καθώς είναι ψηλά στο DOM, γινόταν
    # rows[0] και πατιόταν αντί της δήλωσης, οδηγώντας στα εισερχόμενα μηνύματα.
    CHROME_ROW_PATTERNS = [
        "ΝΕΑ ΜΗΝΥΜΑΤΑ", "ΕΙΣΕΡΧΟΜΕΝΑ", "ΑΠΟΣΥΝΔΕΣΗ", "ΑΛΛΕΣ ΕΦΑΡΜΟΓΕΣ",
        "Ο ΛΟΓΑΡΙΑΣΜΟΣ ΜΟΥ",
    ]

    async def _rows_with_action(self, actions: List[str]) -> List[dict]:
        """
        Γραμμές πίνακα που περιέχουν κουμπί/link με ένα από τα `actions`.
        Επιστρέφει [{idx, text}] — το idx δείχνει σε self.page.locator('tr').
        Χρησιμοποιείται και για τη λίστα ΠΕΡΙΟΔΩΝ («Επεξεργασία Δηλώσεων»)
        και για τη λίστα ΔΗΛΩΣΕΩΝ μιας περιόδου («Προβολή»).

        Οι γραμμές του σκελετού της σελίδας (CHROME_ROW_PATTERNS) αποκλείονται.
        """
        await self._settle()
        # Ξεκινάμε από τα ΚΟΥΜΠΙΑ και ανεβαίνουμε στη γραμμή τους (closest('tr')),
        # αντί να διατρέχουμε γραμμές και να τις φιλτράρουμε.
        # ΓΙΑΤΙ: η προηγούμενη έκδοση πετούσε κάθε γραμμή που περιέχει <table>
        # («γραμμή-περιτύλιγμα»). Στη σελίδα υποχρεώσεων ΦΠΑ το κελί «Ενέργειες»
        # έχει το κουμπί μέσα σε φωλιασμένο πίνακα, οπότε ΟΛΕΣ οι γραμμές
        # περιόδων πετάγονταν και έβγαινε «Δεν βρέθηκαν περίοδοι» — παρότι τα
        # 4 κουμπιά «Επεξεργασία Δηλώσεων» ήταν εμφανώς εκεί.
        # Επιστρέφουμε και το `btn` (δείκτης του κουμπιού σε ΟΛΗ τη σελίδα), ώστε
        # ο caller να πατάει κατευθείαν το σωστό κουμπί χωρίς scope σε γραμμή.
        return await self.page.evaluate(
            """([css, actions, chrome]) => {
                   const norm = s => s.toUpperCase()
                       .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                       // Λατινικά ομοιογράμματα -> ελληνικά, ΑΚΡΙΒΩΣ όπως το
                       // label_norm() της Python. Χωρίς αυτό, οι δύο πλευρές
                       // δεν συμφωνούν: το portal γράφει «Έκδοση Aποδεικτικού»
                       // με ΛΑΤΙΝΙΚΟ A και η σύγκριση αποτυγχάνει σιωπηλά.
                       .replace(/[ABEHIKMNOPTXYZ]/g,
                                c => 'ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ'['ABEHIKMNOPTXYZ'.indexOf(c)]);
                   const allTr = [...document.querySelectorAll('tr')];
                   const out = [], seen = new Set();
                   [...document.querySelectorAll(css)].forEach((el, btn) => {
                       const label = ((el.value || el.innerText || el.textContent || '')
                                       .replace(/\\s+/g, ' ')).trim();
                       if (!label) return;
                       if (norm(label).includes('ΥΠΟΒΟΛΗ ΤΡΟΠΟΠΟΙΗΤΙΚΗΣ')) return;
                       if (!actions.some(a => label.includes(a))) return;

                       // Το closest('tr') δίνει τον ΕΣΩΤΕΡΙΚΟ tr του φωλιασμένου
                       // πίνακα του κελιού, που δεν έχει κείμενο δεδομένων (και
                       // έτσι χανόταν το «Τροποποιητική»). Ανεβαίνουμε προς τα έξω
                       // ώσπου να βρεθεί γραμμή με περιεχόμενο ΠΕΡΑ από το label.
                       let chosen = null, rowText = '';
                       for (let node = el.closest('tr'); node;
                            node = node.parentElement
                                   ? node.parentElement.closest('tr') : null) {
                           const t = node.innerText.trim().replace(/\\s+/g, ' ');
                           if (norm(t).replace(norm(label), '').trim().length > 0) {
                               chosen = node; rowText = t; break;
                           }
                       }
                       // Καμία γραμμή με δεδομένα: σύνδεσμος μενού («2.Προβολή»)
                       if (!chosen) return;
                       // Σκελετός σελίδας (μπάρα μηνυμάτων κ.λπ.) — ποτέ δεδομένα
                       if (chrome.some(c => norm(rowText).includes(c))) return;

                       const idx = allTr.indexOf(chosen);
                       if (seen.has(idx)) return;   // ένα κουμπί ανά γραμμή
                       seen.add(idx);
                       out.push({idx, btn, label, text: rowText});
                   });
                   return out;
               }""",
            [self.CLICKABLE_CSS, actions, self.CHROME_ROW_PATTERNS],
        )

    # Ό,τι μπορεί να είναι κλικαρίσιμο μέσα σε κελί. Πολύ ευρύτερο από το
    # CLICKABLE_CSS επίτηδες: στη σελίδα υποχρεώσεων ΦΠΑ τα κουμπιά «Επεξεργασία
    # Δηλώσεων» ΔΕΝ είναι a/button/input[submit|button] — δεν εμφανίζονταν καθόλου
    # στα clickables — γι' αυτό εδώ δεν υποθέτουμε τύπο στοιχείου.
    CELL_CLICKABLE_CSS = ("a, button, input, [onclick], [href], "
                          "[role='button'], img, span, div")

    # Ενέργειες που ΔΕΝ πατάμε ΠΟΤΕ: αλλάζουν κατάσταση στο portal. Στο κελί
    # «Ενέργειες» της λίστας δηλώσεων το «Υποβολή τροπ/κής» είναι ΠΡΩΤΟ, πριν το
    # «Προβολή» — παίρνοντας «το πρώτο κλικαρίσιμο» θα ξεκινούσαμε υποβολή
    # τροποποιητικής δήλωσης. Ο έλεγχος γίνεται σε normalized μορφή, γιατί το
    # portal γράφει άλλοτε «Υποβολή τροπ/κής» και άλλοτε «Υποβολή Τροποποιητικής».
    NEVER_CLICK = ["ΥΠΟΒΟΛΗ", "ΟΡΙΣΤΙΚΟΠΟΙΗΣΗ", "ΔΙΑΓΡΑΦΗ", "ΑΚΥΡΩΣΗ",
                   "ΠΛΗΡΩΜΗ", "ΑΠΟΣΤΟΛΗ", "ΝΕΑ ΔΗΛΩΣΗ",
                   # Το «Μητρώο & Επικοινωνία» έχει δίπλα-δίπλα με τις
                   # βεβαιώσεις: «Αλλαγή Κωδικού TAXISnet», «Αλλαγή Στοιχείων
                   # Μητρώου», «Δήλωση Λογαριασμού IBAN». Ένα λάθος κλικ εκεί
                   # αλλάζει κωδικό πρόσβασης ή στοιχεία της επιχείρησης.
                   "ΑΛΛΑΓΗ", "ΔΗΛΩΣΗ ΛΟΓΑΡΙΑΣΜΟΥ", "ΕΞΟΥΣΙΟΔΟΤΗΣ"]

    # Γραμμές που δεν αγγίζουμε, με βάση το κείμενο ΤΗΣ ΓΡΑΜΜΗΣ και όχι του
    # κουμπιού (χρήσιμο όταν η ετικέτα είναι ουδέτερη, π.χ. σκέτο «Συνέχεια»).
    # Το «ΝΕΑ ΔΗΛΩΣΗ» ΔΕΝ είναι εδώ: παρότι ο τίτλος του πίνακα λέει «ΝΕΑ ΔΗΛΩΣΗ
    # Φ.Ε.Ν.Π», το «Συνέχεια» της γραμμής «άρθρου 45 ν.4172/2013(N)» είναι η
    # κανονική πλοήγηση ΣΤΟ έντυπο (επιβεβαιωμένο από τον χρήστη), όχι υποβολή.
    NEVER_ROW: List[str] = []

    # Το print-to-PDF έσωζε ό,τι σελίδα βρισκόταν μπροστά (π.χ. το μενού) με
    # όνομα σωστού εγγράφου. Για λογιστική χρήση αυτό είναι χειρότερο από καθαρή
    # αποτυχία, γι' αυτό μένει κλειστό ακόμη και σε headless που το υποστηρίζει.
    ALLOW_PRINT_TO_PDF = False

    async def _action_cells(self, header: str, actions: Optional[List[str]] = None,
                            avoid: Optional[List[str]] = None,
                            allow: Optional[List[str]] = None) -> List[dict]:
        """
        Εντοπίζει τη στήλη με κεφαλίδα `header` (π.χ. «Ενέργειες») και επιστρέφει
        για κάθε γραμμή δεδομένων το κλικαρίσιμο στοιχείο ΑΥΤΗΣ της στήλης.

        Δουλεύει με τη ΔΟΜΗ του πίνακα (κεφαλίδα → δείκτης στήλης → κελί), όχι με
        labels ή τύπους στοιχείων, γι' αυτό είναι ανθεκτικό σε φωλιασμένους
        πίνακες και σε κουμπιά που δεν είναι κανονικά <button>/<input>.

        Κάθε στόχος σημαδεύεται με data-gdf-click="k" ώστε το κλικ να γίνεται
        ακριβώς σε αυτό το στοιχείο, χωρίς δείκτες που μπορεί να μετακινηθούν.
        """
        await self._settle()
        return await self.page.evaluate(
            """([header, cellCss, actions, avoid, never, neverRow, allow]) => {
                   const norm = s => s.toUpperCase()
                       .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                       // Λατινικά ομοιογράμματα -> ελληνικά, ΑΚΡΙΒΩΣ όπως το
                       // label_norm() της Python. Χωρίς αυτό, οι δύο πλευρές
                       // δεν συμφωνούν: το portal γράφει «Έκδοση Aποδεικτικού»
                       // με ΛΑΤΙΝΙΚΟ A και η σύγκριση αποτυγχάνει σιωπηλά.
                       .replace(/[ABEHIKMNOPTXYZ]/g,
                                c => 'ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ'['ABEHIKMNOPTXYZ'.indexOf(c)]);
                   const H = norm(header);
                   document.querySelectorAll('[data-gdf-click]')
                       .forEach(e => e.removeAttribute('data-gdf-click'));

                   // Το κελί κεφαλίδας πρέπει να είναι ΤΟ ΙΔΙΟ το «Ενέργειες»,
                   // όχι κελί που το περιέχει κάπου μέσα του: τα κελιά-περιτυλίγματα
                   // του layout περιέχουν ΟΛΟ το κείμενο της σελίδας, άρα και τη
                   // λέξη «Ενέργειες», και τότε διαλέγαμε λάθος πίνακα (κατέληγε
                   // να πατά το κουμπί «Δηλώσεις» και να γυρίζει στα έντυπα).
                   const isHeader = td => {
                       const t = norm(td.innerText.trim());
                       return t === H || (t.includes(H) && t.length <= H.length + 5);
                   };

                   let best = [];
                   for (const table of document.querySelectorAll('table')) {
                       const rows = [...table.rows];
                       let hdrRow = -1, col = -1;
                       for (let r = 0; r < rows.length && col < 0; r++) {
                           const cells = [...rows[r].cells];
                           if (cells.length < 2) continue;   // μονοκύτταρη = περιτύλιγμα
                           const c = cells.findIndex(isHeader);
                           if (c >= 0) { hdrRow = r; col = c; }
                       }
                       if (col < 0) continue;
                       const found = [];
                       for (let r = hdrRow + 1; r < rows.length; r++) {
                           const cells = [...rows[r].cells];
                           if (col >= cells.length) continue;
                           // Γραμμή δεδομένων: έχει κείμενο. Οι γραμμές που έχουν
                           // ΜΟΝΟ κουμπί δίνουν κενό innerText (τα value των input
                           // δεν μετρούν) — αυτές είναι περιτυλίγματα, όχι δεδομένα.
                           const rowText = rows[r].innerText.trim()
                                               .replace(/\\s+/g, ' ');
                           if (!rowText) continue;
                           // Γραμμές που δεν αγγίζουμε (π.χ. «ΝΕΑ ΔΗΛΩΣΗ»), με
                           // βάση το κείμενο της γραμμής και όχι του κουμπιού
                           if (neverRow.some(n => norm(rowText).includes(n)))
                               continue;
                           const cell = cells[col];
                           // ΟΛΑ τα υποψήφια του κελιού, όχι το πρώτο: το κελί
                           // «Ενέργειες» έχει πολλά κουμπιά («Υποβολή τροπ/κής»,
                           // «Προβολή», «Κατάσταση») και θέλουμε ΣΥΓΚΕΚΡΙΜΕΝΟ.
                           const cands = [...cell.querySelectorAll(cellCss)];
                           if (!cands.length && cell.getAttribute('onclick'))
                               cands.push(cell);
                           // Το κουμπί μπορεί να είναι εικόνα — τότε δεν έχει
                           // κείμενο, οπότε πέφτουμε σε alt/title.
                           const labelOf = el => ((el.value || el.innerText ||
                                       el.textContent || el.getAttribute('alt') ||
                                       el.getAttribute('title') || '')
                                      .replace(/\\s+/g, ' ')).trim();
                           let pool = cands.map(el => ({el, label: labelOf(el)}))
                               // Ποτέ ενέργειες που αλλάζουν κατάσταση — εκτός
                               // αν το label ζητήθηκε ΡΗΤΑ στο allow (π.χ. η
                               // έκδοση ενημερότητας, που ΕΙΝΑΙ ο σκοπός μας).
                               .filter(c => allow.some(
                                             a => norm(c.label) === norm(a))
                                         || !never.some(
                                             n => norm(c.label).includes(n)))
                               .filter(c => !avoid.some(
                                   a => norm(c.label).includes(norm(a))));
                           let target = null, label = '';
                           if (actions.length) {
                               // Ακριβές label πρώτα, μετά υποσύνολο· και από τα
                               // ταιριαστά το ΠΙΟ ΣΥΝΤΟΜΟ, ώστε να μη διαλέγεται
                               // ένα div-περιτύλιγμα που περιέχει όλα τα κουμπιά.
                               for (const exact of [true, false]) {
                                   const hits = pool.filter(c => actions.some(a =>
                                       exact ? norm(c.label) === norm(a)
                                             : norm(c.label).includes(norm(a))));
                                   if (hits.length) {
                                       hits.sort((x, y) =>
                                           x.label.length - y.label.length);
                                       target = hits[0].el; label = hits[0].label;
                                       break;
                                   }
                               }
                               // Ζητήθηκε συγκεκριμένη ενέργεια και δεν υπάρχει:
                               // ΔΕΝ πατάμε τυχαίο κουμπί.
                               if (!target) continue;
                           } else {
                               const first = pool.find(c => c.label) || pool[0];
                               if (!first) continue;
                               target = first.el; label = first.label;
                           }
                           found.push({el: target, label, text: rowText,
                                       tag: target.tagName.toLowerCase(),
                                       type: target.getAttribute('type') || ''});
                       }
                       // Ο πίνακας με τις ΠΕΡΙΣΣΟΤΕΡΕΣ γραμμές, όχι ο πρώτος που
                       // έδωσε κάτι — αλλιώς ένας τυχαίος πίνακας 1 γραμμής νικά.
                       if (found.length > best.length) best = found;
                   }
                   return best.map((f, k) => {
                       f.el.setAttribute('data-gdf-click', String(k));
                       return {k, label: f.label || '(χωρίς ετικέτα)',
                               text: f.text, tag: f.tag, type: f.type};
                   });
               }""",
            [header, self.CELL_CLICKABLE_CSS, actions or [], avoid or [],
             self.NEVER_CLICK, self.NEVER_ROW, allow or []],
        )

    async def _action_cells_wait(self, header: str, what: str,
                                 actions: Optional[List[str]] = None,
                                 avoid: Optional[List[str]] = None,
                                 allow: Optional[List[str]] = None,
                                 attempts: int = 8) -> List[dict]:
        """Σαν το _action_cells, με αναμονή να φορτώσει η σελίδα."""
        for attempt in range(1, attempts + 1):
            cells = await self._action_cells(header, actions, avoid, allow)
            if cells:
                if attempt > 1:
                    self.log(f"  ⏳ {what}: εμφανίστηκαν στην προσπάθεια {attempt}")
                return cells
            await self.page.wait_for_timeout(1_000)
        return []

    async def _click_row_action(self, item: dict, what: str) -> bool:
        """
        Πατάει το κουμπί μιας γραμμής, ανεξάρτητα από ποιον εντοπισμό προήλθε:
        `k` → από τη στήλη «Ενέργειες» (data-gdf-click), `btn` → από labels.
        """
        if "k" in item:
            return await self._click_marked(item["k"], what)
        return await self._click_button_index(item["btn"], what)

    async def _find_row_actions(self, header: str, actions: List[str],
                                what: str,
                                allow: Optional[List[str]] = None) -> List[dict]:
        """
        Εντοπισμός γραμμών με ενέργεια, με δύο στρατηγικές:
          1. Από τη ΣΤΗΛΗ `header` (π.χ. «Ενέργειες») — δουλεύει ακόμη κι όταν τα
             κουμπιά δεν είναι a/button/input, που είναι η περίπτωση του ΦΠΑ.
          2. Fallback: από τα labels των clickables.
        """
        cells = await self._action_cells_wait(header, what, actions=actions,
                                              allow=allow)
        if cells:
            kinds = {f"{c['tag']}[{c['type']}]" if c["type"] else c["tag"]
                     for c in cells}
            self.log(
                f"  🧭 {what}: {len(cells)} γραμμές από τη στήλη «{header}» "
                f"(στοιχεία: {', '.join(sorted(kinds))})"
            )
            return cells
        self.log(
            f"  ↩️ {what}: η στήλη «{header}» δεν έδωσε γραμμές — "
            f"δοκιμή με labels", "error",
        )
        return await self._rows_with_action_wait(actions, what)

    async def _click_marked(self, k: int, what: str) -> bool:
        """Κλικ στο στοιχείο που σημαδεύτηκε με data-gdf-click="k"."""
        try:
            el = self.page.locator(f'[data-gdf-click="{k}"]')
            await self._click_and_follow(el)
            return True
        except Exception as e:
            self.log(f"  ⚠️ Απέτυχε το κλικ για {what}: {e}", "error")
            return False

    async def _dump_table_html(self, header: str, tag: str):
        """
        Διαγνωστικό: το HTML του πίνακα που περιέχει την κεφαλίδα `header`.
        Χρειάζεται όταν δεν αναγνωρίζουμε τι είδους στοιχεία είναι τα κουμπιά.
        """
        try:
            html = await self.page.evaluate(
                """(header) => {
                       const norm = s => s.toUpperCase()
                           .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                           // Λατινικά ομοιογράμματα -> ελληνικά, όπως label_norm()
                           .replace(/[ABEHIKMNOPTXYZ]/g,
                                    c => 'ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ'['ABEHIKMNOPTXYZ'.indexOf(c)]);
                       const H = norm(header);
                       for (const t of document.querySelectorAll('table'))
                           if (norm(t.innerText).includes(H))
                               return t.outerHTML;
                       return '(δεν βρέθηκε πίνακας με ' + header + ')';
                   }""",
                header,
            )
            path = DEBUG_SHOT.with_name(f"gov_debug_{tag}.html")
            path.write_text(html[:60_000], encoding="utf-8")
            self.log(f"  🧩 HTML πίνακα «{header}»: {path}", "error")
        except Exception:
            pass

    async def _rows_with_action_wait(self, actions: List[str], what: str,
                                     attempts: int = 12) -> List[dict]:
        """
        Σαν το _rows_with_action, αλλά ΠΕΡΙΜΕΝΕΙ να εμφανιστούν οι γραμμές.

        ΓΙΑΤΙ: το _settle() μπορεί να επιστρέψει ενώ η πλοήγηση δεν έχει
        ολοκληρωθεί, οπότε το evaluate έτρεχε πάνω στο ΠΑΛΙΟ document. Στο ΦΠΑ
        αυτό διάβαζε ακόμη την αρχική σελίδα συντομεύσεων του TaxisNet και
        έβγαζε «Δεν βρέθηκαν περίοδοι», παρότι η σελίδα υποχρεώσεων φόρτωνε
        κανονικά ένα κλάσμα του δευτερολέπτου αργότερα.
        """
        for attempt in range(1, attempts + 1):
            rows = await self._rows_with_action(actions)
            if rows:
                if attempt > 1:
                    self.log(f"  ⏳ {what}: εμφανίστηκαν στην προσπάθεια {attempt}")
                return rows
            if attempt in (1, attempts // 2):
                try:
                    self.log(
                        f"  ⏳ {what}: ακόμη τίποτα (προσπάθεια {attempt}) — "
                        f"σελίδα '{await self.page.title()}' στο {self.page.url}"
                    )
                except Exception:
                    pass
            await self.page.wait_for_timeout(1_000)
        return []

    async def _click_button_index(self, btn: int, what: str) -> bool:
        """
        Πατάει το κουμπί με δείκτη `btn` στη λίστα CLICKABLE_CSS όλης της σελίδας
        — τον δείκτη τον δίνει το _rows_with_action. Αποφεύγει το scope-σε-γραμμή,
        που έσπαγε όταν το κουμπί ήταν σε φωλιασμένο πίνακα μέσα στο κελί.
        """
        try:
            el = self.page.locator(self.CLICKABLE_CSS).nth(btn)
            await self._click_and_follow(el)
            return True
        except Exception as e:
            self.log(f"  ⚠️ Απέτυχε το κλικ για {what}: {e}", "error")
            return False

    def _pick_declaration(self, rows: List[dict]) -> Optional[dict]:
        """
        Μέσα στη λίστα δηλώσεων ΜΙΑΣ περιόδου: επιστρέφει την ΤΡΟΠΟΠΟΙΗΤΙΚΗ αν
        υπάρχει (την πιο πρόσφατη), αλλιώς την πρώτη (αρχική).
        """
        if not rows:
            return None
        for r in rows:
            r["is_tropo"] = "ΤΡΟΠΟΠΟΙΗΤΙΚ" in gr_norm(r["text"])
        amendments = [r for r in rows if r["is_tropo"]]
        if amendments:
            # Το «Προβολή» που πατιέται είναι αυτό ΤΗΣ ΓΡΑΜΜΗΣ της τροποποιητικής
            # (ο caller κάνει scope στο tr), δηλαδή το πιο κοντινό σε αυτήν.
            if len(amendments) > 1:
                self.log(
                    f"  🔁 {len(amendments)} τροποποιητικές — κατεβαίνει η πιο πρόσφατη "
                    f"(τελευταία στη λίστα)"
                )
            else:
                self.log("  🔁 Υπάρχει τροποποιητική — κατεβαίνει αυτή αντί της αρχικής")
            return amendments[-1]
        # Χωρίς τροποποιητική αναμένεται ΜΙΑ μόνο δήλωση/«Προβολή» στην οθόνη.
        if len(rows) > 1:
            self.log(
                f"  ⚠️ Καμία τροποποιητική, αλλά βρέθηκαν {len(rows)} δηλώσεις — "
                f"κατεβαίνει η πρώτη (αρχική)", "error",
            )
        return rows[0]

    async def _back_to(self, list_page, list_url: str):
        """
        Επιστροφή στη λίστα δηλώσεων μετά την προβολή ενός PDF: αν άνοιξε νέο
        tab το κλείνουμε, αλλιώς γυρίζουμε πίσω στην ίδια καρτέλα.
        """
        if self.page is not list_page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = list_page
            return
        try:
            await self.page.go_back()
            await self._settle()
        except Exception:
            pass
        if self.page.url != list_url:
            await self._goto(list_url)

    async def _own_afm(self) -> Optional[str]:
        """Το ΑΦΜ του συνδεδεμένου χρήστη, από την κεφαλίδα της σελίδας."""
        try:
            text = await self.page.inner_text("body")
        except Exception:
            return None
        m = re.search(r"Α\.?Φ\.?Μ\.?\s*:?\s*(\d{9})", text)
        return m.group(1) if m else None

    async def _on_entity_page(self) -> bool:
        if "LegalEntities" in self.page.url:
            return True
        try:
            return "Επιλογή Νομικού Προσώπου" in await self.page.content()
        except Exception:
            return False

    async def _afm_choices(self) -> List[dict]:
        """
        Τα επιλέξιμα ΑΦΜ (9ψήφια) της σελίδας «Επιλογή Νομικού Προσώπου».

        ΔΕΝ περιορίζεται σε a/button/input: στο portal διαπιστώσαμε ότι κουμπιά
        υλοποιούνται και ως <div> (τα «Επεξεργασία Δηλώσεων» του ΦΠΑ), οπότε με
        στενό selector η σελίδα φαινόταν «χωρίς επιλέξιμο ΑΦΜ».

        Κρατάει το ΠΙΟ ΕΣΩΤΕΡΙΚΟ στοιχείο για κάθε ΑΦΜ, ώστε ένα div-περιτύλιγμα
        να μη μετριέται δεύτερη φορά.
        """
        await self._settle()
        return await self.page.evaluate(
            """(css) => {
                   document.querySelectorAll('[data-gdf-afm]')
                       .forEach(e => e.removeAttribute('data-gdf-afm'));
                   const txt = el => ((el.value || el.innerText ||
                                       el.textContent || '')
                                      .replace(/\\s+/g, ' ')).trim();
                   const out = [];
                   let k = 0;
                   for (const el of document.querySelectorAll(css)) {
                       const t = txt(el);
                       if (!/^[0-9]{9}$/.test(t)) continue;
                       // Αν κάποιο παιδί έχει το ΙΔΙΟ κείμενο, αυτό εδώ είναι
                       // περιτύλιγμα — κρατάμε το εσωτερικό.
                       if ([...el.querySelectorAll(css)].some(c => txt(c) === t))
                           continue;
                       el.setAttribute('data-gdf-afm', String(k));
                       out.push({k, label: t});
                       k++;
                   }
                   return out;
               }""",
            self.CELL_CLICKABLE_CSS,
        )

    async def _select_taxpayer(self, is_atomiki: bool):
        """
        Τα portals income/vat παρεμβάλλουν σελίδα «Επιλογή Νομικού Προσώπου»,
        γιατί ο λογαριασμός μπορεί να εκπροσωπεί ΚΑΙ άλλες οντότητες.

        ΚΡΙΣΙΜΟ: σε ΑΤΟΜΙΚΗ επιχείρηση ο φορολογούμενος είναι ο ΙΔΙΟΣ ο χρήστης.
        Αν επιλεγεί εδώ νομικό πρόσωπο, κατεβαίνουν τα έγγραφα ΑΛΛΗΣ οντότητας
        (το είχαμε δει: ΤΡΙΚΚΑ ΑΛΙΚΗ «για λογαριασμό του» ΚΠΤΑ ΚΑΤΑΣΚΕΥΑΣΤΙΚΗ,
        με μηνιαίο ΦΠΑ αντί τριμηνιαίου).
        """
        if not await self._on_entity_page():
            return  # δεν υπάρχει τέτοιο βήμα — προχωράμε κανονικά

        own = await self._own_afm()
        afm_links = await self._afm_choices()

        if is_atomiki:
            # Δεκτό ΜΟΝΟ το ίδιο ΑΦΜ του χρήστη· ποτέ άλλη οντότητα.
            mine = [a for a in afm_links if own and a["label"] == own]
            if mine:
                self.log(f"  👤 Ατομική: επιλογή του ίδιου ΑΦΜ {own}")
                await self._click_and_follow(
                    self.page.locator(f'[data-gdf-afm="{mine[0]["k"]}"]'))
                return
            # Η λίστα δείχνει ΜΟΝΟ όσα νομικά πρόσωπα εκπροσωπεί ο χρήστης — το
            # γράφει και η βοήθεια της σελίδας — άρα ο ΙΔΙΟΣ δεν είναι ποτέ εκεί
            # όταν έχει και ρόλο εκπροσώπου. Για να δράσει ως εαυτός του υπάρχει
            # «Επιλογή Ρόλου» στο μενού.
            if await self._select_own_role(own):
                return

            others = [a["label"] for a in afm_links]
            raise RuntimeError(
                f"Δηλώθηκε ΑΤΟΜΙΚΗ επιχείρηση (ΑΦΜ χρήστη {own}), αλλά το portal "
                f"ζητά επιλογή νομικού προσώπου και προσφέρει μόνο: {others}, "
                f"και δεν βρέθηκε τρόπος να επιλεγεί ο ρόλος του ίδιου του "
                f"χρήστη. Δεν επιλέγω άλλη οντότητα — θα κατέβαιναν έγγραφα "
                f"άλλου φορολογούμενου. Αν ο πελάτης είναι ΝΟΜΙΚΟ πρόσωπο, "
                f"σβήσε το toggle «Ατομική Επιχείρηση»."
            )

    async def _select_own_role(self, own: Optional[str]) -> bool:
        """
        Επιλέγει τον ρόλο «ο ίδιος ο χρήστης» μέσω της «Επιλογή Ρόλου».

        ΓΙΑΤΙ: όταν ο λογαριασμός εκπροσωπεί και νομικά πρόσωπα, το portal
        μπαίνει κατευθείαν στην «Επιλογή Νομικού Προσώπου», που περιέχει ΜΟΝΟ
        τις άλλες οντότητες. Χωρίς αυτό το βήμα, η ατομική επιχείρηση του ίδιου
        του χρήστη ήταν απροσπέλαστη και το ΦΠΑ απλώς αποτύγχανε.

        Επιστρέφει True αν επιλέχθηκε ρόλος και μπορούμε να συνεχίσουμε.
        """
        if not await self._click_labeled(
            ["Επιλογή Ρόλου", "Επιλογή ρόλου"], "Επιλογή Ρόλου",
            avoid=["Ν.Π.", "ΝΟΜΙΚΟΥ"],
        ):
            return False

        shot = DEBUG_SHOT.with_name("gov_debug_roles.png")
        try:
            await self.page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass

        # Ο ρόλος του ίδιου αναγνωρίζεται από το ΑΦΜ του. Αν δεν φαίνεται ΑΦΜ,
        # δοκιμάζουμε λεκτικά που χρησιμοποιεί το portal για το φυσικό πρόσωπο.
        choices = await self._afm_choices()
        self.log(f"  🎭 Ρόλοι με ΑΦΜ: {[c['label'] for c in choices]}")
        mine = [c for c in choices if own and c["label"] == own]
        if mine:
            self.log(f"  👤 Επιλογή ρόλου ιδίου ΑΦΜ {own}")
            await self._click_and_follow(
                self.page.locator(f'[data-gdf-afm="{mine[0]["k"]}"]'))
            return True

        # Το portal γράφει «για τον εαυτό μου» / «ως Εκπρόσωπος Νομικού
        # Προσώπου» — χωρίς ΑΦΜ. Το «για τον εαυτό μου» είναι το ακριβές label
        # που επιβεβαιώθηκε από το log· τα υπόλοιπα μένουν ως εφεδρεία.
        # Το avoid εμποδίζει να επιλεγεί ο ρόλος εκπροσώπου.
        if await self._click_labeled(
            ["για τον εαυτό μου", "Φυσικό Πρόσωπο", "Ο ΕΑΥΤΟΣ ΜΟΥ",
             "Ατομική Επιχείρηση", "Ίδιος", "Εαυτός"],
            "ρόλος φυσικού προσώπου",
            avoid=["ΕΚΠΡΟΣΩΠΟΣ", "ΝΟΜΙΚΟΥ"],
        ):
            self.log("  👤 Επιλέχθηκε ρόλος «για τον εαυτό μου»")
            return True

        labels = [i["label"] for i in await self._clickables()]
        self.log(
            f"  ⚠️ Η «Επιλογή Ρόλου» άνοιξε αλλά δεν αναγνωρίστηκε ο ρόλος του "
            f"ίδιου (ΑΦΜ {own}). Διαθέσιμα: {labels}. Screenshot: {shot}",
            "error",
        )
        return False

        if not afm_links:
            raise RuntimeError(
                f"Σελίδα επιλογής νομικού προσώπου χωρίς επιλέξιμο ΑΦΜ: {self.page.url}"
            )
        if len(afm_links) > 1:
            raise RuntimeError(
                "Ο λογαριασμός εκπροσωπεί πολλές οντότητες: "
                f"{[a['label'] for a in afm_links]}. Πες μου ποιο ΑΦΜ θέλεις "
                "για να προσθέσω πεδίο επιλογής."
            )
        self.log(f"  🏢 Επιλογή νομικού προσώπου ΑΦΜ {afm_links[0]['label']}")
        el = self.page.locator(f'[data-gdf-afm="{afm_links[0]["k"]}"]')
        await self._click_and_follow(el)

    async def _click_first(self, selectors: List[str], timeout: int = 6_000, label: str = "",
                            optional: bool = False) -> bool:
        for sel in selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=timeout // max(len(selectors), 1))
                if el:
                    await self._click_and_follow(el)
                    return True
            except PwTimeout:
                continue
        if label and not optional:
            self.log(
                f"  ⚠️ Δεν βρέθηκε κανένα από τα κουμπιά/links για '{label}' "
                f"(δοκιμάστηκαν: {selectors}) στη σελίδα {self.page.url}",
                "error",
            )
        return False

    async def _select_year(self, year: str):
        # Σκόπευση του select ΚΟΝΤΑ στην ετικέτα έτους — όχι τυχαίο select της σελίδας
        # (το γενικό fallback "select" έπιανε λάθος dropdown σε κάποιες σελίδες).
        candidates = [
            "select:near(:text('ΕΤΟΥΣ'))",
            "select:near(:text('Έτος'))",
            "select:near(:text('έτος'))",
            "select[name*='year' i]",
            "select[name*='etos' i]",
            "select[id*='year' i]",
        ]
        sel = None
        for css in candidates:
            try:
                sel = await self.page.wait_for_selector(css, timeout=2_500)
                if sel:
                    break
            except PwTimeout:
                continue

        if not sel:
            self.log(f"  ⚠️ Δεν βρέθηκε dropdown επιλογής έτους στη σελίδα {self.page.url}", "error")
            return

        # Πρώτα κλικ πάνω στο dropdown (όπως θα έκανε ο χρήστης) ώστε να «ανοίξει»,
        # και μετά επιλογή έτους — κάποιες σελίδες δεν αντιδρούν σε καθαρό select_option.
        try:
            await sel.click()
        except Exception:
            pass

        for opt in [{"value": year}, {"label": year}]:
            try:
                await sel.select_option(**opt)
            except Exception:
                continue
            # Η επιλογή έτους συνήθως κάνει submit/reload. Δίνουμε χρόνο να
            # ΞΕΚΙΝΗΣΕΙ η πλοήγηση πριν περιμένουμε να τελειώσει — αλλιώς το
            # wait_for_load_state επιστρέφει αμέσως και η πλοήγηση σκάει μετά,
            # καταστρέφοντας το context της επόμενης ενέργειας.
            await self.page.wait_for_timeout(600)
            await self._settle()
            self.log(f"  📅 Επιλέχθηκε έτος {year}")
            return
        self.log(f"  ⚠️ Βρέθηκε dropdown έτους αλλά δεν δέχτηκε την τιμή '{year}'", "error")

    async def _pdf(self, filepath: Path, download_sel: Optional[str] = None, doc_label: str = "doc"):
        """
        Σειρά προτεραιότητας:
          1. Πραγματικό PDF (download event ή re-fetch του viewer URL)
          2. Κουμπί λήψης/εκτύπωσης — και ξανά έλεγχος για PDF μετά το κλικ
          3. print-to-PDF (τελευταία λύση — βγάζει σωστό αποτέλεσμα μόνο για HTML σελίδες)
        """
        url = await self.save_real_pdf(filepath)
        if url:
            self.log(f"  ✅ Αποθηκεύτηκε το πραγματικό PDF: {url}", "success")
            return

        if download_sel:
            try:
                btn = await self.page.wait_for_selector(download_sel, timeout=5_000)
                await self._click_and_follow(btn)
            except PwTimeout:
                self.log(
                    f"  ⚠️ Δεν βρέθηκε κουμπί λήψης/εκτύπωσης για {doc_label} "
                    f"(δοκιμάστηκε: {download_sel})",
                    "error",
                )
            else:
                url = await self.save_real_pdf(filepath)
                if url:
                    self.log(f"  ✅ Αποθηκεύτηκε το πραγματικό PDF: {url}", "success")
                    return

        title = await self.page.title()
        shot_path = DEBUG_SHOT.with_name(f"gov_debug_{doc_label}.png")
        try:
            await self.page.screenshot(path=str(shot_path), full_page=True)
        except Exception:
            pass

        # ΔΕΝ σώζουμε print-to-PDF ως fallback. Δύο ξεχωριστοί λόγοι:
        #  (α) σε ορατό browser το page.pdf() κλείνει τον browser και χάνονται όλα
        #      τα επόμενα έγγραφα,
        #  (β) ΚΑΙ ΣΕ HEADLESS, όπου τεχνικά δουλεύει, έσωζε τη ΛΑΘΟΣ σελίδα
        #      (π.χ. το μενού αντί για το Ε3) με όνομα που έμοιαζε σωστό.
        # Ο λόγος (β) ισχύει ανεξάρτητα από το headless, γι' αυτό ο έλεγχος ΔΕΝ
        # γίνεται με το _headless: αλλιώς, περνώντας σε headless, θα επέστρεφε
        # σιωπηλά η αποθήκευση λάθος εγγράφων.
        if not self.ALLOW_PRINT_TO_PDF:
            raise RuntimeError(
                f"Δεν εντοπίστηκε πραγματικό PDF για {doc_label}: η σελίδα "
                f"'{title}' ({self.page.url}) δεν έδωσε αρχείο και δεν βρέθηκε "
                f"κουμπί λήψης/εκτύπωσης. Δεν αποθηκεύεται τίποτα, για να μη "
                f"σωθεί λάθος έγγραφο. Screenshot: {shot_path}"
            )

        self.log("  ⚠️ Δεν εντοπίστηκε πραγματικό PDF — fallback σε print-to-PDF", "error")
        self.log(
            f"  🖨️ Print-to-PDF fallback: σελίδα '{title}' στο {self.page.url} — "
            f"ΠΡΟΣΟΧΗ: αν αυτή δεν είναι η σελίδα με τα πραγματικά στοιχεία, το PDF θα βγει άδειο/λάθος.",
            "error",
        )
        await self.save_as_pdf(filepath)

    # ------------------------------------------------------------------
    # Έντυπο Ν  (νομικά πρόσωπα)
    # ------------------------------------------------------------------
    async def _open_declaration_view(self, portal: str, dtype: str, year: str,
                                     what: str) -> dict:
        """
        Κοινή ροή για ΦΠΑ και εισόδημα: από τη σελίδα υποχρεώσεων του εντύπου
        («displayLiabilitiesForYear.htm?declarationType=…&year=…») φτάνει στην
        ΠΡΟΒΟΛΗ της δήλωσης, και επιστρέφει τη γραμμή που επιλέχθηκε.

        Η σελίδα υποχρεώσεων μπορεί να έχει μία ή δύο βαθμίδες:
          • «Επεξεργασία Δηλώσεων» → λίστα δηλώσεων → «Προβολή»   (όπως στο ΦΠΑ)
          • ή απευθείας «Προβολή» στη γραμμή
        Δοκιμάζονται και οι δύο, γιατί διαφέρει ανά έντυπο και έτος.

        Πάμε ΚΑΤΕΥΘΕΙΑΝ στο URL αντί από τα μενού: η σελίδα «Δηλώσεις Εισοδήματος»
        είναι η ΥΠΗΡΕΣΙΑ ΥΠΟΒΟΛΗΣ («Από εδώ μπορείτε να υποβάλλετε…») και τα
        «Συνέχεια» της ξεκινούν ΝΕΑ δήλωση — δεν θέλουμε να περνάμε από εκεί.
        """
        url = LIABILITIES_URL.format(portal=portal, dtype=dtype, year=year)
        self.log(f"  🔗 {url}")
        await self._goto(url)

        # Πρώτα η δίβαθμη μορφή: «Επεξεργασία Δηλώσεων» ανά περίοδο/έτος
        rows = await self._find_row_actions(
            "Ενέργειες", ["Επεξεργασία Δηλώσεων", "Επεξεργασία"], f"{what} — υποχρεώσεις"
        )
        if rows:
            for r in rows:
                self.log(f"     • {r['text'][:100]}")
            if not await self._click_row_action(rows[0], f"Επεξεργασία Δηλώσεων ({what})"):
                raise RuntimeError(f"απέτυχε το κλικ «Επεξεργασία Δηλώσεων» για {what}")

        return await self._declaration_from_list(what, dtype, year)

    async def _declaration_from_list(self, what: str, tag: str, year: str,
                                     actions: Optional[List[str]] = None) -> dict:
        """
        Από τη λίστα δηλώσεων (αρχική + τροποποιητικές) πατά τη ζητούμενη ενέργεια
        της σωστής γραμμής και επιστρέφει τη γραμμή που επιλέχθηκε.

        Το `actions` επιτρέπει να ζητηθεί ΣΥΓΚΕΚΡΙΜΕΝΗ ενέργεια της ίδιας γραμμής:
        στο έντυπο Ν το κελί «Ενέργειες» έχει «Προβολή», «Προβολή Ε2»,
        «Προβολή Ε3», «Προβολή TAXISNet» κ.ά. Η αντιστοίχιση δοκιμάζει ΠΡΩΤΑ
        ακριβές label, γι' αυτό το «Προβολή» δεν μπερδεύεται με το «Προβολή Ε3».
        """
        actions = actions or ["Προβολή", "Εκτύπωση", "Ανάκτηση"]
        decls = await self._find_row_actions(
            "Ενέργειες", actions, f"{what} — δηλώσεις"
        )
        if not decls:
            shot = DEBUG_SHOT.with_name(f"gov_debug_{tag}_{year}.png")
            try:
                await self.page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            await self._dump_table_html("Ενέργειες", f"{tag}_{year}")
            raise DocumentNotAvailable(
                f"δεν βρέθηκε ενέργεια {actions} για {what} στη σελίδα "
                f"{self.page.url} — πιθανόν δεν υπάρχει αυτό το έντυπο για τη "
                f"δήλωση. Screenshot: {shot}"
            )

        for d in decls:
            self.log(f"     – {d['text'][:100]}")
        pick = self._pick_declaration(decls)
        self.log(f"     → «{pick['label']}» στη γραμμή: {pick['text'][:70]}")
        if not await self._click_row_action(pick, f"{actions[0]} ({what})"):
            raise RuntimeError(f"απέτυχε το κλικ «{actions[0]}» για {what}")
        return pick

    async def _download_e3_nomiko(self, client_name: str, year: str,
                                  dl_dir: Path) -> str:
        """
        Ε3 νομικού προσώπου: κατεβαίνει από το κουμπί «Προβολή Ε3» της ΙΔΙΑΣ
        γραμμής της δήλωσης Ν — δεν είναι ξεχωριστό έντυπο με δικό του URL.

        ΔΕΝ βρίσκεται στο webtax (incomefp): εκείνο είναι το portal των ΦΥΣΙΚΩΝ
        προσώπων, γι' αυτό αποτύγχανε σε ΟΕ.
        """
        await self._open_n_declarations(year)
        pick = await self._declaration_from_list(
            f"Ε3 νομικού προσώπου ({year})", "incomeE3", year,
            actions=["Προβολή Ε3"],
        )
        doc_type = "Ε3_ΤΡΟΠΟΠΟΙΗΤΙΚΗ" if pick["is_tropo"] else "Ε3"
        # shift_year=False: όπως και το Ν, η σελίδα δίνει ΦΟΡΟΛΟΓΙΚΟ έτος
        fname = self.safe_filename(client_name, year, doc_type, shift_year=False)
        await self._pdf(dl_dir / fname, self.INCOME_PDF_SEL, doc_label="E3_nomiko")
        self.log(f"✅ {fname}", "success")
        return fname

    # Η γραμμή του εντύπου Ν στη σελίδα «Δηλώσεις Εισοδήματος». Το κείμενο είναι
    # «Δήλωση Φορολογίας Εισοδήματος Νομικών Προσώπων και Νομικών Οντοτήτων
    # άρθρου 45 v.4172/2013(N)» — ΠΡΟΣΟΧΗ: το portal γράφει «v.4172» με ΛΑΤΙΝΙΚΟ
    # v, γι' αυτό δεν ψάχνουμε «ν.4172» αλλά «ΑΡΘΡΟΥ 45» και «4172».
    N_ROW_MARKERS = ["ΑΡΘΡΟΥ 45", "4172"]

    async def _enter_n_form(self, year: str) -> bool:
        """
        Πατά το «Συνέχεια» ΔΙΠΛΑ στη γραμμή «…άρθρου 45 …(N)» της σελίδας
        «Δηλώσεις Εισοδήματος». Επιστρέφει False αν η γραμμή δεν υπάρχει.

        Ο πίνακας ΔΕΝ έχει κεφαλίδα «Ενέργειες» (η στήλη των κουμπιών είναι
        κενή), οπότε ο εντοπισμός γίνεται με labels και όχι με στήλη.
        Η γραμμή αναγνωρίζεται από το ΚΕΙΜΕΝΟ της, ώστε να μη πατηθεί το
        «Συνέχεια» άλλου εντύπου (Φ-01 010, E5, Φ-01 012 …).
        """
        rows = await self._rows_with_action_wait(["Συνέχεια"], "έντυπα εισοδήματος")
        if not rows:
            return False
        for r in rows:
            self.log(f"     • {r['text'][:95]}")
        target = next(
            (r for r in rows
             if all(m in gr_norm(r["text"]) for m in self.N_ROW_MARKERS)),
            None,
        )
        if target is None:
            self.log(
                "  ↩️ Δεν βρέθηκε η γραμμή «άρθρου 45 …(N)» στα έντυπα εισοδήματος",
                "error",
            )
            return False
        self.log(f"  ➡️ «Συνέχεια» στη γραμμή Ν: {target['text'][:75]}")
        return await self._click_row_action(target, "Συνέχεια (έντυπο Ν)")

    async def _pick_year_row(self, year: str, what: str) -> bool:
        """
        Στη σελίδα που ακολουθεί, διαβάζει τις καταγραφές ΕΤΩΝ και επιλέγει αυτή
        που αντιστοιχεί στο ζητούμενο έτος. Επιστρέφει False αν δεν υπάρχει.

        Το έτος μπορεί να εμφανίζεται ως γραμμή πίνακα ή ως dropdown, γι' αυτό
        δοκιμάζονται και τα δύο. Όλα τα διαθέσιμα έτη γράφονται στο log, ώστε να
        φαίνεται αμέσως αν το portal χρησιμοποιεί οικονομικό ή φορολογικό έτος.
        """
        rows = await self._find_row_actions(
            "Ενέργειες",
            ["Επεξεργασία Δηλώσεων", "Επεξεργασία", "Προβολή", "Συνέχεια"],
            f"{what} — έτη",
        )
        if rows:
            for r in rows:
                self.log(f"     • {r['text'][:95]}")
            match = [r for r in rows if year in r["text"]]
            if len(match) == 1:
                self.log(f"  📅 Επιλογή καταγραφής έτους {year}: "
                         f"{match[0]['text'][:70]}")
                return await self._click_row_action(match[0], f"έτος {year} ({what})")
            if len(match) > 1:
                self.log(
                    f"  ⚠️ {len(match)} καταγραφές περιέχουν το {year} — "
                    f"επιλέγεται η πρώτη: {match[0]['text'][:60]}", "error",
                )
                return await self._click_row_action(match[0], f"έτος {year} ({what})")
            self.log(
                f"  ↩️ Καμία καταγραφή για το έτος {year}. Διαθέσιμες: "
                f"{[r['text'][:45] for r in rows]}", "error",
            )
            return False

        # Χωρίς πίνακα ετών: μήπως υπάρχει dropdown έτους;
        self.log("  ↩️ Χωρίς πίνακα ετών — δοκιμή dropdown")
        await self._select_year(year)
        return True

    # Κουμπιά λήψης/εκτύπωσης στη σελίδα προβολής μιας δήλωσης εισοδήματος
    INCOME_PDF_SEL = (
        "a[href*='.pdf'], button:has-text('Λήψη'), a:has-text('Λήψη PDF'), "
        "a:has-text('PDF'), a:has-text('Εκτύπωση'), button:has-text('Εκτύπωση')"
    )

    async def _open_n_declarations(self, year: str) -> None:
        """
        Φτάνει στη σελίδα «Αποθηκευμένες Δηλώσεις» του εντύπου Ν για το `year`:
        income portal → «Συνέχεια» στη γραμμή «άρθρου 45 …(N)» → καταγραφή έτους.

        Κοινό βήμα για το Ν ΚΑΙ για το Ε3 νομικού προσώπου, αφού και τα δύο
        κατεβαίνουν από την ΙΔΙΑ γραμμή αυτής της σελίδας («Προβολή» και
        «Προβολή Ε3» αντίστοιχα).
        """
        await self._goto(INCOME_ENTRY)
        await self._select_taxpayer(self.is_atomiki)

        if await self._enter_n_form(year):
            if not await self._pick_year_row(year, f"έντυπο Ν ({year})"):
                raise DocumentNotAvailable(
                    f"δεν βρέθηκε καταγραφή για το έτος {year} στο έντυπο Ν — "
                    f"δες τα διαθέσιμα έτη πιο πάνω. Σελίδα: {self.page.url}"
                )
            return
        # Εφεδρική διαδρομή: απευθείας στο URL υποχρεώσεων του εντύπου
        self.log("  ↩️ Εφεδρική διαδρομή: απευθείας URL υποχρεώσεων")
        url = LIABILITIES_URL.format(portal="income", dtype="incomeN", year=year)
        self.log(f"  🔗 {url}")
        await self._goto(url)
        rows = await self._find_row_actions(
            "Ενέργειες", ["Επεξεργασία Δηλώσεων", "Επεξεργασία"],
            f"έντυπο Ν ({year}) — υποχρεώσεις",
        )
        if rows and not await self._click_row_action(
            rows[0], f"Επεξεργασία Δηλώσεων (Ν {year})"
        ):
            raise RuntimeError(f"απέτυχε το κλικ «Επεξεργασία Δηλώσεων» για Ν {year}")

    async def download_n(self, client_name: str, year: str, dl_dir: Path) -> str:
        self.log(f"📄 Έντυπο Ν ({year})…")
        await self._open_n_declarations(year)

        # Ακριβώς «Προβολή» — ΟΧΙ «Προβολή Ε2/Ε3/TAXISNet» της ίδιας γραμμής
        pick = await self._declaration_from_list(
            f"Έντυπο Ν ({year})", "incomeN", year, actions=["Προβολή"]
        )
        doc_type = "Ν_ΤΡΟΠΟΠΟΙΗΤΙΚΗ" if pick["is_tropo"] else "Ν"

        # shift_year=False: η σελίδα λέει ρητά «Φορολογικό Έτος 01/01/2025 -
        # 31/12/2025» για το 2025 που ζητήθηκε, άρα το έτος ΔΕΝ μετατοπίζεται
        # (αντίθετα με το webtax, όπου «ΔΗΛΩΣΕΙΣ ΕΤΟΥΣ 2025» = φορ. έτος 2024).
        fname = self.safe_filename(client_name, year, doc_type, shift_year=False)
        await self._pdf(dl_dir / fname, self.INCOME_PDF_SEL, doc_label="N")
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # Μητρώο επιχείρησης  (νέο myAADE portal — «Μητρώο & Επικοινωνία»)
    # ------------------------------------------------------------------
    async def _click_tile(self, label: str, what: str,
                          avoid: Optional[List[str]] = None,
                          attempts: int = 12) -> bool:
        """
        Κλικ σε «πλακίδιο» του νέου myAADE. Η σελίδα είναι Angular εφαρμογή:
        τα πλακίδια εμφανίζονται ΜΕΤΑ την εκτέλεση JavaScript, οπότε δεν αρκεί
        το networkidle — περιμένουμε να υπάρξει όντως το στοιχείο.

        Το ΑΚΡΙΒΕΣ label είναι κρίσιμο εδώ: στην ίδια οθόνη υπάρχουν «Αλλαγή
        Κωδικού TAXISnet» και «Αλλαγή Στοιχείων Μητρώου» (δες NEVER_CLICK).
        """
        target = label_norm(label)
        blocked = list(self.NEVER_CLICK) + [label_norm(a) for a in (avoid or [])]
        for attempt in range(1, attempts + 1):
            items = await self._tile_choices()
            # Ακριβές ταίριασμα πρώτα· μετά υποσύνολο, αλλά ΠΟΤΕ σε ό,τι
            # απαγορεύεται ρητά ή ζητήθηκε να αποφευχθεί.
            for exact in (True, False):
                for it in items:
                    n = label_norm(it["label"])
                    if any(bad in n for bad in blocked):
                        continue
                    if (n == target) if exact else (target in n):
                        self.log(f"  🔲 Κλικ στο πλακίδιο «{it['label']}»")
                        await self._click_and_follow(
                            self.page.locator(f'[data-gdf-tile="{it["k"]}"]'))
                        return True
            if attempt == 1:
                self.log(f"  ⏳ {what}: αναμονή να φορτώσει η εφαρμογή…")
            await self.page.wait_for_timeout(1_000)

        labels = [i["label"] for i in await self._tile_choices()]
        self.log(f"  ⚠️ Δεν βρέθηκε «{label}». Διαθέσιμα: {labels}", "error")
        return False

    async def _tile_choices(self) -> List[dict]:
        """
        Τα «πλακίδια» του νέου myAADE, ΑΝΕΞΑΡΤΗΤΑ από τύπο στοιχείου.

        ΓΙΑΤΙ ΟΧΙ _clickables(): εκείνο ψάχνει μόνο a/button/input, και τα
        πλακίδια δεν είναι τίποτα από αυτά — η σελίδα «Μητρώο & Επικοινωνία»
        εμφάνιζε κανονικά «Βεβαιώσεις Μητρώου» κ.λπ., αλλά στα clickables
        έβγαινε μόνο το μενού του portal. Ίδιο μοτίβο με τα κουμπιά του ΦΠΑ,
        που ήταν <div>.

        Κρατάει το ΠΙΟ ΕΣΩΤΕΡΙΚΟ στοιχείο κάθε κειμένου, ώστε να μη διαλεγεί
        το περιτύλιγμα που περιέχει ολόκληρο το πλέγμα των πλακιδίων.
        """
        await self._settle()
        return await self.page.evaluate(
            """(css) => {
                   document.querySelectorAll('[data-gdf-tile]')
                       .forEach(e => e.removeAttribute('data-gdf-tile'));
                   const txt = el => ((el.value || el.innerText ||
                                       el.textContent || '')
                                      .replace(/\\s+/g, ' ')).trim();
                   const out = [];
                   let k = 0;
                   for (const el of document.querySelectorAll(css)) {
                       const t = txt(el);
                       // Τα labels πλακιδίων είναι σύντομα· ό,τι μεγαλύτερο
                       // είναι περιτύλιγμα με το κείμενο ολόκληρης της σελίδας.
                       if (!t || t.length > 90) continue;
                       if ([...el.querySelectorAll(css)].some(c => txt(c) === t))
                           continue;
                       el.setAttribute('data-gdf-tile', String(k));
                       out.push({k, label: t});
                       k++;
                   }
                   return out;
               }""",
            self.CELL_CLICKABLE_CSS,
        )

    async def download_mitroo(self, client_name: str, year: str,
                              dl_dir: Path) -> str:
        """
        Βεβαίωση Μητρώου της επιχείρησης.

        ΔΕΝ εξαρτάται από έτος — είναι η τρέχουσα εικόνα του μητρώου, γι' αυτό
        το `run()` το κατεβάζει ΜΙΑ φορά ανά τρέξιμο (YEAR_INDEPENDENT_DOCS) και
        το όνομα φέρει την ημερομηνία λήψης αντί για έτος.
        """
        self.log("📄 Μητρώο επιχείρησης…")
        await self._goto(REGISTRY_ENTRY)

        # Βήμα 1: «Βεβαιώσεις Μητρώου»
        if not await self._click_tile("Βεβαιώσεις Μητρώου", "Βεβαιώσεις Μητρώου"):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε το πλακίδιο «Βεβαιώσεις Μητρώου» στη σελίδα "
                f"{self.page.url}. Screenshot: {await self._shot('mitroo_step1')}"
            )

        # Βήμα 2: «Τρέχουσα Εικόνα Οντότητας/Επιχείρησης».
        # ΠΡΟΣΟΧΗ: δίπλα του υπάρχει «Τρέχουσα Εικόνα ΦΥΣΙΚΟΥ ΠΡΟΣΩΠΟΥ» και δύο
        # «Ιστορικό Μεταβολών». Το κείμενο του πλακιδίου σπάει σε δύο γραμμές,
        # αλλά το _clickables() ενοποιεί τα κενά, οπότε το ακριβές label ισχύει.
        # Το «Φυσικού Προσώπου» μπαίνει ρητά στο avoid: θα έδινε βεβαίωση του
        # ΑΤΟΜΟΥ αντί της επιχείρησης.
        if not await self._click_tile(
            "Τρέχουσα Εικόνα Οντότητας/Επιχείρησης",
            "Τρέχουσα Εικόνα Οντότητας/Επιχείρησης",
            avoid=["ΦΥΣΙΚΟΥ ΠΡΟΣΩΠΟΥ", "ΙΣΤΟΡΙΚΟ"],
        ):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε το «Τρέχουσα Εικόνα Οντότητας/Επιχείρησης» στη "
                f"σελίδα {self.page.url}. "
                f"Screenshot: {await self._shot('mitroo_step2')}"
            )

        # Βήμα 3: επιλογή ΟΛΩΝ των στοιχείων προς έκδοση.
        # Η σελίδα χρησιμοποιεί λίστα πολλαπλής επιλογής, όχι κουτάκια — αλλά
        # δοκιμάζονται και τα δύο, γιατί άλλες οθόνες του portal έχουν κουτάκια.
        picked = await self._select_all_options()
        if picked == 0:
            # Τα κουτάκια ΜΟΝΟ ως εναλλακτική: όταν υπάρχει λίστα επιλογών, τα
            # μόνα checkboxes της σελίδας ανήκουν σε άσχετους πίνακες δεδομένων
            # (π.χ. «Στοιχεία Ενεργών ΦΗΜ») — δεν πρέπει να αγγιχτούν, και η
            # προειδοποίηση «έμειναν ανεπίλεκτες» ήταν παραπλανητική.
            picked = await self._check_all_boxes()
        if picked == 0:
            self.log("  ⚠️ Δεν επιλέχθηκε ΚΑΜΙΑ ενότητα — η βεβαίωση θα βγει "
                     "ελλιπής. Δες το screenshot πριν την έκδοση.", "error")

        # Screenshot ΠΡΙΝ την έκδοση: δείχνει ακριβώς τι θα εκδοθεί. Χωρίς αυτό,
        # μια βεβαίωση με λιγότερες ενότητες απ' όσες πρέπει (π.χ. 2 σελίδες
        # αντί για 4) φαίνεται απολύτως φυσιολογική στο αρχείο.
        pre = await self._shot("mitroo_before_ekdosi")
        self.log(f"  📷 Πριν την έκδοση: {pre}", "success")

        # Βήμα 4: «Έκδοση» και σύλληψη του PDF
        self.reset_pdf_captures()
        if not await self._click_tile("Έκδοση", "Έκδοση βεβαίωσης",
                                      avoid=["ΙΣΤΟΡΙΚΟ"]):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε κουμπί «Έκδοση» στη σελίδα {self.page.url}. "
                f"Screenshot: {await self._shot('mitroo_step3')}"
            )

        fname = self.registry_filename(client_name)
        await self._pdf(
            dl_dir / fname,
            "a[href*='.pdf'], a:has-text('PDF'), a:has-text('Εκτύπωση'), "
            "button:has-text('Εκτύπωση'), button:has-text('Λήψη'), a:has-text('Λήψη')",
            doc_label="mitroo",
        )
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # Ασφαλιστική ενημερότητα  (e-ΕΦΚΑ)
    # ------------------------------------------------------------------
    async def _check_labeled_box(self, fragment: str, what: str) -> bool:
        """
        Τσεκάρει ΤΟ ΕΝΑ κουτάκι του οποίου η γραμμή περιέχει το `fragment`.

        Δεν χρησιμοποιείται το _check_all_boxes(): εκεί τσεκάρονται όλα, ενώ εδώ
        ο χρήστης διάλεξε συγκεκριμένες αιτίες και ΜΟΝΟ αυτές επιτρέπεται να
        πατηθούν — η αίτηση εκδίδεται δεσμευτικά για την αιτία που δηλώνεται.

        Η αντιστοίχιση γίνεται με εγγύτητα, όπως στο _click_near: ανεβαίνουμε
        τους γονείς και κρατάμε το κουτάκι του ΠΙΟ ΣΤΕΝΟΥ προγόνου που περιέχει
        το κείμενο. Χωρίς αυτό, σε σελίδα-πίνακα ο πρόγονος είναι ολόκληρη η
        φόρμα και θα ταίριαζε το πρώτο κουτάκι της σελίδας.
        """
        found = await self.page.evaluate(
            """([css, want]) => {
                   const norm = s => (s || '').toUpperCase()
                       .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                       .replace(/[ABEHIKMNOPTXYZ]/g,
                                c => 'ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ'['ABEHIKMNOPTXYZ'.indexOf(c)])
                       .replace(/\\s+/g, ' ').trim();
                   document.querySelectorAll('[data-gdf-box]')
                       .forEach(e => e.removeAttribute('data-gdf-box'));
                   let best = null, bestDepth = 1e9;
                   for (const el of document.querySelectorAll(css)) {
                       if (el.disabled) continue;
                       let depth = 0;
                       for (let p = el.parentElement; p; p = p.parentElement) {
                           depth++;
                           if (norm(p.innerText).includes(want)) {
                               if (depth < bestDepth) {
                                   bestDepth = depth; best = el;
                               }
                               break;
                           }
                       }
                   }
                   if (!best) return null;
                   best.setAttribute('data-gdf-box', '1');
                   const row = best.closest('tr, li, label, div');
                   return {depth: bestDepth,
                           label: norm(row ? row.innerText : '').slice(0, 70)};
               }""",
            [self.CHECKBOX_CSS, label_norm(fragment)],
        )
        if not found:
            self.log(f"  ⚠️ Δεν βρέθηκε κουτάκι για «{fragment}»", "error")
            return False

        box = self.page.locator('[data-gdf-box="1"]')
        # ΠΡΑΓΜΑΤΙΚΟ κλικ, όχι el.checked = true: η σελίδα είναι JSF και στέλνει
        # AJAX σε κάθε αλλαγή — χωρίς κλικ το server-side μοντέλο δεν ενημερώνεται
        # και η υποβολή φεύγει με μηδέν επιλεγμένες αιτίες.
        await box.scroll_into_view_if_needed(timeout=5_000)
        await box.click(timeout=5_000)
        await self.page.wait_for_timeout(400)

        checked = await self.page.evaluate(
            """() => { const e = document.querySelector('[data-gdf-box="1"]');
                       if (!e) return null;
                       const a = e.getAttribute('aria-checked');
                       return e.checked === true || a === 'true'; }""")
        if checked:
            self.log(f"    ✓ {fragment}")
            return True
        self.log(f"  ⚠️ Το κουτάκι «{fragment}» δεν έμεινε επιλεγμένο", "error")
        return False

    async def _select_insurance_kind(self, kind_label: str) -> bool:
        """Επιλέγει τιμή στο «Είδος Ασφαλ. Ενημερότητας» (κανονικό <select>)."""
        marked = await self.page.evaluate(
            """(want) => {
                   const norm = s => (s || '').toUpperCase()
                       .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                       .replace(/\\s+/g, ' ').trim();
                   document.querySelectorAll('[data-gdf-sel]')
                       .forEach(e => e.removeAttribute('data-gdf-sel'));
                   for (const s of document.querySelectorAll('select')) {
                       for (const o of s.options) {
                           if (norm(o.textContent) === norm(want)) {
                               s.setAttribute('data-gdf-sel', '1');
                               return o.value;
                           }
                       }
                   }
                   return null;
               }""",
            kind_label,
        )
        if marked is None:
            self.log(f"  ⚠️ Δεν βρέθηκε επιλογή «{kind_label}» σε κανένα "
                     f"dropdown της σελίδας", "error")
            return False
        await self.page.locator('[data-gdf-sel="1"]').select_option(
            value=marked, timeout=5_000)
        await self.page.wait_for_timeout(400)
        self.log(f"  ▾ Είδος: {kind_label}")
        return True

    async def download_asfalistiki(self, client_name: str, year: str,
                                   dl_dir: Path) -> List[str]:
        """
        Αποδεικτικό Ασφαλιστικής Ενημερότητας από τον e-ΕΦΚΑ.

        ΥΠΟΒΑΛΛΕΙ ΑΙΤΗΣΗ — δεν ανακτά υπάρχον έγγραφο. Γι' αυτό οι αιτίες
        έρχονται ρητά από τη φόρμα και δεν επιλέγεται τίποτα αυτόματα.

        ΜΙΑ ΥΠΟΒΟΛΗ ΑΝΑ ΑΙΤΙΑ: οι οδηγίες της ίδιας της σελίδας λένε «αφού
        πρώτα επιλέξετε μια μόνο αιτία κάθε φορά». Ο χρήστης μπορεί να διαλέξει
        πολλές στο UI· εδώ γίνονται διαδοχικές υποβολές, μία για καθεμία, με
        καθαρή επαναφόρτωση της φόρμας ενδιάμεσα.

        Αν μια αιτία αποτύχει ΜΕΤΑ την υποβολή, ΔΕΝ ξαναϋποβάλλεται: η αίτηση
        έχει ήδη καταχωρηθεί στον ΕΦΚΑ και επανάληψη θα δημιουργούσε διπλή.
        """
        keys = [k for k in (getattr(self, "insurance_reasons", None) or [])
                if k in INSURANCE_REASONS]
        if not keys:
            raise DocumentNotAvailable(
                "δεν επιλέχθηκε αιτία χορήγησης για την ασφαλιστική "
                "ενημερότητα — η αίτηση υποβάλλεται δεσμευτικά για "
                "συγκεκριμένη αιτία, οπότε δεν την επιλέγω αυτόματα"
            )
        kind_key = getattr(self, "insurance_kind", "") or "01"
        kind_label = INSURANCE_KINDS.get(kind_key)
        if not kind_label:
            raise DocumentNotAvailable(
                f"άγνωστο «Είδος Ασφαλ. Ενημερότητας»: {kind_key!r}")

        self.log(f"📄 Ασφαλιστική ενημερότητα — {len(keys)} "
                 f"{'αιτία' if len(keys) == 1 else 'αιτίες'}, είδος: {kind_label}")

        files: List[str] = []
        for n, key in enumerate(keys, 1):
            full_label, fragment = INSURANCE_REASONS[key]
            self.log(f"  ── Αιτία {n}/{len(keys)}: {full_label[:70]}")

            # Καθαρή φόρμα σε κάθε αιτία: μετά από υποβολή η σελίδα αλλάζει, και
            # τυχόν προηγούμενη επιλογή δεν πρέπει να μείνει τσεκαρισμένη.
            await self._goto(EFKA_ENTRY)
            if not await self._on_efka_form():
                raise DocumentNotAvailable(
                    f"δεν φορτώθηκε η φόρμα του e-ΕΦΚΑ στο {self.page.url} — "
                    f"πιθανόν να μη μεταφέρθηκε η σύνδεση TaxisNet σε αυτή την "
                    f"υπηρεσία. Screenshot: {await self._shot('efka_form')}"
                )

            if not await self._check_labeled_box(fragment, "αιτία χορήγησης"):
                raise DocumentNotAvailable(
                    f"δεν επιλέχθηκε η αιτία «{full_label[:60]}» στη σελίδα "
                    f"{self.page.url}. "
                    f"Screenshot: {await self._shot(f'efka_reason_{key}')}"
                )
            if not await self._select_insurance_kind(kind_label):
                raise DocumentNotAvailable(
                    f"δεν επιλέχθηκε το είδος «{kind_label}». "
                    f"Screenshot: {await self._shot(f'efka_kind_{key}')}"
                )

            pre = await self._shot(f"efka_before_ypovoli_{key}")
            self.log(f"  📷 Πριν την υποβολή: {pre}", "success")

            # Δύο κουμπιά δίπλα-δίπλα, «Υποβολή» και «Καθαρισμός», και η λέξη
            # «Υποβολή» υπάρχει και στο κείμενο των οδηγιών πιο κάτω. Διαλέγουμε
            # με εγγύτητα στο «Είδος Ασφαλ. Ενημερότητας», που είναι το πεδίο
            # ακριβώς από πάνω τους.
            #
            # ΣΗΜΕΙΩΣΗ: το NEVER_CLICK μπλοκάρει την ΥΠΟΒΟΛΗ στα portals της
            # ΑΑΔΕ και ΣΩΣΤΑ — εκεί θα υπέβαλλε δηλώσεις. Εδώ η υποβολή είναι
            # ακριβώς ο ζητούμενος σκοπός, και το _click_near δεν περνά από το
            # NEVER_CLICK, οπότε η γενική προστασία μένει ανέπαφη.
            self.reset_pdf_captures()
            if not await self._click_near("Υποβολή", "Είδος Ασφαλ. Ενημερότητας",
                                          "Υποβολή αίτησης"):
                raise DocumentNotAvailable(
                    f"δεν βρέθηκε το κουμπί «Υποβολή» δίπλα στο «Είδος Ασφαλ. "
                    f"Ενημερότητας» στη σελίδα {self.page.url}. "
                    f"Screenshot: {await self._shot(f'efka_submit_{key}')}"
                )
            self.log("  📨 Η αίτηση υποβλήθηκε")
            await self.page.wait_for_timeout(2_000)

            fname = self.dated_filename(
                client_name, f"Ασφαλιστική_Ενημερότητα_{key}")
            try:
                await self._pdf(
                    dl_dir / fname,
                    "a[href*='.pdf'], a:has-text('PDF'), a:has-text('Εκτύπωση'), "
                    "button:has-text('Εκτύπωση'), button:has-text('Λήψη'), "
                    "a:has-text('Λήψη'), a:has-text('Εκτύπωση Αποδεικτικού')",
                    doc_label=f"asfalistiki_{key}",
                )
            except Exception as e:
                # Η αίτηση ΕΧΕΙ ήδη υποβληθεί — δεν ξαναπροσπαθούμε, θα γινόταν
                # δεύτερη αίτηση στον ΕΦΚΑ. Το λέμε καθαρά και προχωράμε.
                self.log(f"  ⚠️ Η αίτηση για «{full_label[:50]}» υποβλήθηκε, "
                         f"αλλά δεν κατέβηκε το αρχείο: {e}", "error")
                self.log("     Κατέβασέ το από το ιστορικό αιτήσεων του "
                         "e-ΕΦΚΑ — ΜΗΝ ξαναϋποβάλεις.", "error")
                continue

            self.log(f"✅ {fname}", "success")
            files.append(fname)

        if not files:
            raise RuntimeError(
                "Καμία ασφαλιστική ενημερότητα δεν κατέβηκε. Οι αιτήσεις "
                "μπορεί να έχουν ήδη υποβληθεί — έλεγξε το ιστορικό αιτήσεων "
                "στον e-ΕΦΚΑ πριν ξαναδοκιμάσεις."
            )
        return files

    async def _on_efka_form(self) -> bool:
        """True αν φορτώθηκε όντως η φόρμα αιτήσεων του e-ΕΦΚΑ."""
        try:
            text = label_norm(await self.page.inner_text("body"))
        except Exception:
            return False
        return label_norm("Αιτίες Χορήγησης Ασφαλιστικής Ενημερότητας") in text

    async def _click_near(self, label: str, near_text: str, what: str,
                          attempts: int = 12) -> bool:
        """
        Πατάει το στοιχείο με ετικέτα `label` που βρίσκεται ΠΙΟ ΚΟΝΤΑ στο
        κείμενο `near_text`.

        ΓΙΑΤΙ: στην αρχική του Αποδεικτικού Ενημερότητας υπάρχουν ΔΥΟ κουμπιά
        «Είσοδος» με πανομοιότυπη ετικέτα — ένα για την έκδοση και ένα για το
        ιστορικό αιτήσεων. Σκέτο ταίριασμα ετικέτας θα διάλεγε το πρώτο που
        βρει, δηλαδή στην τύχη.

        Η εγγύτητα υπολογίζεται ανεβαίνοντας τους γονείς: κρατάμε το κουμπί
        του ΠΙΟ ΣΤΕΝΟΥ κοινού προγόνου που περιέχει και το ζητούμενο κείμενο.
        """
        target, near = label_norm(label), label_norm(near_text)
        for attempt in range(1, attempts + 1):
            found = await self.page.evaluate(
                """([css, want, near]) => {
                       const norm = s => s.toUpperCase()
                           .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                           // Λατινικά ομοιογράμματα -> ελληνικά, όπως label_norm()
                           .replace(/[ABEHIKMNOPTXYZ]/g,
                                    c => 'ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ'['ABEHIKMNOPTXYZ'.indexOf(c)]);
                       document.querySelectorAll('[data-gdf-near]')
                           .forEach(e => e.removeAttribute('data-gdf-near'));
                       const txt = el => ((el.value || el.innerText ||
                                           el.textContent || '')
                                          .replace(/\\s+/g, ' ')).trim();
                       let best = null, bestDepth = 1e9;
                       for (const el of document.querySelectorAll(css)) {
                           if (norm(txt(el)) !== want) continue;
                           // Πόσα επίπεδα πάνω χρειάζεται για να βρεθεί
                           // πρόγονος που περιέχει ΚΑΙ το ζητούμενο κείμενο;
                           let depth = 0;
                           for (let p = el.parentElement; p; p = p.parentElement) {
                               depth++;
                               if (norm(p.innerText || '').includes(near)) {
                                   if (depth < bestDepth) {
                                       bestDepth = depth; best = el;
                                   }
                                   break;
                               }
                           }
                       }
                       if (!best) return null;
                       best.setAttribute('data-gdf-near', '1');
                       return {label: txt(best), depth: bestDepth};
                   }""",
                [self.CELL_CLICKABLE_CSS, target, near],
            )
            if found:
                self.log(f"  🔘 «{found['label']}» δίπλα στο «{near_text[:40]}»")
                await self._click_and_follow(
                    self.page.locator('[data-gdf-near="1"]'))
                return True
            if attempt == 1:
                self.log(f"  ⏳ {what}: αναμονή να φορτώσει η εφαρμογή…")
            await self.page.wait_for_timeout(1_000)

        labels = [t["label"] for t in await self._tile_choices()]
        self.log(f"  ⚠️ Δεν βρέθηκε «{label}» κοντά στο «{near_text[:40]}». "
                 f"Διαθέσιμα: {labels}", "error")
        return False

    async def _click_any(self, label: str, what: str,
                         attempts: int = 10) -> bool:
        """
        Πατά ΟΠΟΙΟΔΗΠΟΤΕ από τα στοιχεία που ταιριάζουν με `label`, δοκιμάζοντας
        από το ΤΕΛΕΥΤΑΙΟ προς το πρώτο μέχρι να πετύχει κάποιο κλικ.

        ΓΙΑΤΙ: μετά την έκδοση, το «Ψηφιακό αρχείο Αποδεικτικού Ενημερότητας»
        υπάρχει ΔΥΟ φορές — μία στην ενότητα «Γενικά Στοιχεία Αίτησης» της
        σελίδας και μία μέσα στο modal «Αποθήκευση Αίτησης» που εμφανίζεται από
        πάνω. Το κουμπί της σελίδας είναι καλυμμένο από το overlay, οπότε το
        κλικ πάνω του αποτυγχάνει. Το modal είναι αργότερα στο DOM, γι' αυτό
        ξεκινάμε από το τέλος.
        """
        target = label_norm(label)
        for attempt in range(1, attempts + 1):
            items = [t for t in await self._tile_choices()
                     if target in label_norm(t["label"])]
            for it in reversed(items):
                try:
                    el = self.page.locator(f'[data-gdf-tile="{it["k"]}"]')
                    await el.scroll_into_view_if_needed(timeout=2_000)
                    await self._click_and_follow(el)
                    self.log(f"  🔘 «{it['label'][:60]}»")
                    return True
                except Exception:
                    continue      # καλυμμένο από το overlay — δοκιμή επόμενου
            if attempt == 1 and not items:
                self.log(f"  ⏳ {what}: αναμονή…")
            await self.page.wait_for_timeout(1_000)

        labels = [t["label"] for t in await self._tile_choices()]
        self.log(f"  ⚠️ Δεν πατήθηκε «{label}». Διαθέσιμα: {labels}", "error")
        return False

    async def _pick_radio(self, text: str, what: str) -> bool:
        """
        Επιλέγει το radio button του οποίου η ετικέτα ταιριάζει με `text`.

        Με ΠΡΑΓΜΑΤΙΚΟ κλικ — όπως παντού σε αυτό το portal, η προγραμματική
        αλλαγή δεν ενημερώνει το μοντέλο της Angular εφαρμογής.
        """
        want = label_norm(text)
        radios = self.page.locator("input[type='radio']")
        try:
            count = await radios.count()
        except Exception:
            count = 0
        available = []
        for i in range(count):
            r = radios.nth(i)
            try:
                # Η ετικέτα είναι σε γειτονικό στοιχείο, όχι μέσα στο input
                lbl = await r.evaluate(
                    """el => {
                           const row = el.closest('label, li, tr, div');
                           return ((row ? row.innerText : '') || '')
                                  .replace(/\\s+/g, ' ').trim();
                       }""")
            except Exception:
                continue
            available.append(lbl[:70])
            if label_norm(lbl) == want or want in label_norm(lbl):
                try:
                    await r.scroll_into_view_if_needed(timeout=2_000)
                    await r.click(timeout=3_000)
                    self.log(f"  🔘 Λόγος έκδοσης: {lbl[:70]}")
                    return True
                except Exception as e:
                    self.log(f"  ⚠️ Απέτυχε η επιλογή λόγου: "
                             f"{str(e).splitlines()[0]}", "error")
                    return False
        self.log(f"  ⚠️ Δεν βρέθηκε ο λόγος «{text[:50]}». "
                 f"Διαθέσιμοι: {available}", "error")
        return False

    async def _shot(self, tag: str) -> Path:
        """Screenshot διάγνωσης· επιστρέφει τη διαδρομή για το μήνυμα σφάλματος."""
        path = DEBUG_SHOT.with_name(f"gov_debug_{tag}.png")
        try:
            await self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
        return path

    # Στοιχεία που λειτουργούν ως κουτάκι επιλογής. ΔΕΝ αρκεί το
    # input[type=checkbox]: σε Angular εφαρμογές τα κουτάκια συχνά είναι custom
    # στοιχεία με role="checkbox" ή aria-checked, ή ακόμη και <div> με κλάση.
    CHECKBOX_CSS = ("input[type='checkbox'], [role='checkbox'], [aria-checked], "
                    "mat-checkbox, .mat-checkbox, .checkbox")

    async def _checkbox_state(self) -> List[dict]:
        """Απογραφή όλων των κουτακιών: τι είναι, τι γράφουν, αν είναι επιλεγμένα."""
        return await self.page.evaluate(
            """(css) => [...document.querySelectorAll(css)].map((el, i) => {
                   const aria = el.getAttribute('aria-checked');
                   const inner = el.querySelector('input[type=checkbox]');
                   const checked =
                       el.checked === true ? true :
                       aria !== null ? aria === 'true' :
                       inner ? inner.checked :
                       el.classList.contains('checked');
                   // Το label είναι συχνά σε ΓΕΙΤΟΝΙΚΟ στοιχείο, όχι μέσα: το
                   // ίδιο το κουτάκι συχνά περιέχει μόνο ένα σύμβολο (□/☑),
                   // οπότε προτιμάμε το κείμενο της γραμμής όταν λέει
                   // περισσότερα.
                   const own = (el.innerText || '').trim();
                   const near = (el.closest('tr, li, label, .row')?.innerText
                                 || '').trim();
                   const label = near.length > own.length ? near : own;
                   return {
                       i, checked,
                       tag: el.tagName.toLowerCase(),
                       type: el.getAttribute('type') || '',
                       disabled: !!el.disabled,
                       label: (label.replace(/\\s+/g, ' ')).slice(0, 60),
                   };
               })""",
            self.CHECKBOX_CSS,
        )

    async def _select_all_options(self) -> int:
        """
        Επιλέγει ΟΛΕΣ τις επιλογές σε λίστες πολλαπλής επιλογής και επιστρέφει
        πόσες επιλέχθηκαν.

        ΓΙΑΤΙ ΧΩΡΙΣΤΑ ΑΠΟ ΤΑ CHECKBOXES: στη «Τρέχουσα Εικόνα Οντότητας» τα
        στοιχεία προς έκδοση («Σχέσεις Επιχείρησης», «Μέλη/Εταίροι», «Δραστηριότητες»,
        «Εγκαταστάσεις» κ.λπ.) δεν είναι κουτάκια αλλά <select multiple>. Επειδή
        ψάχναμε checkboxes, δεν επιλεγόταν ΚΑΜΙΑ ενότητα και η βεβαίωση έβγαινε
        με 2 σελίδες αντί για 4.

        Το select_option της Playwright πυροδοτεί input/change, που χρειάζεται
        το Angular για να ενημερώσει το μοντέλο του.
        """
        total = 0
        hidden = 0
        selects = self.page.locator("select[multiple]")
        try:
            count = await selects.count()
        except Exception:
            count = 0
        for i in range(count):
            sel = selects.nth(i)
            try:
                # ΚΡΥΦΕΣ λίστες: το Angular κρατά στο DOM αντίγραφα για άλλες
                # λειτουργίες (myselect2/3/4). Χωρίς αυτόν τον έλεγχο, κάθε μία
                # κατανάλωνε 5 δευτερόλεπτα σε timeout και γέμιζε το log με
                # τεράστια μηνύματα σφάλματος — ενώ δεν ήταν καν πρόβλημα.
                if not await sel.is_visible():
                    hidden += 1
                    continue
                labels = await sel.evaluate(
                    "el => [...el.options].map(o => (o.text || '').trim())")
                if not labels:
                    continue
                # ΠΡΑΓΜΑΤΙΚΑ ΚΛΙΚ σε κάθε επιλογή, όχι select_option():
                # το select_option αλλάζει το DOM και στέλνει input/change, αλλά
                # το AngularJS δεν ενημέρωνε το ng-model — οι επιλογές ΦΑΙΝΟΝΤΑΝ
                # επιλεγμένες (γκρι φόντο στο screenshot) και η βεβαίωση έβγαινε
                # πάλι 2 σελίδες αντί για 4.
                # Με size=9 η λίστα αποδίδεται inline, οπότε τα <option> είναι
                # κανονικά κλικαρίσιμα. Το ControlOrMeta κρατά τις προηγούμενες
                # επιλογές (σκέτο κλικ θα τις άδειαζε σε κάθε βήμα).
                opts = sel.locator("option")
                for j in range(len(labels)):
                    try:
                        opt = opts.nth(j)
                        await opt.scroll_into_view_if_needed(timeout=2_000)
                        await opt.click(modifiers=["ControlOrMeta"],
                                        timeout=3_000)
                    except Exception:
                        continue
                await self.page.wait_for_timeout(400)

                chosen = await sel.evaluate(
                    "el => [...el.selectedOptions].map(o => (o.text||'').trim())")
                if len(chosen) < len(labels):
                    # Εφεδρικά, προγραμματική επιλογή ΟΛΩΝ μαζί — ποτέ ανά μία,
                    # γιατί το select_option αντικαθιστά την επιλογή και θα
                    # ακύρωνε τις προηγούμενες.
                    self.log(f"  ↩️ Τα κλικ έδωσαν {len(chosen)}/{len(labels)} — "
                             f"συμπλήρωση προγραμματικά")
                    try:
                        await sel.select_option(index=list(range(len(labels))),
                                                timeout=5_000)
                        await self.page.wait_for_timeout(300)
                        chosen = await sel.evaluate(
                            "el => [...el.selectedOptions]"
                            ".map(o => (o.text||'').trim())")
                    except Exception:
                        pass
                total += len(chosen)
                self.log(f"  📋 Λίστα με {len(labels)} επιλογές — "
                         f"επιλέχθηκαν {len(chosen)}")
                for lb in labels:
                    mark = "✓" if lb in chosen else "·"
                    self.log(f"       {mark} {lb}")
                missing = [lb for lb in labels if lb not in chosen]
                if missing:
                    self.log(f"  ⚠️ Δεν επιλέχθηκαν: {missing}", "error")
            except Exception as e:
                # Μόνο η πρώτη γραμμή: τα μηνύματα της Playwright είναι
                # δεκάδες γραμμές call log και πνίγουν το υπόλοιπο αρχείο.
                self.log(f"  ⚠️ Λίστα επιλογών {i}: "
                         f"{str(e).splitlines()[0]}", "error")
        if hidden:
            self.log(f"  · {hidden} κρυφές λίστες παραλείφθηκαν")
        return total

    async def _check_all_boxes(self) -> int:
        """
        Επιλέγει ΟΛΕΣ τις ενότητες της βεβαίωσης και επιστρέφει πόσες
        τσεκαρίστηκαν.

        Γίνεται με ΠΡΑΓΜΑΤΙΚΑ κλικ και όχι θέτοντας `checked` από JavaScript:
        η σελίδα είναι Angular και χωρίς τα events το μοντέλο της δεν ενημερώνεται
        — τα κουτάκια θα φαίνονταν τσεκαρισμένα αλλά η βεβαίωση θα έβγαινε κενή.

        Επαναλαμβάνει όσο υπάρχει πρόοδος, γιατί ένα «επιλογή όλων» αλλάζει τα
        υπόλοιπα και ένα κλικ μπορεί να εμφανίσει νέες επιλογές.
        """
        boxes = self.page.locator(self.CHECKBOX_CSS)
        total_checked = 0

        before = await self._checkbox_state()
        self.log(f"  🔎 Βρέθηκαν {len(before)} κουτάκια επιλογής "
                 f"({sum(1 for b in before if b['checked'])} ήδη επιλεγμένα)")
        for b in before:
            kind = f"{b['tag']}[{b['type']}]" if b["type"] else b["tag"]
            self.log(f"       · {kind:22} {'✓' if b['checked'] else '·'} "
                     f"{b['label']}")

        for _ in range(12):
            try:
                count = await boxes.count()
            except Exception:
                break
            if count == 0:
                break
            progressed = False
            state = await self._checkbox_state()
            for i in range(count):
                if i < len(state) and (state[i]["checked"] or state[i]["disabled"]):
                    continue          # ήδη επιλεγμένο ή ανενεργό
                box = boxes.nth(i)
                try:
                    # scroll_into_view: κουτάκια εκτός ορατού πεδίου δεν
                    # πατιούνται, και πριν προσπερνιόνταν σιωπηλά — γι' αυτό
                    # έβγαιναν 2 σελίδες αντί για 4.
                    await box.scroll_into_view_if_needed(timeout=2_000)
                    try:
                        await box.click(timeout=3_000)
                    except Exception:
                        # Καλυμμένο από διακοσμητικό στοιχείο (συχνό σε custom
                        # checkboxes) — force ΜΟΝΟ ως δεύτερη προσπάθεια, ποτέ
                        # στην πρώτη: παρακάμπτει και το disabled, οπότε θα
                        # πατούσε επ' άπειρον κουτάκια που δεν αλλάζουν.
                        await box.click(timeout=3_000, force=True)
                except Exception:
                    continue
                # Μετράμε μόνο αν ΟΝΤΩΣ άλλαξε κατάσταση
                fresh = await self._checkbox_state()
                if i < len(fresh) and fresh[i]["checked"]:
                    total_checked += 1
                    progressed = True
            if not progressed:
                break
            await self.page.wait_for_timeout(300)

        after = await self._checkbox_state()
        remaining = [b for b in after if not b["checked"] and not b["disabled"]]
        self.log(f"  ☑️ Επιλέχθηκαν {total_checked} ενότητες — "
                 f"{sum(1 for b in after if b['checked'])}/{len(after)} τελικά")
        if remaining:
            self.log(f"  ⚠️ Έμειναν ανεπίλεκτες: "
                     f"{[b['label'][:40] for b in remaining]}", "error")
        return total_checked

    # ------------------------------------------------------------------
    # Φορολογική ενημερότητα  (νέο myAADE — Αποδεικτικό Ενημερότητας)
    # ------------------------------------------------------------------
    async def download_forologiki(self, client_name: str, year: str,
                                  dl_dir: Path) -> str:
        """
        Αποδεικτικό Φορολογικής Ενημερότητας.

        ΕΚΔΙΔΕΙ νέο έγγραφο — δεν ανακτά υπάρχον. Το αποδεικτικό δεσμεύεται από
        τον ΛΟΓΟ ΕΚΔΟΣΗΣ που δηλώνεται, γι' αυτό ο λόγος έρχεται από τη φόρμα
        και ΔΕΝ επιλέγεται αυτόματα. Χωρίς επιλεγμένο λόγο, δεν προχωράμε.
        """
        reason_key = getattr(self, "clearance_reason", "") or ""
        reason = CLEARANCE_REASONS.get(reason_key)
        if not reason:
            raise DocumentNotAvailable(
                "δεν επιλέχθηκε λόγος έκδοσης για τη φορολογική ενημερότητα — "
                "το αποδεικτικό εκδίδεται δεσμευτικά για συγκεκριμένο σκοπό, "
                "οπότε δεν τον επιλέγω αυτόματα"
            )

        self.log(f"📄 Φορολογική ενημερότητα — λόγος: {reason}")
        await self._goto(CLEARANCE_ENTRY)

        # Βήμα 1: το «Είσοδος» ΤΗΣ ΕΚΔΟΣΗΣ, όχι του ιστορικού αιτήσεων
        if not await self._click_near("Είσοδος", "Έκδοση Αποδεικτικού",
                                      "Είσοδος (έκδοση αποδεικτικού)"):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε το «Είσοδος» της έκδοσης στη σελίδα "
                f"{self.page.url}. Screenshot: {await self._shot('forologiki_step1')}"
            )

        # Βήμα 2: λόγος έκδοσης
        if not await self._pick_radio(reason, "λόγος έκδοσης"):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε ο λόγος έκδοσης «{reason}» στη σελίδα "
                f"{self.page.url}. Screenshot: {await self._shot('forologiki_step2')}"
            )

        # Βήμα 3: ΑΦΜ φορέα, αν δόθηκε (απαιτείται μόνο σε ορισμένους λόγους)
        afm = (getattr(self, "clearance_afm", "") or "").strip()
        if afm:
            filled = False
            for sel in ("input[placeholder*='ΑΦΜ' i]",
                        "input[name*='afm' i]", "input[id*='afm' i]"):
                try:
                    box = self.page.locator(sel).first
                    if await box.is_visible():
                        await box.fill(afm, timeout=3_000)
                        filled = True
                        self.log(f"  🔢 ΑΦΜ φορέα: {afm}")
                        break
                except Exception:
                    continue
            if not filled:
                self.log(f"  ⚠️ Δεν βρέθηκε πεδίο «ΑΦΜ Φορέα» για να μπει το "
                         f"{afm}", "error")
        else:
            self.log("  · Χωρίς ΑΦΜ φορέα (δεν δόθηκε)")

        pre = await self._shot("forologiki_before_ekdosi")
        self.log(f"  📷 Πριν την έκδοση: {pre}", "success")

        # Βήμα 4: έκδοση
        self.reset_pdf_captures()
        if not await self._click_tile("Έκδοση Αποδεικτικού Ενημερότητας",
                                      "Έκδοση Αποδεικτικού Ενημερότητας"):
            raise DocumentNotAvailable(
                f"δεν βρέθηκε το κουμπί «Έκδοση Αποδεικτικού Ενημερότητας» στη "
                f"σελίδα {self.page.url}. "
                f"Screenshot: {await self._shot('forologiki_step4')}"
            )

        # Βήμα 5: το ίδιο το αρχείο.
        # Μετά την έκδοση εμφανίζεται modal «Αποθήκευση Αίτησης» με το κουμπί
        # «Ψηφιακό αρχείο Αποδεικτικού Ενημερότητας» — το κείμενο σπάει σε δύο
        # γραμμές, γι' αυτό ψάχνουμε το διακριτό «Ψηφιακό αρχείο».
        await self.page.wait_for_timeout(1_500)
        if not await self._click_any("Ψηφιακό αρχείο",
                                     "ψηφιακό αρχείο αποδεικτικού"):
            raise RuntimeError(
                f"Το αποδεικτικό ΕΚΔΟΘΗΚΕ αλλά δεν κατέβηκε το αρχείο: δεν "
                f"πατήθηκε το «Ψηφιακό αρχείο Αποδεικτικού Ενημερότητας». "
                f"Μπορείς να το κατεβάσεις από «Οι Αιτήσεις μου». "
                f"Screenshot: {await self._shot('forologiki_step5')}"
            )

        fname = self.dated_filename(client_name, "Φορολογική_Ενημερότητα")
        await self._pdf(
            dl_dir / fname,
            "a[href*='.pdf'], a:has-text('PDF'), a:has-text('Εκτύπωση'), "
            "button:has-text('Εκτύπωση'), button:has-text('Λήψη'), a:has-text('Λήψη')",
            doc_label="forologiki",
        )
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # Ε1  (φυσικά πρόσωπα — webtax portal)
    # ------------------------------------------------------------------
    async def download_e1(self, client_name: str, year: str, dl_dir: Path) -> str:
        self.log(f"📄 Ε1 ({year})…")
        await self._goto(WEBTAX_ENTRY)
        # Ενδιάμεση σελίδα καλωσορίσματος με κουμπί "Είσοδος στην εφαρμογή" (όχι πάντα παρούσα)
        await self._click_first([
            "button:has-text('Είσοδος στην εφαρμογή')", "a:has-text('Είσοδος στην εφαρμογή')"
        ], timeout=4_000, optional=True)
        await self._select_year(year)
        # Το μπλε κουμπί «Ε1» στη στήλη «Ψηφιακό Αρχείο Δήλωσης». Παλιότερα εδώ
        # γινόταν has-text('Ε1'), που είναι substring match: έπιανε και το
        # "Ε1 - ΣΥΝΟΨΗ" ή ανενεργά κουμπιά (έτη χωρίς δήλωση) και μετά έσκαγε
        # αργότερα στο PDF. Το _click_labeled δοκιμάζει ΠΡΩΤΑ ακριβές label και
        # παρακάμπτει τα disabled.
        found = await self._click_labeled(
            ["Ε1"],
            f"Ε1 ({year})",
            avoid=["ΣΥΝΟΨΗ", "myDATA", "Ε2", "Ε3"],
        )
        if not found:
            raise DocumentNotAvailable(
                f"δεν βρέθηκε ενεργό κουμπί Ε1 για το {year} — πιθανόν δεν έχει "
                f"υποβληθεί δήλωση, ή ο φορολογούμενος είναι νομικό πρόσωπο "
                f"(τα νομικά πρόσωπα δεν έχουν Ε1). Σελίδα: {self.page.url}"
            )
        fname = self.safe_filename(client_name, year, "Ε1")
        await self._pdf(dl_dir / fname,
                        "a:has-text('PDF'), a:has-text('Εκτύπωση'), button:has-text('Εκτύπωση')",
                        doc_label="E1")
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # Ε3  (ατομικές: webtax portal — νομικά πρόσωπα: income portal)
    # ------------------------------------------------------------------
    async def download_e3(self, client_name: str, year: str, dl_dir: Path) -> str:
        self.log(f"📄 Ε3 ({year})…")
        if not self.is_atomiki:
            return await self._download_e3_nomiko(client_name, year, dl_dir)
        await self._goto(WEBTAX_ENTRY)
        # Ενδιάμεση σελίδα καλωσορίσματος με κουμπί "Είσοδος στην εφαρμογή" (όχι πάντα παρούσα)
        await self._click_first([
            "button:has-text('Είσοδος στην εφαρμογή')", "a:has-text('Είσοδος στην εφαρμογή')"
        ], timeout=4_000, optional=True)
        await self._select_year(year)
        # Προτεραιότητα στο Ε3 ΤΟΥ ΥΠΟΧΡΕΟΥ. Αν δεν υπάρχει, παίρνουμε το
        # ΣΥΖΥΓΟΥ/ΜΣΣ αλλά το σώζουμε ως "Ε3_ΣΥΖΥΓΟΥ" ώστε να μη μπερδεύεται με
        # το Ε3 του πελάτη. Το "Ε3 - myDATA" (στοιχεία myDATA, όχι η δήλωση) και
        # οι "ΣΥΝΟΨΗ ..." αποκλείονται πάντα.
        # Τα λατινικά "E3" δεν χρειάζονται πια ξεχωριστά: το label_norm() μέσα
        # στο _click_labeled τα κανονικοποιεί σε ελληνικά.
        clicked = await self._click_labeled(
            ["Ε3 ΥΠΟΧΡΕΟΥ/ΜΣΣ", "Ε3 ΥΠΟΧΡΕΟΥ", "Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ"],
            f"Ε3 ({year})",
            avoid=["myDATA", "ΣΥΝΟΨΗ"],
        )
        if not clicked:
            raise DocumentNotAvailable(
                f"δεν βρέθηκε Ε3 (ούτε ΥΠΟΧΡΕΟΥ ούτε ΣΥΖΥΓΟΥ/ΜΣΣ) για το {year} "
                f"στη σελίδα {self.page.url} — δες τα διαθέσιμα labels παραπάνω."
            )

        doc_type = "Ε3"
        if "ΣΥΖΥΓΟΥ" in label_norm(clicked):
            doc_type = "Ε3_ΣΥΖΥΓΟΥ"
            self.log(
                "  ℹ️ Δεν υπάρχει «Ε3 ΥΠΟΧΡΕΟΥ» για αυτόν τον φορολογούμενο — "
                "λήφθηκε το «Ε3 ΣΥΖΥΓΟΥ/ΜΣΣ» και αποθηκεύεται ως Ε3_ΣΥΖΥΓΟΥ.",
                "error",
            )
        fname = self.safe_filename(client_name, year, doc_type)
        await self._pdf(dl_dir / fname,
                        "a:has-text('PDF'), a:has-text('Εκτύπωση'), button:has-text('Εκτύπωση')",
                        doc_label="E3")
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # Εκκαθαριστικό  (income portal)
    # ------------------------------------------------------------------
    async def download_ekkatharistiko(self, client_name: str, year: str, dl_dir: Path) -> str:
        self.log(f"📄 Εκκαθαριστικό / Πράξη Προσδιορισμού Φόρου ({year})…")
        await self._goto(WEBTAX_ENTRY)
        # Ενδιάμεση σελίδα καλωσορίσματος με κουμπί "Είσοδος στην εφαρμογή" (όχι πάντα παρούσα)
        await self._click_first([
            "button:has-text('Είσοδος στην εφαρμογή')", "a:has-text('Είσοδος στην εφαρμογή')"
        ], timeout=4_000, optional=True)
        await self._select_year(year)

        # Από το φορολογικό έτος 2014 και μετά δεν λέγεται πια "Εκκαθαριστικό" αλλά
        # "Πράξη Διοικητικού Προσδιορισμού Φόρου": στη στήλη «Ψηφιακό Αρχείο Πράξης
        # Προσδιορισμού Φόρου» το κουμπί λέγεται σκέτο "ΥΠΟΧΡΕΟΥ" (ή "ΣΥΖΥΓΟΥ/ΜΣΣ").
        # Το ακριβές label "ΥΠΟΧΡΕΟΥ" είναι μοναδικό — το "Ε2 ΥΠΟΧΡΕΟΥ" δεν ταιριάζει.
        # Τα "ΣΥΝΟΨΗ ..." είναι περίληψη, όχι το έγγραφο, γι' αυτό δεν τα ζητάμε.
        clicked = await self._click_labeled(
            ["ΥΠΟΧΡΕΟΥ", "ΣΥΖΥΓΟΥ/ΜΣΣ", "Εκκαθαριστικό"],
            f"Εκκαθαριστικό / Πράξη Προσδιορισμού Φόρου ({year})",
            avoid=["ΣΥΝΟΨΗ", "Ε2", "Ε1", "Ε3"],
        )
        if not clicked:
            raise DocumentNotAvailable(
                f"δεν βρέθηκε Εκκαθαριστικό/Πράξη Προσδιορισμού Φόρου για το {year} "
                f"στη σελίδα {self.page.url}"
            )

        # Ίδια λογική με το Ε3: αν πήραμε το ΣΥΖΥΓΟΥ/ΜΣΣ, φαίνεται στο όνομα.
        doc_type = "Εκκαθαριστικό"
        if "ΣΥΖΥΓΟΥ" in label_norm(clicked):
            doc_type = "Εκκαθαριστικό_ΣΥΖΥΓΟΥ"
            self.log(
                "  ℹ️ Δεν υπάρχει Πράξη Προσδιορισμού «ΥΠΟΧΡΕΟΥ» — λήφθηκε το "
                "«ΣΥΖΥΓΟΥ/ΜΣΣ» και αποθηκεύεται ως Εκκαθαριστικό_ΣΥΖΥΓΟΥ.",
                "error",
            )
        fname = self.safe_filename(client_name, year, doc_type)
        await self._pdf(dl_dir / fname,
                        "a:has-text('PDF'), a:has-text('Εκτύπωση'), button:has-text('Εκτύπωση')",
                        doc_label="ekkatharistiko")
        self.log(f"✅ {fname}", "success")
        return fname

    # ------------------------------------------------------------------
    # ΦΠΑ
    # ------------------------------------------------------------------
    async def download_fpa(self, client_name: str, year: str, dl_dir: Path) -> List[str]:
        """
        Ροή ΦΠΑ (Περιοδική Δήλωση = έντυπο Φ2):
          1. Επιλογή νομικού προσώπου (αν ζητηθεί)
          2. Στη ΓΡΑΜΜΗ Φ2: επιλογή έτους στο dropdown της στήλης «Έτος» —
             αυστηρά αυτό της γραμμής Φ2, όχι κάποιο άλλο της σελίδας
          3. Κλικ «Συνέχεια»
          4. Μέτρηση καταχωρήσεων της σελίδας: 4 για ατομική (τρίμηνα),
             12 για τα υπόλοιπα (μήνες) — προειδοποίηση αν διαφέρει
          5. Για ΚΑΘΕ καταχώρηση: κλικ «Επεξεργασία Δηλώσεων», και μετά
             – αν υπάρχει «Τροποποιητική»: κλικ στο «Προβολή» ΤΗΣ γραμμής της
             – αλλιώς: κλικ στο μοναδικό «Προβολή» της οθόνης
             → PDF, αριθμημένο ΦΠΑ_1 … ΦΠΑ_ν (με σήμανση ΤΡΟΠΟΠΟΙΗΤΙΚΗ)
        """
        self.log(f"📄 ΦΠΑ ({year})…")
        await self._goto(VAT_ENTRY)
        await self._select_taxpayer(self.is_atomiki)

        # Η σελίδα δείχνει πίνακα εντύπων (Φ1, Φ2, Φ4, Φ5…) με ΞΕΧΩΡΙΣΤΟ dropdown
        # έτους και κουμπί ανά γραμμή. Δουλεύουμε αυστηρά μέσα στη γραμμή Φ2.
        row = await self._row_locator("Φ2")
        if row is None:
            labels = [i["label"] for i in await self._clickables()]
            # Χωρίς γραμμή Φ2 ο φορολογούμενος δεν έχει υποχρέωση περιοδικής
            # δήλωσης ΦΠΑ (π.χ. απαλλασσόμενος) — απουσία, όχι βλάβη.
            raise DocumentNotAvailable(
                f"δεν βρέθηκε γραμμή Φ2 (Περιοδική Δήλωση ΦΠΑ) — πιθανόν δεν "
                f"υπάρχει υποχρέωση ΦΠΑ. Σελίδα: {self.page.url}. "
                f"Διαθέσιμα: {labels}"
            )

        if not await self._select_year_in(row, year):
            raise RuntimeError(
                f"Δεν μπόρεσε να επιλεγεί το έτος {year} στο dropdown της γραμμής Φ2 "
                f"(δες τα διαθέσιμα έτη παραπάνω)"
            )

        # Στη γραμμή Φ2, μετά την επιλογή έτους, το κουμπί είναι «Συνέχεια».
        # (Το «Επεξεργασία Δηλώσεων» έρχεται ΑΡΓΟΤΕΡΑ, ανά περίοδο, στην επόμενη
        # σελίδα — γι' αυτό δεν το ζητάμε εδώ: παλιότερα ήταν πρώτο στη λίστα
        # προτίμησης και μπορούσε να πατηθεί λάθος κουμπί άλλης γραμμής.)
        clicked = await self._click_labeled(
            ["Συνέχεια", "Επεξεργασία Δηλώσεων", "Επεξεργασία"],
            "Συνέχεια (γραμμή Φ2)",
            scope=row,
        )
        if not clicked:
            # Μπορεί το κουμπί να είναι έξω από τη γραμμή — δοκιμή σε όλη τη σελίδα
            clicked = await self._click_labeled(
                ["Συνέχεια", "Επεξεργασία Δηλώσεων", "Επεξεργασία"], "Συνέχεια"
            )
        if not clicked:
            raise RuntimeError("Δεν βρέθηκε κουμπί «Συνέχεια» για τη γραμμή Φ2")

        # ── Σελίδα «Υποχρεώσεις Φορολογουμένου»: μία γραμμή ΑΝΑ ΠΕΡΙΟΔΟ
        # (π.χ. «1ος Μήνας 2026» ή «1ο Τρίμηνο»), καθεμία με το ΔΙΚΟ ΤΗΣ κουμπί
        # «Επεξεργασία Δηλώσεων». Οι δηλώσεις είναι ένα επίπεδο πιο βαθιά.
        periods_page = self.page
        periods_url = self.page.url
        periods = await self._find_row_actions(
            "Ενέργειες", ["Επεξεργασία Δηλώσεων", "Επεξεργασία"], "περίοδοι ΦΠΑ"
        )
        if not periods:
            # Διαγνωστικά: τι υπάρχει όντως στη σελίδα, για να μη ψάχνουμε στα τυφλά
            labels = [i["label"] for i in await self._clickables()]
            shot = DEBUG_SHOT.with_name("gov_debug_fpa_periods.png")
            try:
                await self.page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            await self._dump_table_html("Ενέργειες", "fpa_periods")
            raise RuntimeError(
                f"Δεν βρέθηκαν περίοδοι με «Επεξεργασία Δηλώσεων» στη σελίδα "
                f"{self.page.url}. Clickables που βρέθηκαν: {labels}. "
                f"Screenshot: {shot}"
            )

        # Επιβεβαίωση ότι ΟΝΤΩΣ βρέθηκε ο πίνακας περιόδων και όχι τυχαίος άλλος:
        # κάθε γραμμή περιόδου γράφει «… Τρίμηνο …» ή «… Μήνας …». Χωρίς αυτόν τον
        # έλεγχο, μια λάθος γραμμή πατιόταν στα τυφλά και η ροή έφευγε σε άσχετη
        # σελίδα, με τελικό μήνυμα «δεν κατέβηκε καμία δήλωση» που έκρυβε την αιτία.
        if not any("ΤΡΙΜΗΝ" in gr_norm(p["text"]) or "ΜΗΝΑ" in gr_norm(p["text"])
                   for p in periods):
            shot = DEBUG_SHOT.with_name("gov_debug_fpa_periods.png")
            try:
                await self.page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            await self._dump_table_html("Ενέργειες", "fpa_periods")
            raise RuntimeError(
                f"Βρέθηκαν {len(periods)} γραμμές στη στήλη «Ενέργειες», αλλά "
                f"καμία δεν μοιάζει με περίοδο (Τρίμηνο/Μήνας) — μάλλον "
                f"εντοπίστηκε λάθος πίνακας. Γραμμές: "
                f"{[p['text'][:40] for p in periods]}. Σελίδα: {self.page.url}. "
                f"Screenshot: {shot}"
            )

        # ── Έλεγχος πλήθους καταχωρήσεων: η ατομική δηλώνει ΦΠΑ ανά τρίμηνο (4
        # τον χρόνο), τα υπόλοιπα (διπλογραφικά) ανά μήνα (12). Αν ο αριθμός δεν
        # είναι ο αναμενόμενος, συνήθως σημαίνει λάθος έτος/γραμμή ή ημιτελές
        # φορτωμένη σελίδα. Είναι ΠΡΟΕΙΔΟΠΟΙΗΣΗ και όχι σφάλμα, γιατί υπάρχουν
        # νόμιμες εξαιρέσεις (έναρξη/διακοπή μέσα στο έτος, αλλαγή καθεστώτος,
        # τρέχον έτος που δεν έχει ακόμη όλες τις περιόδους).
        # Το καθεστώς ΔΕΝ προκύπτει από το is_atomiki: το portal έδειξε υποκείμενο
        # με Β΄/Γ΄ κατ. βιβλία που δηλώνει ΤΡΙΜΗΝΙΑΙΑ. Το διαβάζουμε από τα labels
        # των περιόδων («1ο Τρίμηνο» vs «1ος Μήνας»).
        # Ο αριθμός είναι ΑΝΩΤΑΤΟ ΟΡΙΟ, όχι ακριβής τιμή: στο τρέχον έτος
        # εμφανίζονται μόνο οι περίοδοι που έχουν λήξει (π.χ. τον Ιούλιο 2026
        # μόνο 1ο και 2ο τρίμηνο), και υπάρχουν έναρξη/διακοπή μέσα στο έτος.
        joined = gr_norm(" ".join(p["text"] for p in periods))
        if "ΤΡΙΜΗΝ" in joined:          # έλεγχος ΠΡΙΝ το «ΜΗΝΑ» — το «ΤΡΙΜΗΝΟ» το περιέχει
            regime, max_periods = "τριμηνιαία", 4
        elif "ΜΗΝΑ" in joined:
            regime, max_periods = "μηνιαία", 12
        else:
            regime, max_periods = None, None

        if max_periods is None:
            self.log(f"  🔢 {len(periods)} καταχωρήσεις (το καθεστώς δεν αναγνωρίστηκε)")
        elif len(periods) > max_periods:
            self.log(
                f"  ⚠️ Βρέθηκαν {len(periods)} καταχωρήσεις, ενώ σε {regime} δήλωση "
                f"δεν μπορούν να υπάρχουν πάνω από {max_periods} στο έτος. Πιθανόν "
                f"μπερδεύτηκε γραμμή/έτος — συνεχίζω με ό,τι βρέθηκε.",
                "error",
            )
        else:
            self.log(
                f"  🔢 {len(periods)}/{max_periods} καταχωρήσεις ({regime} δήλωση)"
            )

        # Περίοδοι χωρίς υποβληθείσα δήλωση δεν έχουν τι να κατεβάσουν.
        # ΠΡΟΣΟΧΗ: το «Δεν έχει υποβληθεί» περιέχει επίσης «υποβληθεί», άρα
        # πρέπει να αποκλειστεί ρητά — και με gr_norm(), γιατί οι τόνοι
        # χαλάνε τη σύγκριση (δες gr_norm στο base.py).
        def has_submitted(text: str) -> bool:
            n = gr_norm(text)
            if "ΔΕΝ ΕΧΕΙ ΥΠΟΒΛΗΘΕΙ" in n or "ΔΕΝ ΥΠΟΒΛΗΘΗΚΕ" in n:
                return False
            return "ΥΠΟΒΛΗΘΕΙ" in n

        submitted = [p for p in periods if has_submitted(p["text"])]
        if submitted:
            skipped = [p for p in periods if p not in submitted]
            for p in skipped:
                self.log(f"  ⏭️ Παραλείπεται (χωρίς δήλωση): {p['text'][:80]}")
            periods = submitted

        self.log(f"  📋 Βρέθηκαν {len(periods)} περίοδοι:")
        for p in periods:
            self.log(f"     • {p['text'][:110]}")

        # Τα σημάδια data-gdf-click ΧΑΝΟΝΤΑΙ σε κάθε πλοήγηση, γιατί η σελίδα
        # ξαναφορτώνει όταν γυρίζουμε από μια περίοδο. Άρα δεν κρατάμε δείκτες
        # από την πρώτη σάρωση (οι περίοδοι 2+ έσκαγαν): κρατάμε το ΚΕΙΜΕΝΟ κάθε
        # περιόδου ως ταυτότητα και ξανασαρώνουμε τη σελίδα σε κάθε επανάληψη.
        period_texts = [p["text"] for p in periods]

        saved: List[str] = []
        for n, ptext in enumerate(period_texts, start=1):
            self.log(f"  ── Περίοδος {n}/{len(period_texts)}: {ptext[:90]}")
            self.reset_pdf_captures()

            fresh = await self._find_row_actions(
                "Ενέργειες", ["Επεξεργασία Δηλώσεων", "Επεξεργασία"],
                f"περίοδος {n}",
            )
            period = next((f for f in fresh if f["text"] == ptext), None)
            if period is None:
                self.log(
                    f"  ⚠️ Η περίοδος {n} δεν βρέθηκε ξανά στη σελίδα — "
                    f"παραλείπεται. Διαθέσιμες: {[f['text'][:40] for f in fresh]}",
                    "error",
                )
                await self._back_to(periods_page, periods_url)
                continue

            if not await self._click_row_action(
                period, f"Επεξεργασία Δηλώσεων (περίοδος {n})"
            ):
                self.log(f"  ⚠️ Παραλείπεται η περίοδος {n}", "error")
                await self._back_to(periods_page, periods_url)
                continue

            # Λίστα δηλώσεων ΤΗΣ περιόδου: αρχική και (ίσως) τροποποιητικές.
            # ΔΙΑΓΝΩΣΤΙΚΟ: κρατάμε screenshot ΚΑΙ url αυτής της σελίδας. Είναι το
            # μόνο σημείο της ροής που δεν φαινόταν πουθενά όταν κάτι χαλούσε,
            # γιατί το _back_to() γυρίζει στη λίστα περιόδων πριν το τελικό σφάλμα.
            decls = await self._find_row_actions(
                "Ενέργειες", ["Προβολή", "Ανάκτηση"], f"δηλώσεις περιόδου {n}"
            )
            # Το screenshot ΜΕΤΑ την αναμονή, αλλιώς αποτύπωνε τη σελίδα πριν
            # ολοκληρωθεί η πλοήγηση και έδειχνε λάθος περιεχόμενο.
            self.log(f"     ↪ σελίδα δηλώσεων: {self.page.url}")
            shot = DEBUG_SHOT.with_name(f"gov_debug_fpa_period{n}.png")
            try:
                await self.page.screenshot(path=str(shot), full_page=True)
                self.log(f"     📷 {shot}")
            except Exception:
                pass

            if not decls:
                labels = [i["label"] for i in await self._clickables()]
                self.log(
                    f"  ⚠️ Περίοδος {n}: δεν βρέθηκε «Προβολή». "
                    f"Διαθέσιμα: {labels}", "error",
                )
                if n == 1:   # μία φορά αρκεί για διάγνωση
                    await self._dump_table_html("Ενέργειες", "fpa_declarations")
                await self._back_to(periods_page, periods_url)
                continue
            for d in decls:
                self.log(f"       – {d['text'][:100]}")

            pick = self._pick_declaration(decls)
            suffix = f"ΦΠΑ_{n}_ΤΡΟΠΟΠΟΙΗΤΙΚΗ" if pick["is_tropo"] else f"ΦΠΑ_{n}"
            # shift_year=False: το ΦΠΑ του 2025 αφορά περιόδους ΤΟΥ 2025
            fname = self.safe_filename(client_name, year, suffix, shift_year=False)

            # Το «Προβολή» ΤΗΣ γραμμής που επιλέχθηκε (τροποποιητική αν υπάρχει,
            # αλλιώς η μοναδική) — πατιέται με τον δείκτη του κουμπιού.
            self.log(f"     → «{pick['label']}» στη γραμμή: {pick['text'][:70]}")
            if not await self._click_row_action(
                pick, f"Προβολή δήλωσης περιόδου {n}"
            ):
                await self._back_to(periods_page, periods_url)
                continue

            # Αν μια περίοδος δεν δώσει PDF, συνεχίζουμε με τις επόμενες αντί να
            # χαθεί όλο το ΦΠΑ — το _pdf() πλέον πετάει σφάλμα αντί να σώζει
            # λάθος αρχείο.
            try:
                await self._pdf(dl_dir / fname,
                                "a:has-text('PDF'), a:has-text('Εκτύπωση'), button:has-text('Εκτύπωση')",
                                doc_label=f"fpa_{n}")
            except Exception as e:
                self.log(f"  ⚠️ Περίοδος {n}: {e}", "error")
                await self._back_to(periods_page, periods_url)
                continue
            self.log(f"✅ {fname}", "success")
            saved.append(fname)

            # Πίσω στη λίστα περιόδων για την επόμενη περίοδο
            await self._back_to(periods_page, periods_url)
            if self.page.url != periods_url:
                await self._goto(periods_url)

        if not saved:
            raise RuntimeError(f"Δεν κατέβηκε καμία δήλωση ΦΠΑ για το {year}")

        # Ένωση των περιόδων σε ΕΝΑ αρχείο ανά έτος. Οι περίοδοι κατεβαίνουν με
        # τη σειρά, οπότε το ενωμένο βγαίνει χρονολογικά σωστό.
        if len(saved) > 1:
            # Το ενωμένο φέρει σκέτο «ΦΠΑ» και το έτος. Όπου υπάρχει
            # τροποποιητική έχει ήδη προτιμηθεί έναντι της αρχικής, οπότε δεν
            # χρειάζεται σήμανση στο όνομα.
            merged = self.safe_filename(client_name, year, "ΦΠΑ",
                                        shift_year=False)
            if self.merge_pdfs([dl_dir / f for f in saved], dl_dir / merged):
                self.log(f"✅ {merged}", "success")
                return [merged]
        return saved

    # ------------------------------------------------------------------
    # Κεντρική
    # Ανώτατος χρόνος ανά έγγραφο. Γενναιόδωρο επίτηδες: το ΦΠΑ με μηνιαίες
    # περιόδους κάνει 12 λήψεις και το portal είναι αργό σε ώρες αιχμής. Δεν
    # είναι όριο επίδοσης — είναι ασφαλιστική δικλείδα για κόλλημα.
    DOC_TIMEOUT = 600   # δευτερόλεπτα

    # ------------------------------------------------------------------
    async def run(self, username: str, password: str, client_name: str,
                  years: List[str], documents: List[str], dl_dir: Path,
                  is_atomiki: bool = True,
                  clearance_reason: str = "", clearance_afm: str = "",
                  insurance_reasons: Optional[List[str]] = None,
                  insurance_kind: str = "01") -> List[str]:
        self.is_atomiki = is_atomiki
        # Λόγος έκδοσης και ΑΦΜ φορέα για τη φορολογική ενημερότητα — έρχονται
        # από τη φόρμα, γιατί το αποδεικτικό δεσμεύεται από τον σκοπό του.
        self.clearance_reason = clearance_reason
        self.clearance_afm = clearance_afm
        # Ασφαλιστική ενημερότητα: πολλές αιτίες, μία υποβολή ανά αιτία.
        self.insurance_reasons = list(insurance_reasons or [])
        self.insurance_kind = insurance_kind
        self.log(f"📆 Έτη: {', '.join(years)}")
        self.log(f"👤 Τύπος: {'Ατομική επιχείρηση' if is_atomiki else 'Νομικό πρόσωπο'}")
        # Ο browser τρέχει από πίσω, εκτός οθόνης. Με GOV_BROWSER=visible
        # εμφανίζεται κανονικά — χρήσιμο όταν κάτι χαλάει και θέλουμε να δούμε
        # ζωντανά τι κάνει το portal.
        visible = os.environ.get("GOV_BROWSER", "").lower() == "visible"
        await self.setup(headless=not visible)
        self.log("🖥️ Browser: ορατός" if visible
                 else "🖥️ Browser: τρέχει από πίσω (GOV_BROWSER=visible για εμφάνιση)")

        handlers = {
            "e1":             self.download_e1,
            "e3":             self.download_e3,
            "n":              self.download_n,
            "ekkatharistiko": self.download_ekkatharistiko,
            "fpa":            self.download_fpa,
            "mitroo":         self.download_mitroo,
            "forologiki":     self.download_forologiki,
            "asfalistiki":    self.download_asfalistiki,
        }

        downloaded: List[str] = []
        missing: List[str] = []   # δεν υπάρχουν για αυτόν τον φορολογούμενο/έτος
        failed: List[str] = []    # όντως χάλασαν
        try:
            # Το interception ενεργοποιείται ΜΕΤΑ το login, για να μην επηρεαστεί
            # η αλυσίδα redirects του SSO (login.gsis.gr).
            await self.login(username, password)
            await self.start_pdf_interception()

            # Το login γίνεται ΜΙΑ φορά και τα έτη διατρέχονται μέσα στην ίδια
            # συνεδρία — πολύ ταχύτερο από ξεχωριστό τρέξιμο ανά έτος, και δεν
            # ταλαιπωρεί το portal με επαναλαμβανόμενες συνδέσεις.
            done_once: set = set()   # έγγραφα χωρίς έτος, ήδη κατεβασμένα
            for year in years:
                self.log(f"══ Έτος {year} ══")
                for doc in documents:
                    if doc not in handlers:
                        continue
                    # Το μητρώο είναι η τρέχουσα εικόνα της επιχείρησης, όχι
                    # έγγραφο έτους — με 3 επιλεγμένα έτη θα κατέβαινε 3 φορές.
                    if doc in YEAR_INDEPENDENT_DOCS:
                        if doc in done_once:
                            continue
                        done_once.add(doc)
                    try:
                        # Καθαρίζουμε ό,τι PDF πιάστηκε από το προηγούμενο
                        # έγγραφο, ώστε να μην αποθηκευτεί λάθος αρχείο.
                        self.reset_pdf_captures()
                        # Το ΦΠΑ επιστρέφει λίστα (μία δήλωση ανά περίοδο),
                        # τα υπόλοιπα ένα όνομα αρχείου.
                        result = await asyncio.wait_for(
                            handlers[doc](client_name, year, dl_dir),
                            timeout=self.DOC_TIMEOUT)
                        if isinstance(result, list):
                            downloaded.extend(result)
                        else:
                            downloaded.append(result)
                    except asyncio.TimeoutError:
                        # Δίχτυ ασφαλείας. Ένα κόλλημα σε ΕΝΑ έγγραφο δεν πρέπει
                        # ποτέ να παγώνει όλη τη λήψη: το page.close() σε popup
                        # λήψης κρεμόταν για πάντα και η διαδικασία σταματούσε
                        # σιωπηλά στο Μητρώο, χωρίς σφάλμα και χωρίς σύνοψη.
                        # Η αιτία εκείνη διορθώθηκε — αυτό πιάνει την επόμενη.
                        label = f"{DOCUMENT_LABELS.get(doc, doc)} {year}"
                        failed.append(label)
                        self.log(f"⚠️ {label}: ξεπέρασε τα "
                                 f"{self.DOC_TIMEOUT // 60} λεπτά και "
                                 f"εγκαταλείφθηκε — συνεχίζω με το επόμενο",
                                 "error")
                    except DocumentNotAvailable as e:
                        # Αναμενόμενη απουσία: δεν είναι βλάβη, χωρίς screenshot
                        label = f"{DOCUMENT_LABELS.get(doc, doc)} {year}"
                        missing.append(label)
                        self.log(f"ℹ️ {label}: {e}")
                    except Exception as e:
                        label = f"{DOCUMENT_LABELS.get(doc, doc)} {year}"
                        failed.append(label)
                        self.log(f"⚠️ {label}: {e}", "error")
                        # Ξεχωριστό screenshot ανά έγγραφο ΚΑΙ έτος — αλλιώς το
                        # ένα σφάλμα έσβηνε το screenshot του προηγούμενου.
                        shot = DEBUG_SHOT.with_name(
                            f"gov_debug_{doc}_{year}_error.png")
                        try:
                            await self.page.screenshot(path=str(shot),
                                                       full_page=True)
                            self.log(f"  📸 Screenshot: {shot}", "error")
                        except Exception:
                            pass
        finally:
            await self.cleanup()

        # Σύνοψη: ποια κατέβηκαν, ποια δεν υπήρχαν, ποια χάλασαν. Πριν φαινόταν
        # μόνο ο αριθμός των αρχείων, οπότε μια αποτυχία περνούσε απαρατήρητη.
        self.log("── Σύνοψη ──")
        self.log(f"  ✅ Κατέβηκαν {len(downloaded)}: {', '.join(downloaded) or '—'}")
        if missing:
            self.log(f"  ℹ️ Δεν υπήρχαν: {', '.join(missing)}")
        if failed:
            self.log(f"  ⚠️ Απέτυχαν: {', '.join(failed)} — δες τα σφάλματα πιο πάνω",
                     "error")
        return downloaded
