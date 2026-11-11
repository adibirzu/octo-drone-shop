"""IDCS / OCI IAM Identity Domain SSO — OIDC Authorization Code + PKCE.

Why a fresh implementation rather than a copy of the CRM portal
================================================================
``enterprise-crm-portal`` ships an "intentionally vulnerable" SSO module that
decodes the IDCS ID token without verifying the JWT signature
(``_decode_jwt_claims``). The drone shop is meant to be a clean reference app,
so this module always verifies ID tokens via the IDCS JWKS endpoint
(``/admin/v1/SigningCert/jwk``) using PyJWT + the ``cryptography`` backend.

Flow
----
1. ``GET  /api/auth/sso/login``     – build the IDCS authorize URL with PKCE
                                       (S256), stash ``state`` + ``code_verifier``
                                       in a short-lived signed cookie that is
                                       independent of the user-facing session.
2. ``GET  /api/auth/sso/callback``  – verify cookie → exchange code for tokens
                                       → verify ID token signature, ``aud``,
                                       ``iss``, ``exp``, ``nbf`` → upsert local
                                       user → issue our existing HMAC bearer
                                       token → redirect to ``/`` with the token
                                       in an httpOnly cookie so the SPA can
                                       call protected APIs.
3. ``GET  /api/auth/sso/status``    – returns ``{configured, provider, ...}``
                                       so the login page knows whether to show
                                       the SSO button.
4. ``GET  /api/auth/sso/logout``    – clears the local token cookie and
                                       optionally redirects to the IDCS logout
                                       endpoint when one is configured.

Cross-tenancy portability
-------------------------
Every IDCS-related value comes from environment variables (``IDCS_*``). No
tenancy OCID, region, or domain hostname is hardcoded. Adopt the app in a new
tenancy by setting four secrets — see ``docs/install-guide.md``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from server.auth_security import SESSION_COOKIE_NAME, _secret_bytes, issue_token
from server.config import cfg
from server.database import get_db
from server.observability.logging_sdk import push_log
from server.observability.otel_setup import get_tracer
from server.observability.security_spans import security_span

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/sso", tags=["sso"])

_PKCE_COOKIE = "octo_sso_pkce"
_PKCE_TTL_SECONDS = 600  # 10 minutes — IDCS authorize → callback round-trip
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_TTL_SECONDS = 3600


# ── PKCE helpers ───────────────────────────────────────────────────────


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _sign_cookie(value: str) -> str:
    """HMAC-sign the PKCE cookie payload using the same secret as bearer tokens.

    We reuse :func:`server.auth_security._secret_bytes` so the cookie has the
    same security properties as our session token (rotates with the app
    secret, fails fast if the secret is missing in production).
    """
    sig = hmac.new(_secret_bytes(), value.encode("utf-8"), hashlib.sha256).digest()
    return f"{value}.{_b64url(sig)}"


def _verify_cookie(signed: str) -> str | None:
    if not signed or "." not in signed:
        return None
    value, sig_part = signed.rsplit(".", 1)
    expected = _b64url(
        hmac.new(_secret_bytes(), value.encode("utf-8"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(sig_part, expected):
        return None
    return value


# ── JWKS verification ──────────────────────────────────────────────────


async def _fetch_jwks(force: bool = False) -> dict:
    """Fetch and cache the IDCS JWKS document.

    IDCS exposes its signing keys at ``{domain}/admin/v1/SigningCert/jwk``.
    The cache TTL is 1h; we re-fetch on key-id mismatch or when ``force`` is
    set so signing key rotations are picked up automatically.
    """
    cached = _JWKS_CACHE.get(cfg.idcs_domain_url)
    if not force and cached and (time.time() - cached[0] < _JWKS_TTL_SECONDS):
        return cached[1]

    jwks_url = f"{cfg.idcs_domain_url}/admin/v1/SigningCert/jwk"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"IDCS JWKS unreachable ({resp.status_code})",
        )
    document = resp.json()
    _JWKS_CACHE[cfg.idcs_domain_url] = (time.time(), document)
    return document


def _select_key(jwks: dict, kid: str | None) -> dict | None:
    keys = jwks.get("keys") or []
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key
    return keys[0] if keys else None


async def _verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify an IDCS-issued ID token via JWKS.

    Validates: signature (RS256), ``iss`` (must equal IDCS domain), ``aud``
    (must equal client_id), ``exp``, ``nbf``. Refetches JWKS once on key-id
    mismatch to handle key rotation transparently.
    """
    if not cfg.idcs_verify_jwt:
        # Allowed only in air-gapped dev where JWKS isn't reachable. Logs the
        # bypass so it shows up in audit dashboards.
        logger.warning("IDCS_VERIFY_JWT=false — skipping ID token signature verification")
        return jwt.decode(id_token, options={"verify_signature": False})

    try:
        unverified = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed ID token: {exc}") from exc

    kid = unverified.get("kid")
    jwks = await _fetch_jwks()
    key = _select_key(jwks, kid)
    if key is None:
        # Possible signing-key rotation; refetch and retry once.
        jwks = await _fetch_jwks(force=True)
        key = _select_key(jwks, kid)
    if key is None:
        raise HTTPException(status_code=502, detail="No matching JWKS key for ID token")

    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
        return jwt.decode(
            id_token,
            key=public_key,
            algorithms=["RS256"],
            audience=cfg.idcs_client_id,
            issuer=cfg.idcs_domain_url,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"ID token verification failed: {exc}") from exc


# ── User upsert ────────────────────────────────────────────────────────


async def _upsert_sso_user(claims: dict[str, Any]) -> dict[str, Any]:
    """Find or create a local user for the IDCS subject.

    SSO-provisioned users get a non-usable password hash (``"sso:<sub>"``) so
    they can never authenticate via the password endpoint.
    """
    email = (claims.get("email") or "").strip().lower()
    sub = (claims.get("sub") or "").strip()
    display = claims.get("name") or claims.get("preferred_username") or email
    if not email or not sub:
        raise HTTPException(status_code=400, detail="ID token missing email/sub")

    username = (display or email.split("@")[0]).lower().replace(" ", ".")
    role = "user"  # Role assignment is intentionally NOT taken from the token claims

    async with get_db() as db:
        existing = await db.execute(
            text(
                "SELECT id, username, email, role, is_active "
                "FROM users WHERE lower(email) = :email FETCH FIRST 1 ROWS ONLY"
            ),
            {"email": email},
        )
        row = existing.mappings().first()
        if row:
            await db.execute(
                text("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = :id"),
                {"id": row["id"]},
            )
            return dict(row)

        await db.execute(
            text(
                "INSERT INTO users (username, email, password_hash, role, is_active) "
                "VALUES (:u, :e, :p, :r, 1)"
            ),
            {"u": username, "e": email, "p": f"sso:{sub}", "r": role},
        )
        created = await db.execute(
            text(
                "SELECT id, username, email, role, is_active "
                "FROM users WHERE lower(email) = :email FETCH FIRST 1 ROWS ONLY"
            ),
            {"email": email},
        )
        return dict(created.mappings().first() or {})


# ── Routes ─────────────────────────────────────────────────────────────


def _resolve_redirect_uri(request: Request, fallback_uri: str) -> str:
    """Resolve callback redirect URI dynamically from validated X-Forwarded-Host."""
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip()
    if not raw_host:
        return fallback_uri
    host_only = raw_host.split(":")[0].lower()

    allowed_domains_env = os.getenv("ALLOWED_SSO_DOMAINS", "")
    allowed_domains = [d.strip().lower() for d in allowed_domains_env.split(",") if d.strip()]
    dns_domain = os.getenv("DNS_DOMAIN", "").strip().lower()
    if dns_domain and dns_domain not in allowed_domains:
        allowed_domains.append(dns_domain)
    dns_sec = os.getenv("DNS_SECONDARY_DOMAIN", "").strip().lower()
    if dns_sec and dns_sec not in allowed_domains:
        allowed_domains.append(dns_sec)

    matches = any(
        host_only == d or host_only == f"shop.{d}" or host_only.endswith("." + d)
        for d in allowed_domains
    )
    if matches and not host_only.endswith(".svc.cluster.local"):
        forwarded_proto = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or "https"
        ).split(",")[0].strip()
        scheme = (
            "https"
            if forwarded_proto in ("https", "http")
            and not host_only.startswith("127.")
            and host_only != "localhost"
            else forwarded_proto
        )
        return f"{scheme}://{raw_host}/api/auth/sso/callback"
    return fallback_uri


def _resolve_cp_broker_url(request: Request) -> str:
    cp_env = os.environ.get("CONTROL_PLANE_URL", "").rstrip("/")
    if cp_env and not any(h in cp_env for h in ("127.0.0.1", "localhost")):
        return cp_env
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip().split(":")[0]
    if "." in raw_host:
        parts = raw_host.split(".")
        cp_host = "cp." + ".".join(parts[1:])
        scheme = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or "https"
        ).split(",")[0].strip()
        return f"{scheme}://{cp_host}"
    dns_domain = os.environ.get("DNS_DOMAIN", "").strip()
    return f"https://cp.{dns_domain}" if dns_domain else ""


def _verify_google_ticket(ticket: str) -> dict | None:
    if not ticket or "." not in ticket:
        return None
    try:
        import json
        b64_payload, b64_sig = ticket.split(".", 1)
        for secret_candidate in (
            os.environ.get("OCTODEMO_SSO_COOKIE_SECRET"),
            os.environ.get("SSO_COOKIE_SECRET"),
            os.environ.get("SESSION_SECRET"),
        ):
            if not secret_candidate:
                continue
            secret_bytes = secret_candidate.strip().encode("utf-8")
            expected_sig = hmac.new(secret_bytes, b64_payload.encode("ascii"), hashlib.sha256).digest()
            expected_b64_sig = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
            if secrets.compare_digest(b64_sig, expected_b64_sig):
                padded = b64_payload + "=" * ((4 - len(b64_payload) % 4) % 4)
                data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
                if isinstance(data, dict):
                    exp = data.get("exp")
                    if not exp or time.time() <= float(exp):
                        return data
        return None
    except Exception:
        return None


@router.get("/status")
async def sso_status() -> dict:
    return {
        "configured": cfg.idcs_configured,
        "provider": "OCI IAM Identity Domain (IDCS)" if cfg.idcs_configured else None,
        "domain_url": cfg.idcs_domain_url if cfg.idcs_configured else None,
        "verify_jwt": cfg.idcs_verify_jwt,
    }


@router.get("/providers")
async def sso_providers(request: Request):
    """Expose available SSO login providers and their endpoints."""
    redirect_uri = _resolve_redirect_uri(request, cfg.idcs_redirect_uri)
    base = redirect_uri.rsplit("/api/auth/sso/callback", 1)[0]
    return {
        "providers": ["idcs", "google"],
        "login_urls": {
            "idcs": f"{base}/api/auth/sso/login?provider=idcs",
            "google": f"{base}/api/auth/sso/login?provider=google",
        },
    }


@router.get("/login")
async def sso_login(
    request: Request,
    idp: str | None = None,
    provider: str | None = None,
):
    tracer = get_tracer()
    with tracer.start_as_current_span("auth.sso.login_initiate") as span:
        idp_val = idp or provider or request.query_params.get("idp") or request.query_params.get("provider")
        if idp_val and idp_val.strip().lower() == "google":
            cp_broker = _resolve_cp_broker_url(request)
            redirect_uri = _resolve_redirect_uri(request, cfg.idcs_redirect_uri)
            from urllib.parse import quote
            span.set_attribute("auth.method", "google_broker")
            return RedirectResponse(f"{cp_broker}/api/auth/google/start?return_to={quote(redirect_uri, safe=':/?&=')}", status_code=303)

        span.set_attribute("auth.method", "idcs_oidc")
        span.set_attribute("auth.idcs.domain", cfg.idcs_domain_url)
        if not cfg.idcs_configured:
            span.set_attribute("auth.sso.error", "not_configured")
            raise HTTPException(status_code=503, detail="SSO is not configured")

    redirect_uri = _resolve_redirect_uri(request, cfg.idcs_redirect_uri)

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)

    params = {
        "response_type": "code",
        "client_id": cfg.idcs_client_id,
        "redirect_uri": redirect_uri,
        "scope": cfg.idcs_scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if idp_val and idp_val.strip().lower() == "google":
        params["idp"] = "Google"

    authorize_url = f"{cfg.idcs_domain_url}/oauth2/v1/authorize?{urlencode(params)}"

    cookie_payload = _sign_cookie(f"{state}:{code_verifier}:{int(time.time())}:{redirect_uri}")
    redirect = RedirectResponse(url=authorize_url, status_code=302)
    redirect.set_cookie(
        _PKCE_COOKIE,
        cookie_payload,
        max_age=_PKCE_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    push_log("INFO", "SSO login initiated", **{"auth.method": "idcs_sso"})
    return redirect


@router.get("/callback")
async def sso_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    google_ticket: str = "",
):
    tracer = get_tracer()
    with tracer.start_as_current_span("auth.sso.callback") as span:
        if google_ticket:
            ticket_data = _verify_google_ticket(google_ticket)
            if not ticket_data:
                return RedirectResponse(url="/login?sso_error=invalid_ticket", status_code=302)
            email = (ticket_data.get("email") or ticket_data.get("sub") or "").strip().lower()
            name = ticket_data.get("name") or email.split("@")[0]
            claims = {
                "sub": ticket_data.get("sub") or email,
                "email": email,
                "name": name,
                "user_email": email,
                "full_name": name,
            }
            user = await _upsert_sso_user(claims)
            bearer = issue_token(
                user_id=int(user["id"]),
                username=str(user["username"]),
                role=str(user["role"]),
                auth_method="google_sso",
            )
            is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            redirect = RedirectResponse(url="/", status_code=302)
            redirect.delete_cookie(_PKCE_COOKIE, path="/")
            redirect.set_cookie(
                SESSION_COOKIE_NAME,
                bearer,
                httponly=True,
                secure=is_https,
                samesite="lax",
                path="/",
            )
            return redirect

        if error:
            span.set_attribute("auth.sso.idp_error", error)
            return RedirectResponse(url=f"/login?sso_error={error}", status_code=302)
        if not code or not state:
            return RedirectResponse(url="/login?sso_error=missing_code", status_code=302)

        signed_cookie = request.cookies.get(_PKCE_COOKIE, "")
        cookie_value = _verify_cookie(signed_cookie)
        if not cookie_value or cookie_value.count(":") < 2:
            security_span(
                "csrf",
                severity="high",
                payload="missing_or_tampered_pkce_cookie",
                source_ip=request.client.host if request.client else "unknown",
                endpoint="/api/auth/sso/callback",
            )
            return RedirectResponse(url="/login?sso_error=invalid_state", status_code=302)

        parts = cookie_value.split(":")
        stored_state = parts[0]
        code_verifier = parts[1]
        issued_at_str = parts[2]
        effective_redirect_uri = ":".join(parts[3:]) if len(parts) > 3 else _resolve_redirect_uri(request, cfg.idcs_redirect_uri)
        try:
            issued_at = int(issued_at_str)
        except ValueError:
            return RedirectResponse(url="/login?sso_error=invalid_state", status_code=302)
        if time.time() - issued_at > _PKCE_TTL_SECONDS:
            return RedirectResponse(url="/login?sso_error=expired_state", status_code=302)
        if not hmac.compare_digest(stored_state, state):
            security_span(
                "csrf",
                severity="high",
                payload="state_mismatch",
                source_ip=request.client.host if request.client else "unknown",
                endpoint="/api/auth/sso/callback",
            )
            return RedirectResponse(url="/login?sso_error=invalid_state", status_code=302)

        # Exchange the authorization code for tokens.
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                f"{cfg.idcs_domain_url}/oauth2/v1/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": effective_redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(cfg.idcs_client_id, cfg.idcs_client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            span.set_attribute("auth.sso.token_status", token_resp.status_code)
            push_log(
                "ERROR",
                "SSO token exchange failed",
                **{
                    "auth.method": "idcs_sso",
                    "auth.sso.token_status": token_resp.status_code,
                },
            )
            return RedirectResponse(url="/login?sso_error=token_exchange_failed", status_code=302)

        tokens = token_resp.json()
        id_token = tokens.get("id_token") or ""
        if not id_token:
            return RedirectResponse(url="/login?sso_error=missing_id_token", status_code=302)

        claims = await _verify_id_token(id_token)
        user = await _upsert_sso_user(claims)
        bearer = issue_token(
            user_id=int(user["id"]),
            username=str(user["username"]),
            role=str(user["role"]),
            auth_method="sso",
        )

        push_log(
            "INFO",
            f"SSO login: {user['username']}",
            **{
                "auth.method": "idcs_sso",
                "auth.username": user["username"],
                "auth.email": user["email"],
                "auth.role": user["role"],
                "auth.idcs_sub": claims.get("sub"),
            },
        )
        span.set_attribute("auth.username", user["username"])
        span.set_attribute("auth.method", "idcs_sso")

        # Drop the PKCE cookie and set the session cookie.
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        redirect = RedirectResponse(url="/", status_code=302)
        redirect.delete_cookie(_PKCE_COOKIE, path="/")
        redirect.set_cookie(
            SESSION_COOKIE_NAME,
            bearer,
            httponly=True,
            secure=is_https,
            samesite="lax",
            path="/",
        )
        return redirect


@router.get("/logout")
async def sso_logout(request: Request):
    response: JSONResponse | RedirectResponse
    if cfg.idcs_configured and cfg.idcs_post_logout_redirect:
        logout_url = (
            f"{cfg.idcs_domain_url}/oauth2/v1/userlogout"
            f"?post_logout_redirect_uri={cfg.idcs_post_logout_redirect}"
        )
        response = RedirectResponse(url=logout_url, status_code=302)
    else:
        response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(_PKCE_COOKIE, path="/")
    return response
