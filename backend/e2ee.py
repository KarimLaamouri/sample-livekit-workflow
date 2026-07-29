"""Versioned E2EE key derivation for LiveKit media encryption.

This module manages a SEPARATE secret namespace from DATABASE_ENCRYPTION_KEY
and DATABASE_BLIND_INDEX_KEY (which live in encryption.py). The secrets loaded
here must NOT be the same values as those keys.

Key derivation:
    HMAC-SHA256(master_secret_for_version, consultation_id) → hex digest

The derived key is returned to clients via TokenResponse.e2ee_key and used by
LiveKit's ExternalE2EEKeyProvider on the frontend. Because the derivation is
deterministic, every participant in the same consultation receives the identical
key without persisting any key material in the database.

Environment variables:
    E2EE_MASTER_SECRET_V1, E2EE_MASTER_SECRET_V2, ...
        Versioned 32-byte master secrets (base64 or hex encoded).
        At least V1 must be present.

    E2EE_CURRENT_KEY_VERSION (int, default 1)
        The version stamp assigned to newly created consultations.
        Must have a corresponding E2EE_MASTER_SECRET_V{n} loaded.

Rotation procedure:
    1. Generate a new secret:  openssl rand -base64 32
    2. Add E2EE_MASTER_SECRET_V{n+1} to the environment (keep old ones).
    3. Deploy.
    4. Set E2EE_CURRENT_KEY_VERSION={n+1}.
    5. Old versions can be retired after CONSULTATION_TTL_MINUTES has elapsed
       and no consultation with that version is still active/reconnecting.
"""

import base64
import hashlib
import hmac
import os
import re


def _load_key(env_var: str, expected_bytes: int) -> bytes:
    """Load and validate a cryptographic key from an environment variable.

    Accepts base64-encoded, hex-encoded, or raw UTF-8 strings.
    Mirrors the loading logic in encryption.py but is intentionally a private
    copy so this module has zero import-time coupling to encryption.py.
    """
    key_str = os.getenv(env_var)
    if not key_str:
        raise RuntimeError(
            f"{env_var} is not set. This environment variable is required for "
            f"E2EE key derivation. Set it to a {expected_bytes * 8}-bit key "
            f"(e.g., generate with: openssl rand -base64 {expected_bytes})"
        )

    # Try base64 first
    try:
        key = base64.b64decode(key_str, validate=True)
        if len(key) == expected_bytes:
            return key
    except Exception:
        pass

    # Try hex
    try:
        key = bytes.fromhex(key_str)
        if len(key) == expected_bytes:
            return key
    except Exception:
        pass

    # Try raw string (not recommended but allow for dev)
    key = key_str.encode("utf-8")
    if len(key) == expected_bytes:
        return key

    raise RuntimeError(
        f"{env_var} must be a {expected_bytes}-byte key (base64 or hex encoded). "
        f"Current value is {len(key_str)} characters."
    )


# ---------------------------------------------------------------------------
# Load versioned master secrets at import time
# ---------------------------------------------------------------------------

_MASTER_SECRETS: dict[int, bytes] = {}
_ENV_PATTERN = re.compile(r"^E2EE_MASTER_SECRET_V(\d+)$")

for env_name, _env_value in os.environ.items():
    match = _ENV_PATTERN.match(env_name)
    if match:
        version = int(match.group(1))
        _MASTER_SECRETS[version] = _load_key(env_name, 32)

if not _MASTER_SECRETS:
    raise RuntimeError(
        "No E2EE master secrets found in the environment. "
        "At least E2EE_MASTER_SECRET_V1 must be set "
        "(generate with: openssl rand -base64 32). "
        "These must be DIFFERENT values from DATABASE_ENCRYPTION_KEY "
        "and DATABASE_BLIND_INDEX_KEY."
    )

if 1 not in _MASTER_SECRETS:
    raise RuntimeError(
        "E2EE_MASTER_SECRET_V1 is required but was not found. "
        "Other versions were found: "
        f"{sorted(_MASTER_SECRETS.keys())}. "
        "V1 must always be present as the baseline version."
    )

# ---------------------------------------------------------------------------
# Determine the current key version for new consultations
# ---------------------------------------------------------------------------

E2EE_CURRENT_KEY_VERSION: int = int(os.getenv("E2EE_CURRENT_KEY_VERSION", "1"))

if E2EE_CURRENT_KEY_VERSION not in _MASTER_SECRETS:
    raise RuntimeError(
        f"E2EE_CURRENT_KEY_VERSION is set to {E2EE_CURRENT_KEY_VERSION}, but "
        f"E2EE_MASTER_SECRET_V{E2EE_CURRENT_KEY_VERSION} is not loaded. "
        f"Loaded versions: {sorted(_MASTER_SECRETS.keys())}. "
        f"Either set the corresponding secret or adjust "
        f"E2EE_CURRENT_KEY_VERSION."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def derive_e2ee_key(consultation_id: str, version: int) -> str:
    """Derive a deterministic E2EE key for a consultation.

    Args:
        consultation_id: The consultation's unique identifier.
        version: The master secret version to use (stored on the consultation row).

    Returns:
        A 64-character hex string (HMAC-SHA256 digest).

    Raises:
        RuntimeError: If the requested version's secret is not loaded.
    """
    secret = _MASTER_SECRETS.get(version)
    if secret is None:
        raise RuntimeError(
            f"E2EE master secret version {version} is not loaded in the "
            f"environment (E2EE_MASTER_SECRET_V{version} is missing). "
            f"A consultation still references this version, so the secret "
            f"cannot be removed until all consultations using it have expired. "
            f"Loaded versions: {sorted(_MASTER_SECRETS.keys())}."
        )
    return hmac.new(secret, consultation_id.encode("utf-8"), hashlib.sha256).hexdigest()


def current_e2ee_key_version() -> int:
    """Return the key version to stamp on newly created consultations."""
    return E2EE_CURRENT_KEY_VERSION
