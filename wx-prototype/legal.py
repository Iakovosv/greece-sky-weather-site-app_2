"""Legal pages: όροι, απόρρητο, επιστροφές.

Written against what this codebase actually does, not from a generic template.
Every claim below is checkable in the source:

* Tokens are HMAC-signed JWTs held in the browser's localStorage (``entitlements.py``).
  The server sets no cookies, so there is no cookie banner to place.
* Anonymous forecasts go to ``/api/brief`` with a latitude and longitude. Those
  coordinates are the visitor's interest, and they are not written to disk.
* GRIB files are cached under ``WX_CACHE_DIR``; the station database holds the
  location of a weather station its owner registered.
* Payments are handled by Stripe; no card data reaches this application.

Two things in here are deliberately blunt. First, the distance from this service
to a paid subscription: the operator has not yet had these pages reviewed by a
lawyer, and the pages say so rather than borrowing the confident tone of a
template. Second, weather is not guaranteed. A forecast is an estimate, and the
terms say that in the place a customer will actually read it, because the gap
between "prediction" and "promise" is exactly what gets litigated.
"""
from __future__ import annotations

import html
import os

# Kept in one place so the footer, the pages and any future invoice agree.
PRODUCT = "Greece Sky and Weather"
COPYRIGHT_YEAR = 2026
LAST_UPDATED = "23 Σεπτεμβρίου 2026"

# Contact details are configuration, not constants, because a wrong handle in a
# footer is invisible until a customer writes to it. Override these in .env if the
# defaults below are not the real accounts.
_DEFAULTS = {
    "youtube": "https://www.youtube.com/@GreeceSkyandWeather",
    "facebook": "https://www.facebook.com/GreeceSkyandWeather",
    "email": "greekskyweather@gmail.com",
}


def contacts() -> dict[str, str]:
    """Social and support links, from the environment with safe defaults."""
    return {
        key: (os.environ.get(f"WX_{key.upper()}_URL")
              or os.environ.get(f"WX_{key.upper()}")
              or default)
        for key, default in _DEFAULTS.items()
    }


# The routes call contacts() so a late .env load is still picked up; these module
# level names are only the defaults, and nothing else should read them.
CONTACT_EMAIL = _DEFAULTS["email"]
YOUTUBE_URL = _DEFAULTS["youtube"]
FACEBOOK_URL = _DEFAULTS["facebook"]

_DOC_CSS = (
    ":root{color-scheme:dark}"
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "max-width:820px;margin:0 auto;padding:24px 20px 60px;"
    "background:#070b14;color:#eef2f8;line-height:1.72;font-size:15px}"
    # Same wash as the app, so following a legal link does not jump to a
    # different-looking document.
    "body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;"
    "background:"
    "radial-gradient(1100px 700px at 8% -10%,rgba(77,163,255,.22),transparent 62%),"
    "radial-gradient(900px 650px at 104% 4%,rgba(139,92,246,.20),transparent 62%),"
    "radial-gradient(1000px 700px at 50% 116%,rgba(16,185,129,.15),transparent 62%),"
    "linear-gradient(180deg,#070b14,#0b1220 55%,#070b14)}"
    "h1{font-size:25px;margin:0 0 6px;letter-spacing:-.01em}"
    "h2{font-size:17px;margin:30px 0 8px}"
    "p,li{color:#c7d2e2}"
    "ul{padding-left:22px}"
    "a{color:#4da3ff}"
    ".upd{color:#9aa7bd;font-size:13px;margin:0 0 22px}"
    ".warn{background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.30);"
    "color:#fbbf24;border-radius:10px;padding:14px 16px;"
    "margin:22px 0;font-size:14px;line-height:1.65}"
    ".back{display:inline-block;margin-bottom:18px;font-size:13.5px}"
    "footer{margin-top:40px;padding-top:18px;border-top:1px solid rgba(255,255,255,.10);"
    "color:#9aa7bd;font-size:12.5px}"
)


def _page(title: str, body: str, email: str) -> str:
    """Wrap a document body in the shared shell.

    The title is escaped on the way in; the bodies are authored constants, so they
    are inserted as-is. ``email`` is escaped by the caller - it comes from the
    environment, so it is not a trusted string.
    """
    return (
        "<!doctype html><html lang='el'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_DOC_CSS}</style></head><body>"
        "<a class='back' href='/'>&larr; Πίσω στην πρόγνωση</a>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class='upd'>Τελευταία ενημέρωση: {LAST_UPDATED}</p>"
        f"{body}"
        "<footer>"
        f"<a href='/'>{PRODUCT}</a> &middot; "
        f"<a href='mailto:{email}'>{email}</a> &middot; "
        f"<a href='/licenses'>Άδειες δεδομένων</a>"
        f"<br>&copy; {COPYRIGHT_YEAR} {PRODUCT}"
        "</footer></body></html>"
    )


_REVIEW_NOTICE = (
    "<div class='warn'><b>Σημείωση.</b> Το κείμενο αυτό περιγράφει με ακρίβεια τι "
    "κάνει η υπηρεσία τεχνικά, αλλά <b>δεν έχει ελεγχθεί από νομικό</b>. Πριν από "
    "πραγματική εμπορική διάθεση και ενεργοποίηση πληρωμών, ζήτησε επισκόπηση από "
    "δικηγόρο — ιδίως για την ελληνική και ενωσιακή νομοθεσία προστασίας "
    "καταναλωτή, ΑΠΔΠΧ και ΦΠΑ.</div>"
)


def terms_page() -> str:
    email = html.escape(contacts()["email"], quote=True)
    return _page("Όροι Χρήσης", _REVIEW_NOTICE + f"""
<h2>1. Τι είναι η υπηρεσία</h2>
<p>Το {PRODUCT} παρέχει σημειακή μετεωρολογική πρόγνωση για τοποθεσίες στην
Ελλάδα, βασισμένο σε ανοιχτά μετεωρολογικά δεδομένα (GFS/NOAA, ICON/DWD,
ECMWF, ERA5/Copernicus). Οι δείκτες αστάθειας, οι ραδιοβολίσεις Skew-T και τα
διαγράμματα υπολογίζονται από εμάς πάνω στα ανοιχτά δεδομένα.</p>

<h2>2. Η πρόγνωση δεν είναι εγγύηση</h2>
<p>Ο καιρός προβλέπεται, δεν βεβαιώνεται. Κάθε τιμή που εμφανίζεται είναι
<b>εκτίμηση με σφάλμα</b>, όχι βεβαιότητα. Οι μετεωρολογικές προβλέψεις
περιέχουν εγγενή αβεβαιότητα που αυξάνεται με τον χρόνο· ενδέχεται να
διαφέρουν σημαντικά από τις πραγματικές συνθήκες.</p>
<p><b>Μην βασίζεσαι σε αυτή την υπηρεσία ως μοναδική πηγή για αποφάσεις που
αφορούν ασφάλεια, ζωή, υγεία ή περιουσία</b> — πτήσεις, ναυσιπλοΐα, ορεινές
δραστηριότητες, γεωργικές επεμβάσεις, βιομηχανικές εργασίες. Για τέτοιες
αποφάσεις χρησιμοποίησε και τις επίσημες πηγές (ΕΜΥ, αρμόδιες αρχές) και την
κρίση σου.</p>
<p>Δεν φέρουμε ευθύνη για ζημία που προέκυψε από απόφαση βασισμένη στην
πρόγνωση, στον βαθμό που το επιτρέπει ο νόμος.</p>

<h2>3. Βαθμίδες και λογαριασμοί</h2>
<ul>
<li><b>Δωρεάν:</b> 48 ώρες πρόγνωσης (θερμοκρασία, αίσθηση, βροχή, άνεμος,
ριπές, βάση νεφών, μετεόγραμμα, αναζήτηση τοποθεσίας και αγαπημένα).</li>
<li><b>Δοκιμή:</b> 48 ώρες πρόσβασης στην πλήρη έκταση της βαθμίδας PRO, χωρίς
χρέωση και χωρίς κάρτα.</li>
<li><b>PRO:</b> 240 ώρες (10 ημέρες) συν ραδιοβόλιση Skew-T, δείκτες αστάθειας
και σύγκριση τριών μοντέλων.</li>
</ul>
<p>Η πρόσβαση εκδίδεται ως υπογεγραμμένο token που αποθηκεύεται στον browser
σου (localStorage). Δεν υπάρχει λογαριασμός με κωδικό: το token είναι το
εισιτήριο. Αν το διαγράψεις, χάνεις την πρόσβαση μέχρι να ξαναγίνει έκδοση.</p>

<h2>4. Πληρωμές και συνδρομές</h2>
<p>Οι πληρωμές εκτελούνται από τον πάροχο πληρωμών Stripe. Δεν βλέπουμε ούτε
αποθηκεύουμε στοιχεία κάρτας. Η χρέωση γίνεται εκ των προτέρων για την περίοδο
που επιλέγεις.</p>
<p><b>Αυτόματη ανανέωση.</b> Η συνδρομή <b>ανανεώνεται αυτόματα</b> στο τέλος
κάθε περιόδου, μέχρι να την ακυρώσεις. Η αυτόματη ανανέωση είναι ενεργή εξ
αρχής. Μπορείς να την απενεργοποιήσεις οποιαδήποτε στιγμή από τη «Διαχείριση
συνδρομής» μέσα στην υπηρεσία· η ενέργεια είναι διακριτική αλλά πάντα διαθέσιμη
στη ροή της συνδρομής. Η απενεργοποίηση σταματά τις επόμενες χρεώσεις και η
πρόσβαση συνεχίζεται μέχρι το τέλος της περιόδου που έχεις ήδη πληρώσει. Δες την
<a href='/refunds'>Πολιτική Επιστροφών</a>.</p>

<h2>5. Επιτρεπόμενη χρήση</h2>
<p>Μπορείς να χρησιμοποιείς την υπηρεσία για προσωπικούς ή επαγγελματικούς
σκοπούς. Δεν επιτρέπεται:</p>
<ul>
<li>αυτοματοποιημένη μαζική άντληση δεδομένων (scraping) πέρα από κανονική χρήση,</li>
<li>αναδιανομή ή μεταπώληση της υπηρεσίας ή των παραγόμενων δεδομένων ως δική σου,</li>
<li>παράκαμψη των ορίων των βαθμίδων ή των τεχνικών μέτρων πρόσβασης,</li>
<li>χρήση που βλάπτει τη διαθεσιμότητα της υπηρεσίας για άλλους.</li>
</ul>
<p>Οι πηγές δεδομένων έχουν τις δικές τους άδειες και υποχρεώσεις αναφοράς·
καταγράφονται στο <a href='/licenses'>μητρώο αδειών</a>.</p>

<h2>6. Διαθεσιμότητα</h2>
<p>Παρέχουμε την υπηρεσία «ως έχει», χωρίς εγγύηση διαθεσιμότητας (SLA). Η
υπηρεσία εξαρτάται από δημόσιες πηγές δεδομένων (NOMADS/NOAA, DWD, ECMWF,
Copernicus) που μπορεί να καθυστερήσουν ή να μην είναι διαθέσιμες· σε αυτή την
περίπτωση η πρόγνωση μπορεί να λείπει προσωρινά. Στόχος μας είναι η συνεχής
λειτουργία, αλλά δεν την εγγυόμαστε.</p>
<p>Μπορούμε να αλλάξουμε ή να καταργήσουμε λειτουργίες. Αν καταργήσουμε
ουσιώδη λειτουργία της βαθμίδας PRO, εφαρμόζεται η πολιτική επιστροφών.</p>

<h2>7. Μεταβολές όρων</h2>
<p>Μπορούμε να επικαιροποιήσουμε τους όρους. Η ημερομηνία ενημέρωσης
εμφανίζεται στην κορυφή. Η συνέχιση χρήσης μετά από αλλαγή σημαίνει αποδοχή
της. Για ουσιώδεις αλλαγές που επηρεάζουν πληρωμένους χρήστες, θα ειδοποιήσουμε
με email ή με ανακοίνωση στην υπηρεσία.</p>

<h2>8. Επικοινωνία</h2>
<p>Ερωτήσεις, προβλήματα, αιτήματα: <a href='mailto:{email}'>{email}</a>.</p>
""", email)


def privacy_page() -> str:
    email = html.escape(contacts()["email"], quote=True)
    return _page("Πολιτική Απορρήτου", _REVIEW_NOTICE + f"""
<h2>1. Σύνοψη</h2>
<p>Η υπηρεσία είναι σχεδιασμένη να συλλέγει όσο το δυνατόν λιγότερα. Δεν
θέτουμε cookies, δεν χρησιμοποιούμε analytics ή διαφημίσεις τρίτων, και δεν
πουλάμε δεδομένα. Δεν υπάρχει ούτε cookie banner, γιατί δεν υπάρχουν cookies
προς συγκατάθεση.</p>

<h2>2. Τι δεδομένα επεξεργαζόμαστε</h2>
<ul>
<li><b>Συντεταγμένες αναζήτησης.</b> Όταν ζητάς πρόγνωση, η τοποθεσία (π.χ.
Αθήνα ή συντεταγμένες) μεταδίδεται στο αίτημα. Είναι το τι σε
ενδιαφέρει, όχι το πού βρίσκεσαι, και <b>δεν αποθηκεύεται σε βάση</b>.</li>
<li><b>Αγαπημένα.</b> Οι τοποθεσίες που αποθηκεύεις ως αγαπημένες μένουν μόνο
στο localStorage του browser σου. Δεν αποστέλλονται στον διακομιστή μας και δεν
φαίνονται σε άλλη συσκευή.</li>
<li><b>Τοπική αποθήκευση στον browser.</b> Το token πρόσβασης αποθηκεύεται στο
localStorage της συσκευής σου, όχι σε cookie και όχι στον server μας. Δεν
αποστέλλεται αυτόματα· το στέλνεις μόνο όταν ζητάς περιεχόμενο PRO. Αν έχεις
συνδρομή, το token περιέχει και το αναγνωριστικό της συνδρομής (έναν κωδικό της
Stripe, χωρίς όνομα ή email), ώστε να μπορείς να τη διαχειριστείς χωρίς
λογαριασμό.</li>
<li><b>Στοιχεία σταθμού.</b> Αν καταχωρίσεις μετεωρολογικό σταθμό, αποθηκεύεται
το αναγνωριστικό του, το όνομα και οι συντεταγμένες του. Είναι δεδομένα θέσης
σταθμού, όχι προσώπου.</li>
<li><b>Πληρωμές.</b> Εκτελούνται από τη Stripe. Λαμβάνουμε μόνο ό,τι χρειάζεται
για την ενεργοποίηση της συνδρομής (κατάσταση πληρωμής, αναγνωριστικό πελάτη).
Τα στοιχεία κάρτας δεν περνούν ποτέ από τους διακομιστές μας.</li>
<li><b>Δεδομένα καιρού από σταθμούς.</b> Αν στείλεις μετρήσεις από σταθμό
(Ecowitt), αποθηκεύονται μετρήσεις και το αναγνωριστικό της συσκευής.</li>
</ul>

<h2>3. Δεδομένα σύνδεσης</h2>
<p>Όπως κάθε διαδικτυακή υπηρεσία, ο διακομιστής βλέπει τη διεύθυνση IP κατά τη
διάρκεια της σύνδεσης. Δεν τη συσχετίζουμε με προφίλ χρήστη ούτε τη
χρησιμοποιούμε για διαφήμιση.</p>

<h2>4. Αποδέκτες και μεταβιβάσεις</h2>
<p>Για να λειτουργήσει η πρόγνωση, ο διακομιστής ζητά δεδομένα από δημόσιες
πηγές (NOAA, DWD, ECMWF, Copernicus) και γεωκωδικοποίηση/υψόμετρο (Photon/OSM,
OpenTopoData). Σε αυτές τις κλήσεις μεταδίδονται συντεταγμένες της τοποθεσίας
που ζήτησες — όχι στοιχεία ταυτοποίησης. Ο πάροχος πληρωμών είναι η Stripe. Δεν
φορτώνουμε πλακίδια χάρτη, οπότε κανένας πάροχος χαρτών δεν βλέπει την IP σου
μέσω αυτής της σελίδας.</p>

<h2>5. Χρόνος διατήρησης</h2>
<p>Τα μετεωρολογικά αρχεία προσωρινής αποθήκευσης διαγράφονται αυτόματα. Τα
στοιχεία σταθμού παραμένουν όσο ο σταθμός είναι καταχωρισμένος. Τα δεδομένα
πληρωμών τηρούνται από τη Stripe σύμφωνα με τις δικές της υποχρεώσεις και τη
φορολογική νομοθεσία.</p>

<h2>6. Τα δικαιώματά σου</h2>
<p>Κατά τον ΓΚΠΔ έχεις δικαίωμα πρόσβασης, διόρθωσης, διαγραφής, περιορισμού,
εναντίωσης και φορητότητας. Για τα περισσότερα από αυτά αρκεί να ζητήσεις
διαγραφή του σταθμού σου ή να διαγράψεις το τοπικό token. Για οτιδήποτε άλλο:
<a href='mailto:{email}'>{email}</a>. Έχεις επίσης δικαίωμα
καταγγελίας στην Αρχή Προστασίας Δεδομένων Προσωπικού Χαρακτήρα (ΑΠΔΠΧ,
dpa.gr).</p>

<h2>7. Παιδιά</h2>
<p>Η υπηρεσία δεν απευθύνεται σε παιδιά κάτω των 15 ετών και δεν συλλέγουμε
εν γνώσει μας δεδομένα από αυτά.</p>

<h2>8. Μεταβολές</h2>
<p>Αν αλλάξει ο τρόπος επεξεργασίας, θα ενημερώσουμε αυτή τη σελίδα και την
ημερομηνία στην κορυφή.</p>

<h2>9. Επικοινωνία</h2>
<p>Υπεύθυνος επεξεργασίας και σημείο επαφής:
<a href='mailto:{email}'>{email}</a>.</p>
""", email)


def refunds_page() -> str:
    email = html.escape(contacts()["email"], quote=True)
    return _page("Πολιτική Επιστροφών και Ακυρώσεων", _REVIEW_NOTICE + f"""
<h2>1. Δοκιμή πριν την πληρωμή</h2>
<p>Υπάρχει δωρεάν δοκιμή 48 ωρών με την πλήρη έκταση της βαθμίδας PRO, χωρίς
κάρτα. Συνιστούμε να τη χρησιμοποιήσεις πριν πληρώσεις, ώστε να ξέρεις τι
αγοράζεις.</p>

<h2>2. Ακύρωση</h2>
<p>Μπορείς να ακυρώσεις τη συνδρομή σου οποιαδήποτε στιγμή, χωρίς αιτιολόγηση
και χωρίς δέσμευση. Η ακύρωση σταματά τις επόμενες ανανεώσεις· η πρόσβαση
συνεχίζεται μέχρι το τέλος της περιόδου που έχεις ήδη πληρώσει. Για ακύρωση:
<a href='mailto:{email}'>{email}</a>.</p>

<h2>3. Επιστροφή χρημάτων</h2>
<p>Ισχύουν τα εξής:</p>
<ul>
<li><b>Εντός 14 ημερών από την πρώτη πληρωμή:</b> επιστροφή του ποσού, χωρίς
αιτιολόγηση. Είναι το δικαίωμα υπαναχώρησης του καταναλωτή.</li>
<li><b>Διπλή χρέωση ή τεχνικό σφάλμα:</b> πλήρης επιστροφή, οποτεδήποτε.</li>
<li><b>Διακοπή της υπηρεσίας</b> που καθιστά τη συνδρομή άχρηστη: αναλογική
επιστροφή του υπολοίπου της περιόδου.</li>
<li><b>Μετά τις 14 ημέρες, σε ενεργή συνδρομή:</b> δεν γίνεται επιστροφή των
παρελθόντων περιόδων. Ακύρωσε για να σταματήσει η επόμενη χρέωση.</li>
</ul>

<h2>4. Πώς ζητάς επιστροφή</h2>
<p>Στείλε email στο <a href='mailto:{email}'>{email}</a> με το
email της αγοράς και, αν το έχεις, το αναγνωριστικό συναλλαγής της Stripe. Θα
απαντήσουμε εντός 5 εργάσιμων ημερών. Η επιστροφή εκτελείται μέσω Stripe στο
μέσο πληρωμής σου· ο χρόνος εμφάνισης εξαρτάται από την τράπεζά σου.</p>

<h2>5. Τι δεν καλύπτεται</h2>
<p>Δεν επιστρέφεται ποσό για δυσαρέσκεια με ακρίβεια πρόγνωσης. Ο καιρός
προβλέπεται με σφάλμα και αυτό είναι γνωστό εκ των προτέρων — δες τους
<a href='/terms'>Όρους Χρήσης</a>, ενότητα 2. Αν κάτι δεν λειτουργεί τεχνικά,
αυτό καλύπτεται από την ενότητα 3.</p>

<h2>6. Επικοινωνία</h2>
<p>Για κάθε θέμα χρέωσης: <a href='mailto:{email}'>{email}</a>.
Αν δεν λυθεί, μπορείς να απευθυνθείς στον συνήγορο του καταναλωτή.</p>
""", email)
