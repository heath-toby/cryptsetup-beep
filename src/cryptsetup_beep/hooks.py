"""mkinitcpio.conf detection and editing for the wizard's Page 4.

We never edit silently. The wizard shows the proposed change to the user
(via a focusable read-only TextView so Orca can read it line by line) and
only invokes pkexec to apply once the user accepts.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

CONF_PATH = Path("/etc/mkinitcpio.conf")


@dataclass
class HooksStatus:
    has_hook: bool          # cryptsetup-beep is already in HOOKS=
    has_sd_encrypt: bool    # sd-encrypt is in HOOKS= (we depend on this)
    raw_line: str           # the original HOOKS= line, unmodified
    proposed_line: str      # what we'd write if applying


def inspect(path: Path = CONF_PATH) -> HooksStatus:
    line = ""
    for raw in path.read_text().splitlines():
        if raw.lstrip().startswith("HOOKS="):
            line = raw
            break

    has_hook = "cryptsetup-beep" in line
    has_sd_encrypt = bool(re.search(r"\bsd-encrypt\b", line))

    if has_hook or not has_sd_encrypt:
        proposed = line
    else:
        proposed = re.sub(
            r"\bsd-encrypt\b",
            "cryptsetup-beep sd-encrypt",
            line,
            count=1,
        )

    return HooksStatus(
        has_hook=has_hook,
        has_sd_encrypt=has_sd_encrypt,
        raw_line=line,
        proposed_line=proposed,
    )


def apply(status: HooksStatus, path: Path = CONF_PATH) -> Path:
    """Write the proposed change to disk after backing up the original.

    Caller is expected to be running with privilege (this function is invoked
    by --write-config under pkexec). Returns the path of the backup written.
    """
    if status.has_hook or not status.has_sd_encrypt:
        return path
    backup = path.with_name(
        f"mkinitcpio.conf.bak-cryptsetup-beep-{int(time.time())}"
    )
    shutil.copy2(path, backup)

    text = path.read_text()
    new_text = text.replace(status.raw_line, status.proposed_line, 1)
    path.write_text(new_text)
    return backup


def diff_summary(status: HooksStatus) -> str:
    """Human-readable summary the wizard renders in a TextView."""
    if status.has_hook:
        return "✓ cryptsetup-beep is already in HOOKS — no change needed."
    if not status.has_sd_encrypt:
        return (
            "⚠ sd-encrypt is not in HOOKS. cryptsetup-beep needs the systemd-based\n"
            "encrypt hook. Add sd-encrypt yourself before applying."
        )
    return (
        "The following line in /etc/mkinitcpio.conf will be edited:\n\n"
        f"  - {status.raw_line}\n"
        f"  + {status.proposed_line}\n\n"
        "A backup will be written to /etc/mkinitcpio.conf.bak-cryptsetup-beep-<timestamp>."
    )
