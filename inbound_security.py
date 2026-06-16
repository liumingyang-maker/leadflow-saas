from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

INBOUND_TOKEN_PREFIX = "in_"
INBOUND_TOKEN_BYTES = 32
HONEYPOT_FIELD = "_website"

ALLOWED_FIELDS = {
    "name": 120,
    "email": 254,
    "phone": 64,
    "company": 200,
    "message": 5000,
    "source": 100,
    "page_url": 2048,
    "referrer": 2048,
}

FIELD_ALIASES = {
    "name": ("name", "Name", "fullname", "contact_name", "your-name"),
    "email": ("email", "Email", "e-mail", "mail", "your-email"),
    "phone": ("phone", "Phone", "tel", "mobile", "whatsapp"),
    "company": ("company", "Company", "company_name", "organization"),
    "message": (
        "message",
        "Message",
        "msg",
        "comment",
        "comments",
        "inquiry",
        "content",
        "your-message",
    ),
    "source": ("source", "Source"),
    "page_url": ("page_url", "pageUrl", "url", "website", "Website", "site"),
    "referrer": ("referrer", "referer"),
}


class InboundValidationError(ValueError):
    pass


@dataclass(frozen=True)
class InboundPayload:
    data: dict[str, str]
    fingerprint: str


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def max_body_bytes() -> int:
    return _env_int("INBOUND_MAX_BODY_BYTES", 32 * 1024, minimum=1, maximum=256 * 1024)


def token_ip_limit() -> int:
    return _env_int("INBOUND_RATE_TOKEN_IP_LIMIT", 60)


def tenant_limit() -> int:
    return _env_int("INBOUND_RATE_TENANT_LIMIT", 300)


def rate_window_seconds() -> int:
    return _env_int("INBOUND_RATE_WINDOW_SECONDS", 60)


def idempotency_ttl_seconds() -> int:
    return _env_int("INBOUND_IDEMPOTENCY_TTL_SECONDS", 24 * 60 * 60)


def fingerprint_ttl_seconds() -> int:
    return _env_int("INBOUND_FINGERPRINT_TTL_SECONDS", 5 * 60)


def generate_inbound_token() -> str:
    return INBOUND_TOKEN_PREFIX + secrets.token_urlsafe(INBOUND_TOKEN_BYTES)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secret_digest(value: str, key_material: str) -> str:
    return hmac.new(key_material.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_origin(origin: str | None) -> str:
    if not origin:
        return ""
    origin = origin.strip()
    if origin in {"null", "file://"}:
        return ""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def parse_allowed_origins(value) -> set[str]:
    if isinstance(value, str):
        parts = re.split(r"[\s,]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = []
    return {origin for item in parts if (origin := normalize_origin(item))}


def is_origin_allowed(origin: str | None, allowed_origins: set[str]) -> tuple[bool, str]:
    if not origin:
        return True, ""
    normalized = normalize_origin(origin)
    if not normalized:
        return False, ""
    return normalized in allowed_origins, normalized


def normalize_idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key:
        return ""
    if len(key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise InboundValidationError("invalid idempotency key")
    return key


def _first_value(data: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = data.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _has_forbidden_control_chars(value: str) -> bool:
    return any(ord(char) < 32 and char not in "\r\n\t" for char in value)


def _validate_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise InboundValidationError("invalid url") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InboundValidationError("invalid url")
    return value


def normalize_payload(raw: dict) -> InboundPayload:
    if not isinstance(raw, dict):
        raise InboundValidationError("invalid payload")
    normalized: dict[str, str] = {}
    for field, max_len in ALLOWED_FIELDS.items():
        value = _first_value(raw, FIELD_ALIASES[field])
        if _has_forbidden_control_chars(value):
            raise InboundValidationError("invalid control character")
        if len(value) > max_len:
            raise InboundValidationError(f"{field} too long")
        normalized[field] = value

    email = normalized["email"]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise InboundValidationError("invalid email")
    if not email and not normalized["phone"]:
        raise InboundValidationError("email or phone required")
    normalized["page_url"] = _validate_url(normalized["page_url"])
    normalized["referrer"] = _validate_url(normalized["referrer"])
    if not normalized["company"]:
        normalized["company"] = normalized["name"] or (
            email.split("@")[0] if email else "Website visitor"
        )

    fingerprint = stable_digest(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return InboundPayload(data=normalized, fingerprint=fingerprint)
