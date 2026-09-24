# Άδειες χρήσης / Licence audit

Τελευταία ενημέρωση: 2026-09-23

Καταγραφή κάθε εξωτερικής πηγής δεδομένων, βιβλιοθήκης και asset, με την άδειά
του και τις υποχρεώσεις που συνεπάγεται για **εμπορική** διάθεση.

**Κατάσταση: το project δεν είναι ακόμη έτοιμο για εμπορική διάθεση.**
Υπάρχουν τρεις αποκλεισμοί — βλ. [Αποκλεισμοί](#αποκλεισμοί-εμπορικής-διάθεσης).

---

## 1. Πηγές δεδομένων

| Πηγή | Τι παρέχει | Άδεια | Εμπορική | Αναδιανομή | Υποχρεώσεις |
|---|---|---|---|---|---|
| **NOAA/NWS GFS** (NOMADS, AWS) | Παγκόσμιο 0.25°, GRIB2 | US Public Domain (17 USC §105) | ✅ | ✅ | Καμία νομική· ζητείται αναφορά. Απαγορεύεται να δηλώσεις σχέση/έγκριση NOAA |
| **ECMWF Open Data** (IFS) | 0.25°, GRIB2 | **CC BY 4.0** | ✅ ρητά | ✅ | Αναφορά + σύνδεσμος + ένδειξη αλλαγών + disclaimer |
| **DWD ICON / ICON-EU** | 7 km Ευρώπη | **CC BY 4.0** (GeoNutzV) | ✅ ρητά | ✅ | «Source: Deutscher Wetterdienst» δίπλα στα δεδομένα |
| **Copernicus ERA5 / EU-DEM** | Reanalysis· DEM 25 m | CC BY 4.0 / Copernicus | ✅ | ✅ | «Generated using Copernicus … information [έτος]» |
| **MET Norway** Locationforecast | Τοπικό μοντέλο | CC BY 4.0 / NLOD | ✅ | ✅ | «Data from MET Norway» |
| **Photon geocoding** (Komoot) | Αναζήτηση/αντίστροφη | Λογισμικό Apache-2.0· δεδομένα ODbL | ✅ | ✅ | «© OpenStreetMap contributors» — υποχρεωτικό |
| **OpenTopoData** (public API) | Υψόμετρο DEM | Λογισμικό **MIT** | ⚠️ τεχνικά ναι, πρακτικά όχι | ✅ | Δες αποκλεισμό #3 |
| **EU-DEM dataset** | DEM 25 m Ευρώπη | Copernicus, CC BY 4.0 | ✅ | ✅ | Αναφορά Copernicus |
| **Open-Meteo — free tier** | Συγκέντρωση μοντέλων | Ιδιωτικοί όροι | ❌ **ΟΧΙ** | ⚠️ | Δες αποκλεισμό #1 |
| **tile.openstreetmap.org** | Πλακίδια χάρτη | Ιδιωτική πολιτική OSMF | ❌ **ΟΧΙ** | ❌ | Δες αποκλεισμό #2 |
| **CARTO basemaps** | Πλακίδια χάρτη | Basemaps T&C | ✅ **ναι** στο free tier | ✅ | **Απαιτείται API key** (αλλιώς watermark). Attribution CARTO + OSM υποχρεωτικό. 5M tiles/μήνα |
| **Stadia Maps** | Πλακίδια χάρτη | Stadia ToS | ❌ **ΟΧΙ** στο free | ✅ | Το free tier είναι ρητά non-commercial. Εμπορική χρήση από $20/μήνα |
| **Protomaps (hosted)** | Πλακίδια χάρτη (vector) | BSD / CC0 για tileset | ⚠️ μόνο για GitHub Sponsors | ✅ | Το hosted API είναι δωρεάν μόνο για non-commercial |
| **OpenFreeMap** | Πλακίδια χάρτη (vector) | Δημόσιος server | ✅ δηλώνεται | ⚠️ | Χωρίς SLA. **Vector only** — δεν ταιριάζει στο Leaflet raster setup |
| **Self-hosted tiles** | Πλακίδια χάρτη | — | ✅ | ✅ | Κανένας τρίτος όρος. Χρειάζεται δικός σου server/χώρος |
| **RainViewer** (ραντάρ) | Radar tiles | Ιδιωτικοί όροι | ⚠️ έλεγξε πριν το launch | ⚠️ | Δεν ενσωματώθηκε |
| **Blitzortung** (κεραυνοί) | Real-time strikes | CC BY-SA 4.0 | ⚠️ share-alike, νομικά περίπλοκο | ⚠️ | Δεν ενσωματώθηκε |

### Παράγωγα δεδομένα (δείκτες, Skew-T, μετεογράμματα)

Τα πεδία (geopotential, θερμοκρασία, υγρασία, άνεμος) κατεβαίνουν ως ακατέργαστα
GRIB2 από NOAA/ECMWF/DWD και **υπολογίζονται από εμάς** με MetPy/xarray πάνω στα
ανοιχτά grid δεδομένα. Αυτό είναι επεξεργασία, όχι αναδιανομή βάσης δεδομένων:

- Οι δείκτες (CAPE, CIN, LCL, shear, SRH, freezing level) είναι **δικοί μας υπολογισμοί**.
- Τα Skew-T και τα μετεογράμματα είναι **δικά μας rendered** γραφήματα.
- Δεν αντιγράφονται οπτικοποιήσεις τρίτων.

Η CC BY 4.0 επιτρέπει ρητά «remix, transform, and build upon the material for any
purpose, even commercially». Η υποχρέωση αναφοράς αφορά τα δεδομένα εισόδου.

---

## Αποκλεισμοί εμπορικής διάθεσης

### #1 — Open-Meteo free tier (διορθώθηκε στον κώδικα)

Οι όροι του free tier είναι ρητά μη-εμπορικοί:

> You may only use the free API services for non-commercial purposes.

Με ρητό ορισμό της εμπορικής χρήσης:

> - Operating websites or apps that have subscriptions or display advertisements.

**Κατάσταση: το `wx.py` δεν καλεί καθόλου το Open-Meteo.** Όλες οι πηγές είναι
πλέον GFS, ICON-EU, ECMWF, Photon και OpenTopoData. Το `wx.openmeteo_point()` και
το `wx.geocode()` του Open-Meteo έχουν αφαιρεθεί· η γεωκωδικοποίηση γίνεται με
Photon. Επαληθεύεται από το `/api/health`:

```json
"non_commercial_sources_used": false
```

### #2 — Πλακίδια χάρτη OpenStreetMap (ΔΕΝ διορθώθηκε πλήρως)

Ο server `tile.openstreetmap.org` χρηματοδοτείται από δωρεές και η πολιτική του
απαγορεύει ρητά τη χρήση υψηλού φόρτου και δεν εγγυάται διαθεσιμότητα:

> We may block access, without notice, if your usage degrades the service.
> Availability is best-effort: there is no SLA or guarantee.

Και από την κοινότητα, για εμπορική χρήση:

> …you should not rely on the OSMF services for a commercial or otherwise end user
> application… I highly recommend to use a commercial tile provider for any
> business use case.

**Κατάσταση: μετριάστηκε, όχι λυμένο.** Το `app.py` δεν έχει hardcoded τον OSM·
το tile URL είναι μεταβλητή περιβάλλοντος:

- `WX_TILE_URL` — προεπιλογή ο OSM server, ώστε να δουλεύει το development.
- `WX_TILE_SUBDOMAINS` / `WX_TILE_RETINA` — για URLs με `{s}`/`{r}` (π.χ. CARTO).
  Χωρίς αυτά το Leaflet ζητά literal host `{s}` και κάθε πλακίδιο επιστρέφει 404.
- `WX_TILE_COMMERCIAL_OK=1` — δηλώνει ότι ο πάροχος επιτρέπει εμπορική χρήση.
  Αν δεν οριστεί, το UI εμφανίζει **ορατή προειδοποίηση** μέσα στον χάρτη.

#### Πάροχοι: τι επιτρέπει ο καθένας (επαληθευμένο 2026-09-23)

Οι επιλογές δεν είναι ισοδύναμες, και δύο από τις πιο συχνά προτεινόμενες
**δεν** επιτρέπουν εμπορική χρήση στο δωρεάν επίπεδο:

- **CARTO** — ✅ εμπορική χρήση επιτρέπεται στο free tier (5M πλακίδια/μήνα, fair
  use). ⚠️ **Απαιτείται API key**: «Request an API key, then add it to your tile
  URL as a `key` parameter… Please do not try to remove or hide the watermark.»
  Χωρίς key το πλακίδιο έρχεται με υδατογράφημα. Το key δεν απαιτεί λογαριασμό
  ούτε κάρτα. Attribution CARTO + OSM υποχρεωτικό.
- **Stadia Maps** — ❌ **όχι**. «Our free tier may only be used for non-commercial
  or evaluation purposes». Η εμπορική χρήση ξεκινά από Starter $20/μήνα.
  Το δωρεάν επίπεδο έχει 200.000 credits/μήνα και **χωρίς** overage.
- **Protomaps hosted** — ⚠️ δωρεάν μόνο για non-commercial· εμπορική χρήση μόνο
  για GitHub Sponsors.
- **OpenFreeMap** — ✅ δηλώνεται εμπορικά ελεύθερο και χωρίς key, αλλά είναι
  **vector-only** (χρειάζεται MapLibre GL, όχι Leaflet raster) και δεν δίνει SLA.
- **Self-hosting** — ✅ κανένας τρίτος όρος· το ασφαλέστερο για μακροπρόθεσμα.

**Προτεινόμενη ρύθμιση:** CARTO με δωρεάν key, με attribution που να περιλαμβάνει
και τους δύο. Είναι η μόνη free επιλογή που συνδυάζει εμπορική άδεια, raster
πλακίδια και το υπάρχον Leaflet setup χωρίς αλλαγή κώδικα.

### #3 — OpenTopoData public API (μετριάστηκε)

Το λογισμικό είναι MIT, οπότε ο κώδικας είναι ελεύθερος. Το **δημόσιο instance**
όμως έχει:

- Max 1.000 κλήσεις/ημέρα, 1 κλήση/δευτερόλεπτο.
- Η ιστοσελίδα του αναφέρει ότι η δημόσια υπηρεσία συντηρείται από δωρεές και
  ότι οι εντατικές χρήσεις πρέπει να πάνε σε self-hosting ή paid hosting.

1.000 κλήσεις/ημέρα εξαντλούνται με ~40 χρήστες που αλλάζουν σημείο 25 φορές.

**Κατάσταση: μετριάστηκε.** Το DEM καλείται μόνο σε χειροκίνητη επιλογή σημείου
και το αποτέλεσμα διαχωρίζεται από τη ροή της πρόγνωσης — αν αποτύχει, η πρόγνωση
λειτουργεί κανονικά με το υψόμετρο του κελιού του μοντέλου.

Για εμπορική διάθεση, με σειρά προτίμησης:

1. **Self-host το OpenTopoData** (MIT, Docker image) με το EU-DEM 25 m. Δωρεάν,
   χωρίς όρια, τα δεδομένα είναι CC BY 4.0. Είναι η σωστή λύση για Ελλάδα.
2. **Paid hosting** του OpenTopoData — χωρίς όρια, EU-only servers.
3. Χρήση μόνο του `orography` από το GFS (δικό μας, δωρεάν, χωρίς όρια), αλλά
   ανάλυση 25 km — δεν λέει τίποτα για τοπικό ανάγλυφο.

---

## 2. Βιβλιοθήκες λογισμικού

Επαληθευμένες από τα τοπικά LICENSE αρχεία και το `License-Expression` metadata
των εγκατεστημένων πακέτων, όχι από μνήμη.

| Πακέτο | Έκδοση | Άδεια | Εμπορική |
|---|---|---|---|
| fastapi | 0.141.1 | MIT | ✅ |
| starlette | 1.7.0 | BSD-3-Clause | ✅ |
| uvicorn | 0.53.0 | BSD-3-Clause | ✅ |
| pydantic | 2.13.5 | MIT | ✅ |
| httpx | 0.28.1 | BSD-3-Clause | ✅ |
| anyio | 4.15.1 | MIT | ✅ |
| h11 | 0.16.0 | MIT | ✅ |
| xarray | 2026.7.0 | Apache-2.0 | ✅ |
| cfgrib | 0.9.15.1 | Apache-2.0 | ✅ |
| eccodes | 2.48.0 | Apache-2.0 + πρόσθετοι όροι (βλ. παρακάτω) | ✅ |
| MetPy | 1.7.1 | BSD-3-Clause | ✅ |
| Cartopy | 0.26.0 | BSD-3-Clause | ✅ |
| matplotlib | 3.11.2 | PSF-based (BSD-compatible) | ✅ |
| NumPy | 2.5.3 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | ✅ |
| SciPy | 1.18.1 | BSD-3-Clause | ✅ |
| pandas | 3.0.6 | BSD-3-Clause | ✅ |
| Shapely | 2.1.2 | BSD-3-Clause | ✅ |
| pyproj | 3.8.0 | MIT | ✅ |
| Pillow | 12.3.0 | MIT-CMU (HPND) | ✅ |
| Jinja2 | 3.1.6 | BSD-3-Clause | ✅ |

### Προσοχή στο eccodes

Το Python πακέτο `eccodes` είναι Apache-2.0 wrapper· η βιβλιοθήκη ECMWF ecCodes
που χρησιμοποιεί διέπεται από **Apache-2.0 με πρόσθετους όρους**. Η Apache-2.0
επιτρέπει εμπορική χρήση, αλλά η υποχρέωση διατήρησης των notices παραμένει.
Έλεγξε το `NOTICE` του ecCodes πριν από διανομή.

### Cartopy και δεδομένα χαρτών

Το Cartopy είναι BSD-3-Clause. **Τα δεδομένα χαρτών** έχουν δικές τους άδειες —
Natural Earth είναι public domain, GSHHS είναι LGPL. Στο παρόν project δεν
ενσωματώνονται τέτοια αρχεία· τα Skew-T χρησιμοποιούν μόνο γραμμές πλέγματος.

---

## 3. Front-end assets

| Asset | Άδεια | Σημείωση |
|---|---|---|
| **Leaflet 1.9.4** | BSD-2-Clause | Φιλοξενείται τοπικά: `static/leaflet.js`, `static/leaflet.css` |
| **Chart.js 4.4.1** | MIT | Φιλοξενείται τοπικά: `static/chart.umd.min.js` |
| Εικονίδια UI | — | Emoji του λειτουργικού, όχι icon fonts ή SVG sets τρίτων |
| Γραμματοσειρές | — | `system-ui`, `-apple-system`, `Segoe UI`, `Roboto` — τοπικές, όχι webfonts |
| CSS / JS του UI | Ιδιόκτητο | Γράφτηκε για το project, χωρίς εξωτερικά frameworks |

**Γιατί τοπική φιλοξενία:** μια εξάρτηση από CDN τρίτου εισάγει (α) εξάρτηση
διαθεσιμότητας, (β) κίνδυνο αλλαγής άδειας, (γ) θέμα GDPR για διαβίβαση IP
επισκεπτών σε τρίτη χώρα. Οι άδειες BSD-2 και MIT επιτρέπουν ρητά την αναδιανομή.

Τα πλακίδια του χάρτη **δεν** φιλοξενούνται τοπικά — είναι το μόνο σημείο όπου
εξαρτάται από τρίτο, εξ ου και ο αποκλεισμός #2.

---

## 4. Υποχρεωτικά κείμενα αναφοράς

Εμφανίζονται στο footer του UI μέσω του `attribution_block()`. Έτοιμα για αντιγραφή:

**ECMWF (υποχρεωτικό):**
> This service is based on data and products of the European Centre for
> Medium-Range Weather Forecasts (ECMWF). © [έτος] ECMWF.
> Source www.ecmwf.int. This ECMWF data is published under a Creative Commons
> Attribution 4.0 International (CC BY 4.0). ECMWF does not accept any liability
> whatsoever for any error or omission in the data, their availability, or for any
> loss or damage arising from their use. Τα δεδομένα έχουν υποστεί επεξεργασία:
> οι δείκτες υπολογίστηκαν και τα διαγράμματα σχεδιάστηκαν από εμάς.

**DWD:**
> Source: Deutscher Wetterdienst (DWD). CC BY 4.0.
> https://www.dwd.de/EN/service/copyright

**NOAA/NWS:**
> Data source: NOAA/NWS. Public domain. No endorsement by NOAA implied.

**OpenStreetMap / Photon (υποχρεωτικό):**
> © OpenStreetMap contributors, ODbL. Geocoding by Photon.

**Copernicus EU-DEM:**
> Generated using Copernicus data and information, EU-DEM, [έτος].

**Copernicus ERA5 (υποχρεωτικό για την επαλήθευση):**
> Generated using Copernicus Climate Change Service information [έτος].
> Neither the European Commission nor ECMWF is responsible for any use of this
> information.

Η επαλήθευση (`verify.py`) χρησιμοποιεί δύο επιπλέον keyless, εμπορικά συμβατές
πηγές:

| Πηγή | Χρήση | Άδεια | Υποχρεώσεις |
|---|---|---|---|
| **AWS Open Data `noaa-gfs-bdp-pds`** | Αρχειοθετημένοι κύκλοι GFS, με `.idx` | US Public Domain | Αναφορά NOAA/NWS, χωρίς ένδειξη έγκρισης |
| **ARCO-ERA5** (Google public bucket) | ERA5 Zarr, ωριαίο | CC BY 4.0 (Copernicus) | Δήλωση Copernicus, όπως πάνω |

Το ARCO-ERA5 είναι αναδιανομή του ERA5, δεν αλλάζει την άδεια: η υποχρέωση
αναφοράς της Copernicus ισχύει κανονικά σε ό,τι παράγεται και εμφανίζεται.

---

## 5. Τι απομένει πριν το launch

- [ ] **Απόφαση tile provider** και ορισμός `WX_TILE_URL` + `WX_TILE_COMMERCIAL_OK=1`.
      Χωρίς αυτό, ο χάρτης μπορεί να μπλοκάρει και να σπάσει για τους πληρωμένους πελάτες.
- [ ] **Self-host του OpenTopoData** ή paid hosting, ή αποδοχή ότι το DEM θα λείπει
      σε φόρτο.
- [ ] Έλεγχος του `NOTICE` του ecCodes.
- [ ] Έλεγχος όρων RainViewer/Blitzortung πριν ενσωματωθούν (το Blitzortung είναι
      CC BY-SA: share-alike, που μπορεί να επηρεάσει τα παράγωγα).
- [ ] Αν χρησιμοποιηθούν όργανα Ecowitt, έλεγχος των όρων της Ecowitt για
      αναδιανομή των δεδομένων του σταθμού που ανήκει στον πελάτη.
- [x] **Σελίδες Όρων, Απορρήτου και Επιστροφών** — υπάρχουν στα `/terms`,
      `/privacy`, `/refunds` (δημόσια, χωρίς auth) και linkαρίζονται από τον
      server-rendered footer και από το σημείο πληρωμής. Γραμμένες πάνω στη
      συμπεριφορά του κώδικα. **Δεν έχουν ελεγχθεί από νομικό.**
- [ ] **Επιβεβαίωση των social handles.** Οι προεπιλογές
      (`WX_YOUTUBE_URL`, `WX_FACEBOOK_URL`) είναι μαντεψιές που πρέπει να
      ελεγχθούν· λάθος handle δεν φαίνεται μέχρι να γράψει πελάτης.
- [ ] **Νομική επισκόπηση πριν από πραγματική εμπορική διάθεση.** Αυτό το αρχείο
      είναι μηχανικός κατάλογος, όχι νομική συμβουλή. Οι σελίδες το δηλώνουν και
      οι ίδιες.
