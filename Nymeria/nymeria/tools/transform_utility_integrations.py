"""Local transform utility tools."""

from __future__ import annotations

import base64
import calendar
import gzip
import hashlib
import hmac
import io
import json
import logging
import secrets
import string
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    credential_value as _credential_value,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 1_000_000
_MAX_ARCHIVE_BYTES = 8_000_000
_MAX_ZIP_FILES = 50
_MAX_RANDOM_LENGTH = 4096
_TOTP_DIGIT_RANGE = (6, 10)
_TOTP_PERIOD_RANGE = (10, 300)
_HASH_ALIASES = {
    "md5": "md5",
    "sha1": "sha1",
    "sha224": "sha224",
    "sha256": "sha256",
    "sha384": "sha384",
    "sha512": "sha512",
    "sha3-256": "sha3_256",
    "sha3_256": "sha3_256",
    "sha3-384": "sha3_384",
    "sha3_384": "sha3_384",
    "sha3-512": "sha3_512",
    "sha3_512": "sha3_512",
}


def _dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _json_object(value: str, *, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _secret_value(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    field_names: tuple[str, ...],
    settings_name: str,
    env_var: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
) -> str | None:
    value = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_name)
    if value:
        return value
    return _setup_hint(
        provider=provider,
        field_names=field_names,
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )


def _totp_secret(tool_name: str, config: Optional[RunnableConfig]) -> str | None:
    return _secret_value(
        provider="totp",
        provider_aliases=("totp_api", "otp"),
        field_names=("secret", "totp_secret", "totpSecret", "value"),
        settings_name="totp_secret",
        env_var="TOTP_SECRET",
        tool_name=tool_name,
        display_name="TOTP",
        config=config,
    )


def _totp_key(secret: str) -> bytes:
    normalized = "".join(str(secret).strip().split()).upper()
    if not normalized:
        raise ValueError("TOTP secret is empty")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:
        raise ValueError("TOTP secret must be base32 encoded") from exc


def _totp_params(period: int, digits: int, algorithm: str) -> tuple[int, int, str]:
    period = int(period)
    digits = int(digits)
    if not (_TOTP_PERIOD_RANGE[0] <= period <= _TOTP_PERIOD_RANGE[1]):
        raise ValueError(f"period must be between {_TOTP_PERIOD_RANGE[0]} and {_TOTP_PERIOD_RANGE[1]} seconds")
    if not (_TOTP_DIGIT_RANGE[0] <= digits <= _TOTP_DIGIT_RANGE[1]):
        raise ValueError(f"digits must be between {_TOTP_DIGIT_RANGE[0]} and {_TOTP_DIGIT_RANGE[1]}")
    normalized_algorithm = algorithm.strip().lower().replace("-", "")
    if normalized_algorithm not in {"sha1", "sha256", "sha512"}:
        raise ValueError("algorithm must be sha1, sha256, or sha512")
    return period, digits, normalized_algorithm


def _totp_code(secret: str, *, timestamp: int, period: int, digits: int, algorithm: str) -> str:
    key = _totp_key(secret)
    counter = int(timestamp // period)
    digest = hmac.new(key, counter.to_bytes(8, "big"), algorithm).digest()
    offset = digest[-1] & 0x0F
    code_int = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def _zone(timezone_name: str) -> ZoneInfo:
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc


def _parse_datetime(value: str, *, timezone_name: str = "UTC") -> datetime:
    zone = _zone(timezone_name)
    if not value or value.strip().lower() == "now":
        return datetime.now(zone)
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError as exc:
            raise ValueError("date_value must be ISO 8601, a Unix timestamp, or 'now'") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _month_adjusted(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _adjust_datetime(value: datetime, unit: str, amount: float) -> datetime:
    normalized = unit.strip().lower().rstrip("s")
    if normalized in {"year", "month"} and not float(amount).is_integer():
        raise ValueError("year and month adjustments must be whole numbers")
    if normalized == "year":
        return _month_adjusted(value, int(amount) * 12)
    if normalized == "month":
        return _month_adjusted(value, int(amount))
    if normalized == "week":
        return value + timedelta(weeks=amount)
    if normalized == "day":
        return value + timedelta(days=amount)
    if normalized == "hour":
        return value + timedelta(hours=amount)
    if normalized == "minute":
        return value + timedelta(minutes=amount)
    if normalized == "second":
        return value + timedelta(seconds=amount)
    raise ValueError("unit must be years, months, weeks, days, hours, minutes, or seconds")


def _floor_datetime(value: datetime, unit: str) -> datetime:
    normalized = unit.strip().lower().rstrip("s")
    if normalized == "year":
        return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if normalized == "month":
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if normalized == "week":
        start = value - timedelta(days=value.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if normalized == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if normalized == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if normalized == "minute":
        return value.replace(second=0, microsecond=0)
    if normalized == "second":
        return value.replace(microsecond=0)
    raise ValueError("unit must be year, month, week, day, hour, minute, or second")


def _format_datetime(value: datetime, output_format: str) -> str:
    fmt = (output_format or "iso").strip()
    if fmt == "iso":
        return value.isoformat()
    if fmt == "date":
        return value.date().isoformat()
    if fmt == "time":
        return value.timetz().isoformat()
    if fmt == "timestamp":
        return str(value.timestamp())
    if fmt == "rfc2822":
        from email.utils import format_datetime

        return format_datetime(value)
    return value.strftime(fmt)


def _hash_algorithm(name: str) -> str:
    normalized = name.strip().lower().replace(" ", "").replace("_", "-")
    algorithm = _HASH_ALIASES.get(normalized)
    if not algorithm:
        raise ValueError(f"unsupported hash algorithm: {name}")
    return algorithm


def _encode_bytes(data: bytes, encoding: str) -> str:
    normalized = (encoding or "hex").strip().lower()
    if normalized == "hex":
        return data.hex()
    if normalized == "base64":
        return base64.b64encode(data).decode("ascii")
    raise ValueError("encoding must be hex or base64")


def _hashlib_digest(text: str, algorithm: str, *, secret: bytes | None = None) -> bytes:
    name = _hash_algorithm(algorithm)
    data = text.encode("utf-8")
    if secret is not None:
        return hmac.new(secret, data, name).digest()
    digest = hashlib.new(name)
    digest.update(data)
    return digest.digest()


def _private_key(tool_name: str, config: Optional[RunnableConfig]) -> tuple[Any, str | None]:
    from cryptography.hazmat.primitives import serialization

    key_value = _secret_value(
        provider="crypto",
        provider_aliases=("crypto_credentials",),
        field_names=("sign_private_key", "signPrivateKey", "private_key", "privateKey"),
        settings_name="crypto_sign_private_key",
        env_var="CRYPTO_SIGN_PRIVATE_KEY",
        tool_name=tool_name,
        display_name="Crypto",
        config=config,
    )
    if key_value and key_value.startswith("[Error]:"):
        return None, key_value
    passphrase = _credential_value(
        provider="crypto",
        provider_aliases=("crypto_credentials",),
        field_names=("passphrase", "private_key_passphrase", "privateKeyPassphrase"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("crypto_sign_private_key_passphrase")
    try:
        key = serialization.load_pem_private_key(
            str(key_value).encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
    except Exception as exc:
        raise ValueError(f"private key could not be loaded: {exc}") from exc
    return key, None


def _crypto_hash_algorithm(name: str):
    from cryptography.hazmat.primitives import hashes

    normalized = _hash_algorithm(name)
    mapping = {
        "sha1": hashes.SHA1,
        "sha224": hashes.SHA224,
        "sha256": hashes.SHA256,
        "sha384": hashes.SHA384,
        "sha512": hashes.SHA512,
        "sha3_256": hashes.SHA3_256,
        "sha3_384": hashes.SHA3_384,
        "sha3_512": hashes.SHA3_512,
    }
    if normalized == "md5":
        raise ValueError("md5 is not supported for private-key signing")
    return mapping[normalized]()


def _jwt_algorithm(
    *,
    tool_name: str,
    config: Optional[RunnableConfig],
    requested: str = "",
) -> str:
    value = (
        requested.strip()
        or _credential_value(
            provider="jwt",
            provider_aliases=("jwt_auth", "jwtAuth"),
            field_names=("algorithm", "alg"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("jwt_algorithm")
        or "HS256"
    )
    return value.upper()


def _jwt_key(
    *,
    algorithm: str,
    purpose: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> str | None:
    if algorithm.startswith("HS"):
        return _secret_value(
            provider="jwt",
            provider_aliases=("jwt_auth", "jwtAuth"),
            field_names=("secret", "key", "value"),
            settings_name="jwt_secret",
            env_var="JWT_SECRET",
            tool_name=tool_name,
            display_name="JWT",
            config=config,
        )
    if purpose == "sign":
        return _secret_value(
            provider="jwt",
            provider_aliases=("jwt_auth", "jwtAuth"),
            field_names=("private_key", "privateKey"),
            settings_name="jwt_private_key",
            env_var="JWT_PRIVATE_KEY",
            tool_name=tool_name,
            display_name="JWT",
            config=config,
        )
    return _secret_value(
        provider="jwt",
        provider_aliases=("jwt_auth", "jwtAuth"),
        field_names=("public_key", "publicKey", "private_key", "privateKey"),
        settings_name="jwt_public_key",
        env_var="JWT_PUBLIC_KEY",
        tool_name=tool_name,
        display_name="JWT",
        config=config,
    )


@tool
def datetime_current(
    timezone_name: str = "UTC",
    include_time: bool = True,
    output_format: str = "iso",
) -> str:
    """Get the current date or time in a timezone.

    Args:
        timezone_name: IANA timezone name, e.g. "UTC" or "America/New_York".
        include_time: If false, return midnight at the start of the current day.
        output_format: "iso", "date", "time", "timestamp", "rfc2822", or a strftime format.
    """
    try:
        now = datetime.now(_zone(timezone_name))
        if not include_time:
            now = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _format_datetime(now, output_format)
    except Exception as exc:
        logger.debug("datetime_current failed", exc_info=True)
        return f"[Error]: datetime_current failed: {exc}"


@tool
def datetime_add(
    date_value: str,
    amount: float,
    unit: str,
    timezone_name: str = "UTC",
    output_format: str = "iso",
) -> str:
    """Add a duration to a date/time value.

    Args:
        date_value: Input date/time; use "now" for the current time.
        amount: Amount to add.
        unit: years, months, weeks, days, hours, minutes, or seconds.
        timezone_name: Timezone used for naive input values.
        output_format: "iso", "date", "time", "timestamp", "rfc2822", or a strftime format.
    """
    try:
        result = _adjust_datetime(_parse_datetime(date_value, timezone_name=timezone_name), unit, amount)
        return _format_datetime(result, output_format)
    except Exception as exc:
        logger.debug("datetime_add failed", exc_info=True)
        return f"[Error]: datetime_add failed: {exc}"


@tool
def datetime_subtract(
    date_value: str,
    amount: float,
    unit: str,
    timezone_name: str = "UTC",
    output_format: str = "iso",
) -> str:
    """Subtract a duration from a date/time value.

    Args:
        date_value: Input date/time; use "now" for the current time.
        amount: Amount to subtract.
        unit: years, months, weeks, days, hours, minutes, or seconds.
        timezone_name: Timezone used for naive input values.
        output_format: "iso", "date", "time", "timestamp", "rfc2822", or a strftime format.
    """
    try:
        result = _adjust_datetime(_parse_datetime(date_value, timezone_name=timezone_name), unit, -amount)
        return _format_datetime(result, output_format)
    except Exception as exc:
        logger.debug("datetime_subtract failed", exc_info=True)
        return f"[Error]: datetime_subtract failed: {exc}"


@tool
def datetime_format(
    date_value: str,
    output_format: str = "iso",
    timezone_name: str = "UTC",
) -> str:
    """Parse and format a date/time value.

    Args:
        date_value: Input date/time; use "now" for the current time.
        output_format: "iso", "date", "time", "timestamp", "rfc2822", or a strftime format.
        timezone_name: Timezone used for naive input values and output conversion.
    """
    try:
        return _format_datetime(_parse_datetime(date_value, timezone_name=timezone_name), output_format)
    except Exception as exc:
        logger.debug("datetime_format failed", exc_info=True)
        return f"[Error]: datetime_format failed: {exc}"


@tool
def datetime_between(
    start_date: str,
    end_date: str,
    unit: str = "seconds",
    timezone_name: str = "UTC",
    absolute: bool = False,
) -> str:
    """Get the time difference between two date/time values.

    Args:
        start_date: Start date/time; use "now" for the current time.
        end_date: End date/time; use "now" for the current time.
        unit: seconds, minutes, hours, days, weeks, or all.
        timezone_name: Timezone used for naive input values.
        absolute: Whether to return the absolute difference.
    """
    try:
        start = _parse_datetime(start_date, timezone_name=timezone_name)
        end = _parse_datetime(end_date, timezone_name=timezone_name)
        delta_seconds = (end - start).total_seconds()
        if absolute:
            delta_seconds = abs(delta_seconds)
        unit_name = (unit or "seconds").strip().lower()
        divisors = {
            "seconds": 1,
            "second": 1,
            "minutes": 60,
            "minute": 60,
            "hours": 3600,
            "hour": 3600,
            "days": 86400,
            "day": 86400,
            "weeks": 604800,
            "week": 604800,
        }
        if unit_name == "all":
            return _dump_json(
                {
                    "seconds": delta_seconds,
                    "minutes": delta_seconds / 60,
                    "hours": delta_seconds / 3600,
                    "days": delta_seconds / 86400,
                    "weeks": delta_seconds / 604800,
                }
            )
        if unit_name not in divisors:
            return "[Error]: unit must be seconds, minutes, hours, days, weeks, or all."
        return str(delta_seconds / divisors[unit_name])
    except Exception as exc:
        logger.debug("datetime_between failed", exc_info=True)
        return f"[Error]: datetime_between failed: {exc}"


@tool
def datetime_extract(
    date_value: str,
    part: str,
    timezone_name: str = "UTC",
) -> str:
    """Extract one component from a date/time value.

    Args:
        date_value: Input date/time; use "now" for the current time.
        part: year, month, day, hour, minute, second, weekday, week, quarter, or timestamp.
        timezone_name: Timezone used for naive input values.
    """
    try:
        value = _parse_datetime(date_value, timezone_name=timezone_name)
        normalized = part.strip().lower()
        mapping = {
            "year": value.year,
            "month": value.month,
            "day": value.day,
            "hour": value.hour,
            "minute": value.minute,
            "second": value.second,
            "weekday": value.weekday(),
            "week": value.isocalendar().week,
            "iso_week": value.isocalendar().week,
            "quarter": ((value.month - 1) // 3) + 1,
            "timestamp": value.timestamp(),
        }
        if normalized not in mapping:
            return "[Error]: unsupported date part."
        return str(mapping[normalized])
    except Exception as exc:
        logger.debug("datetime_extract failed", exc_info=True)
        return f"[Error]: datetime_extract failed: {exc}"


@tool
def datetime_round(
    date_value: str,
    unit: str = "day",
    mode: str = "floor",
    timezone_name: str = "UTC",
    output_format: str = "iso",
) -> str:
    """Round a date/time down or up to a calendar boundary.

    Args:
        date_value: Input date/time; use "now" for the current time.
        unit: year, month, week, day, hour, minute, or second.
        mode: "floor" or "ceil".
        timezone_name: Timezone used for naive input values.
        output_format: "iso", "date", "time", "timestamp", "rfc2822", or a strftime format.
    """
    try:
        value = _parse_datetime(date_value, timezone_name=timezone_name)
        rounded = _floor_datetime(value, unit)
        if mode.strip().lower() in {"ceil", "roundup", "up"} and rounded != value:
            rounded = _floor_datetime(_adjust_datetime(value, unit, 1), unit)
        elif mode.strip().lower() not in {"floor", "rounddown", "down", "ceil", "roundup", "up"}:
            return '[Error]: mode must be "floor" or "ceil".'
        return _format_datetime(rounded, output_format)
    except Exception as exc:
        logger.debug("datetime_round failed", exc_info=True)
        return f"[Error]: datetime_round failed: {exc}"


@tool
def crypto_hash_text(
    text: str,
    algorithm: str = "sha256",
    encoding: str = "hex",
) -> str:
    """Hash text with a selected digest algorithm.

    Args:
        text: Text to hash.
        algorithm: md5, sha1, sha224, sha256, sha384, sha512, sha3-256, sha3-384, or sha3-512.
        encoding: Output encoding, "hex" or "base64".
    """
    if len(text) > _MAX_TEXT_CHARS:
        return f"[Error]: text is too long; max {_MAX_TEXT_CHARS} characters."
    try:
        return _encode_bytes(_hashlib_digest(text, algorithm), encoding)
    except Exception as exc:
        logger.debug("crypto_hash_text failed", exc_info=True)
        return f"[Error]: crypto_hash_text failed: {exc}"


@tool
def crypto_hmac_text(
    text: str,
    algorithm: str = "sha256",
    encoding: str = "hex",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an HMAC for text using the saved Crypto secret.

    Args:
        text: Text to sign.
        algorithm: md5, sha1, sha224, sha256, sha384, sha512, sha3-256, sha3-384, or sha3-512.
        encoding: Output encoding, "hex" or "base64".
    """
    if len(text) > _MAX_TEXT_CHARS:
        return f"[Error]: text is too long; max {_MAX_TEXT_CHARS} characters."
    try:
        secret = _secret_value(
            provider="crypto",
            provider_aliases=("crypto_credentials",),
            field_names=("hmac_secret", "hmacSecret", "secret", "value"),
            settings_name="crypto_hmac_secret",
            env_var="CRYPTO_HMAC_SECRET",
            tool_name="crypto_hmac_text",
            display_name="Crypto",
            config=config,
        )
        if secret and secret.startswith("[Error]:"):
            return secret
        return _encode_bytes(_hashlib_digest(text, algorithm, secret=str(secret).encode("utf-8")), encoding)
    except Exception as exc:
        logger.debug("crypto_hmac_text failed", exc_info=True)
        return f"[Error]: crypto_hmac_text failed: {exc}"


@tool
def crypto_generate_random(
    kind: str = "uuid",
    length: int = 32,
    alphabet: str = "",
) -> str:
    """Generate a random UUID or random string.

    Args:
        kind: "uuid", "hex", "base64", "ascii", or "custom".
        length: Desired output length for non-UUID kinds.
        alphabet: Characters to use when kind is "custom".
    """
    try:
        normalized = (kind or "uuid").strip().lower()
        if normalized == "uuid":
            return str(uuid.uuid4())
        length = max(1, min(_MAX_RANDOM_LENGTH, int(length)))
        byte_count = max(1, length)
        if normalized == "hex":
            return secrets.token_hex(byte_count)[:length]
        if normalized == "base64":
            return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).decode("ascii").rstrip("=")[:length]
        if normalized == "ascii":
            chars = string.ascii_letters + string.digits
        elif normalized == "custom":
            chars = alphabet
            if not chars:
                return "[Error]: alphabet is required when kind is custom."
        else:
            return "[Error]: kind must be uuid, hex, base64, ascii, or custom."
        return "".join(secrets.choice(chars) for _ in range(length))
    except Exception as exc:
        logger.debug("crypto_generate_random failed", exc_info=True)
        return f"[Error]: crypto_generate_random failed: {exc}"


@tool
def totp_generate_code(
    timestamp: int = 0,
    period: int = 30,
    digits: int = 6,
    algorithm: str = "sha1",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Generate a time-based one-time password from the saved TOTP secret.

    Args:
        timestamp: Optional Unix timestamp. Uses current time when 0.
        period: TOTP time step in seconds, usually 30.
        digits: Code length, usually 6.
        algorithm: Digest algorithm: sha1, sha256, or sha512.
    """
    try:
        secret = _totp_secret("totp_generate_code", config)
        if secret and secret.startswith("[Error]:"):
            return secret
        period, digits, algorithm = _totp_params(period, digits, algorithm)
        now = int(timestamp) if int(timestamp) > 0 else int(time.time())
        valid_from = now - (now % period)
        return _dump_json(
            {
                "code": _totp_code(str(secret), timestamp=now, period=period, digits=digits, algorithm=algorithm),
                "algorithm": algorithm,
                "digits": digits,
                "period": period,
                "valid_from": valid_from,
                "valid_until": valid_from + period,
            }
        )
    except Exception as exc:
        logger.debug("totp_generate_code failed", exc_info=True)
        return f"[Error]: totp_generate_code failed: {exc}"


@tool
def totp_verify_code(
    code: str,
    timestamp: int = 0,
    period: int = 30,
    digits: int = 6,
    algorithm: str = "sha1",
    window: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Verify a time-based one-time password against the saved TOTP secret.

    Args:
        code: TOTP code to verify.
        timestamp: Optional Unix timestamp. Uses current time when 0.
        period: TOTP time step in seconds, usually 30.
        digits: Code length, usually 6.
        algorithm: Digest algorithm: sha1, sha256, or sha512.
        window: Number of time steps before/after timestamp to accept, 0-10.
    """
    clean_code = "".join(str(code).strip().split())
    if not clean_code.isdigit():
        return "[Error]: code must contain only digits."
    try:
        secret = _totp_secret("totp_verify_code", config)
        if secret and secret.startswith("[Error]:"):
            return secret
        period, digits, algorithm = _totp_params(period, digits, algorithm)
        if len(clean_code) != digits:
            return f"[Error]: code must be {digits} digits."
        now = int(timestamp) if int(timestamp) > 0 else int(time.time())
        window = max(0, min(10, int(window)))
        match_offset: int | None = None
        for offset in range(-window, window + 1):
            candidate = _totp_code(
                str(secret),
                timestamp=now + (offset * period),
                period=period,
                digits=digits,
                algorithm=algorithm,
            )
            if hmac.compare_digest(candidate, clean_code):
                match_offset = offset
                break
        current_valid_from = now - (now % period)
        return _dump_json(
            {
                "valid": match_offset is not None,
                "counter_offset": match_offset,
                "algorithm": algorithm,
                "digits": digits,
                "period": period,
                "checked_at": now,
                "current_window": {
                    "valid_from": current_valid_from,
                    "valid_until": current_valid_from + period,
                },
            }
        )
    except Exception as exc:
        logger.debug("totp_verify_code failed", exc_info=True)
        return f"[Error]: totp_verify_code failed: {exc}"


@tool
def crypto_sign_text(
    text: str,
    algorithm: str = "sha256",
    encoding: str = "base64",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Sign text with the saved Crypto private key.

    Args:
        text: Text to sign.
        algorithm: sha1, sha224, sha256, sha384, sha512, sha3-256, sha3-384, or sha3-512.
        encoding: Output encoding, "hex" or "base64".
    """
    if len(text) > _MAX_TEXT_CHARS:
        return f"[Error]: text is too long; max {_MAX_TEXT_CHARS} characters."
    try:
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, padding, rsa

        key, error = _private_key("crypto_sign_text", config)
        if error:
            return error
        data = text.encode("utf-8")
        if isinstance(key, rsa.RSAPrivateKey):
            signature = key.sign(data, padding.PKCS1v15(), _crypto_hash_algorithm(algorithm))
        elif isinstance(key, ec.EllipticCurvePrivateKey):
            signature = key.sign(data, ec.ECDSA(_crypto_hash_algorithm(algorithm)))
        elif isinstance(key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)):
            signature = key.sign(data)
        else:
            return "[Error]: unsupported private key type."
        return _encode_bytes(signature, encoding)
    except Exception as exc:
        logger.debug("crypto_sign_text failed", exc_info=True)
        return f"[Error]: crypto_sign_text failed: {exc}"


@tool
def jwt_decode_token(
    token: str,
    complete: bool = False,
) -> str:
    """Decode a JWT without verifying its signature.

    Args:
        token: JWT to decode.
        complete: Whether to include header/signature metadata.
    """
    if not token.strip():
        return "[Error]: token is required."
    try:
        import jwt

        decoded = jwt.decode(token.strip(), options={"verify_signature": False}, algorithms=None)
        if complete:
            header = jwt.get_unverified_header(token.strip())
            return _dump_json({"header": header, "payload": decoded})
        return _dump_json({"payload": decoded})
    except Exception as exc:
        logger.debug("jwt_decode_token failed", exc_info=True)
        return f"[Error]: jwt_decode_token failed: {exc}"


@tool
def jwt_sign_claims(
    claims_json: str,
    algorithm: str = "",
    headers_json: str = "",
    expires_in_seconds: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Sign JWT claims using saved JWT credentials.

    Args:
        claims_json: JSON object of JWT claims.
        algorithm: Optional algorithm override, e.g. HS256 or RS256.
        headers_json: Optional JSON object of JWT headers.
        expires_in_seconds: If positive and exp is absent, add an expiration this many seconds from now.
    """
    try:
        import jwt

        payload = _json_object(claims_json, field_name="claims_json")
        headers = _json_object(headers_json, field_name="headers_json") if headers_json.strip() else None
        if expires_in_seconds > 0 and "exp" not in payload:
            payload["exp"] = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in_seconds))
        alg = _jwt_algorithm(tool_name="jwt_sign_claims", config=config, requested=algorithm)
        key = _jwt_key(algorithm=alg, purpose="sign", tool_name="jwt_sign_claims", config=config)
        if not key or key.startswith("[Error]:"):
            return key or "[Error]: No JWT key configured."
        return jwt.encode(payload, key, algorithm=alg, headers=headers)
    except Exception as exc:
        logger.debug("jwt_sign_claims failed", exc_info=True)
        return f"[Error]: jwt_sign_claims failed: {exc}"


@tool
def jwt_verify_token(
    token: str,
    algorithm: str = "",
    complete: bool = False,
    ignore_expiration: bool = False,
    ignore_not_before: bool = False,
    leeway_seconds: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Verify a JWT with saved JWT credentials.

    Args:
        token: JWT to verify.
        algorithm: Optional algorithm override, e.g. HS256 or RS256.
        complete: Whether to include the unverified header with the verified payload.
        ignore_expiration: Whether to ignore exp.
        ignore_not_before: Whether to ignore nbf.
        leeway_seconds: Seconds of clock tolerance for exp and nbf.
    """
    if not token.strip():
        return "[Error]: token is required."
    try:
        import jwt

        alg = _jwt_algorithm(tool_name="jwt_verify_token", config=config, requested=algorithm)
        key = _jwt_key(algorithm=alg, purpose="verify", tool_name="jwt_verify_token", config=config)
        if not key or key.startswith("[Error]:"):
            return key or "[Error]: No JWT key configured."
        payload = jwt.decode(
            token.strip(),
            key,
            algorithms=[alg],
            options={
                "verify_exp": not ignore_expiration,
                "verify_nbf": not ignore_not_before,
            },
            leeway=max(0, int(leeway_seconds)),
        )
        if complete:
            return _dump_json({"header": jwt.get_unverified_header(token.strip()), "payload": payload})
        return _dump_json({"payload": payload})
    except Exception as exc:
        logger.debug("jwt_verify_token failed", exc_info=True)
        return f"[Error]: jwt_verify_token failed: {exc}"


@tool
def compression_gzip_text(text: str, encoding: str = "utf-8") -> str:
    """Compress text with gzip and return base64 data.

    Args:
        text: Text to gzip.
        encoding: Text encoding.
    """
    if len(text) > _MAX_TEXT_CHARS:
        return f"[Error]: text is too long; max {_MAX_TEXT_CHARS} characters."
    try:
        raw = text.encode(encoding)
        compressed = gzip.compress(raw)
        return _dump_json(
            {
                "format": "gzip",
                "encoding": "base64",
                "data": base64.b64encode(compressed).decode("ascii"),
                "original_bytes": len(raw),
                "compressed_bytes": len(compressed),
            }
        )
    except Exception as exc:
        logger.debug("compression_gzip_text failed", exc_info=True)
        return f"[Error]: compression_gzip_text failed: {exc}"


@tool
def compression_gunzip_text(data_base64: str, encoding: str = "utf-8") -> str:
    """Decompress base64 gzip data into text.

    Args:
        data_base64: Base64-encoded gzip data.
        encoding: Text encoding for decompressed bytes.
    """
    try:
        compressed = base64.b64decode(data_base64, validate=True)
        if len(compressed) > _MAX_ARCHIVE_BYTES:
            return f"[Error]: compressed data is too large; max {_MAX_ARCHIVE_BYTES} bytes."
        raw = gzip.decompress(compressed)
        if len(raw) > _MAX_TEXT_CHARS:
            return f"[Error]: decompressed text is too large; max {_MAX_TEXT_CHARS} bytes."
        return raw.decode(encoding)
    except Exception as exc:
        logger.debug("compression_gunzip_text failed", exc_info=True)
        return f"[Error]: compression_gunzip_text failed: {exc}"


@tool
def compression_zip_text_files(files_json: str) -> str:
    """Create a zip archive from a JSON object of filename to text.

    Args:
        files_json: JSON object where each key is a relative filename and each value is file text.
    """
    try:
        files = _json_object(files_json, field_name="files_json")
        if len(files) > _MAX_ZIP_FILES:
            return f"[Error]: too many files; max {_MAX_ZIP_FILES}."
        buffer = io.BytesIO()
        total_bytes = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                clean_name = str(name).strip().lstrip("/")
                if not clean_name or ".." in clean_name.split("/"):
                    return f"[Error]: invalid archive filename: {name}"
                if not isinstance(content, str):
                    return f"[Error]: content for {name} must be text."
                raw = content.encode("utf-8")
                total_bytes += len(raw)
                if total_bytes > _MAX_TEXT_CHARS:
                    return f"[Error]: archive input text is too large; max {_MAX_TEXT_CHARS} bytes."
                archive.writestr(clean_name, raw)
        data = buffer.getvalue()
        if len(data) > _MAX_ARCHIVE_BYTES:
            return f"[Error]: archive is too large; max {_MAX_ARCHIVE_BYTES} bytes."
        return _dump_json(
            {
                "format": "zip",
                "encoding": "base64",
                "data": base64.b64encode(data).decode("ascii"),
                "file_count": len(files),
                "archive_bytes": len(data),
            }
        )
    except Exception as exc:
        logger.debug("compression_zip_text_files failed", exc_info=True)
        return f"[Error]: compression_zip_text_files failed: {exc}"


@tool
def compression_unzip_text_files(archive_base64: str, encoding: str = "utf-8") -> str:
    """Extract base64 zip data into a JSON object of filenames and text/base64 content.

    Args:
        archive_base64: Base64-encoded zip archive.
        encoding: Text encoding for extracted files.
    """
    try:
        data = base64.b64decode(archive_base64, validate=True)
        if len(data) > _MAX_ARCHIVE_BYTES:
            return f"[Error]: archive is too large; max {_MAX_ARCHIVE_BYTES} bytes."
        files: dict[str, dict[str, Any]] = {}
        total_size = 0
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > _MAX_ZIP_FILES:
                return f"[Error]: too many files; max {_MAX_ZIP_FILES}."
            for info in infos:
                name = info.filename
                if not name or name.startswith("/") or ".." in name.split("/"):
                    return f"[Error]: unsafe archive filename: {name}"
                total_size += info.file_size
                if total_size > _MAX_TEXT_CHARS:
                    return f"[Error]: extracted content is too large; max {_MAX_TEXT_CHARS} bytes."
                raw = archive.read(info)
                try:
                    files[name] = {"encoding": encoding, "text": raw.decode(encoding)}
                except UnicodeDecodeError:
                    files[name] = {
                        "encoding": "base64",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
        return _dump_json({"files": files, "file_count": len(files)})
    except Exception as exc:
        logger.debug("compression_unzip_text_files failed", exc_info=True)
        return f"[Error]: compression_unzip_text_files failed: {exc}"


TRANSFORM_UTILITY_TOOLS = [
    datetime_current,
    datetime_add,
    datetime_subtract,
    datetime_format,
    datetime_between,
    datetime_extract,
    datetime_round,
    crypto_hash_text,
    crypto_hmac_text,
    crypto_generate_random,
    totp_generate_code,
    totp_verify_code,
    crypto_sign_text,
    jwt_decode_token,
    jwt_sign_claims,
    jwt_verify_token,
    compression_gzip_text,
    compression_gunzip_text,
    compression_zip_text_files,
    compression_unzip_text_files,
]
