#!/bin/bash
# Manual installation script for Gentoo (in lieu of a proper ebuild for now).
# Run from the project root: pkexec ./scripts/install-gentoo.sh
#
# Mirrors what the PKGBUILD does for Arch, but writes files into the standard
# /usr paths directly. Idempotent — re-runnable after edits.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must be run as root (e.g. via pkexec): $0" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "Installing cryptsetup-beep from $PROJECT_ROOT"

# --- Python module + entry point ---
install -d -m 755 /usr/lib/cryptsetup-beep/cryptsetup_beep
install -m 644 src/cryptsetup_beep/*.py /usr/lib/cryptsetup-beep/cryptsetup_beep/
install -m 755 src/bin/cryptsetup-beep /usr/bin/cryptsetup-beep

# --- Runtime player script ---
install -d -m 755 /usr/lib/cryptsetup-beep
install -m 755 scripts/play-beep.sh /usr/lib/cryptsetup-beep/play-beep.sh

# --- WAV files + example config ---
install -d -m 755 /usr/share/cryptsetup-beep
install -m 644 data/beep.wav            /usr/share/cryptsetup-beep/beep.wav
install -m 644 data/beep-retry.wav      /usr/share/cryptsetup-beep/beep-retry.wav
install -m 644 data/beep-pin.wav        /usr/share/cryptsetup-beep/beep-pin.wav
install -m 644 data/beep-pin-retry.wav  /usr/share/cryptsetup-beep/beep-pin-retry.wav
[[ -f data/config.example ]] && \
    install -m 644 data/config.example  /usr/share/cryptsetup-beep/config.example

# --- systemd units ---
install -d -m 755 /usr/lib/systemd/system
install -m 644 units/cryptsetup-beep.path    /usr/lib/systemd/system/
install -m 644 units/cryptsetup-beep.service /usr/lib/systemd/system/

# --- Polkit policy (for the wizard's pkexec save step) ---
if [[ -f data/org.heath-toby.cryptsetup-beep.policy ]]; then
    install -d -m 755 /usr/share/polkit-1/actions
    install -m 644 data/org.heath-toby.cryptsetup-beep.policy \
        /usr/share/polkit-1/actions/
fi

# --- Dracut module ---
install -d -m 755 /usr/lib/dracut/modules.d/95cryptsetup-beep
install -m 755 dracut/95cryptsetup-beep/module-setup.sh \
    /usr/lib/dracut/modules.d/95cryptsetup-beep/

echo ""
echo "Installed. Next steps:"
echo "  1. Run the wizard:    cryptsetup-beep --init"
echo "  2. Rebuild initramfs: sudo emerge --config sys-kernel/gentoo-kernel-bin"
echo "  3. Reboot, listen for the beep at the LUKS prompt."
