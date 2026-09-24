"""Free/PRO entitlement gating.

Design decision worth stating plainly: a CSS `blur(8px)` is presentation, not
access control. If the server sends ten days of data and the browser hides eight
of them, anyone with devtools reads the whole thing. So the blur is the visual
treatment, and the security is that **the server does not send locked data at all**.

The tier is carried in an HMAC-signed token, so a user cannot mint their own PRO
token without the server secret. Everything here is deliberately small and
dependency-free.

Environment
-----------
WX_SECRET        signing key. MUST be set to a random value in production; the
                 default exists only so local development works.
WX_MASTER_CODE   passcode that unlocks PRO for comps/testing. Required in
                 production: a missing or blank value fails startup, because the
                 passcode is an entitlement source and there is deliberately no
                 usable default. An empty value never matches at request time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

import config

# `SECRET` is kept as a module attribute for callers that read it, but every
# sign/verify goes through `secret()`, which resolves the environment on each
# call. That ordering matters: `app.py` loads `.env` *after* importing this
# module, so a module-level snapshot would keep the development default even when
# `WX_SECRET` was configured in `.env` — which is exactly the bug this fixes.
SECRET = config.signing_secret()
MASTER_CODE = config.master_code()

FREE_HOURS = 72
PRO_HOURS = 240          # 10 days
TOKEN_TTL_S = 60 * 60 * 24 * 30
TRIAL_HOURS = 240        # a trial sees the full PRO window
TRIAL_TTL_S = 60 * 60 * 48   # 2 days


def secret() -> str:
    """Current signing key, resolved from the environment on every call."""
    return config.signing_secret()


def master_code() -> str:
    """The comp/test passcode, resolved on every call (same .env timing reason)."""
    return config.master_code()

# Pricing. Kept here so the API and the UI cannot drift apart.
PRICING = {
    "currency": "EUR",
    "monthly": {"price": 2.99, "label": "Μηνιαίο", "period": "μήνα"},
    "yearly": {"price": 19.99, "label": "Ετήσιο", "period": "έτος",
               "badge": "Best Value", "monthly_equivalent": round(19.99 / 12, 2)},
    "trial": {"days": 2, "label": "Δωρεάν δοκιμή 2 ημερών", "cta": "Ξεκίνα δωρεάν δοκιμή 2 ημερών"},
}


def yearly_discount_percent() -> float:
    """Real discount of the yearly plan against 12 monthly payments.

    Computed rather than hardcoded: 2.99*12 = 35.88, so 19.99 is a 44.3% saving.
    Claiming "45%" would be a rounded-up marketing number; this reports what is true.
    """
    m = PRICING["monthly"]["price"] * 12
    y = PRICING["yearly"]["price"]
    return round((1 - y / m) * 100, 1)


@dataclass
class Entitlement:
    tier: str            # "free" or "pro"
    hours: int
    expires_at: int | None
    source: str          # "free" | "passcode" | "subscription" | "trial" | "promo"
    subscription_id: str | None = None
    device: str | None = None
    # The moment PRO access actually ends, after subscription state and any promo
    # window are considered. `expires_at` is when the *token* stops being accepted;
    # the two differ for a subscription whose Stripe period ends before the token
    # does, and for a promo code, whose own expiry governs.
    pro_until: int | None = None
    # Free-form notes about why the effective window is what it is, so the UI can
    # say "promo until <date>" or "subscription ended" rather than guessing.
    notes: tuple[str, ...] = ()

    @property
    def is_pro(self) -> bool:
        return self.tier == "pro"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(secret().encode(), payload, hashlib.sha256).digest())


def issue_token(tier: str = "pro", source: str = "passcode", ttl: int = TOKEN_TTL_S,
                subscription_id: str | None = None,
                device: str | None = None) -> str:
    data: dict = {"tier": tier, "src": source, "exp": int(time.time()) + ttl,
                  "nonce": secrets.token_hex(6)}
    if device:
        # An opaque per-browser id, minted once and carried in the signed token.
        # It exists so a promo code can be limited to *a* visitor without an
        # accounts table. It is not derived from IP, user agent or anything else
        # about the device: it is a random string the server hands out.
        data["dev"] = device
    if subscription_id:
        # Carried in the signed payload so the holder can manage their own
        # subscription without an accounts table. It is only an identifier; the
        # name/email stay on Stripe, which is what the privacy page promises.
        data["sub"] = subscription_id
    payload = json.dumps(data).encode()
    return f"{_b64e(payload)}.{_sign(payload)}"


def verify_token(token: str | None) -> Entitlement:
    """Return the entitlement for a token, falling back to free on anything suspect."""
    free = Entitlement("free", FREE_HOURS, None, "free")
    if not token or "." not in token:
        return free
    body, sig = token.rsplit(".", 1)
    try:
        payload = _b64d(body)
    except Exception:
        return free
    if not hmac.compare_digest(_sign(payload), sig):
        return free
    try:
        data = json.loads(payload)
    except Exception:
        return free
    if int(data.get("exp", 0)) < time.time():
        return free
    if data.get("tier") != "pro":
        return free
    exp = int(data["exp"])
    return Entitlement("pro", PRO_HOURS, exp, data.get("src", "passcode"),
                       data.get("sub"), data.get("dev"), pro_until=exp)


def check_passcode(code: str) -> str | None:
    """Return a PRO token if the master passcode matches, else None.

    Constant-time comparison so the endpoint does not leak the code by timing.
    An unset master code (the production default) disables the endpoint: an
    empty string must never match, which is why this is checked before the
    comparison rather than relying on `hmac.compare_digest("", "")`.
    """
    master = master_code()
    if not master:
        return None
    if code and hmac.compare_digest(code.encode(), master.encode()):
        return issue_token("pro", "passcode")
    return None


def issue_trial(device: str | None = None) -> str:
    """A real 2-day trial: full PRO window, expiring on its own.

    NOTE: nothing here stops the same visitor starting a trial repeatedly — the
    entitlement is stateless, so there is no record to check against. Gating a
    trial to one per account or device needs the accounts layer; until then this
    is a working flow, not an abuse-proof one.
    """
    return issue_token("pro", "trial", ttl=TRIAL_TTL_S, device=device)


def plan_payload() -> dict:
    return {"pricing": PRICING, "yearly_discount_percent": yearly_discount_percent(),
            "free_hours": FREE_HOURS, "pro_hours": PRO_HOURS,
            "trial_days": PRICING["trial"]["days"], "trial_hours": TRIAL_HOURS,
            "free_display": "72 ώρες (3 ημέρες)", "pro_display": "10 ημέρες",
            "pro_locked_days": "Ημέρες 4–10",
            "unlocks": ["Πρόγνωση 10 ημερών", "Skew-T και κατακόρυφη δομή",
                        "Δείκτες αστάθειας (SBCAPE, Shear, SRH, LCL)",
                        "Σύγκριση 3 μοντέλων", "Διόρθωση με τοπικό σταθμό"]}
