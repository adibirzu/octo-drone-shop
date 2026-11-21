import importlib
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from server import config
from server.auth_security import SESSION_COOKIE_NAME
from server.modules import sso

pytestmark = [pytest.mark.unit, pytest.mark.security]
DOMAIN = "https://idcs-example.identity.oraclecloud.com:443"
OCI_ISSUER = "https://identity.oraclecloud.com/"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idcs(request, monkeypatch, app_factory, signing_key):
    domain, discovery, override = getattr(request, "param", (DOMAIN, OCI_ISSUER, ""))
    with monkeypatch.context() as env:
        env.setenv("IDCS_DOMAIN_URL", f"{domain}/")
        env.setenv("IDCS_ISSUER", override)
        env.setenv("IDCS_VERIFY_JWT", "true")
        env.setenv("IDCS_CLIENT_ID", uuid4().hex)
        env.setenv("IDCS_CLIENT_SECRET", uuid4().hex)
        env.setenv("IDCS_REDIRECT_URI", "https://shop.example.test/api/auth/sso/callback")
        settings = importlib.reload(config).Config()
    importlib.reload(config)

    monkeypatch.setattr(sso, "cfg", settings)
    monkeypatch.setattr(sso, "_JWKS_CACHE", {})
    monkeypatch.setattr(sso, "_ISSUER_CACHE", {}, raising=False)
    monkeypatch.setenv("AUTH_TOKEN_SECRET", uuid4().hex)
    monkeypatch.setattr(sso, "push_log", lambda *args, **kwargs: None)
    upsert = AsyncMock(return_value={"id": 1, "username": "customer", "email": "", "role": "customer"})
    monkeypatch.setattr(sso, "_upsert_sso_user", upsert)

    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk["kid"] = "signing-key"

    with (
        respx.mock(assert_all_called=False) as provider,
        TestClient(app_factory(sso.router), base_url="https://shop.example.test") as client,
    ):
        metadata = provider.get(f"{domain}/.well-known/openid-configuration").respond(
            200, json={"issuer": discovery}
        )
        provider.get(f"{domain}/admin/v1/SigningCert/jwk").respond(200, json={"keys": [jwk]})
        exchange = provider.post(f"{domain}/oauth2/v1/token")

        def callback(changes=None):
            now = int(time.time())
            claims = {
                "sub": "customer",
                "iss": discovery,
                "aud": settings.idcs_client_id,
                "iat": now - 60,
                "exp": now + 60,
            }
            claims.update(changes or {})
            token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": jwk["kid"]})
            exchange.respond(200, json={"id_token": token})
            login = client.get("/api/auth/sso/login", follow_redirects=False)
            assert login.status_code == 302
            state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
            return client.get(
                "/api/auth/sso/callback",
                params={"code": "authorization-code", "state": state},
                follow_redirects=False,
            )

        yield callback, metadata, upsert


@pytest.mark.parametrize("idcs", [(DOMAIN, f"{DOMAIN}/", "")], indirect=True)
def test_callback_accepts_canonical_oci_issuer_and_caches_discovery(idcs):
    callback, metadata, _ = idcs
    for _ in range(2):
        response = callback({"iss": OCI_ISSUER})
        assert response.status_code == 302, response.text
        assert response.headers["location"] == "/"
        assert SESSION_COOKIE_NAME in response.cookies
    assert metadata.call_count == 1


@pytest.mark.parametrize(
    "idcs",
    [(DOMAIN, "https://issuer.example.test:443/tenant/", "")],
    indirect=True,
)
def test_callback_matches_discovery_issuer_across_default_port_and_slash(idcs):
    callback, _, _ = idcs
    response = callback({"iss": "https://issuer.example.test/tenant"})
    assert response.status_code == 302, response.text


@pytest.mark.parametrize(
    "idcs", [(DOMAIN, OCI_ISSUER, "https://override.example.test/")], indirect=True
)
def test_callback_uses_only_the_explicit_issuer_override(idcs):
    callback, metadata, _ = idcs
    response = callback({"iss": "https://override.example.test/"})
    assert response.status_code == 302, response.text
    assert callback().status_code == 401
    assert metadata.call_count == 0


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.example/"},
        {"aud": "different-audience"},
        {"exp": 1},
        {"iat": "invalid"},
    ],
    ids=["issuer", "audience", "expiry", "issued-at"],
)
def test_callback_rejects_invalid_claims(idcs, claims):
    callback, _, upsert = idcs
    response = callback(claims)
    assert response.status_code == 401, response.text
    assert SESSION_COOKIE_NAME not in response.cookies
    upsert.assert_not_awaited()


@pytest.mark.parametrize(
    "idcs",
    [("https://tenant.identity.oraclecloud.com.example.test", "https://issuer.example.test/", "")],
    indirect=True,
)
def test_callback_limits_canonical_oci_issuer_to_identity_domains(idcs):
    callback, _, _ = idcs
    response = callback({"iss": OCI_ISSUER})
    assert response.status_code == 401, response.text


def test_callback_uses_trusted_defaults_and_retries_when_discovery_is_unavailable(idcs):
    callback, metadata, _ = idcs
    metadata.respond(503)
    response = callback({"iss": DOMAIN.removesuffix(":443") + "/"})
    assert response.status_code == 302, response.text
    metadata.respond(200, json={"issuer": "https://issuer.example.test/"})
    response = callback({"iss": "https://issuer.example.test/"})
    assert response.status_code == 302, response.text
