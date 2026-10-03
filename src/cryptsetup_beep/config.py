"""Read and write /etc/cryptsetup-beep/config.

The on-disk format is POSIX shell key=value, sourced by play-beep.sh.
Allowed keys are whitelisted both for forward-compatibility and as a defence
when --write-config validates a staging file before moving it to /etc.

Multi-device mode is opt-in: when DEVICE_1_NAME is present, the runtime
walks DEVICE_1..N in order and plays through the first available one. When
no DEVICE_N_* keys are present, the legacy single-device fields drive
playback exactly as before.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path("/etc/cryptsetup-beep/config")
STAGING_PATH = Path("/run/cryptsetup-beep-staging.config")

# Multi-device bounds.
MAX_DEVICES = 99
MIN_PROBE_TIMEOUT = 1
MAX_PROBE_TIMEOUT = 60

ALLOWED_KEYS = frozenset({
    "METHOD",
    "CODEC_MATCH",
    "CODEC_MODULES",
    "PCM_DEVICE",
    "MIXER_MASTER",
    "MIXER_SPEAKER",
    "MIXER_HEADPHONE",
    "MIXER_PCM",
    "BEEP_WAV",
    "PCSPKR_FREQ",
    "PCSPKR_LEN",
    "PROBE_TIMEOUT",
})

# Pattern for the per-device multi-device keys. The suffixes allowed are the
# only ones the runtime understands: NAME, MATCH, PCM, MIXER, VOLUME.
_DEVICE_KEY_RE = re.compile(r"^DEVICE_([0-9]+)_(NAME|MATCH|PCM|MIXER|VOLUME)$")

# Format of DEVICE_N_MATCH values.
_MATCH_RE = re.compile(
    r"^(codec:[A-Za-z0-9_.\-]+|usb:[0-9a-fA-F]{4}:[0-9a-fA-F]{4})$"
)

# Conservative regex for shell-safe values: alphanum, common punctuation,
# spaces inside quotes. Reject anything that could be code injection if the
# value got eval'd by the shell.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-\.\,\:\=\%\/\+\s]*$")


def is_allowed_key(key: str) -> bool:
    """True for a known-good key, static or DEVICE_N_* pattern."""
    if key in ALLOWED_KEYS:
        return True
    return _DEVICE_KEY_RE.match(key) is not None


@dataclass
class DeviceEntry:
    """One entry in the multi-device fallback list.

    `match` uses the same two kinds the runtime understands:
      - "codec:SUBSTRING" — grep against /proc/asound/cardN/codec*
      - "usb:VVVV:PPPP"   — exact match against /proc/asound/cardN/usbid
    `mixer` is optional: "CONTROL VALUE[,STATE]" (e.g. "Headphone 80,unmute").
    `volume` is optional (0-100). When set (>=0), the runtime applies it to
    the card's common controls (Master, PCM, Headphone, Speaker) before
    playing. Lets each device have its own level — handy when a dock
    headphone output is far louder than the internal speakers at the same
    nominal percentage. -1 means "don't touch the volume".
    """

    name: str
    match: str
    pcm: int = 0
    mixer: str = ""
    volume: int = -1


@dataclass
class BeepConfig:
    method: str = "alsa"  # "alsa" or "pcspkr"
    codec_match: str = ""
    codec_modules: str = ""  # space-separated list, kept as a single string
    pcm_device: int = 0
    mixer_master: str = "82%,on"
    mixer_speaker: str = "100%,on"
    mixer_headphone: str = "70%,on"
    mixer_pcm: str = "100%"
    beep_wav: str = "/usr/share/cryptsetup-beep/beep.wav"
    pcspkr_freq: int = 440
    pcspkr_len: int = 500
    probe_timeout: int = 5
    # Optional ordered list of fallback devices. When empty, the single-
    # device fields above drive playback. When non-empty, play-beep.sh
    # iterates these in order and plays through the first available one.
    devices: list[DeviceEntry] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path = CONFIG_PATH) -> "BeepConfig":
        if not path.exists():
            return cls()
        data = _parse_shell_kv(path.read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "BeepConfig":
        kwargs: dict[str, object] = {}
        # snake_case → SHELL_CASE map for scalar fields.
        for f in fields(cls):
            if f.name == "devices":
                continue
            shell_key = f.name.upper()
            if shell_key in data:
                value = data[shell_key]
                if f.type is int or f.type == "int":
                    try:
                        kwargs[f.name] = int(value)
                    except ValueError:
                        pass
                else:
                    kwargs[f.name] = value

        # Devices: scan for DEVICE_N_* keys and reconstruct in numeric order.
        devices_by_index: dict[int, dict[str, str]] = {}
        for key, value in data.items():
            m = _DEVICE_KEY_RE.match(key)
            if not m:
                continue
            idx = int(m.group(1))
            suffix = m.group(2)
            devices_by_index.setdefault(idx, {})[suffix] = value

        device_entries: list[DeviceEntry] = []
        for idx in sorted(devices_by_index):
            fieldset = devices_by_index[idx]
            name = fieldset.get("NAME", "")
            match = fieldset.get("MATCH", "")
            if not name or not match:
                # Incomplete entry — skip. validate() reports the error.
                continue
            try:
                pcm = int(fieldset.get("PCM", "0"))
            except ValueError:
                pcm = 0
            mixer = fieldset.get("MIXER", "")
            try:
                volume = int(fieldset.get("VOLUME", "-1"))
            except ValueError:
                volume = -1
            device_entries.append(DeviceEntry(
                name=name, match=match, pcm=pcm, mixer=mixer, volume=volume,
            ))
        kwargs["devices"] = device_entries

        return cls(**kwargs)  # type: ignore[arg-type]

    def to_shell(self) -> str:
        lines = ["# Generated by cryptsetup-beep --init", ""]
        for f in fields(self):
            if f.name == "devices":
                continue
            shell_key = f.name.upper()
            value = getattr(self, f.name)
            if isinstance(value, int):
                lines.append(f"{shell_key}={value}")
            else:
                # Always quote strings — robust for spaces, percents etc.
                lines.append(f"{shell_key}={shlex.quote(value)}")

        if self.devices:
            lines.append("")
            lines.append("# Multi-device fallback list. Tried in numeric order;")
            lines.append("# first available device wins. Overrides CODEC_MATCH etc.")
            for i, dev in enumerate(self.devices, start=1):
                lines.append(f"DEVICE_{i}_NAME={shlex.quote(dev.name)}")
                lines.append(f"DEVICE_{i}_MATCH={shlex.quote(dev.match)}")
                lines.append(f"DEVICE_{i}_PCM={dev.pcm}")
                if dev.mixer:
                    lines.append(f"DEVICE_{i}_MIXER={shlex.quote(dev.mixer)}")
                if dev.volume >= 0:
                    lines.append(f"DEVICE_{i}_VOLUME={dev.volume}")

        return "\n".join(lines) + "\n"

    def write_staging(self, path: Path = STAGING_PATH) -> Path:
        path.write_text(self.to_shell())
        os.chmod(path, 0o644)
        return path


def validate(data: dict[str, str]) -> list[str]:
    """Return a list of error strings; empty list means valid."""
    errors: list[str] = []
    device_indices: dict[int, dict[str, str]] = {}

    for key, value in data.items():
        if key in ALLOWED_KEYS:
            if not _VALUE_RE.match(value):
                errors.append(f"unsafe characters in {key}")
            continue
        m = _DEVICE_KEY_RE.match(key)
        if not m:
            errors.append(f"unknown key: {key}")
            continue
        idx = int(m.group(1))
        if idx < 1 or idx > MAX_DEVICES:
            errors.append(f"{key}: device index must be 1..{MAX_DEVICES}")
            continue
        suffix = m.group(2)
        if not _VALUE_RE.match(value):
            errors.append(f"unsafe characters in {key}")
            continue
        device_indices.setdefault(idx, {})[suffix] = value

    method = data.get("METHOD", "")
    if method and method not in ("alsa", "pcspkr"):
        errors.append(f"METHOD must be 'alsa' or 'pcspkr', got {method!r}")

    # PROBE_TIMEOUT bounds.
    probe = data.get("PROBE_TIMEOUT")
    if probe is not None:
        try:
            probe_int = int(probe)
        except ValueError:
            errors.append(f"PROBE_TIMEOUT must be an integer, got {probe!r}")
        else:
            if probe_int < MIN_PROBE_TIMEOUT or probe_int > MAX_PROBE_TIMEOUT:
                errors.append(
                    f"PROBE_TIMEOUT must be between {MIN_PROBE_TIMEOUT} and "
                    f"{MAX_PROBE_TIMEOUT}, got {probe_int}"
                )

    # Multi-device validation.
    if device_indices:
        sorted_indices = sorted(device_indices)
        # Contiguous from 1.
        expected = list(range(1, sorted_indices[-1] + 1))
        if sorted_indices != expected:
            missing = sorted(set(expected) - set(sorted_indices))
            errors.append(
                f"DEVICE_* numbering must be contiguous from 1; missing: "
                f"{', '.join(str(n) for n in missing)}"
            )
        for idx, fieldset in device_indices.items():
            # Required fields.
            name = fieldset.get("NAME", "")
            if not name:
                errors.append(f"DEVICE_{idx}_NAME is required and must be non-empty")
            match = fieldset.get("MATCH", "")
            if not match:
                errors.append(f"DEVICE_{idx}_MATCH is required")
            elif not _MATCH_RE.match(match):
                errors.append(
                    f"DEVICE_{idx}_MATCH {match!r} must be codec:SUBSTRING "
                    f"or usb:VVVV:PPPP"
                )
            pcm = fieldset.get("PCM")
            if pcm is None:
                errors.append(f"DEVICE_{idx}_PCM is required")
            else:
                try:
                    pcm_int = int(pcm)
                except ValueError:
                    errors.append(f"DEVICE_{idx}_PCM must be an integer, got {pcm!r}")
                else:
                    if pcm_int < 0:
                        errors.append(f"DEVICE_{idx}_PCM must be >= 0, got {pcm_int}")
            # MIXER is optional but if present must be plausibly shaped.
            mixer = fieldset.get("MIXER", "")
            if mixer and not re.match(
                r"^[A-Za-z0-9_ \-]+ [0-9]+%?(,[A-Za-z]+)?$", mixer
            ):
                errors.append(
                    f"DEVICE_{idx}_MIXER {mixer!r} must look like "
                    f"'CONTROL VALUE[,STATE]'"
                )
            # VOLUME is optional; if present must be an integer 0-100.
            vol = fieldset.get("VOLUME")
            if vol is not None:
                try:
                    vol_int = int(vol)
                except ValueError:
                    errors.append(
                        f"DEVICE_{idx}_VOLUME must be an integer, got {vol!r}"
                    )
                else:
                    if vol_int < 0 or vol_int > 100:
                        errors.append(
                            f"DEVICE_{idx}_VOLUME must be 0..100, got {vol_int}"
                        )

    return errors


def _parse_shell_kv(text: str) -> dict[str, str]:
    """Parse a tiny subset of shell: KEY=VALUE lines, with shell quoting.

    Comments (# ...) and blank lines are ignored. Multi-line values are not
    supported. Values are unquoted via shlex with POSIX rules.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier() and not re.match(r"^[A-Z_][A-Z0-9_]*$", key):
            continue
        # shlex.split handles quotes correctly; we keep the first token
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError:
            continue
        out[key] = tokens[0] if tokens else ""
    return out
