"""Round-trip tests for the config parse/write logic."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cryptsetup_beep.config import (  # noqa: E402
    ALLOWED_KEYS,
    BeepConfig,
    _parse_shell_kv,
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
        # Sanity: all dataclass fields must appear in ALLOWED_KEYS as their
        # SHELL_CASE equivalents, otherwise the privileged write path would
        # reject configs the wizard itself produces.
        for field_name in BeepConfig().__dict__:
            self.assertIn(field_name.upper(), ALLOWED_KEYS,
                          f"{field_name.upper()} missing from ALLOWED_KEYS")


if __name__ == "__main__":
    unittest.main()
