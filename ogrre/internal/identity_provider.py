import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
import jwt
from jwt import PyJWKClient

from ogrre.internal import auth

_log = logging.getLogger(__name__)

CILOGON_JWKS_URI = "https://cilogon.org/oauth2/certs"
CILOGON_ISSUER = "https://cilogon.org"

# Ideally instantiate this once and reuse it — it caches keys internally
jwks_client = PyJWKClient(CILOGON_JWKS_URI)


@dataclass
class AuthResult:
    """
    Normalized authentication result returned by identity providers.
    This is the same for all providers.
    """

    id_token: str
    access_token: str
    refresh_token: str
    user_info: Dict
    provider: str


class IdentityProviderError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


class BaseIdentityProvider:
    name = "base"

    def exchange_authorization_code(self, code: str) -> AuthResult:
        raise NotImplementedError()

    def refresh_session(self, refresh_token: str) -> AuthResult:
        raise NotImplementedError()

    def verify_id_token(self, raw_id_token: str) -> Dict:
        raise NotImplementedError()


class GoogleIdentityProvider(BaseIdentityProvider):
    """
    Google OIDC provider implementation.

    FUTURE EXTENSION (CILogon):
    - Add `CilogonIdentityProvider(BaseIdentityProvider)`.
    - Implement token exchange against CILogon token endpoint.
    - Verify token issuer/audience according to CILogon OIDC metadata.
    - Keep `user_info["email"]` normalization plus subject (`sub`) mapping.
    """

    name = "google"

    def __init__(self) -> None:
        (
            self.token_uri,
            self.client_id,
            self.client_secret,
        ) = auth.get_google_credentials()

    def _validate_configuration(self):
        missing = []
        if not self.token_uri:
            missing.append("token_uri")
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if missing:
            raise IdentityProviderError(
                f"identity provider is misconfigured. missing: {', '.join(missing)}",
                status_code=500,
            )

    def verify_id_token(self, raw_id_token: str) -> Dict:
        try:
            user_info = id_token.verify_oauth2_token(
                raw_id_token, google_requests.Request(), self.client_id
            )
            user_info["email"] = user_info.get("email", "").lower()
            return user_info
        except Exception as e:
            raise IdentityProviderError(f"unable to authenticate: {e}", status_code=401)

    def exchange_authorization_code(self, code: str) -> AuthResult:
        self._validate_configuration()
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": "postmessage",
            "grant_type": "authorization_code",
        }
        response = requests.post(self.token_uri, data=data)
        if response.status_code != 200:
            _log.info(f"token exchange failed: {response.status_code} {response.text}")
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_tokens = response.json()
        raw_id_token = user_tokens.get("id_token")
        if not raw_id_token:
            raise IdentityProviderError("unable to authenticate", status_code=401)

        try:
            user_info = self.verify_id_token(raw_id_token)
        except IdentityProviderError as first_error:
            _log.info(f"unable to authenticate on 1st try: {first_error}")
            _log.info("waiting 2 seconds before retry")
            time.sleep(2.5)
            user_info = self.verify_id_token(raw_id_token)

        return AuthResult(
            id_token=raw_id_token,
            access_token=user_tokens.get("access_token", ""),
            refresh_token=user_tokens.get("refresh_token", ""),
            user_info=user_info,
            provider=self.name,
        )

    def refresh_session(self, refresh_token: str) -> AuthResult:
        self._validate_configuration()
        data = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }
        response = requests.post(self.token_uri, data=data)
        if response.status_code != 200:
            _log.info(f"token refresh failed: {response.status_code} {response.text}")
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_tokens = response.json()
        raw_id_token = user_tokens.get("id_token")
        if not raw_id_token:
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_info = self.verify_id_token(raw_id_token)
        # Google may not rotate refresh tokens during refresh flow.
        next_refresh = user_tokens.get("refresh_token") or refresh_token
        return AuthResult(
            id_token=raw_id_token,
            access_token=user_tokens.get("access_token", ""),
            refresh_token=next_refresh,
            user_info=user_info,
            provider=self.name,
        )


class CilogonIdentityProvider(BaseIdentityProvider):
    """
    Cilogon OIDC provider implementation.

    - Implement token exchange against CILogon token endpoint.
    - Verify token issuer/audience according to CILogon OIDC metadata.
    - Keep `user_info["email"]` normalization plus subject (`sub`) mapping.
    """

    name = "cilogon"

    def __init__(self) -> None:
        (
            self.token_uri,
            self.client_id,
            self.client_secret,
        ) = auth.get_cilogon_credentials()

    def _validate_configuration(self):
        missing = []
        if not self.token_uri:
            missing.append("token_uri")
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if missing:
            raise IdentityProviderError(
                f"identity provider is misconfigured. missing: {', '.join(missing)}",
                status_code=500,
            )

    def verify_id_token(raw_id_token: str, client_id: str) -> dict:
        signing_key = jwks_client.get_signing_key_from_jwt(raw_id_token)
        user_info = jwt.decode(
            raw_id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=CILOGON_ISSUER,
        )
        user_info["email"] = user_info.get("email", "").lower()
        return user_info

    def exchange_authorization_code(self, code: str) -> AuthResult:
        self._validate_configuration()
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": "postmessage",
            "grant_type": "authorization_code",
        }
        response = requests.post(self.token_uri, data=data)
        if response.status_code != 200:
            _log.info(f"token exchange failed: {response.status_code} {response.text}")
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_tokens = response.json()
        raw_id_token = user_tokens.get("id_token")
        if not raw_id_token:
            raise IdentityProviderError("unable to authenticate", status_code=401)

        try:
            user_info = self.verify_id_token(raw_id_token)
        except IdentityProviderError as first_error:
            _log.info(f"unable to authenticate on 1st try: {first_error}")
            _log.info("waiting 2 seconds before retry")
            time.sleep(2.5)
            user_info = self.verify_id_token(raw_id_token)

        return AuthResult(
            id_token=raw_id_token,
            access_token=user_tokens.get("access_token", ""),
            refresh_token=user_tokens.get("refresh_token", ""),
            user_info=user_info,
            provider=self.name,
        )

    def refresh_session(self, refresh_token: str) -> AuthResult:
        self._validate_configuration()
        data = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }
        response = requests.post(self.token_uri, data=data)
        if response.status_code != 200:
            _log.info(f"token refresh failed: {response.status_code} {response.text}")
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_tokens = response.json()
        raw_id_token = user_tokens.get("id_token")
        if not raw_id_token:
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_info = self.verify_id_token(raw_id_token)
        next_refresh = user_tokens.get("refresh_token") or refresh_token
        return AuthResult(
            id_token=raw_id_token,
            access_token=user_tokens.get("access_token", ""),
            refresh_token=next_refresh,
            user_info=user_info,
            provider=self.name,
        )


def build_identity_provider() -> BaseIdentityProvider:
    provider_name = (os.getenv("AUTH_PROVIDER", "google") or "google").lower()
    if provider_name == "google":
        return GoogleIdentityProvider()
    elif provider_name == "cilogon":
        return CilogonIdentityProvider()

    # NOTE: this is intentional to make provider onboarding explicit/safe.
    # Add a new class and extend this switch when enabling CILogon or others.
    raise IdentityProviderError(f"unsupported auth provider: {provider_name}", 500)


def get_bearer_or_session_token(request, bearer_token: Optional[str]) -> Optional[str]:
    session_token = request.cookies.get("ogrre_session")
    if session_token:
        return session_token
    if bearer_token and bearer_token not in {"null", "undefined", "None"}:
        return bearer_token
    return None
