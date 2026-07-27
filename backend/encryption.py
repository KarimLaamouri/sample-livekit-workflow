import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC


def _load_key(env_var: str, expected_bytes: int) -> bytes:
    """Load and validate a cryptographic key from an environment variable.
    
    The key can be provided as:
    - Base64-encoded string (recommended)
    - Hex-encoded string
    - Raw bytes (not recommended for env vars)
    
    Args:
        env_var: Name of the environment variable
        expected_bytes: Expected key length in bytes
        
    Returns:
        Raw key bytes
        
    Raises:
        RuntimeError: If the env var is missing or invalid
    """
    key_str = os.getenv(env_var)
    if not key_str:
        raise RuntimeError(
            f"{env_var} is not set. This environment variable is required for "
            f"database field encryption. Set it to a {expected_bytes * 8}-bit key "
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
    key = key_str.encode('utf-8')
    if len(key) == expected_bytes:
        return key
    
    raise RuntimeError(
        f"{env_var} must be a {expected_bytes}-byte key (base64 or hex encoded). "
        f"Current value is {len(key_str)} characters."
    )


# Load keys at import time - fail fast if missing
_ENCRYPTION_KEY = _load_key("DATABASE_ENCRYPTION_KEY", 32)  # 256-bit for AES-256
_BLIND_INDEX_KEY = _load_key("DATABASE_BLIND_INDEX_KEY", 32)  # 256-bit for HMAC-SHA256


def encrypt_value(plaintext: str) -> bytes:
    """Encrypt a string value using AES-256-GCM.
    
    Args:
        plaintext: The plaintext string to encrypt
        
    Returns:
        Ciphertext bytes with nonce prepended (12-byte nonce + ciphertext + 16-byte tag)
        
    Raises:
        ValueError: If plaintext is not a string
    """
    if not isinstance(plaintext, str):
        raise ValueError(f"encrypt_value expects a string, got {type(plaintext)}")
    
    if plaintext is None:
        raise ValueError("encrypt_value does not accept None - use TypeDecorator's None handling")
    
    # Generate a random 12-byte nonce
    nonce = os.urandom(12)
    
    # Encrypt with AES-256-GCM
    aesgcm = AESGCM(_ENCRYPTION_KEY)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
    
    # Return nonce + ciphertext (nonce is needed for decryption)
    return nonce + ciphertext


def decrypt_value(ciphertext: bytes) -> str:
    """Decrypt a ciphertext value using AES-256-GCM.
    
    Args:
        ciphertext: The ciphertext bytes (nonce + ciphertext + tag)
        
    Returns:
        The decrypted plaintext string
        
    Raises:
        ValueError: If ciphertext is not bytes or invalid format
        InvalidTag: If the ciphertext is corrupted or tampered with
    """
    if not isinstance(ciphertext, bytes):
        raise ValueError(f"decrypt_value expects bytes, got {type(ciphertext)}")
    
    if len(ciphertext) < 12:
        raise ValueError("Ciphertext is too short to contain a valid nonce")
    
    # Extract nonce (first 12 bytes) and actual ciphertext
    nonce = ciphertext[:12]
    actual_ciphertext = ciphertext[12:]
    
    # Decrypt with AES-256-GCM
    aesgcm = AESGCM(_ENCRYPTION_KEY)
    plaintext = aesgcm.decrypt(nonce, actual_ciphertext, None)
    
    return plaintext.decode('utf-8')


def encrypt_json(data: dict) -> bytes:
    """Encrypt a JSON-serializable dictionary using AES-256-GCM.
    
    The entire JSON blob is encrypted as a single ciphertext.
    
    Args:
        data: The dictionary to encrypt
        
    Returns:
        Ciphertext bytes with nonce prepended
        
    Raises:
        ValueError: If data is not a dict or not JSON-serializable
    """
    if not isinstance(data, dict):
        raise ValueError(f"encrypt_json expects a dict, got {type(data)}")
    
    if data is None:
        raise ValueError("encrypt_json does not accept None - use TypeDecorator's None handling")
    
    # Serialize to JSON
    json_str = json.dumps(data, separators=(',', ':'))  # Compact JSON
    return encrypt_value(json_str)


def decrypt_json(ciphertext: bytes) -> dict:
    """Decrypt a ciphertext and parse as JSON.
    
    Args:
        ciphertext: The ciphertext bytes
        
    Returns:
        The decrypted dictionary
        
    Raises:
        ValueError: If ciphertext is invalid or not valid JSON
        InvalidTag: If the ciphertext is corrupted or tampered with
    """
    json_str = decrypt_value(ciphertext)
    return json.loads(json_str)


def blind_index(value: str) -> str:
    """Compute a deterministic blind index for a value using HMAC-SHA256.
    
    This is used for SQL lookups on encrypted columns. The hash is
    case-insensitive and whitespace-trimmed to enable flexible matching.
    
    Args:
        value: The plaintext value to hash
        
    Returns:
        A 64-character hex string (SHA-256 digest)
        
    Raises:
        ValueError: If value is not a string
    """
    if not isinstance(value, str):
        raise ValueError(f"blind_index expects a string, got {type(value)}")
    
    # Normalize: lowercase and trim whitespace for consistent hashing
    normalized = value.lower().strip()
    
    # Compute HMAC-SHA256
    hmac = HMAC(_BLIND_INDEX_KEY, hashes.SHA256(), backend=default_backend())
    hmac.update(normalized.encode('utf-8'))
    digest = hmac.finalize()
    
    # Return as hex string
    return digest.hex()
