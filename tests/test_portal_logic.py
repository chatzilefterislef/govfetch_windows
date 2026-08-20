#!/usr/bin/env python3
"""
Έλεγχοι για τη λογική εντοπισμού στοιχείων στα portals της ΑΑΔΕ.

    python3 tests/test_portal_logic.py

Κάθε έλεγχος αντιστοιχεί σε ΠΡΑΓΜΑΤΙΚΟ bug που εμφανίστηκε και κόστισε χρόνο.
Τρέξ' τους πριν αλλάξεις οτιδήποτε στο _action_cells / _rows_with_action /
_pick_declaration — εκεί συγκεντρώνονται όλες οι παγίδες.

Δεν χρειάζεται σύνδεση στο portal: το DOM στήνεται τοπικά και αναπαράγει τη
δομή που είδαμε στις πραγματικές σελίδες.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright          # noqa: E402
from automation.base import gr_norm, label_norm, launch_browser   # noqa: E402
from automation.myaade import MyAADEAutomation             # noqa: E402

FAILURES: list[str] = []


def check(ok: bool, title: str, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {title}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(title)


class Probe(MyAADEAutomation):
    """MyAADEAutomation χωρίς browser lifecycle — μόνο η λογική εντοπισμού."""

    def __init__(self, page):
        self.page = page
        self.logs: list[str] = []

    def log(self, msg, level="info"):
        self.logs.append(msg)

    async def _settle(self):
        pass


# ── Ελληνικό κείμενο ────────────────────────────────────────────────────────

def test_greek_text() -> None:
    # Το upper() ΚΡΑΤΑΕΙ τους τόνους — είχε σπάσει φίλτρο σιωπηλά
    check("ΥΠΟΒΛΗΘΕΙ" not in "Δεν έχει υποβληθεί".upper(),
          "το upper() κρατά τους τόνους (γι' αυτό υπάρχει το gr_norm)")
    check("ΥΠΟΒΛΗΘΕΙ" in gr_norm("Δεν έχει υποβληθεί"),
          "το gr_norm αφαιρεί τόνους")

    # Λατινικά ομοιογράμματα: το portal γράφει «Ε3» και «E3», «ν.4172» και «v.4172»
    check(label_norm("E3 ΥΠΟΧΡΕΟΥ") == label_norm("Ε3 ΥΠΟΧΡΕΟΥ"),
          "λατινικό E ταιριάζει με ελληνικό Ε")
    check(label_norm("Υπόχρεου") == label_norm("ΥΠΟΧΡΕΟΥ"),
          "πεζά με τόνο ταιριάζουν με κεφαλαία")

    # Η άρνηση περιέχει τη θετική λέξη
    def submitted(t: str) -> bool:
        n = gr_norm(t)
        return "ΔΕΝ ΕΧΕΙ ΥΠΟΒΛΗΘΕΙ" not in n and "ΥΠΟΒΛΗΘΕΙ" in n

    check(submitted("Έχει Υποβληθεί Δήλωση") and
          not submitted("Δεν έχει υποβληθεί δήλωση"),
          "η άρνηση «Δεν έχει υποβληθεί» δεν περνά για υποβληθείσα")


# ── Δομή πίνακα ─────────────────────────────────────────────────────────────

PERIODS_ROWS = "".join(f"""
  <tr><td>{q}ο Τρίμηνο 2025</td><td>περίοδος</td><td>Έχει Υποβληθεί Δήλωση</td>
      <td><table><tr><td><div onclick="g()">Επεξεργασία Δηλώσεων</div></td>
          </tr></table></td></tr>""" for q in (1, 2, 3, 4))

# Πίνακας layout που περιτυλίγει τα πάντα + κουμπί σε μονοκύτταρη γραμμή:
# αυτό έκανε το «includes» να διαλέγει λάθος πίνακα.
PERIODS_PAGE = f"""
<table><tr><td>Έχετε 2 νέα μηνύματα. Πατήστε <a href="#">προβολή</a>
  για να μεταβείτε στα εισερχόμενα μηνύματα σας.</td></tr></table>
<table><tr><td>
  <table>
    <tr><th>Φορολογική Περίοδος</th><th>Ημερολογιακή Περίοδος</th>
        <th>Κατάσταση Υποχρέωσης</th><th>Ενέργειες</th></tr>
    {PERIODS_ROWS}
  </table>
  <table><tr><td><input type="button" value="Δηλώσεις"></td></tr></table>
</td></tr></table>"""


async def test_periods(probe: Probe) -> None:
    await probe.page.set_content(PERIODS_PAGE)
    rows = await probe._action_cells("Ενέργειες",
                                     ["Επεξεργασία Δηλώσεων", "Επεξεργασία"])
    check(len(rows) == 4, "4 γραμμές περιόδων, παρότι τα κουμπιά είναι <div> σε "
                          "φωλιασμένο πίνακα", f"βρέθηκαν {len(rows)}")
    check(all("Τρίμηνο" in r["text"] for r in rows),
          "η γραμμή δίνει το κείμενο ΔΕΔΟΜΕΝΩΝ, όχι του φωλιασμένου κελιού")
    check(all(r["label"] == "Επεξεργασία Δηλώσεων" for r in rows),
          "δεν πιάστηκε το κουμπί «Δηλώσεις» της μονοκύτταρης γραμμής")

    # Ο παλιός τρόπος (labels των a/button/input) δεν βλέπει τα <div>
    old = await probe._rows_with_action(["Επεξεργασία Δηλώσεων"])
    check(len(old) == 0,
          "ο εντοπισμός με labels ΔΕΝ βλέπει κουμπιά <div> (γι' αυτό η στήλη)")


# ── Πολλές ενέργειες στο ίδιο κελί (γραμμή δήλωσης Ν) ───────────────────────

N_ACTIONS = """
  <input type="button" value="Υποβολή τροπ/κής">
  <input type="button" value="Προβολή">
  <input type="button" value="Προβολή Ε2">
  <input type="button" value="Προβολή Ε3">
  <input type="button" value="Δεδομένα myDATA">
  <input type="button" value="Προβολή TAXISNet">
  <input type="button" value="Κατάσταση">"""

N_PAGE = f"""
<table>
  <tr><th>Πηγή</th><th>Φορολογικό Έτος</th><th>Είδος</th><th>Ενέργειες</th></tr>
  <tr><td>TAXISnet</td><td>01/01/2025 - 31/12/2025</td><td>Αρχική</td>
      <td>{N_ACTIONS}</td></tr>
</table>"""


async def test_action_disambiguation(probe: Probe) -> None:
    await probe.page.set_content(N_PAGE)
    for want, expect in [("Προβολή", "Προβολή"),
                         ("Προβολή Ε3", "Προβολή Ε3"),
                         ("Προβολή Ε2", "Προβολή Ε2")]:
        rows = await probe._action_cells("Ενέργειες", [want])
        got = rows[0]["label"] if rows else None
        check(got == expect, f"«{want}» πατά ακριβώς «{expect}»", f"πάτησε {got!r}")

    # Η επικίνδυνη ενέργεια είναι ΠΡΩΤΗ στο κελί — δεν πρέπει να επιλέγεται ποτέ
    for want in ("Υποβολή τροπ/κής", "Υποβολή"):
        rows = await probe._action_cells("Ενέργειες", [want])
        check(len(rows) == 0, f"«{want}» μπλοκάρεται από το NEVER_CLICK")


async def test_allow_is_narrow(probe: Probe) -> None:
    """Η έκδοση ενημερότητας χρειάζεται ρητή εξαίρεση — που δεν πρέπει να διαρρέει."""
    await probe.page.set_content("""
    <table><tr><th>Α</th><th>Ενέργειες</th></tr>
      <tr><td>αίτημα 2025</td><td>
        <input type="button" value="Υποβολή Αιτήματος">
        <input type="button" value="Διαγραφή">
      </td></tr></table>""")

    rows = await probe._action_cells("Ενέργειες", ["Υποβολή Αιτήματος"])
    check(len(rows) == 0, "χωρίς allow, η υποβολή μένει μπλοκαρισμένη")

    rows = await probe._action_cells("Ενέργειες", ["Υποβολή Αιτήματος"],
                                     None, ["Υποβολή Αιτήματος"])
    check(len(rows) == 1 and rows[0]["label"] == "Υποβολή Αιτήματος",
          "με ρητό allow, η ζητούμενη ενέργεια επιτρέπεται")

    rows = await probe._action_cells("Ενέργειες", ["Διαγραφή"],
                                     None, ["Υποβολή Αιτήματος"])
    check(len(rows) == 0, "το allow ΔΕΝ ξεκλειδώνει άλλες επικίνδυνες ενέργειες")

    rows = await probe._action_cells("Ενέργειες", ["Υποβολή"],
                                     None, ["Υποβολή"])
    check(len(rows) == 0, "το allow θέλει ΑΚΡΙΒΕΣ label — χωρίς κλιμάκωση με πρόθεμα")


# ── Επιλογή δήλωσης ─────────────────────────────────────────────────────────

async def test_pick_declaration(probe: Probe) -> None:
    rows = [{"idx": 1, "text": "1ο Τρίμηνο 2025 Αρχική Προβολή"},
            {"idx": 2, "text": "1ο Τρίμηνο 2025 τροποποιητικη δηλωση Προβολή"}]
    pick = probe._pick_declaration(rows)
    check(pick["is_tropo"] and pick["idx"] == 2,
          "επιλέγεται η τροποποιητική (ακόμη και άτονη/πεζή)")

    only = [{"idx": 1, "text": "1ο Τρίμηνο 2025 Αρχική Προβολή"}]
    check(not probe._pick_declaration(only)["is_tropo"],
          "χωρίς τροποποιητική, επιλέγεται η αρχική")

    # Το «Υποβολή τροπ/κής» ΔΕΝ πρέπει να περνά για τροποποιητική δήλωση
    misleading = [{"idx": 1, "text": "Αρχική Οριστική Υποβολή τροπ/κής Προβολή"}]
    check(not probe._pick_declaration(misleading)["is_tropo"],
          "το κουμπί «Υποβολή τροπ/κής» δεν μπερδεύεται με τροποποιητική δήλωση")


# ── Σκελετός σελίδας ────────────────────────────────────────────────────────

async def test_chrome_rows(probe: Probe) -> None:
    await probe.page.set_content("""
    <table><tr><td>Έχετε 2 νέα μηνύματα. Πατήστε <a href="#">προβολή</a>
      για να μεταβείτε στα εισερχόμενα μηνύματα σας.</td></tr></table>
    <table><tr><td><a href="#">2.Προβολή</a></td></tr></table>
    <table>
      <tr><td>1ο Τρίμηνο 2025 ΑΡΧΙΚΗ</td>
          <td><input type="submit" value="Προβολή"></td></tr>
    </table>""")
    rows = await probe._rows_with_action(["Προβολή"])
    texts = " ".join(r["text"] for r in rows)
    check("μηνύματα" not in texts,
          "η μπάρα «νέα μηνύματα» δεν περνά ως γραμμή δήλωσης")
    check(all("2.Προβολή" != r["text"] for r in rows),
          "ο σύνδεσμος μενού «2.Προβολή» δεν περνά ως γραμμή δήλωσης")
    check(len(rows) == 1, "μένει μόνο η πραγματική γραμμή", f"{len(rows)} γραμμές")


# ── Ονόματα αρχείων ─────────────────────────────────────────────────────────

def test_filenames() -> None:
    f = MyAADEAutomation.safe_filename
    check(f("ΠΕΛΑΤΗΣ", "2025", "Ε3") == "2024_ΠΕΛΑΤΗΣ_Ε3.pdf",
          "webtax: «ΔΗΛΩΣΕΙΣ ΕΤΟΥΣ 2025» = φορολογικό έτος 2024")
    check(f("ΠΕΛΑΤΗΣ", "2025", "Ν", shift_year=False) == "2025_ΠΕΛΑΤΗΣ_Ν.pdf",
          "income/ΦΠΑ: το έτος ΔΕΝ μετατοπίζεται")


# ── Επιλογή οντότητας / ρόλου ───────────────────────────────────────────────

# Η σελίδα που είδαμε για ΤΡΙΚΚΑ: ο χρήστης είναι το 068261591, αλλά η λίστα
# προσφέρει ΜΟΝΟ την οντότητα που εκπροσωπεί. Η βοήθεια το λέει ρητά.
ENTITY_PAGE = """
<div>Α.Φ.Μ.:068261591 - ΤΡΙΚΚΑ ΔΗΜ. ΑΛΙΚΗ</div>
<div>Επιλογή Ρόλου</div>
<table>
  <tr><th>Α.Φ.Μ.</th><th>Επωνυμία</th></tr>
  <tr><td><a href="#">802562079</a></td>
      <td>ΚΠΤΑ ΚΑΤΑΣΚΕΥΑΣΤΙΚΗ ΜΟΝΟΠΡΟΣΩΠΗ</td></tr>
</table>"""


async def test_entity_choice(probe: Probe) -> None:
    await probe.page.set_content(ENTITY_PAGE)

    own = await probe._own_afm()
    check(own == "068261591", "διαβάζεται το ΑΦΜ του συνδεδεμένου χρήστη",
          f"βρέθηκε {own!r}")

    choices = await probe._afm_choices()
    labels = [c["label"] for c in choices]
    check(labels == ["802562079"],
          "η λίστα προσφέρει μόνο την οντότητα που εκπροσωπείται")
    check(own not in labels,
          "το ΑΦΜ του ίδιου ΔΕΝ είναι στη λίστα — γι' αυτό χρειάζεται ο ρόλος")

    # Ο έλεγχος που εμποδίζει να κατέβουν έγγραφα ΑΛΛΟΥ φορολογουμένου
    mine = [c for c in choices if c["label"] == own]
    check(not mine, "για ατομική, καμία επιλογή δεν ταιριάζει -> δεν πατιέται "
                    "ξένο ΑΦΜ")


# Η σελίδα «Επιλογή Ρόλου», με τα ΑΚΡΙΒΗ λεκτικά που έδειξε το log
ROLE_PAGE = """
<div>Α.Φ.Μ.:068261591 - ΤΡΙΚΚΑ ΔΗΜ. ΑΛΙΚΗ</div>
<div>Επιλέξτε ρόλο:</div>
<a href="#">για τον εαυτό μου</a>
<a href="#">ως Εκπρόσωπος Νομικού Προσώπου</a>"""


async def test_role_page(probe: Probe) -> None:
    await probe.page.set_content(ROLE_PAGE)
    labels = [i["label"] for i in await probe._clickables()]
    check("για τον εαυτό μου" in labels,
          "η σελίδα ρόλων δεν έχει ΑΦΜ — μόνο λεκτικά")

    # Ο ρόλος του εαυτού επιλέγεται, ο ρόλος εκπροσώπου ΠΟΤΕ
    from automation.base import label_norm
    avoid = ["ΕΚΠΡΟΣΩΠΟΣ", "ΝΟΜΙΚΟΥ"]
    self_ok = label_norm("για τον εαυτό μου") == label_norm(labels[0])
    rep_blocked = any(a in label_norm("ως Εκπρόσωπος Νομικού Προσώπου")
                      for a in avoid)
    check(self_ok, "«για τον εαυτό μου» ταιριάζει ακριβώς")
    check(rep_blocked, "ο ρόλος εκπροσώπου αποκλείεται από το avoid")


# ── Μητρώο & Επικοινωνία (νέο myAADE) ───────────────────────────────────────

# Τα πλακίδια της αρχικής, όπως στη φωτογραφία. Δίπλα στις βεβαιώσεις υπάρχουν
# ενέργειες που αλλάζουν κωδικό πρόσβασης ή στοιχεία της επιχείρησης.
REGISTRY_TILES = """
<div>
  <a href="#!/certificates">Βεβαιώσεις Μητρώου</a>
  <a href="#!/contact">Στοιχεία Επικοινωνίας</a>
  <a href="#!/iban">Δήλωση Λογαριασμού IBAN</a>
  <a href="#!/pwd">Αλλαγή Κωδικού TAXISnet</a>
  <a href="#!/edit">Αλλαγή Στοιχείων Μητρώου</a>
  <a href="#!/msg">Τα Μηνύματά μου</a>
  <a href="#!/auth">Εξουσιοδοτήσεις</a>
</div>"""


async def test_registry_tiles(probe: Probe) -> None:
    await probe.page.set_content(REGISTRY_TILES)
    from automation.myaade import MyAADEAutomation as M

    def blocked(lbl: str) -> bool:
        n = label_norm(lbl)
        return any(bad in n for bad in M.NEVER_CLICK)

    for danger in ("Αλλαγή Κωδικού TAXISnet", "Αλλαγή Στοιχείων Μητρώου",
                   "Δήλωση Λογαριασμού IBAN", "Εξουσιοδοτήσεις"):
        check(blocked(danger), f"«{danger}» δεν πατιέται ποτέ")

    check(not blocked("Βεβαιώσεις Μητρώου"),
          "«Βεβαιώσεις Μητρώου» επιτρέπεται")

    # Το πλακίδιο εντοπίζεται και πατιέται
    ok = await probe._click_tile("Βεβαιώσεις Μητρώου", "τεστ", attempts=1)
    check(ok, "το πλακίδιο «Βεβαιώσεις Μητρώου» εντοπίζεται")


# Η οθόνη «Βεβαιώσεις Μητρώου»: τέσσερα πλακίδια, δύο ζεύγη που ξεκινούν ίδια.
# Το κείμενο σπάει σε δύο γραμμές, όπως στο portal.
CERTIFICATE_TILES = """
<div>
  <a href="#!/a">Τρέχουσα Εικόνα
     Φυσικού Προσώπου</a>
  <a href="#!/b">Ιστορικό Μεταβολών
     Φυσικού Προσώπου</a>
  <a href="#!/c">Τρέχουσα Εικόνα
     Οντότητας/Επιχείρησης</a>
  <a href="#!/d">Ιστορικό Μεταβολών
     Οντότητας/Επιχείρησης</a>
</div>"""


async def test_certificate_tile(probe: Probe) -> None:
    await probe.page.set_content(CERTIFICATE_TILES)
    async def fake_click(el):        # χωρίς πραγματική πλοήγηση στο τεστ
        return None
    probe._click_and_follow = fake_click

    ok = await probe._click_tile(
        "Τρέχουσα Εικόνα Οντότητας/Επιχείρησης", "τεστ",
        avoid=["ΦΥΣΙΚΟΥ ΠΡΟΣΩΠΟΥ", "ΙΣΤΟΡΙΚΟ"], attempts=1)
    check(ok, "εντοπίζεται το «Τρέχουσα Εικόνα Οντότητας/Επιχείρησης»")

    picked = [m for m in probe.logs if "πλακίδιο" in m]
    txt = picked[-1] if picked else ""
    check("Οντότητας/Επιχείρησης" in txt and "Φυσικού" not in txt,
          "πατιέται η ΕΠΙΧΕΙΡΗΣΗ και όχι το φυσικό πρόσωπο", txt)
    check("Ιστορικό" not in txt,
          "πατιέται η ΤΡΕΧΟΥΣΑ ΕΙΚΟΝΑ και όχι το ιστορικό μεταβολών", txt)


# Η ΠΡΑΓΜΑΤΙΚΗ δομή: τα πλακίδια είναι <div>, όχι <a>/<button>, και γύρω τους
# υπάρχει το μενού του portal (που ΕΙΝΑΙ κανονικά links). Το _clickables()
# έβρισκε μόνο το μενού και η λήψη αποτύγχανε με «δεν βρέθηκε το πλακίδιο».
REAL_TILES_PAGE = """
<nav>
  <a href="#">ΑΦΜ &amp; Κλειδάριθμος</a>
  <a href="#">Τα Αιτήματά μου</a>
  <a href="#">Μητρώο &amp; Επικοινωνία</a>
  <a href="#">Αποσύνδεση</a>
</nav>
<div class="tiles">
  <div class="tile" onclick="go('cert')">Βεβαιώσεις Μητρώου</div>
  <div class="tile" onclick="go('contact')">Στοιχεία Επικοινωνίας</div>
  <div class="tile" onclick="go('pwd')">Αλλαγή Κωδικού TAXISnet</div>
  <div class="tile" onclick="go('pos')">Μητρώο POS</div>
</div>"""


async def test_tiles_are_divs(probe: Probe) -> None:
    await probe.page.set_content(REAL_TILES_PAGE)

    narrow = [i["label"] for i in await probe._clickables()]
    check("Βεβαιώσεις Μητρώου" not in narrow,
          "ο στενός selector ΔΕΝ βλέπει τα πλακίδια <div> (η αιτία του σφάλματος)")

    tiles = [t["label"] for t in await probe._tile_choices()]
    check("Βεβαιώσεις Μητρώου" in tiles,
          "το _tile_choices τα βρίσκει ανεξάρτητα από τύπο στοιχείου")
    check("Αλλαγή Κωδικού TAXISnet" in tiles,
          "εντοπίζονται και τα επικίνδυνα, ώστε να μπορούν να αποκλειστούν")

    async def fake_click(el):
        return None
    probe._click_and_follow = fake_click
    probe.logs.clear()
    ok = await probe._click_tile("Βεβαιώσεις Μητρώου", "τεστ", attempts=1)
    check(ok, "το πλακίδιο <div> πατιέται κανονικά")

    probe.logs.clear()
    blocked_ok = not await probe._click_tile("Αλλαγή Κωδικού TAXISnet", "τεστ",
                                             attempts=1)
    check(blocked_ok, "το «Αλλαγή Κωδικού TAXISnet» παραμένει αποκλεισμένο")


async def test_check_all_boxes(probe: Probe) -> None:
    """Επιλογή όλων των ενοτήτων πριν την έκδοση."""
    await probe.page.set_content("""
      <form>
        <input type="checkbox" id="a"><label for="a">Στοιχεία έδρας</label>
        <input type="checkbox" id="b"><label for="b">Δραστηριότητες</label>
        <input type="checkbox" id="c" checked><label for="c">ΦΠΑ</label>
        <input type="checkbox" id="d" disabled><label for="d">Ανενεργό</label>
      </form>""")
    n = await probe._check_all_boxes()
    state = await probe.page.evaluate(
        "() => [...document.querySelectorAll('input')].map(i => i.checked)")
    check(n == 2, "τσεκάρονται μόνο όσα ήταν ανεπίλεκτα", f"{n}")
    check(state[0] and state[1] and state[2],
          "όλα τα ενεργά κουτάκια είναι επιλεγμένα")


async def test_custom_checkboxes(probe: Probe) -> None:
    """
    Οι ενότητες της βεβαίωσης μπορεί να ΜΗΝ είναι input[type=checkbox]: σε
    Angular είναι συχνά custom στοιχεία με role/aria-checked. Αν δεν πιαστούν,
    η βεβαίωση βγαίνει με λιγότερες σελίδες — ακριβώς το σύμπτωμα που είδαμε.
    """
    await probe.page.set_content("""
      <table>
        <tr><td><span role="checkbox" aria-checked="false"
                      onclick="this.setAttribute('aria-checked','true')"
                      >□</span></td><td>Στοιχεία έδρας</td></tr>
        <tr><td><span role="checkbox" aria-checked="false"
                      onclick="this.setAttribute('aria-checked','true')"
                      >□</span></td><td>Δραστηριότητες (ΚΑΔ)</td></tr>
        <tr><td><span role="checkbox" aria-checked="true">☑</span></td>
            <td>Στοιχεία ΦΠΑ</td></tr>
      </table>""")
    state = await probe._checkbox_state()
    check(len(state) == 3, "εντοπίζονται custom κουτάκια με role=checkbox",
          f"{len(state)}")
    check(state[2]["checked"] and not state[0]["checked"],
          "διαβάζεται σωστά το aria-checked")
    check("Στοιχεία έδρας" in state[0]["label"],
          "το label διαβάζεται από τη γειτονική στήλη", state[0]["label"])

    n = await probe._check_all_boxes()
    after = await probe._checkbox_state()
    check(n == 2 and all(b["checked"] for b in after),
          "επιλέγονται όλες οι ενότητες", f"{n} κλικ")


async def test_multiselect_list(probe: Probe) -> None:
    """
    Η ΠΡΑΓΜΑΤΙΚΗ οθόνη «Τρέχουσα Εικόνα Οντότητας»: τα στοιχεία προς έκδοση
    είναι <select multiple>, ΟΧΙ κουτάκια. Επειδή ψάχναμε checkboxes, δεν
    επιλεγόταν καμία ενότητα και η βεβαίωση έβγαινε με 2 σελίδες αντί για 4.
    """
    await probe.page.set_content("""
      <div>Επιλέξτε τα στοιχεία που θέλετε να εκδώσετε</div>
      <select multiple size="9" id="items">
        <option>Σχέσεις Επιχείρησης</option>
        <option>Μέλη/Εταίροι Επιχείρησης</option>
        <option>Συσχετιζόμενοι ΑΦΜ</option>
        <option>Συμμετοχές</option>
        <option>Δραστηριότητες Επιχείρησης</option>
        <option>Εγκαταστάσεις Εσωτερικού</option>
        <option>Εγκαταστάσεις Εξωτερικού</option>
        <option>Στοιχεία Έδρας Αλλοδαπής</option>
        <option>Ενδοκοινοτικές εξ Αποστάσεως Πωλήσεις</option>
      </select>""")

    boxes = await probe._checkbox_state()
    check(len(boxes) == 0,
          "δεν υπάρχουν καθόλου checkboxes σε αυτή την οθόνη (η αιτία)")

    n = await probe._select_all_options()
    chosen = await probe.page.evaluate(
        "() => [...document.getElementById('items').selectedOptions].length")
    check(n == 9 and chosen == 9,
          "επιλέγονται ΟΛΕΣ οι 9 ενότητες της λίστας", f"{n} / επιλεγμένες {chosen}")


async def test_selection_fires_events(probe: Probe) -> None:
    """
    Η επιλογή πρέπει να γίνεται με ΚΛΙΚ, ώστε να πυροδοτούνται τα events που
    περιμένει το AngularJS. Με σκέτο select_option οι επιλογές φαίνονταν
    επιλεγμένες (γκρι φόντο στο screenshot) αλλά το μοντέλο της εφαρμογής δεν
    ενημερωνόταν και η βεβαίωση έβγαινε πάλι 2 σελίδες αντί για 4.
    """
    await probe.page.set_content("""
      <select multiple size="4" id="s">
        <option>Α</option><option>Β</option><option>Γ</option>
      </select>
      <script>
        window.evt = {click: 0, change: 0};
        const s = document.getElementById('s');
        s.addEventListener('click',  () => window.evt.click++);
        s.addEventListener('change', () => window.evt.change++);
      </script>""")

    n = await probe._select_all_options()
    evt = await probe.page.evaluate("() => window.evt")
    check(n == 3, "επιλέγονται και οι τρεις", f"{n}")
    check(evt["click"] >= 3,
          "κάθε επιλογή δέχτηκε ΠΡΑΓΜΑΤΙΚΟ κλικ", f"{evt['click']} κλικ")
    check(evt["change"] >= 1,
          "πυροδοτήθηκε change ώστε να ενημερωθεί το μοντέλο",
          f"{evt['change']} change")


async def test_hidden_selects_skipped(probe: Probe) -> None:
    """
    Το Angular κρατά στο DOM αντίγραφα της λίστας (myselect2/3/4) κρυμμένα.
    Κάθε ένα κατανάλωνε 5 δευτερόλεπτα σε timeout και γέμιζε το log με δεκάδες
    γραμμές call log της Playwright.
    """
    await probe.page.set_content("""
      <select multiple size="3" id="myselect1">
        <option>Ορατή Α</option><option>Ορατή Β</option>
      </select>
      <select multiple size="3" id="myselect2" style="display:none">
        <option>Κρυφή</option>
      </select>
      <div style="display:none">
        <select multiple size="3" id="myselect3"><option>Κρυφή 2</option></select>
      </div>""")

    import time
    t0 = time.monotonic()
    probe.logs.clear()
    n = await probe._select_all_options()
    elapsed = time.monotonic() - t0

    check(n == 2, "επιλέγονται μόνο οι επιλογές της ΟΡΑΤΗΣ λίστας", f"{n}")
    check(elapsed < 5, "οι κρυφές παραλείπονται χωρίς timeout",
          f"{elapsed:.1f}s")
    check(any("κρυφές λίστες παραλείφθηκαν" in m for m in probe.logs),
          "καταγράφεται πόσες κρυφές παραλείφθηκαν")
    check(not any("Timeout" in m for m in probe.logs),
          "καμία γραμμή timeout στο log")


async def test_offscreen_checkbox(probe: Probe) -> None:
    """Κουτάκι εκτός ορατού πεδίου — πριν προσπερνιόταν σιωπηλά."""
    await probe.page.set_content("""
      <div style="height:2000px">γέμισμα</div>
      <input type="checkbox" id="far">
      <label for="far">Εγκαταστάσεις εσωτερικού</label>""")
    n = await probe._check_all_boxes()
    state = await probe._checkbox_state()
    check(n == 1 and state[0]["checked"],
          "κουτάκι πολύ χαμηλά στη σελίδα επιλέγεται (scroll into view)")


async def test_download_popup_does_not_hang(probe: Probe) -> None:
    """
    Σε headless το PDF δεν ανοίγει σε viewer αλλά ΚΑΤΕΒΑΙΝΕΙ: το portal ανοίγει
    popup που δεν φορτώνει ποτέ σελίδα. Η αναμονή για domcontentloaded έσκαγε
    στα 15 δευτερόλεπτα και έριχνε ΟΛΗ τη λήψη — παρότι το αρχείο είχε ήδη
    πιαστεί από το δίκτυο.
    """
    import time
    await probe.page.set_content(
        '<a id="go" target="_blank" download="x.pdf" '
        'href="data:application/pdf;base64,JVBERi0xLjQK">Προβολή</a>')
    probe.logs.clear()
    t0 = time.monotonic()
    failed = None
    try:
        await probe._click_and_follow(probe.page.locator("#go"))
    except Exception as e:                      # noqa: BLE001
        failed = e
    elapsed = time.monotonic() - t0

    check(failed is None, "το popup λήψης δεν ρίχνει τη ροή", str(failed or ""))
    check(elapsed < 12, "δεν περιμένει μέχρι το timeout των 15s",
          f"{elapsed:.1f}s")


async def test_click_near_with_latin_letters(probe: Probe) -> None:
    """
    Η αρχική του Αποδεικτικού Ενημερότητας έχει ΔΥΟ κουμπιά «Είσοδος»: ένα για
    την έκδοση και ένα για το ιστορικό αιτήσεων.

    ΚΑΙ το κείμενο-οδηγό γράφεται με ΛΑΤΙΝΙΚΑ γράμματα: «Έκδοση Aποδεικτικού
    Eνημερότητας» (λατινικά A και E). Η norm() μέσα στη JavaScript δεν έκανε
    folding των ομοιογραμμάτων, όπως κάνει το label_norm() της Python, οπότε οι
    δύο πλευρές δεν συμφωνούσαν και δεν βρισκόταν κανένα κουμπί.
    """
    await probe.page.set_content("""
      <div><h3>Έκδοση Aποδεικτικού Eνημερότητας / Βεβαίωσης Οφειλής</h3>
           <div>Υποβολή Αίτησης <button id="right">Είσοδος</button></div></div>
      <div><h3>Οι Αιτήσεις μου</h3>
           <div>Ιστορικό αιτήσεων <button id="wrong">Είσοδος</button></div></div>""")

    async def fake_click(el):
        return None
    probe._click_and_follow = fake_click

    ok = await probe._click_near("Είσοδος", "Έκδοση Αποδεικτικού", "τεστ",
                                 attempts=1)
    check(ok, "βρίσκεται το «Είσοδος» παρά τα λατινικά γράμματα στον οδηγό")
    if ok:
        which = await probe.page.evaluate(
            "() => document.querySelector('[data-gdf-near]').id")
        check(which == "right",
              "πατιέται το κουμπί ΤΗΣ ΕΚΔΟΣΗΣ, όχι του ιστορικού αιτήσεων",
              f"id={which}")


async def test_pdf_button_behind_modal(probe: Probe) -> None:
    """
    Μετά την έκδοση του αποδεικτικού, το «Ψηφιακό αρχείο Αποδεικτικού
    Ενημερότητας» υπάρχει ΔΥΟ φορές: στη σελίδα και μέσα στο modal
    «Αποθήκευση Αίτησης» που είναι από πάνω. Το κουμπί της σελίδας είναι
    καλυμμένο από το overlay — το κλικ πάνω του αποτυγχάνει.

    Το κείμενο σπάει σε δύο γραμμές, όπως στο portal.
    """
    await probe.page.set_content("""
      <div id="page">
        <div id="onpage" style="padding:20px;background:#2b6cb0;color:#fff">
          Ψηφιακό αρχείο<br>Αποδεικτικού Ενημερότητας</div>
      </div>
      <div id="overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.5)">
        <div style="background:#fff;margin:40px;padding:20px">
          <div>Αποθήκευση Αίτησης</div>
          <div id="inmodal" style="padding:20px;background:#2b6cb0;color:#fff">
            Ψηφιακό αρχείο<br>Αποδεικτικού Ενημερότητας</div>
        </div>
      </div>""")

    clicked = {}
    async def fake_click(el):
        clicked["id"] = await el.evaluate("e => e.id")
    probe._click_and_follow = fake_click

    ok = await probe._click_any("Ψηφιακό αρχείο", "τεστ", attempts=1)
    check(ok, "βρίσκεται το κουμπί παρότι το κείμενο σπάει σε δύο γραμμές")
    check(clicked.get("id") == "inmodal",
          "πατιέται αυτό ΜΕΣΑ στο modal, όχι το καλυμμένο της σελίδας",
          f"πάτησε: {clicked.get('id')}")


def test_merge_pdfs() -> None:
    """
    Οι δηλώσεις ΦΠΑ ενώνονται σε ΕΝΑ αρχείο ανά έτος, με τη σειρά των περιόδων.
    Τα επιμέρους σβήνονται ΜΟΝΟ αφού επαληθευτεί το ενωμένο — πρόκειται για
    φορολογικά στοιχεία πελάτη, καλύτερα τέσσερα αρχεία παρά κανένα.
    """
    import tempfile
    from pypdf import PdfReader, PdfWriter

    logs: list[str] = []

    class T(MyAADEAutomation):
        def __init__(self):
            self.log = lambda m, l="info": logs.append(m)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        parts = []
        for i in range(1, 5):                 # 4 τρίμηνα, 1 σελίδα το καθένα
            w = PdfWriter()
            w.add_blank_page(width=200, height=200)
            p = d / f"2023_ΠΕΛΑΤΗΣ_ΦΠΑ_{i}.pdf"
            with p.open("wb") as fh:
                w.write(fh)
            parts.append(p)

        target = d / "2023_ΠΕΛΑΤΗΣ_ΦΠΑ.pdf"
        ok = T().merge_pdfs(parts, target)

        check(ok, "η ένωση ολοκληρώνεται")
        check(target.exists() and len(PdfReader(str(target)).pages) == 4,
              "το ενωμένο έχει όλες τις σελίδες των επιμέρους")
        check(not any(p.exists() for p in parts),
              "τα επιμέρους σβήνονται μετά την επαλήθευση")
        check(not list(d.glob("*.merging.pdf")),
              "δεν μένει προσωρινό αρχείο")

    # Αποτυχία: κατεστραμμένο αρχείο -> ΤΙΠΟΤΑ δεν σβήνεται
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        good = d / "a_ΦΠΑ_1.pdf"
        w = PdfWriter(); w.add_blank_page(width=200, height=200)
        with good.open("wb") as fh:
            w.write(fh)
        bad = d / "a_ΦΠΑ_2.pdf"
        bad.write_bytes(b"not a pdf at all")

        logs.clear()
        ok = T().merge_pdfs([good, bad], d / "a_ΦΠΑ.pdf")
        check(not ok, "η ένωση αποτυγχάνει σε χαλασμένο αρχείο")
        check(good.exists() and bad.exists(),
              "σε αποτυχία ΚΑΝΕΝΑ επιμέρους δεν χάνεται")


def test_login_success_check() -> None:
    """
    Ο έλεγχος «συνδεθήκαμε;» πρέπει να κοιτά το HOST, όχι ολόκληρο το URL.

    Το URL της ΑΠΟΤΥΧΗΜΕΝΗΣ σύνδεσης περιέχει παράμετρο
    resource_url=…www1.aade.gr…, οπότε ένα «'aade.gr' in url» περνούσε ως
    επιτυχία ενώ ήμασταν ακόμη στη φόρμα του login.gsis.gr. Αποτέλεσμα: κάθε
    έγγραφο αποτύγχανε με «δεν βρέθηκε το έντυπο» αντί για «λάθος κωδικοί».
    """
    from urllib.parse import urlparse, parse_qs

    failed = ("https://login.gsis.gr/mylogin/login.jsp?bmctx=1DB"
              "&authn_try_count=1&p_error_code=OAM-2&locale=el_GR"
              "&resource_url=https%253A%252F%252Fwww1.aade.gr%252Ftaxisnet")
    ok = "https://www1.aade.gr/taxisnet/income/protected/displayTypes.htm"

    def logged_in(url: str) -> bool:
        return urlparse(url).netloc.lower().endswith("aade.gr")

    check("aade.gr" in failed,
          "το URL αποτυχίας ΠΕΡΙΕΧΕΙ «aade.gr» (γι' αυτό ξεγελούσε)")
    check(not logged_in(failed),
          "ο έλεγχος με host αναγνωρίζει την αποτυχία")
    check(logged_in(ok), "και δέχεται την πραγματική επιτυχία")

    code = (parse_qs(urlparse(failed).query).get("p_error_code") or [""])[0]
    check(code == "OAM-2", "διαβάζεται ο κωδικός σφάλματος για σαφές μήνυμα",
          code)


def test_registry_filename() -> None:
    from datetime import date
    name = MyAADEAutomation.registry_filename("GREEN DOT HELLAS ΟΕ")
    check(name.startswith(date.today().isoformat()),
          "η βεβαίωση μητρώου φέρει ΗΜΕΡΟΜΗΝΙΑ, όχι έτος", name)
    check(name.endswith("_Μητρώο.pdf"), "σωστή κατάληξη", name)


async def main() -> None:
    test_greek_text()
    test_filenames()
    test_registry_filename()
    test_login_success_check()
    test_merge_pdfs()
    async with async_playwright() as p:
        # Ίδιο fallback με την εφαρμογή: αλλιώς τα tests δεν τρέχουν καθόλου σε
        # μηχάνημα όπου ο πακεταρισμένος Chromium δεν ξεκινά.
        browser = await launch_browser(p, headless=True, log=print)
        probe = Probe(await browser.new_page())
        await test_periods(probe)
        await test_action_disambiguation(probe)
        await test_allow_is_narrow(probe)
        await test_pick_declaration(probe)
        await test_chrome_rows(probe)
        await test_entity_choice(probe)
        await test_role_page(probe)
        await test_registry_tiles(probe)
        await test_certificate_tile(probe)
        await test_tiles_are_divs(probe)
        await test_check_all_boxes(probe)
        await test_custom_checkboxes(probe)
        await test_multiselect_list(probe)
        await test_selection_fires_events(probe)
        await test_hidden_selects_skipped(probe)
        await test_offscreen_checkbox(probe)
        await test_download_popup_does_not_hang(probe)
        await test_click_near_with_latin_letters(probe)
        await test_pdf_button_behind_modal(probe)
        await browser.close()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} αποτυχίες:")
        for f in FAILURES:
            print(f"   • {f}")
        sys.exit(1)
    print("✅ Όλοι οι έλεγχοι πέρασαν")


if __name__ == "__main__":
    asyncio.run(main())
