# Greece Sky and Weather

Σημειακή πρόγνωση για την Ελλάδα πάνω σε ανοιχτά μετεωρολογικά δεδομένα, με
εμπορικά ασφαλείς άδειες, σύγκριση μοντέλων, δείκτες αστάθειας και ραδιοβόλιση
Skew-T.

Δεν χρησιμοποιείται καμία πηγή με όρους που απαγορεύουν εμπορική χρήση: τα
δεδομένα είναι GFS (public domain), ICON-EU (DWD, CC BY 4.0), ECMWF open data
(CC BY 4.0) και Photon/OSM για geocoding. Ο κατάλογος αδειών και οι εκκρεμότητες
πριν από πραγματική διάθεση είναι στο `LICENSES.md`.

## Εκτέλεση

```bash
pip install -r requirements.txt
cp .env.example .env      # προαιρετικό: βάλε tile key και κάμερες
uvicorn app:app --host 0.0.0.0 --port 12000
```

Η ρύθμιση γίνεται με μεταβλητές περιβάλλοντος. Το `app.py` φορτώνει αυτόματα ένα
`.env` δίπλα του (βλ. `.env.example`), αλλά **οι πραγματικές μεταβλητές περιβάλλοντος
υπερισχύουν**: ένα systemd unit ή ένα `-e` flag δεν αντικαθίσταται σιωπηλά από
αρχείο. Το `.env` είναι στο `.gitignore` γιατί περιέχει κλειδιά. Ο πλήρης κατάλογος
μεταβλητών είναι στο `.env.example`. Οι σημαντικότερες:

| Μεταβλητή | Σκοπός |
|---|---|
| `WX_SECRET` | Κλειδί υπογραφής tokens. **Απαιτείται** στο production |
| `WX_ENV` | `production` ενεργοποιεί αυστηρούς ελέγχους εκκίνησης |
| `WX_ADMIN_TOKEN` | Κλειδί για τα admin endpoints (promo/analytics). Αν λείπει, κλειστά |
| `WX_ANALYTICS` | `0` απενεργοποιεί εντελώς την καταγραφή |
| `WX_ANALYTICS_RETENTION_DAYS` | Πόσες ημέρες κρατούνται τα events (προεπ. `365`, `0` = χωρίς όριο) |
| `WX_CACHE_DIR` / `WX_CACHE_MAX_MB` | Θέση και όριο του cache |
| `WX_DB` | SQLite: σταθμοί, promo codes, redemptions, analytics |
| `WX_TRUST_PROXY` | `1` όταν υπάρχει reverse proxy με `X-Forwarded-For` |
| `WX_RATE_LIMIT_DISABLED` | `1` απενεργοποιεί το rate limit (για tests/dev) |
| `WX_STRIPE_SECRET_KEY` / `WX_STRIPE_PRICE_*` / `WX_PUBLIC_BASE_URL` | Stripe checkout |
| `WX_STRIPE_WEBHOOK_SECRET` | Επαλήθευση webhook |
| `WX_MASTER_CODE` | Master passcode (άλλαξέ το από την προεπιλογή) |

Το `app.py` σερβίρει και το front-end. Χρειάζεται τα `static/leaflet.js`,
`static/leaflet.css` και `static/chart.umd.min.js` (vendored, σερβίρονται από
allow-list route).

## Δομή

| Αρχείο | Ρόλος |
|---|---|
| `app.py` | FastAPI app, HTML/CSS/JS του UI, routes πρόγνωσης, Skew-T, auth |
| `wx.py` | Λήψη και ανάλυση GRIB (GFS, ICON-EU, ECMWF), geocoding, DEM, cache |
| `entitlements.py` | Όρια βαθμίδων, υπογραφή/έλεγχος token, δεδομένα τιμολόγησης |
| `cachestore.py` | Cache αποτελεσμάτων/αρχείων: όρια μεγέθους, εκκαθάριση, αντοχή σε σκουπισμένο αρχείο |
| `billing.py` | Stripe: checkout, webhook lifecycle, κατάσταση συνδρομής, auto-renew |
| `promo.py` | Κωδικοί PRO (δώρου/promo): SQLite, εξαργυρώσεις, όρια, ανάκληση |
| `ratelimit.py` | Token-bucket όρια ανά endpoint (in-process) |
| `analytics.py` | Καταγραφή χρήσης πρώτου μέρους, χωρίς cookies/IP/ακριβείς συντεταγμένες |
| `logging_setup.py` | Δομημένο (JSON) logging |
| `config.py` | Μεταβλητές περιβάλλοντος, validation εκκίνησης, όρια συντεταγμένων |
| `verify.py` | Επαλήθευση αρχειοθετημένης πρόγνωσης έναντι ERA5 (βλ. παρακάτω) |
| `cameras.py` | Ρύθμιση ζωντανών καμερών ουρανού (`WX_CAMERAS`) |
| `envfile.py` | Φόρτωση `.env` (χωρίς εξάρτηση· δεν υπερισχύει του πραγματικού env) |
| `legal.py` | Σελίδες Όρων, Απορρήτου και Επιστροφών· στοιχεία επικοινωνίας |
| `bias.py` | Διόρθωση μεροληψίας με βάση τοπικό σταθμό· πίνακας `model_fcst` (ιστορικό runs) |

## Επαλήθευση έναντι ERA5

Το `/api/verify?lat=&lon=&days=` συγκρίνει **αρχειοθετημένη** πρόγνωση GFS με το
reanalysis ERA5 για την ίδια ώρα, και επιστρέφει bias, MAE και RMSE ανά
ορίζοντα (24/48/72/96/120 h).

Γιατί αρχειοθετημένη και όχι τρέχουσα: αν συγκρίναμε την τρέχουσα πρόγνωση με το
ανάλυμα του ίδιου μοντέλου, το σκορ θα ήταν τεχνητά καλό. Η πηγή είναι το AWS
Open Data mirror της NOAA (`noaa-gfs-bdp-pds`), που κρατά κάθε κύκλο, με το
`.idx` δίπλα σε κάθε GRIB2 ώστε μία μεταβλητή να είναι ένα range request.

Αληθές μέτρο είναι το ERA5 (CC BY 4.0) από το ARCO mirror της Google, που είναι
Zarr ανά ώρα. Το ERA5 έχει υστέρηση ~5 ημερών, γι' αυτό το παράθυρο σταματά μία
εβδομάδα πίσω.

Η σύγκριση γίνεται σε πλαίσιο ±0.75° γύρω από το σημείο, όχι σε μεμονωμένο
κελί: σε ανάλυση 0.25° το κελί της Αθήνας είναι ~25 km, οπότε μια σύγκριση
σημείο-προς-σημείο μετρά τη διαφορά των πλεγμάτων όσο και την πρόγνωση.

Ενδεικτικό αποτέλεσμα (Αθήνα, 20 ζεύγη): MAE θερμοκρασίας 0.77 °C, bias −0.41 °C.

## Ζωντανές κάμερες

Οι κάμερες ρυθμίζονται με μεταβλητή περιβάλλοντος και **δεν** έχουν
προεπιλεγμένα URLs. Η σύντομη μορφή αρκεί:

```bash
WX_CAMERAS='{"ilioupoli":"https://YOUR_SNAPSHOT_URL_1","glinado":"https://YOUR_SNAPSHOT_URL_2"}'
```

Το κλειδί επιλέγει ένα από τα δύο ενσωματωμένα σημεία, οπότε όνομα, περιοχή και
συντεταγμένες έρχονται από εκεί· η τιμή είναι το snapshot URL. Πλήρης μορφή, όταν
χρειάζεται timelapse ή δικό σου όνομα/θέση:

```bash
WX_CAMERAS='[{"id":"ilioupoli","name":"Ilioupoli Sky","lat":37.9333,"lon":23.75,
  "snapshot":"https://cam.example/latest.jpg","timelapse":"https://cam.example/today.mp4"}]'
```

Κάμερα χωρίς `snapshot` εμφανίζεται ως `not_configured` με ρητό μήνυμα, όχι ως
σπασμένη εικόνα. Ο browser φορτώνει την εικόνα απευθείας από το feed, οπότε δεν
γίνεται server-side fetch προς URL που προέρχεται από ρύθμιση.

## Βαθμίδες

Το κλείδωμα εφαρμόζεται **στον server**, όχι με CSS. Στο FREE το `/api/brief`
επιστρέφει 72 ώρες και καθόλου δεδομένα expert· το `/api/skewt` επιστρέφει 403
χωρίς έγκυρο PRO token. Οι κλειδωμένες ημέρες στο carousel είναι μόνο οπτική
ένδειξη: το blur δεν κρύβει αριθμούς, γιατί οι αριθμοί **δεν φτάνουν ποτέ στον
browser** — οι θέσεις τους είναι ουδέτερα placeholders.

- **FREE** — 72 ώρες (3 ημέρες): θερμοκρασία, αίσθηση, βροχή, άνεμος, ριπές,
  βάση νεφών, μετεόγραμμα, ημερήσια σύνοψη, διόρθωση υψομέτρου, χάρτης επιλογής
  σημείου.
- **PRO** — 240 ώρες (10 ημέρες) συν Skew-T, δείκτες αστάθειας (SBCAPE, shear,
  SRH, LCL), σύγκριση 3 μοντέλων με απόκλιση ως ένδειξη συμφωνίας και
  κατακόρυφη δομή.

Το Εξειδικευμένα tab έχει **επιλογέα χρόνου**: ημέρα και ώρα, με βήμα τις ώρες
που δημοσιεύει πραγματικά το GFS (ωριαίο έως +120 h, μετά ανά 3). Οι δείκτες, ο
πίνακας επιπέδων και το Skew-T κινούνται μαζί στο ίδιο βήμα, μέσω του
`/api/expert`. Το snapping γίνεται και στον server (`nearest_gfs_step`), ώστε να
μην αιτείται ποτέ βήμα που δεν υπάρχει — ένα f121 δεν δημοσιεύεται και θα ήταν
404 χωρίς εξήγηση.

## Εμφάνιση

Dark θέμα με iOS-style glassmorphism. Το `--card` είναι ημιδιαφανές
(`rgba(18,24,38,.70)`) και κάθε κάρτα/πίνακας/panel παίρνει
`backdrop-filter: blur(14px) saturate(160%)`, περίγραμμα
`1px solid rgba(255,255,255,.10)` και σκιά `0 8px 32px 0 rgba(0,0,0,.37)`.
Πίσω από όλα υπάρχει ένα gradient wash (`body::before`) — **χωρίς αυτό το blur
δεν έχει τίποτα να θολώσει** και οι κάρτες φαίνονται επίπεδες.

Δύο σημεία που δεν είναι προφανή από το CSS:

* Όπου ο browser δεν υποστηρίζει `backdrop-filter`, το `--card` γίνεται σχεδόν
  αδιαφανές (`@supports not`). Διαφανές φόντο χωρίς blur σημαίνει κείμενο πάνω σε
  φόντο που το element δεν δειγματοληπτεί — legibility bug, όχι degradation.
* Τα χρώματα των canvas (Chart.js gridlines/ticks, panel Skew-T) δεν διαβάζουν
  CSS variables και είναι γραμμένα στο χέρι για σκούρο φόντο.

Οι νομικές σελίδες (`legal.py`) και το `/licenses` έχουν δικό τους stylesheet
με το ίδιο palette, ώστε το κλικ σε νομικό link να μην αλλάζει εμφάνιση.

## Κωδικοί PRO (promo / δώρου)

Ένα σύστημα κωδικών που δίνουν **προσωρινό PRO χωρίς πληρωμή**. Το κρίσιμο
σημείο: ο κωδικός **δεν** ενεργοποιεί flag στο frontend. Η εξαργύρωση γίνεται
στον server (`POST /api/promo/redeem`) και κάθε επόμενο αίτημα PRO ελέγχεται
ξανά server-side, ακριβώς όπως μια πληρωμένη συνδρομή. Η βάση είναι SQLite, στο
ίδιο αρχείο με τους σταθμούς (`WX_DB`).

Κάθε κωδικός έχει: `code`, `duration_days`, `created_at`, `starts_at`,
`expires_at`, `max_redemptions`, `redemption_count`, `active`, προαιρετική λήξη,
προαιρετικό περιορισμό σε συγκεκριμένη συσκευή (`restricted_to`) και σημείωση
δημιουργού. Μετά το τελευταίο redemption ο κωδικός δεν γίνεται δεκτός.

**Το δικαίωμα είναι αθροιστικό, όχι καταστροφικό.** Ο server υπολογίζει το
`effective_pro_until` ως τη **μεταγενέστερη** λήξη από όλα τα ενεργά δικαιώματα
(πληρωμή και κωδικοί). Έτσι ένας συνδρομητής που εξαργυρώνει δώρο 5 ημερών δεν
χάνει τη συνδρομή του: το paid period παραμένει στη Stripe, ανέπαφο, και η
πρόσβαση διαρκεί έως ότου λήξει το τελευταίο από τα δύο. Ο κωδικός δεν
ακυρώνει, δεν τροποποιεί και δεν παρατείνει τη Stripe subscription.

Ο κωδικός «δένεται» με τη συσκευή μέσω τυχαίου αναγνωριστικού που εκδίδεται από
τον server και ταξιδεύει **μέσα στο υπογεγραμμένο token**. Το ίδιο αναγνωριστικό
στέλνεται και ως cookie `HttpOnly` για ευκολία του browser, αλλά η ταυτότητα που
εμπιστεύεται ο server είναι πάντα αυτή του υπογεγραμμένου token — ένα cookie ή
μια τιμή στο localStorage που πειράχτηκε δεν αλλάζει τίποτα, γιατί ο server
αποφασίζει κάθε φορά από την υπογραφή.

### Δημιουργία κωδικού (admin)

Τα admin endpoints απαιτούν το header `X-WX-Admin` με την τιμή του
`WX_ADMIN_TOKEN`. **Αν το `WX_ADMIN_TOKEN` δεν οριστεί, τα endpoints απαντούν
`503`** — μια αδιαμόρφωτη εγκατάσταση δεν εκθέτει διαχείριση κωδικών. Λάθος
κλειδί δίνει `403`.

Παράδειγμα: κωδικός 5 ημερών, μία χρήση.

```bash
curl -s -X POST http://127.0.0.1:12000/api/admin/promo \
  -H "Content-Type: application/json" \
  -H "X-WX-Admin: $WX_ADMIN_TOKEN" \
  -d '{"code":"FRIEND5","duration_days":5,"max_redemptions":1,"created_by":"admin","note":"για τον Γιάννη"}'
```

Απάντηση: `{"created": true, "code": {...}}`.

```bash
# Λίστα κωδικών με χρήσεις
curl -s http://127.0.0.1:12000/api/admin/promo -H "X-WX-Admin: $WX_ADMIN_TOKEN"

# Ανάκληση / επαναφορά
curl -s -X POST http://127.0.0.1:12000/api/admin/promo/FRIEND5/active \
  -H "Content-Type: application/json" -H "X-WX-Admin: $WX_ADMIN_TOKEN" \
  -d '{"active":false}'
```

### Προσωπικός κωδικός (μόνο για έναν χρήστη)

Αν θέλεις δωρεάν PRO για έναν φίλο/tester, όρισε `restricted_to` με το
αναγνωριστικό της συσκευής του. Ο ίδιος ο χρήστης βλέπει το δικό του
αναγνωριστικό στο `/api/promo/status` (`device`) αφού ανοίξει τη σελίδα· το
στέλνεις μετά στο admin. Κωδικός με `restricted_to` απορρίπτεται από κάθε άλλη
συσκευή.

```bash
curl -s -X POST http://127.0.0.1:12000/api/admin/promo \
  -H "Content-Type: application/json" -H "X-WX-Admin: $WX_ADMIN_TOKEN" \
  -d '{"code":"JACKFRIEND5","duration_days":5,"max_redemptions":1,"restricted_to":"<device-id>"}'
```

### Endpoints

| Μέθοδος | Διαδρομή | Auth | Σκοπός |
|---|---|---|---|
| POST | `/api/promo/redeem` | δεν απαιτείται token | Εξαργύρωση κωδικού· επιστρέφει νέο PRO token |
| GET | `/api/promo/status` | προαιρετικά | Ενεργό promo παράθυρο + το device id του καλούντος |
| POST | `/api/admin/promo` | `X-WX-Admin` | Δημιουργία/ενημέρωση κωδικού |
| GET | `/api/admin/promo` | `X-WX-Admin` | Λίστα κωδικών, χρήσεις, στατιστικά |
| POST | `/api/admin/promo/{code}/active` | `X-WX-Admin` | Ανάκληση / επαναφορά |

Λόγοι απόρριψης (HTTP): `404` μη έγκυρος/ληγμένος, `409` ήδη χρησιμοποιημένος
από την ίδια συσκευή, `410` εξαντλημένος ή ληγμένος. Το UI δείχνει ένα σύντομο
μήνυμα για κάθε περίπτωση.

## Analytics (self-hosted)

Καταγραφή χρήσης **πρώτου μέρους**, χωρίς τρίτους και χωρίς μηνιαίο κόστος. Το
endpoint είναι `POST /api/analytics`· τα γεγονότα αποθηκεύονται στην ίδια SQLite
(`WX_DB`) και συνοψίζονται στο `GET /api/admin/analytics` (admin auth).

Τι μετρά: επισκέπτες, συνεδρίες, προβολές σελίδας, referrer (μόνο domain),
συσκευή/browser (κατηγορία), χώρα/περιοχή από τη γλώσσα του browser, χρήση
πρόγνωσης και λειτουργιών. Ενδεικτικά events: `page_view`, `forecast_loaded`,
`location_searched`, `forecast_72h_viewed`, `forecast_240h_viewed`,
`model_comparison_opened`, `expert_opened`, `skewt_opened`, `pro_paywall_viewed`,
`checkout_started`, `subscription_created`, `promo_code_redeemed`.

Τι **δεν** αποθηκεύεται: IP, πλήρες User-Agent, ακριβείς συντεταγμένες, μη
κατακερματισμένο device id. Οι συντεταγμένες μειώνονται σε κύτταρο 0,5° (~50 km),
το device id κατακερματίζεται με salt της εγκατάστασης, ο referrer κρατά μόνο το
domain. Ο browser στέλνει τα events σε **μία batched** κλήση (`sendBeacon`), όχι
ένα request ανά κλικ. Ο client κάνει no-op όταν το `WX_ANALYTICS=0`.

Επειδή οι ακατέργαστες εγγραφές είναι ο μόνος πίνακας στον οποίο μπορεί να
προσθέσει δεδομένα ένας ανώνυμος caller, το ingest είναι **rate-limited** (όπως
κάθε άλλο endpoint — δεν είναι εξαιρεμένο), και οι παλιές εγγραφές διαγράφονται:
καθαρισμός μία φορά την ώρα, με ορίζοντα `WX_ANALYTICS_RETENTION_DAYS` (προεπιλογή
365, `0` τον απενεργοποιεί). Έτσι ο πίνακας μένει φραγμένος χωρίς εξωτερικό cron.
Το `/api/health` αναφέρει `analytics.enabled` και `analytics.retention_days`.

## Cache και απόδοση

Η ροή είναι: λήψη μοντέλου → επεξεργασία → cache → API → χρήστες. Τα
αποτελέσματα υπολογίζονται μία φορά ανά μοντέλο/κύκλο/σημείο και οι επόμενοι
χρήστες διαβάζουν cached. Το `cachestore.py` επιβάλλει όριο μεγέθους με
εκκαθάριση (eviction), και ένα κατεστραμμένο αρχείο cache αντιμετωπίζεται ως miss
και ξαναχτίζεται αντί να σπάσει το αίτημα. Ταυτόχρονα αιτήματα για το **ίδιο** νέο
forecast μοιράζονται έναν υπολογισμό (single-flight), ώστε N χρήστες να μην
προκαλούν N downloads.

## Έλεγχος υγείας και logging

Το `/api/health` ελέγχει τα βασικά dependencies: cache (προσβάσιμο;),
database/promo, Stripe configuration, auth/rate-limit κατάσταση και RAM grids.
Είναι εξαιρεμένο από το rate limit, ώστε ένας throttled έλεγχος να μη δείχνει
την υπηρεσία κάτω. Το logging είναι δομημένο JSON (`logging_setup.py`) και
καταγράφει αποτυχία μοντέλου/κύκλου, σφάλματα cache/API/Stripe, αποτυχίες
εξαργύρωσης και μη αναμενόμενες εξαιρέσεις — **ποτέ** κλειδιά, κάρτες,
κωδικούς πρόσβασης ή tokens.

## Πληρωμές (Stripe)

Οι πληρωμές είναι **προαιρετικές** και ζουν στο `billing.py`. Χωρίς
`WX_STRIPE_SECRET_KEY`, `WX_STRIPE_PRICE_MONTHLY`, `WX_STRIPE_PRICE_YEARLY` και
`WX_PUBLIC_BASE_URL` η εφαρμογή τρέχει κανονικά: passcode και trial λειτουργούν,
το `/api/plans` γυρίζει `checkout_available:false` και το κουμπί πληρωμής είναι
disabled. Δεν υπάρχει κατάσταση όπου το κουμπί δείχνει ενεργό και δεν χρεώνει.

Η **αυτόματη ανανέωση είναι ενεργή εξ αρχής**, γιατί αυτό είναι η προεπιλογή της
Stripe για κάθε συνδρομή: δεν υπάρχει flag που να την ενεργοποιεί, και το
`cancel_at_period_end` είναι deprecated και εκτός των παραμέτρων του Checkout.
Το opt-out γίνεται στη Subscription με `cancel_at` (ίσο με το τέλος της περιόδου,
ώστε να μη χαθεί χρόνος που έχει πληρωθεί) από το `/api/subscription/auto-renew`.
Το κουμπί διαχείρισης είναι διακριτικό — μία γραμμή μέσα στο modal — αλλά πάντα
παρόν για όποιον έχει συνδρομή.

## Νομικές σελίδες και επικοινωνία

Οι σελίδες `/terms`, `/privacy` και `/refunds` είναι δημόσιες (χωρίς auth) και
σερβίρονται από το `legal.py`. Είναι γραμμένες πάνω σε **ό,τι κάνει πραγματικά ο
κώδικας** — ότι το token μένει στο localStorage και δεν υπάρχουν cookies, ότι οι
συντεταγμένες της αναζήτησης δεν γράφονται σε βάση, ότι η Stripe κρατά τα
στοιχεία κάρτας. Οι σελίδες δηλώνουν ρητά ότι **δεν έχουν ελεγχθεί από νομικό**.

Ο footer με τα στοιχεία επικοινωνίας είναι **server-rendered**, όχι JS: ο
crawler του Stripe και κάθε link checker δεν τρέχουν JS, και τα νομικά links δεν
πρέπει να εξαρτώνται από επιτυχία του forecast API.

Τα στοιχεία επικοινωνίας είναι ρύθμιση, ώστε μια διόρθωση handle να μη θέλει
deploy. **Οι προεπιλογές είναι τα δημόσια accounts — επιβεβαίωσε τα handles**,
γιατί λάθος handle σε footer δεν φαίνεται μέχρι να γράψει πελάτης:

```bash
WX_YOUTUBE_URL=https://www.youtube.com/@YOUR_HANDLE
WX_FACEBOOK_URL=https://www.facebook.com/YOUR_PAGE
WX_EMAIL=support@yourdomain.gr
```

## Σύγκριση μοντέλων και συμφωνία

Το PRO δείχνει GFS, ICON-EU και ECMWF δίπλα-δίπλα και υπολογίζει την **απόκλισή
τους** ανά ώρα (μέγιστη μείον ελάχιστη). Το αποτέλεσμα χαρακτηρίζεται «υψηλή»,
«μέτρια» ή «χαμηλή» συμφωνία, με τους αριθμούς (μέση/μέγιστη απόκλιση) ορατούς.

**Δεν είναι βεβαιότητα πρόγνωσης.** Είναι ένδειξη του πόσο συμφωνούν τα μοντέλα
μεταξύ τους, όχι εγγύηση ότι η πρόγνωση θα επαληθευτεί: αν όλα τα μοντέλα κάνουν
το ίδιο λάθος, η συμφωνία θα φαίνεται υψηλή. Το μήνυμα αυτό εμφανίζεται στο UI
δίπλα στην ένδειξη, ώστε να μη διαβαστεί ως «confidence».

## Ιστορικό προγνώσεων

Ο πίνακας `model_fcst` (στο `bias.py`, μέσα στο `WX_DB`) κρατά, για κάθε κύκλο
(`run_utc`) και κάθε σημείο, την προβλεπόμενη τιμή ανά ώρα-στόχο. Το πρωτεύον
κλειδί είναι `(run_utc, valid_utc, lat, lon, source)`, οπότε ο ίδιος κύκλος
εγγράφεται μία φορά και οι επόμενοι κύκλοι προστίθενται δίπλα.

Αυτό επιτρέπει, χωρίς νέο UI, να διαβαστεί πώς άλλαξε η πρόγνωση μεταξύ runs για
ένα σημείο: για σταθερό `valid_utc`, ταξινομώντας κατά `run_utc`, προκύπτει η
σειρά `06:00 → 12:00 → 18:00 → 00:00` για θερμοκρασία, βροχή και άνεμο. Ο ίδιος
υπολογισμός τροφοδοτεί και τη διόρθωση μεροληψίας: το `compute_bias` συγκρίνει
την παλαιότερη διαθέσιμη πρόγνωση για μια ώρα με την πραγματική παρατήρηση του
σταθμού. Δεν υπάρχει αυθαίρετη «AI διόρθωση» — μόνο μετρήσιμο σφάλμα έναντι
παρατηρήσεων.

## Production deployment (VPS)

Σύντομος οδηγός· δεν προστέθηκε καμία νέα υποδομή (ούτε Kubernetes ούτε
microservices ούτε εξωτερική υπηρεσία).

1. **Python & deps**: `pip install -r requirements.txt` στον interpreter που θα
   τρέξει το `uvicorn` (δες «Έλεγχος εγκατάστασης» παραπάνω).
2. **Μυστικά**: όρισε `WX_ENV=production`, ένα τυχαίο `WX_SECRET`, και
   `WX_ADMIN_TOKEN` αν θέλεις admin. Με `WX_ENV=production` και χωρίς
   `WX_SECRET` η εκκίνηση αποτυγχάνει ρητά αντί να τρέξει με αδύναμο κλειδί.
3. **Reverse proxy**: `nginx`/`caddy` με TLS μπροστά από το `uvicorn`. Αν ο proxy
   στέλνει `X-Forwarded-For`, όρισε `WX_TRUST_PROXY=1` ώστε το rate limit να
   χρησιμοποιεί τη σωστή IP (αλλιώς όλοι μοιράζονται το bucket του proxy).
4. **systemd**: μία unit που τρέχει `uvicorn app:app --host 127.0.0.1 --port 12000`
   με `EnvironmentFile=` για το `.env` (ή πραγματικές μεταβλητές).
5. **Δίσκος**: το `WX_CACHE_DIR` θέλει χώρο· όρισε `WX_CACHE_MAX_MB` ώστε η
   εκκαθάριση να κρατά το cache φραγμένο.
6. **Έλεγχος**: `curl -s https://.../api/health`. **Backups**: δες παρακάτω.

### Backups

Backup του μικρού, πολύτιμου αρχείου SQLite (`WX_DB`) — περιέχει σταθμούς,
κωδικούς PRO, εξαργυρώσεις και (αν είναι ενεργό) τα analytics. Χρησιμοποίησε
`.backup` ή `sqlite3 ... "VACUUM INTO"` για συνεπές αντίγραφο ενώ τρέχει, και
κράτα το αντίγραφο εκτός της ίδιας μηχανής. Το `.env` δεν είναι στο backup ως
μέρος του repo (είναι gitignored). **Τα GRIB/NetCDF cache δεν χρειάζονται
backup**: κατεβαίνουν ξανά από τις δημόσιες πηγές και είναι ογκώδη.

## Δοκιμή

```bash
python -m pytest tests/ -q
```

## Έλεγχος εγκατάστασης

Μερικά πακέτα είναι προαιρετικά: χωρίς αυτά η πρόγνωση δουλεύει και μόνο μια
κάρτα υποβαθμίζεται. Αυτό σημαίνει ότι μια μισοτελειωμένη εγκατάσταση **δεν
πετάει σφάλμα**, οπότε δεν φαίνεται. Έλεγξέ την με ένα request:

```bash
curl -s http://127.0.0.1:12000/api/health | python3 -m json.tool
```

Το `optional.astro_ephem` πρέπει να είναι `true`. Αν είναι `false`, λείπει το
`ephem` από τον interpreter που τρέχει το `uvicorn` (συνηθισμένη αιτία: το
`pip install` έγινε σε άλλο virtualenv). Διόρθωση:

```bash
pip install -r requirements.txt   # ή: pip install "ephem>=4.1"
```

## Σημείωση για τα πλακίδια χάρτη

Ο προεπιλεγμένος tile server είναι του OpenStreetMap, που **δεν** επιτρέπεται για
εμπορική χρήση. Οι διαθέσιμες εμπορικά συμβατές επιλογές δεν είναι ισοδύναμες:

| Πάροχος | Εμπορική στο free | Key | Σημείωση |
|---|---|---|---|
| **CARTO** | ✅ | ✅ απαιτείται | 5M πλακίδια/μήνα· raster, δουλεύει με Leaflet as-is |
| **Stadia Maps** | ❌ | — | free tier ρητά non-commercial· από $20/μήνα |
| **Protomaps hosted** | ⚠️ | ✅ | δωρεάν μόνο non-commercial (GitHub Sponsors για εμπορική) |
| **OpenFreeMap** | ✅ | ❌ | vector-only, χρειάζεται MapLibre· χωρίς SLA |
| **Self-hosted** | ✅ | ❌ | κανένας τρίτος όρος |

Προτεινόμενη ρύθμιση (CARTO, δωρεάν key από https://carto.com/basemaps/apikey):

```bash
WX_TILE_URL=https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?api_key=YOUR_KEY
WX_TILE_ATTRIB=&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors
WX_TILE_SUBDOMAINS=abcd
WX_TILE_COMMERCIAL_OK=1
```

Το `WX_TILE_SUBDOMAINS` είναι απαραίτητο για URLs με `{s}`: χωρίς αυτό το Leaflet
ζητά literal host `{s}` και κάθε πλακίδιο επιστρέφει 404. Το `WX_TILE_COMMERCIAL_OK=1`
δηλώνει ότι ο πάροχος επιτρέπει εμπορική χρήση· όσο δεν ορίζεται, το UI εμφανίζει
ορατή προειδοποίηση μέσα στον χάρτη. Λεπτομέρειες στο `LICENSES.md`, αποκλεισμός #2.
