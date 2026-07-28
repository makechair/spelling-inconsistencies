"""Cloudflare Access JWT verification.

Spec 3.5 requires that nothing — HTML, API or SSE — is reachable before
authentication, and 12 lists "Cloudflare misconfiguration -> unintended
exposure" as a risk. The spec's answer to both is entirely inside Cloudflare's
dashboard, which means one deleted policy is the difference between private and
public.

Cloudflare Access signs every proxied request with a JWT in
``Cf-Access-Jwt-Assertion``. Verifying it here makes a dashboard mistake
insufficient on its own to expose the app (docs/spec-review.md A-4).

Checks performed:
  * RS256 signature against the team's JWKS
  * ``aud`` equals the Access application's AUD tag
  * ``iss`` matches the team domain
  * ``exp`` / ``iat`` (handled by PyJWT)
  * ``email`` is in the allow list
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from ..config import Settings

log = logging.getLogger(__name__)

ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_COOKIE = "CF_Authorization"


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Identity:
    email: str
    subject: str | None = None


class AccessVerifier:
    """Verifies Access tokens, with a cached JWKS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: PyJWKClient | None = None
        self._client_created_at = 0.0

    def _jwk_client(self) -> PyJWKClient:
        now = time.monotonic()
        expired = (now - self._client_created_at) > self._settings.jwks_cache_seconds
        if self._client is None or expired:
            self._client = PyJWKClient(
                self._settings.jwks_url(),
                cache_keys=True,
                lifespan=self._settings.jwks_cache_seconds,
            )
            self._client_created_at = now
        return self._client

    def verify(self, token: str) -> Identity:
        if not token:
            raise AuthError("missing Cloudflare Access token")

        try:
            signing_key = self._jwk_client().get_signing_key_from_jwt(token)
        except (jwt.PyJWKClientError, httpx.HTTPError) as exc:
            # Treat an unreachable JWKS as a failure, never as a bypass.
            log.error("unable to fetch Access signing key: %s", exc)
            raise AuthError("cannot verify access token", status_code=503) from exc

        issuer = (self._settings.cf_access_team_domain or "").rstrip("/")
        if not issuer.startswith("http"):
            issuer = f"https://{issuer}"

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._settings.cf_access_aud,
                issuer=issuer,
                options={"require": ["exp", "iat", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid access token: {exc}") from exc

        email = str(claims.get("email") or "").lower()
        if not email:
            raise AuthError("access token carries no email claim", status_code=403)
        if email not in self._settings.allowed_emails:
            log.warning("rejected authenticated user outside the allow list")
            raise AuthError("account is not permitted", status_code=403)

        return Identity(email=email, subject=claims.get("sub"))


def extract_token(headers: dict[str, str], cookies: dict[str, str]) -> str:
    """Access sends the assertion as a header; the cookie is the fallback."""
    for key, value in headers.items():
        if key.lower() == ACCESS_HEADER.lower() and value:
            return value
    return cookies.get(ACCESS_COOKIE, "")
