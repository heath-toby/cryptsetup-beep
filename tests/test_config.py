"""Round-trip tests for the config parse/write logic."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cryptsetup_beep.config import (  # noqa: E402
    ALLOWED_KEYS,
    MAX_PROBE_TIMEOUT,
    BeepConfig,
    DeviceEntry,
    _parse_shell_kv,
    is_allowed_key,
    validate,
)


class RoundTripTests(unittest.TestCase):
    def test_defaults_round_trip(self) -> None:
        config = BeepConfig()
        text = config.to_shell()
        parsed = _parse_shell_kv(text)
        result = BeepConfig.from_dict(parsed)
        self.assertEqual(config, result)

    def test_alsa_round_trip(self) -> None:
        config = BeepConfig(
            method="alsa",
            codec_match="SN6140",
            codec_modules="snd_hda_intel snd_hda_codec_conexant",
            pcm_device=0,
            mixer_master="82%,on",
            mixer_speaker="100%,on",
            mixer_headphone="70%,on",
            mixer_pcm="100%",
        )
        with tempfile.NamedTemporaryFile("w+", suffix=".conf", delete=False) as fh:
            fh.write(config.to_shell())
            path = Path(fh.name)
        try:
            result = BeepConfig.from_file(path)
            self.assertEqual(config, result)
        finally:
            path.unlink(missing_ok=True)

    def test_pcspkr_round_trip(self) -> None:
        config = BeepConfig(
            method="pcspkr",
            codec_match="",
            codec_modules="",
            pcspkr_freq=523,
            pcspkr_len=750,
        )
        text = config.to_shell()
        parsed = _parse_shell_kv(text)
        self.assertEqual(parsed["METHOD"], "pcspkr")
        self.assertEqual(parsed["PCSPKR_FREQ"], "523")
        result = BeepConfig.from_dict(parsed)
        self.assertEqual(config, result)


class ValidationTests(unittest.TestCase):
    def test_default_passes(self) -> None:
        config = BeepConfig()
        text = config.to_shell()
        parsed = _parse_shell_kv(text)
        self.assertEqual(validate(parsed), [])

    def test_unknown_key_rejected(self) -> None:
        bad = {"METHOD": "alsa", "EVIL": "rm -rf /"}
        errors = validate(bad)
        self.assertTrue(any("unknown key" in e for e in errors))

    def test_invalid_method_rejected(self) -> None:
        bad = {"METHOD": "shell-injection;rm -rf /"}
        errors = validate(bad)
        self.assertTrue(any("METHOD" in e or "unsafe" in e for e in errors))

    def test_unsafe_chars_rejected(self) -> None:
        bad = {"METHOD": "alsa", "CODEC_MATCH": "foo`whoami`"}
        errors = validate(bad)
        self.assertTrue(any("unsafe" in e for e in errors))

    def test_known_keys_only(self) -> None:
        # Sanity: all scalar dataclass fields must appear in ALLOWED_KEYS as
        # their SHELL_CASE equivalents, otherwise the privileged write path
        # would reject configs the wizard itself produces. The `devices`
        # field is serialised as DEVICE_N_* keys and validated by pattern,
        # not by ALLOWED_KEYS membership.
        for field_name in BeepConfig().__dict__:
            if field_name == "devices":
                continue
            self.assertIn(field_name.upper(), ALLOWED_KEYS,
                          f"{field_name.upper()} missing from ALLOWED_KEYS")


class MultiDeviceTests(unittest.TestCase):
    """Validation + round-trip coverage for DEVICE_N_* + PROBE_TIMEOUT."""

    def test_device_keys_accepted(self) -> None:
        self.assertTrue(is_allowed_key("DEVICE_1_NAME"))
        self.assertTrue(is_allowed_key("DEVICE_12_MATCH"))
        self.assertTrue(is_allowed_key("DEVICE_1_PCM"))
        self.assertTrue(is_allowed_key("DEVICE_1_MIXER"))
        self.assertFalse(is_allowed_key("DEVICE_1_UNKNOWN"))
        self.assertFalse(is_allowed_key("DEVICE_X_NAME"))

    def test_single_device_entry_valid(self) -> None:
        data = {
            "METHOD": "alsa",
            "DEVICE_1_NAME": "Dock headphones",
            "DEVICE_1_MATCH": "usb:2188:6647",
            "DEVICE_1_PCM": "0",
        }
        self.assertEqual(validate(data), [])

    def test_two_devices_valid(self) -> None:
        data = {
            "METHOD": "alsa",
            "DEVICE_1_NAME": "Dock",
            "DEVICE_1_MATCH": "usb:2188:6647",
            "DEVICE_1_PCM": "0",
            "DEVICE_2_NAME": "Internal",
            "DEVICE_2_MATCH": "codec:SN6140",
            "DEVICE_2_PCM": "0",
            "DEVICE_2_MIXER": "Headphone 80,unmute",
        }
        self.assertEqual(validate(data), [])

    def test_invalid_match_rejected(self) -> None:
        for bad in ("usb:invalid", "codec:", "pci:xyz", "usb:12:34",
                    "codec: ", "usb:2188:664"):
            data = {
                "DEVICE_1_NAME": "x",
                "DEVICE_1_MATCH": bad,
                "DEVICE_1_PCM": "0",
            }
            errors = validate(data)
            self.assertTrue(
                any("DEVICE_1_MATCH" in e for e in errors),
                f"expected rejection for match {bad!r}, got {errors!r}",
            )

    def test_device_numbering_gap_rejected(self) -> None:
        data = {
            "DEVICE_1_NAME": "a",
            "DEVICE_1_MATCH": "codec:X",
            "DEVICE_1_PCM": "0",
            "DEVICE_3_NAME": "c",
            "DEVICE_3_MATCH": "codec:Y",
            "DEVICE_3_PCM": "0",
        }
        errors = validate(data)
        self.assertTrue(any("contiguous" in e for e in errors), errors)

    def test_device_name_required(self) -> None:
        data = {
            "DEVICE_1_MATCH": "codec:X",
            "DEVICE_1_PCM": "0",
        }
        errors = validate(data)
        self.assertTrue(any("DEVICE_1_NAME" in e for e in errors), errors)

    def test_device_pcm_required(self) -> None:
        data = {
            "DEVICE_1_NAME": "a",
            "DEVICE_1_MATCH": "codec:X",
        }
        errors = validate(data)
        self.assertTrue(any("DEVICE_1_PCM" in e for e in errors), errors)

    def test_probe_timeout_bounds(self) -> None:
        self.assertEqual(validate({"PROBE_TIMEOUT": "5"}), [])
        self.assertEqual(validate({"PROBE_TIMEOUT": "1"}), [])
        self.assertEqual(validate({"PROBE_TIMEOUT": str(MAX_PROBE_TIMEOUT)}), [])
        self.assertTrue(validate({"PROBE_TIMEOUT": "0"}))
        self.assertTrue(validate({"PROBE_TIMEOUT": str(MAX_PROBE_TIMEOUT + 1)}))
        self.assertTrue(validate({"PROBE_TIMEOUT": "nope"}))

    def test_probe_timeout_default_in_dataclass(self) -> None:
        config = BeepConfig()
        self.assertEqual(config.probe_timeout, 5)

    def test_multi_device_round_trip(self) -> None:
        config = BeepConfig(
            method="alsa",
            codec_modules="snd_hda_intel snd_usb_audio",
            probe_timeout=7,
            devices=[
                DeviceEntry(name="Dock", match="usb:2188:6647", pcm=0,
                            mixer="Headphone 80,unmute"),
                DeviceEntry(name="Internal", match="codec:SN6140", pcm=0),
            ],
        )
        text = config.to_shell()
        parsed = _parse_shell_kv(text)
        # Scalar keys the shell should see.
        self.assertEqual(parsed["PROBE_TIMEOUT"], "7")
        self.assertEqual(parsed["DEVICE_1_NAME"], "Dock")
        self.assertEqual(parsed["DEVICE_1_MATCH"], "usb:2188:6647")
        self.assertEqual(parsed["DEVICE_1_PCM"], "0")
        self.assertEqual(parsed["DEVICE_1_MIXER"], "Headphone 80,unmute")
        self.assertEqual(parsed["DEVICE_2_NAME"], "Internal")
        self.assertEqual(parsed["DEVICE_2_MATCH"], "codec:SN6140")
        # Round-trip reconstructs the DeviceEntry list in order.
        result = BeepConfig.from_dict(parsed)
        self.assertEqual(len(result.devices), 2)
        self.assertEqual(result.devices[0], config.devices[0])
        self.assertEqual(result.devices[1], config.devices[1])
        self.assertEqual(validate(parsed), [])

    def test_single_and_multi_coexist_in_file(self) -> None:
        # Both sets of fields can be present on disk; the runtime switches
        # modes based on DEVICE_1_NAME. Config validation accepts both.
        data = {
            "METHOD": "alsa",
            "CODEC_MATCH": "SN6140",
            "PCM_DEVICE": "0",
            "DEVICE_1_NAME": "Dock",
            "DEVICE_1_MATCH": "usb:2188:6647",
            "DEVICE_1_PCM": "0",
        }
        self.assertEqual(validate(data), [])
        parsed = BeepConfig.from_dict(data)
        # Single-device scalar fields still populate.
        self.assertEqual(parsed.codec_match, "SN6140")
        # Devices list is populated too; runtime prefers it.
        self.assertEqual(len(parsed.devices), 1)

    def test_device_out_of_range_rejected(self) -> None:
        data = {
            "DEVICE_100_NAME": "x",
            "DEVICE_100_MATCH": "codec:X",
            "DEVICE_100_PCM": "0",
        }
        errors = validate(data)
        self.assertTrue(any("device index" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
