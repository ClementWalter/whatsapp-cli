"""Cryptographic primitives used by the WhatsApp client.

Split into narrow modules so each can be unit-tested with deterministic
inputs against the oracle:

- noise: Noise_XX_25519_AESGCM_SHA256 state machine (handshake-time)
- (later) lthash, media, xeddsa
"""
