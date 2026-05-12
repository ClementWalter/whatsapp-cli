"""Import contact names from macOS Contacts.app (iCloud-synced address book).

WhatsApp's protocol does not transmit address-book labels to linked
devices — those names exist only in the phone's local Contacts. On
macOS we can read them via the Contacts framework (no TCC prompt for
read-only access in most setups). This module spawns a small Swift
program and parses its output, then normalizes phone numbers to E.164
French form so they match the JIDs WhatsApp uses.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

SWIFT_SCRIPT = r"""
import Contacts
import Foundation

let store = CNContactStore()
let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
            CNContactPhoneNumbersKey] as [CNKeyDescriptor]
let req = CNContactFetchRequest(keysToFetch: keys)
do {
    try store.enumerateContacts(with: req) { (c, _) in
        let parts = [c.givenName, c.familyName].filter { !$0.isEmpty }
        let name = parts.isEmpty ? c.organizationName : parts.joined(separator: " ")
        if name.isEmpty { return }
        for ph in c.phoneNumbers {
            let digits = ph.value.stringValue.filter { $0.isNumber }
            if digits.isEmpty { continue }
            print("\(digits)\t\(name)")
        }
    }
} catch {
    FileHandle.standardError.write("ERR: \(error)\n".data(using: .utf8)!)
    exit(1)
}
"""


def normalize_pn(digits: str, default_country: str = "33") -> str | None:
    """Best-effort E.164 normalisation. Returns digits-only string or None.

    Heuristics:
    - 11 digits starting with the default country code → return as-is
    - 10 digits starting with 0 (national French form) → replace 0 with cc
    - 12+ digits starting with 00<cc> → strip 00
    - 12+ digits starting with cc → return as-is
    Other lengths are returned untouched (we'll still try to match).
    """
    if not digits.isdigit():
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        return default_country + digits[1:]
    return digits


def dump_macos_contacts() -> dict[str, str]:
    """Run the Swift helper and return a mapping ``digits → display name``."""
    with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False) as f:
        f.write(SWIFT_SCRIPT)
        path = f.name
    try:
        proc = subprocess.run(
            ["swift", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        Path(path).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(f"swift contacts dump failed: {proc.stderr.strip() or proc.stdout.strip()}")

    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        digits, name = line.split("\t", 1)
        norm = normalize_pn(digits)
        if not norm or not name.strip():
            continue
        # Prefer first occurrence (avoid overwriting "Mom" with "Mom (work)")
        out.setdefault(norm, name.strip())
    return out
