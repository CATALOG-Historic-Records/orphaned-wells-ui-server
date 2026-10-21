import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional

import jwt
from jwt import PyJWKClient
import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from ogrre.internal import auth

_log = logging.getLogger(__name__)

CILOGON_JWKS_URI = "https://cilogon.org/oauth2/certs"
CILOGON_ISSUER = "https://cilogon.org"
CILOGON_AUTHORIZE_URI = "https://cilogon.org/authorize"
CILOGON_USERINFO_URI = "https://cilogon.org/oauth2/userinfo"
CILOGON_DEFAULT_SCOPE = "openid email profile org.cilogon.userinfo"
ORCID_PUBLIC_API_BASE_URI = "https://pub.orcid.org/v3.0"
ORCID_ID_PATTERN = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b")
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"

DEFAULT_CILOGON_IDP_OPTIONS = [
    {
        "label": "Lawrence Berkeley National Laboratory",
        "entityId": "urn:mace:incommon:lbl.gov",
        "logo": "/img/idp/lbl.svg",
    },
    {
        "label": "Lawrence Livermore National Laboratory",
        "entityId": "https://eidp.llnl.gov",
        "logo": "/img/idp/llnl.svg",
    },
    {
        "label": "Oak Ridge National Laboratory",
        "entityId": "https://idp.ornl.gov/idp",
        "logo": "/img/idp/ornl.svg",
    },
    {
        "label": "Argonne National Laboratory",
        "entityId": "https://identityprovider.anl.gov/idp/shibboleth",
        "logo": "/img/idp/anl.svg",
    },
    {
        "label": "ORCID",
        "entityId": "http://orcid.org/oauth/authorize",
        "logo": "/img/idp/orcid.svg",
    },
]

DEFAULT_IDP_LOGOS_BY_ENTITY_ID = {
    item["entityId"]: item["logo"]
    for item in DEFAULT_CILOGON_IDP_OPTIONS
    if item.get("logo")
}

CILOGON_IDP_PRESETS = {
    "lbl": DEFAULT_CILOGON_IDP_OPTIONS[0],
    "lbnl": DEFAULT_CILOGON_IDP_OPTIONS[0],
    "llnl": DEFAULT_CILOGON_IDP_OPTIONS[1],
    "ornl": DEFAULT_CILOGON_IDP_OPTIONS[2],
    "anl": DEFAULT_CILOGON_IDP_OPTIONS[3],
    "orcid": DEFAULT_CILOGON_IDP_OPTIONS[4],
    "pnnl": {
        "label": "Pacific Northwest National Laboratory",
        "entityId": "https://sso.pnnl.gov/idp",
        "logo": "/img/idp/pnnl.svg",
    },
    "uiuc": {
        "label": "University of Illinois Urbana-Champaign",
        "entityId": "urn:mace:incommon:uiuc.edu",
    },
    "berkeley": {
        "label": "University of California, Berkeley",
        "entityId": "urn:mace:incommon:berkeley.edu",
    },
    "ucb": {
        "label": "University of California, Berkeley",
        "entityId": "urn:mace:incommon:berkeley.edu",
    },
    "bnl": {
        "label": "Brookhaven National Laboratory",
        "entityId": "https://federation.bnl.gov/adfs/services/trust",
    },
    "fnal": {
        "label": "Fermi National Accelerator Laboratory",
        "entityId": "https://idp.fnal.gov/idp/shibboleth",
    },
    "ames": {
        "label": "Ames National Laboratory",
        "entityId": "https://federation.ameslab.gov/adfs/services/trust",
    },
    "jlab": {
        "label": "Thomas Jefferson National Accelerator Facility",
        "entityId": "https://misportal.jlab.org/idp/shibboleth",
    },
}


def parse_cilogon_idp_options(raw_value: Optional[str] = None) -> list[Dict[str, str]]:
    """Parse optional CILOGON_IDP_OPTIONS env string into [{label, entityId, logo?}] options."""
    if not raw_value or not raw_value.strip():
        return [dict(item) for item in DEFAULT_CILOGON_IDP_OPTIONS]

    def _dedupe(items: list[Dict]) -> list[Dict[str, str]]:
        seen = set()
        result: list[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            entity_id = str(item.get("entityId") or item.get("entity_id") or "").strip()
            raw_logo = item.get("logo") if "logo" in item else item.get("logo_url")
            logo = (
                str(raw_logo).strip()
                if raw_logo is not None
                else DEFAULT_IDP_LOGOS_BY_ENTITY_ID.get(entity_id, "")
            )
            if label and entity_id and entity_id not in seen:
                seen.add(entity_id)
                entry: Dict[str, str] = {"label": label, "entityId": entity_id}
                if logo:
                    entry["logo"] = logo
                result.append(entry)
        return (
            result if result else [dict(item) for item in DEFAULT_CILOGON_IDP_OPTIONS]
        )

    trimmed = raw_value.strip()
    if trimmed.startswith("["):
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, list):
                return _dedupe(parsed)
        except Exception:
            pass

    delimiter = ";" if ";" in trimmed else ","
    tokens = [token.strip() for token in trimmed.split(delimiter) if token.strip()]
    parsed_options: list[Dict[str, str]] = []
    for token in tokens:
        if "|" in token:
            parts = token.split("|", 2)
            label = parts[0].strip()
            entity_id = parts[1].strip() if len(parts) > 1 else ""
            logo = parts[2].strip() if len(parts) > 2 else ""
            if label and entity_id:
                entry: Dict[str, str] = {"label": label, "entityId": entity_id}
                if logo:
                    entry["logo"] = logo
                parsed_options.append(entry)
            continue
        preset = CILOGON_IDP_PRESETS.get(token.lower())
        if preset:
            parsed_options.append(dict(preset))

    return _dedupe(parsed_options)


# Instantiate once and reuse; PyJWKClient caches keys internally.
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

    def is_configured(self) -> bool:
        return False

    def exchange_authorization_code(
        self, code: str, redirect_uri: Optional[str] = None
    ) -> AuthResult:
        raise NotImplementedError()

    def refresh_session(self, refresh_token: str) -> AuthResult:
        raise NotImplementedError()

    def verify_id_token(self, raw_id_token: str) -> Dict:
        raise NotImplementedError()

    def revoke_token(self, refresh_token: str) -> int:
        return 200


class GoogleIdentityProvider(BaseIdentityProvider):
    """
    Google OIDC provider implementation.
    """

    name = "google"

    def __init__(self) -> None:
        (
            self.token_uri,
            self.client_id,
            self.client_secret,
        ) = auth.get_google_credentials()

    def _refresh_credentials_from_env(self) -> None:
        if not (self.token_uri and self.client_id and self.client_secret):
            (
                env_token_uri,
                env_client_id,
                env_client_secret,
            ) = auth.get_google_credentials()
            self.token_uri = self.token_uri or env_token_uri
            self.client_id = self.client_id or env_client_id
            self.client_secret = self.client_secret or env_client_secret

    def is_configured(self) -> bool:
        self._refresh_credentials_from_env()
        return bool(self.token_uri and self.client_id and self.client_secret)

    def _validate_configuration(self):
        self._refresh_credentials_from_env()
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
        self._refresh_credentials_from_env()
        try:
            user_info = id_token.verify_oauth2_token(
                raw_id_token, google_requests.Request(), self.client_id
            )
            user_info["email"] = (user_info.get("email") or "").strip().lower()
            user_info["provider"] = self.name
            return user_info
        except Exception as e:
            raise IdentityProviderError(f"unable to authenticate: {e}", status_code=401)

    def exchange_authorization_code(
        self, code: str, redirect_uri: Optional[str] = None
    ) -> AuthResult:
        self._validate_configuration()
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri or "postmessage",
            "grant_type": "authorization_code",
        }
        response = requests.post(self.token_uri, data=data, timeout=15)
        if response.status_code != 200:
            _log.info(f"token exchange failed: {response.status_code}")
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
        response = requests.post(self.token_uri, data=data, timeout=15)
        if response.status_code != 200:
            _log.info(f"token refresh failed: {response.status_code}")
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

    def revoke_token(self, refresh_token: str) -> int:
        if not refresh_token:
            return 200
        try:
            response = requests.post(
                GOOGLE_REVOKE_URI,
                params={"token": refresh_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            return response.status_code
        except Exception as e:
            _log.warning(f"failed to revoke Google token: {e}")
            return 200


class CilogonIdentityProvider(BaseIdentityProvider):
    """
    CILogon OIDC provider implementation.

    - Exchanges authorization codes against the CILogon token endpoint.
    - Verifies ID token issuer/audience against CILogon JWKS keys.
    - Enriches user claims (email, name, eppn, idp_name) from CILogon's
      /oauth2/userinfo endpoint when not embedded directly in the ID token.
    """

    name = "cilogon"

    def __init__(self) -> None:
        (
            self.token_uri,
            self.client_id,
            self.client_secret,
        ) = auth.get_cilogon_credentials()
        self.userinfo_uri = os.getenv("cilogon_userinfo_uri") or CILOGON_USERINFO_URI
        self.authorize_uri = os.getenv("cilogon_authorize_uri") or CILOGON_AUTHORIZE_URI
        self.redirect_uri = os.getenv("cilogon_redirect_uri")

    def _refresh_credentials_from_env(self) -> None:
        if not (self.token_uri and self.client_id and self.client_secret):
            (
                env_token_uri,
                env_client_id,
                env_client_secret,
            ) = auth.get_cilogon_credentials()
            self.token_uri = self.token_uri or env_token_uri
            self.client_id = self.client_id or env_client_id
            self.client_secret = self.client_secret or env_client_secret
        if not self.redirect_uri:
            self.redirect_uri = os.getenv("cilogon_redirect_uri")

    def is_configured(self) -> bool:
        self._refresh_credentials_from_env()
        return bool(self.token_uri and self.client_id and self.client_secret)

    def _validate_configuration(self):
        self._refresh_credentials_from_env()
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
        self._refresh_credentials_from_env()
        if not self.client_id:
            raise IdentityProviderError(
                "identity provider is misconfigured. missing: client_id",
                status_code=500,
            )
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(raw_id_token)
            user_info = jwt.decode(
                raw_id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=CILOGON_ISSUER,
            )
            user_info["email"] = (user_info.get("email") or "").strip().lower()
            user_info["provider"] = self.name
            return user_info
        except Exception as e:
            raise IdentityProviderError(f"unable to authenticate: {e}", status_code=401)

    def fetch_userinfo(self, access_token: str) -> Dict:
        if not access_token:
            return {}
        try:
            response = requests.post(
                self.userinfo_uri,
                data={"access_token": access_token},
                timeout=15,
            )
            if response.status_code != 200:
                _log.info(
                    f"CILogon userinfo request returned status {response.status_code}"
                )
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            _log.warning(f"failed to fetch CILogon userinfo: {e}")
            return {}

    @staticmethod
    def _merge_userinfo_claims(user_info: Dict, extra_claims: Dict) -> Dict:
        if not extra_claims:
            return user_info

        email = (
            (extra_claims.get("email") or user_info.get("email") or "").strip().lower()
        )
        if email:
            user_info["email"] = email

        name = (extra_claims.get("name") or user_info.get("name") or "").strip()
        if not name:
            given = (extra_claims.get("given_name") or "").strip()
            family = (extra_claims.get("family_name") or "").strip()
            name = f"{given} {family}".strip()
        if name:
            user_info["name"] = name

        for claim_key in (
            "given_name",
            "family_name",
            "eppn",
            "idp",
            "idp_name",
            "subject_id",
            "oidc",
            "eduPersonOrcid",
        ):
            val = extra_claims.get(claim_key)
            if val and not user_info.get(claim_key):
                user_info[claim_key] = val

        return user_info

    @staticmethod
    def _extract_orcid_id(user_info: Dict) -> Optional[str]:
        candidates = []
        for key in ("eduPersonOrcid", "oidc", "eppn", "subject_id", "sub"):
            raw_val = user_info.get(key)
            if isinstance(raw_val, (list, tuple)):
                candidates.extend(str(item) for item in raw_val if item)
            elif raw_val:
                candidates.append(str(raw_val))

        for candidate in candidates:
            match = ORCID_ID_PATTERN.search(candidate)
            if match:
                return match.group(1)
        return None

    def fetch_orcid_public_emails(self, orcid_id: str) -> list[str]:
        if not orcid_id:
            return []
        try:
            response = requests.get(
                f"{ORCID_PUBLIC_API_BASE_URI}/{orcid_id}/email",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if response.status_code != 200:
                _log.info(
                    f"ORCID public email lookup returned status {response.status_code}"
                )
                return []
            data = response.json()
            raw_emails = data.get("email") if isinstance(data, dict) else []
            if not isinstance(raw_emails, list):
                return []

            valid_entries = [
                entry
                for entry in raw_emails
                if isinstance(entry, dict) and str(entry.get("email") or "").strip()
            ]
            valid_entries.sort(
                key=lambda entry: (
                    not bool(entry.get("verified", True)),
                    not bool(entry.get("primary", False)),
                )
            )
            emails: list[str] = []
            for entry in valid_entries:
                normalized = str(entry.get("email") or "").strip().lower()
                if normalized and normalized not in emails:
                    emails.append(normalized)
            return emails
        except Exception as e:
            _log.warning(f"failed to fetch ORCID public emails for {orcid_id}: {e}")
            return []

    def _enrich_orcid_email_if_missing(self, user_info: Dict) -> None:
        if user_info.get("email"):
            return
        orcid_id = self._extract_orcid_id(user_info)
        if not orcid_id:
            return
        user_info["orcid"] = orcid_id
        orcid_emails = self.fetch_orcid_public_emails(orcid_id)
        if orcid_emails:
            user_info["email"] = orcid_emails[0]
            user_info["orcid_emails"] = orcid_emails

    def exchange_authorization_code(
        self, code: str, redirect_uri: Optional[str] = None
    ) -> AuthResult:
        self._validate_configuration()
        effective_redirect_uri = redirect_uri or self.redirect_uri or "postmessage"
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": effective_redirect_uri,
            "grant_type": "authorization_code",
        }
        response = requests.post(self.token_uri, data=data, timeout=15)
        if response.status_code != 200:
            _log.info(f"token exchange failed: {response.status_code}")
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

        access_token = user_tokens.get("access_token", "")
        if access_token and (not user_info.get("email") or not user_info.get("name")):
            userinfo_claims = self.fetch_userinfo(access_token)
            self._merge_userinfo_claims(user_info, userinfo_claims)
        self._enrich_orcid_email_if_missing(user_info)

        return AuthResult(
            id_token=raw_id_token,
            access_token=access_token,
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
        response = requests.post(self.token_uri, data=data, timeout=15)
        if response.status_code != 200:
            _log.info(f"token refresh failed: {response.status_code}")
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_tokens = response.json()
        raw_id_token = user_tokens.get("id_token")
        if not raw_id_token:
            raise IdentityProviderError("unable to authenticate", status_code=401)

        user_info = self.verify_id_token(raw_id_token)
        access_token = user_tokens.get("access_token", "")
        if access_token and not user_info.get("email"):
            userinfo_claims = self.fetch_userinfo(access_token)
            self._merge_userinfo_claims(user_info, userinfo_claims)
        self._enrich_orcid_email_if_missing(user_info)

        next_refresh = user_tokens.get("refresh_token") or refresh_token
        return AuthResult(
            id_token=raw_id_token,
            access_token=access_token,
            refresh_token=next_refresh,
            user_info=user_info,
            provider=self.name,
        )

    def get_idp_options(self) -> list[Dict[str, str]]:
        raw_options = (
            os.getenv("CILOGON_IDP_OPTIONS")
            or os.getenv("cilogon_idp_options")
            or os.getenv("REACT_APP_CILOGON_IDP_OPTIONS")
        )
        return parse_cilogon_idp_options(raw_options)


class MultiIdentityProvider(BaseIdentityProvider):
    """
    Dispatcher that supports both Google and CILogon authentication flows
    simultaneously while preserving the BaseIdentityProvider interface.
    """

    def __init__(
        self,
        default_provider: str = "google",
        google_provider: Optional[GoogleIdentityProvider] = None,
        cilogon_provider: Optional[CilogonIdentityProvider] = None,
    ) -> None:
        self.default_provider = (
            default_provider if default_provider in {"google", "cilogon"} else "google"
        )
        self.name = self.default_provider
        self.google = google_provider or GoogleIdentityProvider()
        self.cilogon = cilogon_provider or CilogonIdentityProvider()

    def get_provider(self, provider_name: Optional[str] = None) -> BaseIdentityProvider:
        normalized = (
            (provider_name or self.default_provider or "google").strip().lower()
        )
        if normalized == "google":
            return self.google
        if normalized == "cilogon":
            return self.cilogon
        raise IdentityProviderError(
            f"unsupported auth provider: {normalized}", status_code=400
        )

    def _detect_provider_from_token(
        self, raw_id_token: str
    ) -> Optional[BaseIdentityProvider]:
        try:
            parts = (raw_id_token or "").split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                unverified = json.loads(
                    base64.urlsafe_b64decode(padded).decode("utf-8")
                )
                issuer = (unverified.get("iss") or "").strip()
                if issuer == CILOGON_ISSUER or issuer.endswith(".cilogon.org"):
                    return self.cilogon
                if issuer in GOOGLE_ISSUERS:
                    return self.google
        except Exception:
            pass
        return None

    def verify_id_token(
        self, raw_id_token: str, provider: Optional[str] = None
    ) -> Dict:
        if provider:
            return self.get_provider(provider).verify_id_token(raw_id_token)
        detected = self._detect_provider_from_token(raw_id_token)
        if detected is not None:
            return detected.verify_id_token(raw_id_token)
        return self.get_provider(None).verify_id_token(raw_id_token)

    def exchange_authorization_code(
        self,
        code: str,
        redirect_uri: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> AuthResult:
        target = self.get_provider(provider)
        return target.exchange_authorization_code(code, redirect_uri=redirect_uri)

    def refresh_session(
        self, refresh_token: str, provider: Optional[str] = None
    ) -> AuthResult:
        target = self.get_provider(provider)
        return target.refresh_session(refresh_token)

    def revoke_token(self, refresh_token: str, provider: Optional[str] = None) -> int:
        target = self.get_provider(provider)
        return target.revoke_token(refresh_token)

    def get_enabled_providers(self) -> Dict:
        return {
            "google": {
                "enabled": self.google.is_configured(),
                "client_id": self.google.client_id or "",
            },
            "cilogon": {
                "enabled": self.cilogon.is_configured(),
                "client_id": self.cilogon.client_id or "",
                "authorize_uri": self.cilogon.authorize_uri,
                "scope": CILOGON_DEFAULT_SCOPE,
                "idp_options": self.cilogon.get_idp_options(),
            },
        }


def build_identity_provider() -> MultiIdentityProvider:
    provider_name = (os.getenv("AUTH_PROVIDER", "google") or "google").strip().lower()
    if provider_name not in {"google", "cilogon", "both"}:
        raise IdentityProviderError(f"unsupported auth provider: {provider_name}", 500)
    default_provider = "cilogon" if provider_name == "cilogon" else "google"
    return MultiIdentityProvider(default_provider=default_provider)


def get_bearer_or_session_token(request, bearer_token: Optional[str]) -> Optional[str]:
    session_token = request.cookies.get("ogrre_session")
    if session_token:
        return session_token
    if bearer_token and bearer_token not in {"null", "undefined", "None"}:
        return bearer_token
    return None
