"""Text-mode setup wizard for cryptsetup-beep.

A parallel to wizard.py (GTK3) for TTY / no-DE sessions. Dispatched
automatically from __main__.cmd_init when no DISPLAY / WAYLAND_DISPLAY
is set, or when GTK fails to import. Plain input()/print() — no curses,
no dialog(1), no third-party deps. Toby uses Fenrir on the TTY which
reads each printed line as it appears, so a linear prompt flow is the
right accessibility shape.

Design mirrors the GTK wizard where it can: same device labels
(audio._candidate_label equivalent), same volume semantics (default 70,
capture current, custom 0-100), same "add = top priority" ordering,
same final sudo cryptsetup-beep --write-config apply path.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from typing import Callable

from cryptsetup_beep import audio, hooks, pcspkr
from cryptsetup_beep.config import BeepConfig, DeviceEntry, STAGING_PATH

# PROBE_TIMEOUT default when the TUI writes a config. Higher than the
# library default (5) because the main user of this path has a TB dock
# that needs ~5-10s to enumerate before the beep can play through it.
_DEFAULT_PROBE_TIMEOUT = 15
_DEFAULT_VOLUME = 70


# --- Prompting primitives --------------------------------------------------

def _prompt_choice(prompt: str, options: list[str]) -> int:
    """Print a numbered menu, read a choice, return the 1-based index.

    Re-prompts on invalid / empty input. Raises KeyboardInterrupt
    unchanged so callers can clean up and exit gracefully.
    """
    low, high = 1, len(options)
    while True:
        print()
        print(prompt)
        for i, option in enumerate(options, start=1):
            print(f"  {i}. {option}")
        raw = input(f"Choice ({low}-{high}): ").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            print(f"Please enter a number between {low} and {high}.")
            continue
        if value < low or value > high:
            print(f"Please enter a number between {low} and {high}.")
            continue
        return value


def _prompt_int(prompt: str, low: int, high: int) -> int:
    """Prompt for an integer in [low, high], re-asking on bad input."""
    while True:
        raw = input(f"{prompt} ({low}-{high}): ").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            print(f"Please enter a number between {low} and {high}.")
            continue
        if value < low or value > high:
            print(f"Please enter a number between {low} and {high}.")
            continue
        return value


def _candidate_label(dev: audio.DeviceInfo) -> str:
    """Human-readable device label — matches wizard._candidate_label."""
    parts = [dev.card_name]
    if dev.codec_name:
        parts.append(f"codec {dev.codec_name}")
    if dev.usb_vendor_product:
        parts.append(f"USB {dev.usb_vendor_product}")
    return " — ".join(parts)


# --- Sub-flows -------------------------------------------------------------

def _ask_volume(device_label: str, card_index: int) -> int:
    """Prompt the user for a volume (0-100). Offers default, capture, custom."""
    current = audio.get_current_volume(card_index)
    options = [
        f"Default ({_DEFAULT_VOLUME})",
        (
            f"Capture current (currently {current})"
            if current is not None
            else "Capture current (not available)"
        ),
        "Enter custom (0-100)",
    ]
    while True:
        print()
        print(f'Volume for "{device_label}":')
        choice = _prompt_choice("Pick an option:", options)
        if choice == 1:
            return _DEFAULT_VOLUME
        if choice == 2:
            if current is None:
                print("No mixer control reports a current value for this card.")
                continue
            return current
        if choice == 3:
            return _prompt_int("Volume", 0, 100)


def _build_entry(dev: audio.DeviceInfo, volume: int) -> DeviceEntry:
    """DeviceEntry from a discovered device + chosen volume."""
    match = dev.suggested_match()
    pcm = dev.pcm_devices[0] if dev.pcm_devices else 0
    name = dev.codec_name or dev.card_id or dev.card_name
    return DeviceEntry(name=name, match=match, pcm=pcm, mixer="", volume=volume)


def _print_fallback_list(entries: list[DeviceEntry]) -> None:
    """Pretty-print the current fallback list (shared by multi-device loop
    and the summary)."""
    if not entries:
        print("  (empty)")
        return
    for i, entry in enumerate(entries, start=1):
        vol = f", volume {entry.volume}" if entry.volume >= 0 else ""
        print(f"  {i}. {entry.name} ({entry.match}, PCM {entry.pcm}{vol})")


def _multi_device_loop(
    devices: list[audio.DeviceInfo],
) -> list[DeviceEntry] | None:
    """Interactive add-devices-to-fallback-list loop. Returns the final
    ordered list, or None if the user chose Back."""
    chosen: list[DeviceEntry] = []
    while True:
        print()
        print("Current fallback list (tried in order):")
        _print_fallback_list(chosen)
        options = [_candidate_label(d) for d in devices]
        done_idx = len(options) + 1
        back_idx = len(options) + 2
        options.append("Done — go to summary")
        options.append("Back — return to device selection")
        choice = _prompt_choice("Available devices to add:", options)
        if choice == done_idx:
            if not chosen:
                print("Add at least one device before continuing.")
                continue
            return chosen
        if choice == back_idx:
            return None
        # User picked a real device.
        dev = devices[choice - 1]
        label = _candidate_label(dev)
        volume = _ask_volume(label, dev.card_index)
        entry = _build_entry(dev, volume)
        # Insert at position 0 so each new add becomes top priority —
        # matches the GTK wizard's "add fallback first, add preferred
        # device next and it takes over" mental model.
        chosen.insert(0, entry)
        print(f'Added "{entry.name}" to position 1 of the fallback list.')


def _collect_codec_modules(entries: list[DeviceEntry]) -> str:
    """Union of modules_for_card() across every entry's card. Space-separated
    string suitable for CODEC_MODULES in the config."""
    seen: set[str] = set()
    modules: list[str] = []
    all_devs = {d.card_index: d for d in audio.discover_all_output_devices()}
    # Resolve each entry to a card via a live scan — matching USB vendor:product
    # or codec substring, same way the runtime play-beep.sh does. If the device
    # isn't present right now (e.g., unplugged dock being pre-configured for
    # future use), modules_for_card can't help — the dracut/initcpio hook
    # auto-adds snd_usb_audio when it sees a usb: match, which covers that case.
    for entry in entries:
        for card_index, dev in all_devs.items():
            matched = False
            if entry.match.startswith("codec:") and dev.codec_name:
                token = entry.match[len("codec:"):]
                if token.lower() in dev.codec_name.lower():
                    matched = True
            elif entry.match.startswith("usb:") and dev.usb_vendor_product:
                if entry.match[len("usb:"):].lower() == dev.usb_vendor_product.lower():
                    matched = True
            if matched:
                for m in audio.modules_for_card(card_index):
                    if m not in seen:
                        seen.add(m)
                        modules.append(m)
                break
    return " ".join(modules)


# --- Summary + apply -------------------------------------------------------

def _print_summary(config: BeepConfig) -> None:
    print()
    print("=== Summary ===")
    print()
    if config.method == "pcspkr":
        print("Beep method: PC speaker")
        print(f"Frequency: {config.pcspkr_freq} Hz")
        print(f"Length:    {config.pcspkr_len} ms")
    else:
        print("Beep method: ALSA")
        print()
        print("Fallback device list (tried in order):")
        _print_fallback_list(config.devices)
        print()
        print(f"Probe timeout: {config.probe_timeout} seconds")
        if config.codec_modules:
            print(f"Codec modules: {config.codec_modules}")


def _apply(config: BeepConfig) -> int:
    """Write staging + call sudo cryptsetup-beep --write-config. Streams
    the privileged process's output to the terminal."""
    try:
        config.write_staging(STAGING_PATH)
    except OSError as exc:
        print(f"Could not write staging file: {exc}", file=sys.stderr)
        return 1
    print()
    print(f"Staging file written to {STAGING_PATH}.")
    print("Invoking sudo cryptsetup-beep --write-config …")
    print()
    # TTY use: sudo lets Toby type his password at the terminal; pkexec
    # would raise an unexpected GUI prompt here (see feedback_pkexec).
    result = subprocess.run(
        ["sudo", "cryptsetup-beep", "--write-config", str(STAGING_PATH)],
        check=False,
    )
    if result.returncode != 0:
        print()
        print(f"write-config exited with status {result.returncode}.", file=sys.stderr)
        return result.returncode
    print()
    print(hooks.post_apply_message())
    return 0


# --- Top-level entry -------------------------------------------------------

def run() -> int:
    """Text-mode wizard entry. Returns a shell exit code."""
    try:
        return _run_inner()
    except KeyboardInterrupt:
        print()
        print("Cancelled. No changes made.")
        return 0
    except EOFError:
        print()
        print("End of input. Cancelled. No changes made.")
        return 0


def _run_inner() -> int:
    print("Welcome to cryptsetup-beep setup.")
    print()
    print(
        "This picks a beep to play when the LUKS passphrase prompt appears "
        "in the initramfs. You can test the PC speaker first, or skip "
        "straight to sound-card setup."
    )

    choice = _prompt_choice(
        "Would you like to test the PC speaker?",
        ["Yes", "No"],
    )
    config: BeepConfig | None = None
    if choice == 1:
        try:
            pcspkr.test()
        except pcspkr.PcSpkrUnavailable as exc:
            print(f"PC speaker test unavailable: {exc}")
            print("Falling through to sound-card setup.")
        else:
            keep = _prompt_choice(
                "Did you hear it? Use the PC speaker?",
                [
                    "Yes — use PC speaker for the beep",
                    "No — set up a sound card instead",
                ],
            )
            if keep == 1:
                config = BeepConfig(method="pcspkr")

    # If we didn't commit to pcspkr, run the ALSA flow (which loops back
    # here on "Back" from the summary).
    while config is None:
        config = _run_alsa_flow()

    while True:
        _print_summary(config)
        choice = _prompt_choice(
            "What next?",
            [
                "Apply (writes /etc/cryptsetup-beep/config and rebuilds initramfs — asks for your password)",
                "Back — go to device selection",
            ],
        )
        if choice == 1:
            return _apply(config)
        # Back: for pcspkr, flip to the ALSA flow fresh. For alsa, re-run
        # the alsa flow from the device-selection screen.
        config = None
        while config is None:
            config = _run_alsa_flow()


def _run_alsa_flow() -> BeepConfig | None:
    """Device selection → either single-device or the multi-device loop →
    BeepConfig. Returns None if the user cancels (Back from multi loop
    with nothing picked); caller re-enters."""
    devices = audio.discover_all_output_devices()
    if not devices:
        print()
        print("No ALSA output devices found. Can't configure the beep.")
        print("Check that /proc/asound exists and that your sound hardware")
        print("is detected by the kernel.")
        return BeepConfig()  # bail out with an empty config so apply fails cleanly

    print()
    print("Available sound devices (choose one, or set up a fallback list):")
    options = [_candidate_label(d) for d in devices]
    multi_idx = len(options) + 1
    options.append("Multi-device configuration (fallback list)")
    choice = _prompt_choice("Pick a device:", options)

    if choice == multi_idx:
        entries = _multi_device_loop(devices)
        if entries is None:
            return None  # user went Back; caller re-prompts device selection
    else:
        dev = devices[choice - 1]
        label = _candidate_label(dev)
        volume = _ask_volume(label, dev.card_index)
        entries = [_build_entry(dev, volume)]

    codec_modules = _collect_codec_modules(entries)
    return BeepConfig(
        method="alsa",
        devices=entries,
        codec_modules=codec_modules,
        probe_timeout=_DEFAULT_PROBE_TIMEOUT,
    )


if __name__ == "__main__":
    sys.exit(run())
