"""Vendored WhatsApp protobuf schemas (MPL-2.0 from whatsmeow).

Schemas live in ``vendor/``; generated Python bindings in ``_gen/``.
Regenerate with::

    protoc --proto_path=wa/proto/vendor --python_out=wa/proto/_gen \\
           wa/proto/vendor/*.proto

Keep the vendor/ files unmodified to preserve MPL-2.0 file-scope.
"""

import sys
from pathlib import Path

# Generated bindings reference each other by package name (e.g. ``waCert_pb2``),
# so the _gen directory must be on sys.path.
_GEN = Path(__file__).parent / "_gen"
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))

from . import _gen  # noqa: E402
from ._gen import waAdv_pb2, waCert_pb2, waCompanionReg_pb2, waWa6_pb2  # noqa: E402

__all__ = ["waAdv_pb2", "waCert_pb2", "waCompanionReg_pb2", "waWa6_pb2"]
