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
| `WX_MAX_BODY_MB` | Όριο μεγέθους σώματος αιτήματος σε MB (προεπ. `1`, `0` = χωρίς όριο) |
| `WX_RUN_LOOKUP_TTL_S` | Δευτερόλεπτα που θυμάται ο εντοπισμός του τρέχοντος κύκλου GFS (προεπ. `300`) |
| `WX_USE_RAM_GRIDS` | `1` ενεργοποιεί το shared model-run grid (προεπ. **off**) |
| `WX_GRID_KEEP_RUNS` | Πόσα runs ανά μοντέλο κρατά στο δίσκο (προεπ. `2`) |
| `WX_RAM_ICON_SCOPE` | `greek` (προεπ.) ή `europe` για το ICON grid |
| `WX_RAM_REFRESH_S` / `WX_RAM_RETRY_S` | Cadence refresh (προεπ. `21600` / `1200`) |
| `WX_DB` | SQLite: σταθμοί, promo codes, redemptions, analytics |
| `WX_TRUST_PROXY` | `1` όταν υπάρχει reverse proxy με `X-Forwarded-For` |
| `WX_RATE_LIMIT_DISABLED` | `1` απενεργοποιεί το rate limit (για tests/dev) |
| `WX_STRIPE_SECRET_KEY` / `WX_STRIPE_PRICE_*` / `WX_PUBLIC_BASE_URL` | Stripe checkout |
| `WX_STRIPE_WEBHOOK_SECRET` | Επαλήθευση webhook |
| `WX_MASTER_CODE` | Master passcode για comps/tests. **Απαιτείται** στο production: αν λείπει ή είναι κενό, η εκκίνηση αποτυγχάνει (δεν υπάρχει usable default) |
| `WX_VAPID_PUBLIC_KEY` / `WX_VAPID_PRIVATE_KEY` | Κλειδιά Web Push (ειδοποιήσεις). Και τα δύο base64url. Χωρίς αυτά τα `/api/push/*` απαντούν `503` |
| `WX_VAPID_SUBJECT` | `mailto:` που στέλνεται στον πάροχο push (προεπ. `WX_CONTACT_EMAIL`) |
| `WX_NOTIFY_INTERVAL_S` | Κάθε πόσο σαρώνει ο scheduler τα forecasts για ειδοποιήσεις (προεπ. `900`) |
| `WX_NOTIFY_PRO_REFRESH_S` | Κάθε πόσο ξαναρωτά τη Stripe για συνδρομητές (προεπ. `21600`) |
| `WX_NOTIFY_RETENTION_DAYS` | Ημέρες διατήρησης του ιστορικού ειδοποιήσεων (προεπ. `30`) |

Το `app.py` σερβίρει και το front-end. Χρειάζεται μόνο το `static/chart.umd.min.js`
(vendored, σερβίρεται από allow-list route). Δεν φορτώνεται βιβλιοθήκη χάρτη: η
θέση επιλέγεται με αναζήτηση κειμένου, geolocation ή χειροκίνητες συντεταγμένες,
οπότε δεν υπάρχει tile provider να αδειοδοτηθεί.

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
| `notify.py` | Ειδοποιήσεις Web Push (PRO): συνδρομές συσκευής, κανόνες, dedupe, scheduler, VAPID |
| `static/sw.js` | Service worker: λήψη push, εμφάνιση notification, άνοιγμα εφαρμογής στο κλικ |
| `static/manifest.webmanifest` | PWA manifest (installable app, iOS web push) |

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

Οι κάμερες έχουν **δύο ξεχωριστά επίπεδα**: τα δημόσια στοιχεία που βλέπει ο
browser, και την ιδιωτική πηγή (RTSP URL/credentials) που μένει server-side.

### Δημόσια στοιχεία (`WX_CAMERAS`)

Δεν υπάρχουν προεπιλεγμένα URLs. Η σύντομη μορφή αρκεί:

```bash
WX_CAMERAS='{"ilioupoli":"https://YOUR_SNAPSHOT_URL_1","glinado":"https://YOUR_SNAPSHOT_URL_2"}'
```

Το κλειδί επιλέγει ένα από τα δύο ενσωματωμένα σημεία, οπότε όνομα, περιοχή και
συντεταγμένες έρχονται από εκεί· η τιμή είναι το snapshot URL. Πλήρης μορφή:

```bash
WX_CAMERAS='[{"id":"ilioupoli","name":"Ilioupoli Sky","lat":37.9333,"lon":23.75,
  "snapshot":"https://cam.example/latest.jpg","timelapse":"https://cam.example/today.mp4",
  "snapshot_interval_min":5,
  "live_enabled":true,"live_provider":"youtube","youtube_live_id":"PUBLIC_ID"}]'
```

Κάμερα χωρίς `snapshot` (ή με snapshot που δεν είναι έγκυρο public http/https URL)
εμφανίζεται ως `not_configured` με ρητό μήνυμα, όχι ως σπασμένη εικόνα. `enabled:
false` κρύβει την κάμερα εντελώς — ούτε στη λίστα ούτε στο detail.

### Snapshot mode (default)

Η default λειτουργία· χαμηλή κατανάλωση. Ο browser δείχνει την τελευταία εικόνα
του feed και την ξαναφορτώνει ανά `snapshot_interval_min` (1/2/5/10/15/30/60
λεπτά· default 5). Δεν υπάρχει video/stream προς τον server.

### Live mode (provider abstraction)

Το live **δεν** είναι WebRTC: είναι *provider*. Σήμερα `live_provider="youtube"`
με ένα **δημόσιο** `youtube_live_id`, που ενσωματώνεται με τον επίσημο YouTube
player σε `youtube-nocookie.com`. Το `live_provider` είναι whitelisted — άγνωστη
τιμή → κανένα live block. Το LIVE ανοίγει μόνο μετά από κλικ (`🔴 LIVE`), δεν
γίνεται autoplay στο page load, και καμία διεύθυνση κάμερας δεν φτάνει στον
browser. Άλλοι providers (`webrtc`, `hls`) μπαίνουν αργότερα ως νέα εγγραφή στο
`LIVE_PROVIDERS`, χωρίς redesign.

### Ιδιωτική πηγή (`WX_CAMERA_SOURCES`) — server-side μόνο

```bash
WX_CAMERA_SOURCES='[{"id":"ilioupoli","url":"rtsp://CAMERA_LAN_HOST:554/Streaming/Channels/101",
  "username":"viewer","secret_ref":"WX_CAMERA_ILIOUPOLI_PASS"}]'
WX_CAMERA_ALLOWED_HOSTS=CAMERA_LAN_HOST
```

* Μόνο το μέλλον streaming pipeline τη διαβάζει (`cameras.source_for`), **καμία**
  HTTP διαδρομή δεν την επιστρέφει.
* Τα credentials **δεν** μπαίνουν στο URL — `username` + `secret_ref` (όνομα
  μεταβλητής που κρατά το password).
* `WX_CAMERA_ALLOWED_HOSTS` (προαιρετικό) περιορίζει hosts.
* `audio:true` απορρίπτεται: **video-only**.
* Για να προσθέσεις κάμερα αργότερα: μια εγγραφή στο `WX_CAMERAS` (public) και,
  όταν συνδεθεί το pipeline, μια στο `WX_CAMERA_SOURCES` (private). Καμία αλλαγή
  κώδικα σε πολλά σημεία.

Endpoints: `GET /api/cameras` (λίστα) και `GET /api/cameras/{id}` (detail) —
δημόσια, ίδια βάση με πριν, χωρίς URL parameter. Άγνωστο ή disabled id → 404.

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

### Το per-point cache

Το προεπιλεγμένο μονοπάτι κατεβάζει ένα μικρό GRIB κουτί **ανά forecast step ανά
σημείο** και το κρατά στο δίσκο, με κλειδί `μοντέλο|κύκλος|step|lat,lon`
(`wx.py`). Είναι σωστό και δοκιμασμένο, αλλά τα downloads κλιμακώνονται με τις
**τοποθεσίες**: δύο χρήστες σε διαφορετική γειτονιά δεν μοιράζονται τίποτα.
(Το ICON-EU και το ECMWF δεν έχουν αυτό το θέμα: κατεβάζουν ολόκληρο το πεδίο και
επιλέγουν το σημείο μετά το decode, οπότε το κλειδί τους δεν περιέχει συντεταγμένες.)

### Το shared model-run grid (προαιρετικό)

Για να μην κλιμακώνονται τα downloads με τους χρήστες, υπάρχει το `grids.py` +
`scheduler.py`: κατεβάζει **ολόκληρο το Greece box μία φορά ανά κύκλο**, το
αποκωδικοποιεί σε numpy arrays και απαντά σε κάθε αίτημα με παρεμβολή από τη RAM
(χωρίς δίκτυο, χωρίς GRIB decode). Forecast και ειδοποιήσεις διαβάζουν το **ίδιο**
grid, ώστε μια ειδοποίηση να μην μπορεί να διαφωνεί με την πρόγνωση που εμφανίζει.

Είναι **opt-in** (`WX_USE_RAM_GRIDS=1`) και παραμένει off-by-default. Όταν είναι
off, τίποτα από αυτό δεν ενεργοποιείται: κανένα disk read, κανένα archive, και το
per-point μονοπάτι λειτουργεί ακριβώς όπως πριν.

Τρία πράγματα το κάνουν φθηνό και ασφαλές:

* **Run-identity short-circuit.** Πριν από κάθε λήψη, ο scheduler ρωτά ποιος
  κύκλος είναι *πραγματικά δημοσιευμένος*. Αν είναι ήδη φορτωμένος, το πέρασμα
  δεν κατεβάζει τίποτα. Άρα νέο fetch γίνεται **μόνο όταν υπάρχει νέος κύκλος**,
  όχι κάθε φορά που ξυπνάει ο loop.
* **Disk persistence.** Κάθε επιτυχής κύκλος γράφεται σε `.npz` κάτω από
  `WX_CACHE_DIR/grids/`. Μετά από restart ή deploy, το τελευταίο έγκυρο run
  φορτώνεται **lazy** με το πρώτο αίτημα — όχι re-download. Αν ο νέος κύκλος
  αποτύχει, το προηγούμενο persisted run παραμένει διαθέσιμο.
* **Retention.** Κρατούνται τα `WX_GRID_KEEP_RUNS` (προεπ. **2**) νεότερα runs
  ανά μοντέλο+scope, τα υπόλοιπα διαγράφονται. Ο χώρος είναι φραγμένος εξ
  αρχής, όχι από πίεση eviction.

Αποτύπωμα (Greece scope, μετρημένο): GFS ≈ 14 MB RAM / ≈ 14 MB δίσκος, ICON-EU
≈ 1.8 MB. Σύνολο ≈ 16 MB. Το `WX_RAM_ICON_SCOPE=europe` είναι ξεχωριστή,
ακριβότερη επιλογή (~65 MB για το ICON) και δεν ενεργοποιείται από μόνο του.

Το `/api/health` αναφέρει `ram_grids.models` (κύκλος, ηλικία, stale κατάσταση) και
`ram_grids.persist` (φάκελος, πόσα runs στο δίσκο, bytes).

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

## Ειδοποιήσεις Web Push (PRO)

Οι ειδοποιήσεις είναι λειτουργία **PRO** και ελέγχονται στον server, όπως κάθε
άλλη PRO λειτουργία: τα `/api/push/*` και `/api/notify/*` περνούν από
`require_pro()` και επιστρέφουν `403` σε FREE χρήστες. Δεν υπάρχει flag στο
frontend που να αποφασίζει ποιος είναι PRO.

Το UI είναι **PWA**. Ο service worker (`static/sw.js`) σερβίρεται από τη ρίζα
(`/sw.js`, με `Service-Worker-Allowed: /`), ώστε το scope του να καλύπτει όλη την
εφαρμογή. Το `static/manifest.webmanifest` δηλώνει `display: standalone`, που
είναι η προϋπόθεση για web push στο iOS — εκεί οι ειδοποιήσεις λειτουργούν
**μόνο** αφού ο χρήστης προσθέσει την εφαρμογή στην οθόνη αφετηρίας, και η σελίδα
εμφανίζει σχετική οδηγία.

### Ρύθμιση

```bash
pip install "pywebpush>=2.0" "cryptography>=42"
python -c "import notify; notify.print_vapid_keys()"
# Αντίγραψε τις δύο γραμμές στο .env:
#   WX_VAPID_PUBLIC_KEY=...
#   WX_VAPID_PRIVATE_KEY=...
```

Χωρίς τα κλειδιά, τα `/api/push/*` απαντούν `503` και «Οι ειδοποιήσεις δεν είναι
διαθέσιμες» — η υπόλοιπη εφαρμογή λειτουργεί κανονικά. Το ιδιωτικό κλειδί δεν
γράφεται ποτέ σε log ούτε επιστρέφεται σε client· μόνο το δημόσιο στέλνεται στον
browser μέσω `/api/push/config`.

### Πώς συμπεριφέρεται

- **Κανόνες.** Βροχή, άνεμος, καύσωνας/κρύο, καταιγίδα. Κάθε κανόνας έχει κατώφλι
  `warn` και `severe` με χρονικό παράθυρο (προεπ. 24 h) και ελάχιστο χρόνο
  προειδοποίησης (προεπ. 1 h).
- **Ώρες ησυχίας.** Τα `warn` σιωπούν τη νύχτα· τα `severe` περνούν πάντα. Ο
  έλεγχος γίνεται **πριν** την καταγραφή του event, ώστε μια ειδοποίηση που
  σιώπησε να μπορεί αργότερα να σταλεί αν κλιμακωθεί.
- **Dedupe (at-least-once).** Κάθε περιστατικό έχει κλειδί
  `rule|ημερομηνία|παράθυρο|κύτταρο`. Μια εγγραφή `pending` ξαναδοκιμάζεται με
  backoff μέχρι `retry_max`, μετά μένει `failed`. Το OS `tag` είναι το ίδιο
  κλειδί, οπότε δεν στοιβάζονται δύο όψεις της ίδιας ειδοποίησης.
- **Cooldown / όριο ημέρας.** Ένας κανόνας δεν επαναλαμβάνεται μέσα στο cooldown
  του και υπάρχει ανώτατο όριο ειδοποιήσεων την ημέρα.
- **Τοποθεσία ειδοποιήσεων.** Χωριστή από την τοποθεσία περιήγησης: το να δεις
  πρόγνωση αλλού **δεν** αλλάζει πού στέλνονται οι ειδοποιήσεις. Αποθηκεύεται
  χονδρικοποιημένη σε πλέγμα 0,1°.
- **Απασχόληση Stripe.** Ο scheduler δεν ρωτά τη Stripe ανά σάρωση· ξαναελέγχει
  ανά `WX_NOTIFY_PRO_REFRESH_S` (προεπ. 6 h) και, σε αποτυχία, **διατηρεί** την
  προηγούμενη κατάσταση αντί να κόψει την πρόσβαση.

### Endpoints

| Endpoint | Ρόλος |
|---|---|
| `GET /api/push/config` | Δημόσιο VAPID key + αν το push είναι διαθέσιμο |
| `POST /api/push/subscribe` | Αποθήκευση συνδρομής (PRO). Αν το token δεν έχει device id, ο server δίνει ένα και επιστρέφει νέο token |
| `POST /api/push/unsubscribe` | Απενεργοποίηση (προαιρετικά διαγραφή δεδομένων) |
| `GET /api/notify/state` | Τρέχουσα κατάσταση για το UI |
| `POST /api/notify/location` | Αλλαγή τοποθεσίας ειδοποιήσεων |
| `POST /api/notify/prefs` | Ενεργό/ανενεργό, κανόνες, ώρες ησυχίας |
| `POST /api/notify/test` | Δοκιμαστική ειδοποίηση (για έλεγχο από τον χρήστη) |

Όταν ένας χρήστης PRO έρχεται από passcode ή Stripe (token χωρίς device id), το
πρώτο notify write του δίνει ένα device id και επιστρέφει νέο υπογεγραμμένο
token· ο browser το αποθηκεύει. Έτσι η συνδρομή push αποκτά σταθερή ταυτότητα
χωρίς να αλλάξει η ροή πληρωμής ή passcode.

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

### Ενεργοποίηση του shared model-run grid

Προαιρετικό βήμα, όταν αποφασιστεί ότι τα downloads δεν πρέπει να κλιμακώνονται
με τους χρήστες. Δεν αλλάζει αριθμούς πρόγνωσης — μόνο από πού διαβάζονται.

Στο `.env` του VPS:

```
WX_USE_RAM_GRIDS=1
WX_GRID_KEEP_RUNS=2
# WX_RAM_ICON_SCOPE=europe   # μόνο αν θέλεις 7 km εκτός Ελλάδας (~65 MB)
```

Μετά:

```bash
systemctl restart <η-unit>          # ή όποιο restart χρησιμοποιείς
curl -s https://.../api/health | python3 -m json.tool | grep -A8 ram_grids
```

Τι να δεις στο `ram_grids`:

* `models.gfs.run` / `models.icon.run` — ο κύκλος που σερβίρεται τώρα. Αν λείπει
  ένα μοντέλο, το πρώτο refresh pass δεν έχει τελειώσει ακόμη (το GFS θέλει
  καμιά δεκαριά λεπτά) — το per-point path καλύπτει στο μεταξύ, δεν υπάρχει κενό.
* `models.*.stale` — `true` σημαίνει ότι ο νέος κύκλος απέτυχε και σερβίρεται ο
  προηγούμενος. Αυτό είναι το σωστό degrade, όχι σφάλμα.
* `models.*.age_s` — ηλικία σε δευτερόλεπτα. Πάνω από `WX_RAM_REFRESH_S` +
  περιθώριο σημαίνει ότι ο loop δεν έχει τρέξει.
* `persist.disk_runs` — πόσα runs υπάρχουν στο δίσκο ανά μοντέλο. Με
  `WX_GRID_KEEP_RUNS=2` περιμένεις το πολύ `2`.
* `persist.disk_bytes` — συνολικό μέγεθος. Για Greece scope, ≈ 16 MB.

**Επαλήθευση ότι δουλεύει**: ζήτα δύο διαφορετικές τοποθεσίες και δες ότι ο
`gfs_run` είναι ίδιος και ότι το `age_s` δεν μηδενίζεται (δεν ξαναχτίστηκε).
Μετά κάνε restart και ξαναζήτα: το `run` πρέπει να είναι το ίδιο, **χωρίς** νέο
download, και στα logs να εμφανίζεται `grid restored from disk`.

**Επαναφορά**: βάλε `WX_USE_RAM_GRIDS=0` (ή σβήσε τη γραμμή) και κάνε restart.
Το per-point cache αναλαμβάνει αμέσως· τα αρχεία κάτω από `WX_CACHE_DIR/grids/`
μπορούν να διαγραφούν χειροκίνητα.

### Μελλοντικό live streaming (RTSP → YouTube)

**Δεν έχει εγκατασταθεί τίποτα από αυτά.** Είναι το σχέδιο για όταν έρθει η ώρα
να ενεργοποιηθεί πραγματική κάμερα· ο σημερινός κώδικας είναι η ασφαλής βάση.

Η ροή που θέλουμε:

```
Hikvision RTSP ──► server-side stream layer ──► YouTube Live ──► YouTube embed
   (private LAN)      (video-only, no audio)      (public)         (στο app)
```

Τι θα χρειαστεί στον VPS τότε (ξεχωριστή απόφαση, όχι τώρα):

* ένα service που διαβάζει RTSP από το ιδιωτικό δίκτυο κάμερας και το στέλνει
  στο YouTube Live (π.χ. `ffmpeg`/`MediaMTX` σε **βίντεο μόνο**: `-an`, καμία
  διαδρομή audio),
* κρεντενσιαλς κάμερας σε `.env`/secret store του VPS — ποτέ στο repo, ποτέ στον
  browser,
* το ιδιωτικό δίκτυο να μην εκτίθεται στο Internet· μόνο ο stream layer βγαίνει
  προς τα έξω,
* το δημόσιο `youtube_live_id` στο `WX_CAMERAS`.

Ο browser δεν μαθαίνει ποτέ RTSP URL, IP, port ή credential — μόνο το δημόσιο
YouTube id μέσω `cameras.public_camera`.

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

**Δεν χρησιμοποιούνται πλακίδια χάρτη.** Το UI δεν φορτώνει βιβλιοθήκη χάρτη και
δεν ζητά raster tiles, οπότε δεν υπάρχει πάροχος πλακιδίων προς αδειοδότηση. Η
θέση επιλέγεται με αναζήτηση κειμένου (Photon), geolocation, αποθηκευμένα
αγαπημένα ή χειροκίνητες συντεταγμένες.

Οι μεταβλητές `WX_TILE_*` έχουν **αφαιρεθεί**: δεν διαβάζονταν από κανένα σημείο
του κώδικα και η τεκμηρίωσή τους δημιουργούσε την εντύπωση λειτουργίας που δεν
υπάρχει. Το ιστορικό της απόφασης και οι εναλλακτικοί πάροχοι (αν ποτέ
προστεθεί χάρτης) παραμένουν στο `LICENSES.md`.
