from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    key_id: str
    private_key_pem: bytes

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        timestamp = str(timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000)
        sign_path = path.split("?", 1)[0]
        message = f"{timestamp}{method.upper()}{sign_path}".encode()
        key = serialization.load_pem_private_key(self.private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be RSA")
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


@dataclass(frozen=True, slots=True)
class PolymarketUSCredentials:
    key_id: str
    secret_key_base64: str

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        timestamp = str(timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000)
        raw = base64.b64decode(self.secret_key_base64)
        if len(raw) < 32:
            raise ValueError("Polymarket US secret key must decode to at least 32 bytes")
        key = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = key.sign(message)
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode(),
        }

