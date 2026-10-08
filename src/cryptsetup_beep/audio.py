"""ALSA enumeration and preview-playback helpers.

Used by the wizard for Page 3 (device selection + preview button) and by
`cryptsetup-beep --test` to play the configured beep on the live system.
The runtime player in initramfs is the shell script play-beep.sh — we don't
share code with it; we just produce equivalent behaviour.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptsetup_beep.config import BeepConfig, DeviceEntry


@dataclass(frozen=True)
class DeviceInfo:
    """Snapshot of one ALSA card for the multi-device wizard.

    Produced by `discover_all_output_devices()`. Carries everything the
    wizard needs to suggest a MATCH string (codec: or usb:) and a PCM
    index, plus a friendly description to show the user.
    """

    card_index: int
    card_id: str                     # /proc/asound/card<N>/id
    card_name: str                   # friendly name from /proc/asound/cards
    codec_name: str | None           # first "Codec: ..." line, if any
    usb_vendor_product: str | None   # /proc/asound/card<N>/usbid, if any
    pcm_devices: list[int]           # playback PCM indices present

    def suggested_match(self) -> str:
        """Default MATCH string for this device: usb: if USB, else codec:."""
        if self.usb_vendor_product:
            return f"usb:{self.usb_vendor_product}"
        if self.codec_name:
            tokens = self.codec_name.split()
            for token in reversed(tokens):
                if re.match(r"^[A-Z0-9]{3,}$", token):
                    return f"codec:{token}"
            return f"codec:{self.codec_name}"
        return ""


@dataclass(frozen=True)
class AlsaDevice:
    """A single playback target the wizard offers the user."""

    pcm_spec: str           # "plughw:CARD=Generic_1,DEV=0" — exactly what aplay -D wants
    card_index: int         # numeric card index from /proc/asound
    pcm_device: int         # device index on that card
    description: str        # human label, e.g. "HDA Intel PCH, SN6140 Analog"
    codec_string: str = ""  # e.g. "Conexant SN6140" — derived from /proc/asound

    @property
    def codec_match(self) -> str:
        """Substring suitable for /proc/asound/cardN/codec* grep at boot."""
        if not self.codec_string:
            return ""
        # Take the last token that looks like a model code — e.g. "SN6140"
        # for "Conexant SN6140". Falls back to the whole string.
        tokens = self.codec_string.split()
        for token in reversed(tokens):
            if re.match(r"^[A-Z0-9]{3,}$", token):
                return token
        return self.codec_string


_APLAY_LINE = re.compile(
    r"^card (?P<card>\d+): (?P<card_id>\S+) \[(?P<card_name>[^\]]+)\], "
    r"device (?P<device>\d+): [^\[]*\[(?P<device_name>[^\]]+)\]"
)


def enumerate_alsa_devices() -> list[AlsaDevice]:
    """Return playback devices from `aplay -l`.

    Parses lines like
        card 1: Generic_1 [HD-Audio Generic], device 0: SN6140 Analog [SN6140 Analog]
    and constructs plughw:CARD=<id>,DEV=<n> for each. We use the named CARD=
    form rather than the numeric `plughw:N,M` because it survives card-number
    drift between the running system and other contexts.
    """
    try:
        output = subprocess.check_output(
            ["aplay", "-l"], text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    devices: list[AlsaDevice] = []
    for line in output.splitlines():
        match = _APLAY_LINE.match(line.rstrip())
        if not match:
            continue
        card_index = int(match.group("card"))
        card_id = match.group("card_id")
        card_name = match.group("card_name")
        pcm_device = int(match.group("device"))
        device_name = match.group("device_name")
        codec = _codec_string_for_card(card_index)

        spec = f"plughw:CARD={card_id},DEV={pcm_device}"
        description = f"{card_name}: {device_name}"
        if codec:
            description += f" (codec {codec})"

        devices.append(
            AlsaDevice(
                pcm_spec=spec,
                card_index=card_index,
                pcm_device=pcm_device,
                description=description,
                codec_string=codec,
            )
        )
    return devices


def modules_for_card(card_index: int) -> list[str]:
    """Find every kernel module needed to drive ALSA card `card_index`.

    Walks /sys/class/sound/cardN's device tree upward, collecting the driver
    of every parent device along the way. This captures HDA (snd_hda_intel
    on the PCI parent), USB audio (snd_usb_audio on the USB interface, plus
    xhci_hcd/usbcore on the controllers), and anything else with a sysfs
    representation.

    For HDA cards, also includes every currently-loaded snd_hda_codec_*
    module — those bind dynamically to codec slots discovered at runtime, so
    they don't show up as a driver of any sysfs device until probe time.
    """
    modules: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        normalised = name.replace("-", "_")
        if normalised and normalised not in seen:
            seen.add(normalised)
            modules.append(normalised)

    card_dir = Path(f"/sys/class/sound/card{card_index}")
    if card_dir.exists():
        device = (card_dir / "device").resolve()
        # Walk upward through the sysfs device tree
        sys_root = Path("/sys")
        while device != sys_root and device.exists():
            driver_link = device / "driver"
            if driver_link.is_symlink():
                add(os.path.basename(os.readlink(str(driver_link))))
            parent = device.parent
            if parent == device:
                break
            device = parent

    # HDA codec modules bind to codec slots inside snd_hda_intel rather than
    # to a sysfs device with their own driver symlink — sweep /proc/modules
    # for them whenever we see snd_hda_intel in the device chain.
    if "snd_hda_intel" in seen:
        try:
            with open("/proc/modules") as f:
                for line in f:
                    name = line.split()[0]
                    if name.startswith("snd_hda_codec_"):
                        add(name)
        except OSError:
            pass

    # Filter out modules that aren't real loadable .ko files — built-in
    # kernel modules (xhci_hcd, often) and aliases like "usb"/"pcieport"
    # that modinfo can't resolve. mkinitcpio's add_module would warn on
    # each, even though the resulting initramfs is fine without them.
    return [m for m in modules if _is_loadable_module(m)]


def _is_loadable_module(name: str) -> bool:
    """Return True iff `name` corresponds to a real .ko file on this kernel.

    `modinfo -F filename` returns the path for loadable modules, the literal
    string "(builtin)" for built-in modules, and exits non-zero for unknown
    names. We only want loadable modules — the others either don't need to
    be in the initramfs (builtins are already there) or aren't really there.
    """
    try:
        result = subprocess.run(
            ["modinfo", "-F", "filename", name],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return True  # If modinfo isn't installed, be permissive.
    if result.returncode != 0:
        return False
    return result.stdout.strip() != "(builtin)"


def get_current_volume(card_index: int) -> int | None:
    """Read the current playback percentage from the first common output control
    that responds on the given card. Returns an int 0-100, or None if no
    control responded (very unusual — most cards expose at least one of these).

    Used by the wizard's "Capture current" button so the user can level-match a
    device's boot-time volume to what they're hearing live before saving.
    """
    for control in ("Master", "PCM", "Headphone", "Speaker"):
        try:
            out = subprocess.run(
                ["amixer", "-c", str(card_index), "get", control],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if out.returncode != 0:
            continue
        match = re.search(r"\[(\d+)%\]", out.stdout)
        if match:
            return int(match.group(1))
    return None


def apply_volume_to_card(card_index: int, volume: int) -> None:
    """Set the given 0-100 volume on the card's output chain. Matches what
    DEVICE_N_VOLUME does in play-beep.sh so wizard tests behave exactly as
    the boot runtime will.

    HDA (and most USB audio) controls Master → PCM → Headphone/Speaker as a
    SERIAL attenuator chain, so writing 70% to every control stacks down to
    ~34% effective. Instead:
      1. Unmute every common control (so no muted link kills audio).
      2. Set volume on the first-available top-level control, in priority
         order: Master → PCM → Headphone → Speaker. Only one is set, so our
         requested percent is the ACTUAL effective percent.
    """
    if volume < 0 or volume > 100:
        return
    card = str(card_index)
    # Unmute every plausible control (silent on controls that don't exist).
    for control in ("Master", "PCM", "Headphone", "Speaker"):
        subprocess.run(
            ["amixer", "-c", card, "-q", "sset", control, "unmute"],
            stderr=subprocess.DEVNULL, check=False,
        )
    # Set volume on the first available top-level control.
    for control in ("Master", "PCM", "Headphone", "Speaker"):
        result = subprocess.run(
            ["amixer", "-c", card, "sset", control, f"{volume}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode == 0:
            return


def preview_beep_entry(device: AlsaDevice, entry: DeviceEntry, wav_path: Path) -> None:
    """Preview a MULTI-DEVICE entry — mirrors exactly what play-beep.sh does
    per device in multi-device mode:
      1. apply the entry's volume (if set) to the card's common controls
      2. apply the entry's mixer string (if set) — fine-grained override
      3. aplay through plughw:card,pcm
    Deliberately does NOT touch the single-device config.mixer_* values —
    those are irrelevant when the runtime is in multi-device mode. Keeps the
    test faithful to boot-time behaviour.

    Raises subprocess.CalledProcessError on aplay failure; mixer failures are
    swallowed as usual (controls missing from a card are normal).
    """
    card = str(device.card_index)
    if entry.volume >= 0:
        apply_volume_to_card(device.card_index, entry.volume)
    if entry.mixer:
        parts = entry.mixer.split(" ", 1)
        if len(parts) == 2:
            control, value = parts
            subprocess.run(
                ["amixer", "-c", card, "-q", "sset", control]
                + value.replace(",", " ").split(),
                stderr=subprocess.DEVNULL, check=False,
            )
    subprocess.run(
        ["aplay", "-q", "-D", device.pcm_spec, str(wav_path)],
        check=True,
    )


def preview_beep(device: AlsaDevice, wav_path: Path, config: BeepConfig) -> None:
    """Play the beep for the wizard's Preview button.

    Sets the same mixer values the runtime player would, then runs aplay.
    Raises subprocess.CalledProcessError on aplay failure; mixer failures are
    swallowed (they're common and not fatal — e.g. some cards lack 'PCM').
    """
    card = str(device.card_index)
    for control, value in (
        ("Master", config.mixer_master),
        ("Speaker", config.mixer_speaker),
        ("Headphone", config.mixer_headphone),
        ("PCM", config.mixer_pcm),
    ):
        if not value:
            continue
        args = ["amixer", "-c", card, "-q", "sset", control] + value.replace(",", " ").split()
        subprocess.run(args, stderr=subprocess.DEVNULL, check=False)

    subprocess.run(
        ["aplay", "-q", "-D", device.pcm_spec, str(wav_path)],
        check=True,
    )


def play_configured(config: BeepConfig) -> None:
    """Used by `cryptsetup-beep --test`. Plays via the configured method."""
    if config.method == "pcspkr":
        subprocess.run(
            ["beep", "-f", str(config.pcspkr_freq), "-l", str(config.pcspkr_len)],
            check=True,
        )
        return

    # ALSA path: find any card whose codec matches CODEC_MATCH.
    for asound in sorted(Path("/proc/asound").glob("card[0-9]*")):
        card_index = int(asound.name.removeprefix("card"))
        codec_files = list(asound.glob("codec*"))
        if config.codec_match:
            if not any(
                config.codec_match.lower() in p.read_text(errors="replace").lower()
                for p in codec_files
                if p.is_file()
            ):
                continue
        device = AlsaDevice(
            pcm_spec=f"plughw:{card_index},{config.pcm_device}",
            card_index=card_index,
            pcm_device=config.pcm_device,
            description=f"card {card_index}",
        )
        preview_beep(device, Path(config.beep_wav), config)
        return
    raise RuntimeError(f"no ALSA card matched CODEC_MATCH={config.codec_match!r}")


# Helpers --------------------------------------------------------------------


def _codec_string_for_card(card_index: int) -> str:
    """Read the first 'Codec: ...' line from /proc/asound/cardN/codec*."""
    for codec in sorted(Path(f"/proc/asound/card{card_index}").glob("codec*")):
        if not codec.is_file():
            continue
        try:
            for line in codec.read_text(errors="replace").splitlines():
                if line.startswith("Codec:"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            continue
    return ""


_CARDS_HEADER_RE = re.compile(
    r"^\s*(?P<idx>\d+)\s+\[(?P<id>[^\]]+)\]:\s*(?P<desc>.+)$"
)


def discover_all_output_devices() -> list[DeviceInfo]:
    """Return every ALSA card that has at least one playback PCM.

    Reads directly from /proc/asound (not `aplay -l`) so the wizard can
    recommend MATCH strings based on exactly the signals the runtime
    player sees in the initramfs: codec name from /proc/asound/cardN/codec*,
    USB vendor:product from /proc/asound/cardN/usbid.

    Cards with no playback PCM (microphone-only USB mics, etc.) are
    filtered out — they're never useful as a beep target.
    """
    cards_text = ""
    try:
        cards_text = Path("/proc/asound/cards").read_text(errors="replace")
    except OSError:
        return []

    names_by_index: dict[int, tuple[str, str]] = {}
    current_index: int | None = None
    for line in cards_text.splitlines():
        header = _CARDS_HEADER_RE.match(line)
        if header is not None:
            current_index = int(header.group("idx"))
            card_id = header.group("id").strip()
            # The description runs onto the next line in the standard format;
            # we take the header's trailing text as a first-pass label.
            names_by_index[current_index] = (card_id, header.group("desc").strip())
            continue
        # The second line usually carries the longer human description.
        if current_index is not None and line.strip():
            card_id, _ = names_by_index[current_index]
            names_by_index[current_index] = (card_id, line.strip())
            current_index = None

    devices: list[DeviceInfo] = []
    for card_dir in sorted(Path("/proc/asound").glob("card[0-9]*")):
        try:
            card_index = int(card_dir.name.removeprefix("card"))
        except ValueError:
            continue

        # Playback PCMs expose /dev/snd/pcmC<idx>D<n>p; /proc/asound/cardN
        # also exposes pcm<n>p directories. Prefer the /proc view (available
        # in the initramfs too) and extract the device index from each.
        pcm_devices: list[int] = []
        for pcm_dir in sorted(card_dir.glob("pcm*p")):
            name = pcm_dir.name  # e.g. "pcm0p"
            digits = name[3:-1]
            if digits.isdigit():
                pcm_devices.append(int(digits))
        if not pcm_devices:
            continue

        codec = _codec_string_for_card(card_index) or None

        usbid: str | None = None
        usbid_path = card_dir / "usbid"
        if usbid_path.is_file():
            try:
                raw = usbid_path.read_text(errors="replace").strip()
                if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$", raw):
                    usbid = raw.lower()
            except OSError:
                pass

        card_id, card_name = names_by_index.get(
            card_index, (card_dir.name, card_dir.name)
        )
        devices.append(
            DeviceInfo(
                card_index=card_index,
                card_id=card_id,
                card_name=card_name,
                codec_name=codec,
                usb_vendor_product=usbid,
                pcm_devices=pcm_devices,
            )
        )

    return devices
