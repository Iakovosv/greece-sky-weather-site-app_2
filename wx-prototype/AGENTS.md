# AGENTS.md — Greece Sky and Weather

Repository-specific notes for future agent sessions. Read this before changing
anything; the commercial invariants below are easy to break by accident.

## What this is

A single-process FastAPI app that serves both the API and the HTML/JS UI, over
open weather data (GFS, ICON-EU, ECMWF open data). Everything runs on one VPS:
no Kubernetes, no microservices, no paid third-party services. Keep it that way —
adding infrastructure is an explicit non-goal.

## Layout and where logic lives

| File | Responsibility |
|---|---|
| `app.py` | FastAPI app, the UI string, all HTTP routes, forecast assembly |
| `wx.py` | GRIB download/parse (GFS, ICON-EU, ECMWF), geocoding, DEM, cache |
| `entitlements.py` | Tier limits, token signing/verification, pricing metadata |
| `cachestore.py` | Result/file cache: size cap, eviction, corruption tolerance |
| `billing.py` | Stripe checkout, webhook lifecycle, subscription state, auto-renew |
| `promo.py` | PRO promo/gift codes over SQLite: codes, redemptions, revoke |
| `ratelimit.py` | In-process token-bucket limits per endpoint |
| `analytics.py` | First-party usage events; privacy-preserving by construction |
| `logging_setup.py` | Structured (JSON) logging |
| `config.py` | Environment variables, startup validation, coordinate bounds |
| `verify.py` | Archived-forecast vs ERA5 verification |
| `bias.py` | Local bias correction; `model_fcst` table holds run history |
| `legal.py` | Terms / Privacy / Refunds pages, contact details |

## Hard invariants — do not change without an explicit request

- **FREE = 72 h, PRO = 240 h (10 days).** Defined in `entitlements.py` as
  `FREE_HOURS` / `PRO_HOURS`. Tests assert these values. The legal/README text
  must match the code, not the other way around.
- **PRO is decided server-side, every request.** Never trust a client flag,
  `localStorage` value, or the presence of a token alone. The entitlement is
  recomputed (including subscription state) on each PRO request.
- **No PRO weather data reaches a FREE browser.** Locked forecast values are
  never sent and hidden with CSS; the numbers simply are not in the response.
- **The entitlement is additive, not destructive.** `effective_pro_until` is the
  latest end across all active entitlements (paid + promo). A promo must never
  shorten or cancel a Stripe subscription, and vice versa.
- **`/api/expert`, `/api/skewt` and any PRO route must call `require_pro(...)`**
  or the equivalent server check. `test_entitlements_api.py` guards this.
- **Admin endpoints are closed when unconfigured.** `WX_ADMIN_TOKEN` unset means
  404/503, never an open admin surface.
- **Production fails fast on entitlement secrets.** `config.assert_production_ready()`
  (called from the startup hook) raises `ConfigError` when `WX_ENV=production` and
  `WX_SECRET` or `WX_MASTER_CODE` is unset. Neither has a usable production
  default. Do not move these checks to import time: the hook runs after
  `envfile.load()`, so a value in `.env` is honoured. Staging/dev keep the warning
  only, never the fatal error.
- **Never log secrets**: no Stripe keys, card data, `WX_SECRET`,
  `WX_ADMIN_TOKEN`, raw tokens, or unnecessary personal data.
- **Analytics keeps no directly identifying data**: no IP, no full User-Agent,
  no exact coordinates (a ~0.5° cell only). The device id *is* stored, but only
  as a hash salted with a per-installation secret kept in the database, so it is
  not reversible and not correlatable outside this database. The privacy page
  describes this correctly; do not describe it as "anonymous".
- **The analytics table stays bounded.** Raw events are the one table an
  anonymous caller can grow, so `POST /api/analytics` is rate-limited (not on
  the exempt list) and `analytics.maybe_prune()` deletes rows past
  `WX_ANALYTICS_RETENTION_DAYS` (default 365) roughly hourly. Do not add
  `/api/analytics` back to `_LIMIT_EXEMPT`.

## Conventions

- Comments explain *why*, not *what*. Greek user-facing strings, English code
  comments.
- Weather calculations are only changed with verification evidence. "Could be
  better" is not a reason to touch a formula.
- Model agreement ("confidence") is presented as agreement between models, never
  as a probability the forecast is correct.
- Prefer extending an existing module over adding a new one. Avoid heavy
  dependencies, and never add a paid external service where a self-hosted or
  free option exists.

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # full suite
uvicorn app:app --host 0.0.0.0 --port 12000
```

Config is via environment variables; `.env` is auto-loaded but real environment
variables win. See `.env.example` for the full list. `WX_RATE_LIMIT_DISABLED=1`
and a temp `WX_DB`/`WX_CACHE_DIR` are the usual test setup (see
`tests/test_entitlements_api.py` for the pattern).

## Testing expectations

For any change to entitlement, promo, billing or forecast limits, add or update
an end-to-end test in `tests/test_entitlements_api.py` that goes through the real
HTTP surface. The scenarios that must keep passing: FREE blocked from PRO, active
PAID allowed, cancelled/expired blocked, active promo allowed, expired promo
blocked, exhausted code blocked, same device cannot redeem twice, invalid code
blocked, forged local token still blocked, and concurrent identical forecast
requests sharing one computation.
