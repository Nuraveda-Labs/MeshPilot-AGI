"""HTTP middleware — Cloudflare/origin hardening (mirrors leaselens app/*).

Order (add inner→outer; Starlette runs the LAST-added outermost):
    SecurityHeaders (inner) → CORS → TrustedHost → RateLimit → BodySizeLimit
    → OriginAuth (outer — rejects direct-to-origin /internal|/jobs first).
Wired in `meshpilot.server`.
"""
from __future__ import annotations

from meshpilot.middleware.bodylimit import BodySizeLimitMiddleware
from meshpilot.middleware.originauth import OriginAuthMiddleware
from meshpilot.middleware.ratelimit import RateLimitMiddleware, client_ip
from meshpilot.middleware.security import SecurityHeadersMiddleware

__all__ = [
    "SecurityHeadersMiddleware",
    "BodySizeLimitMiddleware",
    "OriginAuthMiddleware",
    "RateLimitMiddleware",
    "client_ip",
]
