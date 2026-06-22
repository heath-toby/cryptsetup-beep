"""Initramfs hook detection and editing.

We never edit silently. The wizard shows the proposed change to the user
(via a focusable read-only TextView so Orca can read it line by line) and
only invokes pkexec to apply once the user accepts.

Two initramfs backends are supported:
  - mkinitcpio (Arch): edits HOOKS= in /etc/mkinitcpio.conf
  - dracut (Gentoo, Fedora, others): drops a .conf in /etc/dracut.conf.d/
On a system with both, mkinitcpio wins (it's the more invasive integration
and typically indicates an Arch host).
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union


MKINITCPIO_CONF = Path("/etc/mkinitcpio.conf")
DRACUT_CONF_DIR = Path("/etc/dracut.conf.d")
DRACUT_CONF_FILE = DRACUT_CONF_DIR / "cryptsetup-beep.conf"
DRACUT_CONF_LINE = 'add_dracutmodules+=" cryptsetup-beep "'

Backend = Literal["mkinitcpio", "dracut", "none"]


def detect_backend() -> Backend:
    """Determine which initramfs system is in use on this host."""
    if MKINITCPIO_CONF.is_file():
        return "mkinitcpio"
    if shutil.which("dracut"):
        return "dracut"
    return "none"


# ----- mkinitcpio path (Arch) -----


@dataclass
class MkinitcpioStatus:
    backend: Literal["mkinitcpio"]
    has_hook: bool          # cryptsetup-beep is already in HOOKS=
    has_sd_encrypt: bool    # sd-encrypt is in HOOKS= (we depend on this)
    raw_line: str           # the original HOOKS= line, unmodified
    proposed_line: str      # what we'd write if applying


def _inspect_mkinitcpio(path: Path = MKINITCPIO_CONF) -> MkinitcpioStatus:
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

    return MkinitcpioStatus(
        backend="mkinitcpio",
        has_hook=has_hook,
        has_sd_encrypt=has_sd_encrypt,
        raw_line=line,
        proposed_line=proposed,
    )


def _apply_mkinitcpio(
    status: MkinitcpioStatus, path: Path = MKINITCPIO_CONF
) -> Path | None:
    if status.has_hook or not status.has_sd_encrypt:
        return None
    backup = path.with_name(
        f"mkinitcpio.conf.bak-cryptsetup-beep-{int(time.time())}"
    )
    shutil.copy2(path, backup)
    text = path.read_text()
    new_text = text.replace(status.raw_line, status.proposed_line, 1)
    path.write_text(new_text)
    return backup


# ----- dracut path (Gentoo etc.) -----


@dataclass
class DracutStatus:
    backend: Literal["dracut"]
    has_conf: bool          # /etc/dracut.conf.d/cryptsetup-beep.conf has the enable line
    raw_line: str           # current file content (empty if file absent)
    proposed_line: str      # the line we'd write


def _inspect_dracut() -> DracutStatus:
    has = False
    raw = ""
    if DRACUT_CONF_FILE.is_file():
        raw = DRACUT_CONF_FILE.read_text().strip()
        has = DRACUT_CONF_LINE in raw
    return DracutStatus(
        backend="dracut",
        has_conf=has,
        raw_line=raw,
        proposed_line=DRACUT_CONF_LINE,
    )


def _apply_dracut(status: DracutStatus) -> Path | None:
    if status.has_conf:
        return None
    DRACUT_CONF_DIR.mkdir(parents=True, exist_ok=True)
    backup = None
    if DRACUT_CONF_FILE.exists():
        backup = DRACUT_CONF_FILE.with_name(
            f"cryptsetup-beep.conf.bak-{int(time.time())}"
        )
        shutil.copy2(DRACUT_CONF_FILE, backup)
    DRACUT_CONF_FILE.write_text(DRACUT_CONF_LINE + "\n")
    DRACUT_CONF_FILE.chmod(0o644)
    return backup


# ----- public API used by wizard + __main__ -----


HooksStatus = Union[MkinitcpioStatus, DracutStatus]


def inspect() -> HooksStatus:
    backend = detect_backend()
    if backend == "mkinitcpio":
        return _inspect_mkinitcpio()
    if backend == "dracut":
        return _inspect_dracut()
    raise RuntimeError(
        "No supported initramfs system found (mkinitcpio or dracut required)."
    )


def apply(status: HooksStatus) -> Path | None:
    if isinstance(status, MkinitcpioStatus):
        return _apply_mkinitcpio(status)
    if isinstance(status, DracutStatus):
        return _apply_dracut(status)
    raise TypeError(f"unknown status type: {type(status).__name__}")


def diff_summary(status: HooksStatus) -> str:
    """Human-readable summary the wizard renders in a TextView."""
    if isinstance(status, MkinitcpioStatus):
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
    if isinstance(status, DracutStatus):
        if status.has_conf:
            return (
                "✓ /etc/dracut.conf.d/cryptsetup-beep.conf is already in place — "
                "no change needed."
            )
        return (
            "The following file will be created:\n\n"
            "  /etc/dracut.conf.d/cryptsetup-beep.conf\n"
            f"    {status.proposed_line}\n\n"
            "This enables the cryptsetup-beep dracut module so the next initramfs "
            "build includes it."
        )
    raise TypeError(f"unknown status type: {type(status).__name__}")


def can_apply(status: HooksStatus) -> bool:
    """Whether the wizard's Apply button should be enabled.

    For mkinitcpio: only if sd-encrypt is in HOOKS= (otherwise the beep
    would never fire — sd-encrypt is what posts the ask-password file).
    For dracut: always — the module enables itself via dracut.conf.d and
    systemd-cryptsetup is the standard LUKS path, which is assumed present.
    """
    if isinstance(status, MkinitcpioStatus):
        return status.has_sd_encrypt
    if isinstance(status, DracutStatus):
        return True
    return False


def post_apply_message() -> str:
    """What to tell the user after the config write succeeded."""
    backend = detect_backend()
    if backend == "mkinitcpio":
        return "Config saved. Running mkinitcpio -P now…"
    if backend == "dracut":
        return (
            "Config saved.\n\n"
            "Init complete. Please rebuild your initramfs for the changes "
            "to take effect.\n\n"
            "On Gentoo, this is typically:\n"
            "  sudo emerge --config sys-kernel/gentoo-kernel-bin\n"
            "(or whichever sys-kernel/* package you use).\n\n"
            "On other dracut-based distros:\n"
            "  sudo dracut --regenerate-all --force"
        )
    return "Config saved."
