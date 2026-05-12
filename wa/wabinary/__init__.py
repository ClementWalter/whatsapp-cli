"""WhatsApp binary XML codec.

Encodes and decodes the compact binary node format used inside the Noise
transport, after the initial handshake. See tokens.py for the tag byte
constants and the single/double-byte token dictionaries.
"""

from .encoder import encode_node, encode_bytes
from .decoder import decode_node
from .jid import JID
from .node import Node

__all__ = ["Node", "JID", "encode_node", "encode_bytes", "decode_node"]
