"""CLI entry point. Dispatch flags to the wizard, the test player, the
initramfs regeneration, or the privileged write-config path."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cryptsetup_beep import audio, hooks
from cryptsetup_beep.config import (
    BeepConfig,
    CONFIG_PATH,
    _parse_shell_kv,
    is_allowed_key,
    validate,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cryptsetup-beep",
        description="Audible cue for LUKS passphrase prompts in the initramfs.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true",
                       help="Run the interactive setup wizard (default).")
    group.add_argument("--test", action="store_true",
                       help="Play the configured beep on the live system.")
    group.add_argument("--regen", action="store_true",
                       help="Re-run mkinitcpio (requires admin password).")
    group.add_argument("--write-config", metavar="STAGING_FILE",
                       help="(internal, invoked by the wizard under pkexec).")
    args = parser.parse_args(argv)

    if args.write_config:
        return cmd_write_config(Path(args.write_config))
    if args.test:
        return cmd_test()
    if args.regen:
        return cmd_regen()
    return cmd_init()


def cmd_init() -> int:
    try:
        from cryptsetup_beep import wizard
    except ImportError as exc:
        print(
            f"GTK3/PyGObject is required for the setup wizard but failed to "
            f"import: {exc}",
            file=sys.stderr,
        )
        return 1
    return wizard.run()


def cmd_test() -> int:
    cfg = BeepConfig.from_file()
    try:
        audio.play_configured(cfg)
    except subprocess.CalledProcessError as exc:
        print(f"playback failed: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"required tool not installed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_regen() -> int:
    backend = hooks.detect_backend()
    if backend == "mkinitcpio":
        if os.geteuid() != 0:
            return _reexec_under_pkexec(["--regen"])
        return _run_mkinitcpio()
    if backend == "dracut":
        # We deliberately don't run dracut directly — the right rebuild
        # command depends on the user's kernel package (gentoo-kernel-bin,
        # gentoo-sources via genkernel, etc.).
        print(
            "On a dracut system, --regen does not rebuild the initramfs because "
            "the correct command depends on your kernel package.\n"
            "\n"
            "On Gentoo: sudo emerge --config sys-kernel/gentoo-kernel-bin\n"
            "Elsewhere: sudo dracut --regenerate-all --force"
        )
        return 0
    print("No supported initramfs system found.", file=sys.stderr)
    return 1


def cmd_write_config(staging: Path) -> int:
    """Privileged path: validate the staging file, install it, edit the
    initramfs config if necessary, and either rebuild (mkinitcpio) or
    print a reminder (dracut)."""
    if os.geteuid() != 0:
        print("--write-config must be run as root (use pkexec)", file=sys.stderr)
        return 2
    if not staging.is_file():
        print(f"staging file not found: {staging}", file=sys.stderr)
        return 2

    try:
        text = staging.read_text()
    except OSError as exc:
        print(f"could not read {staging}: {exc}", file=sys.stderr)
        return 2

    parsed = _parse_shell_kv(text)
    extra = sorted(k for k in parsed if not is_allowed_key(k))
    if extra:
        print(f"refused: unknown keys in staging file: {extra}", file=sys.stderr)
        return 2
    errors = validate(parsed)
    if errors:
        print("refused: " + "; ".join(errors), file=sys.stderr)
        return 2

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(staging, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o644)

    backend = hooks.detect_backend()
    status = hooks.inspect()

    if backend == "mkinitcpio":
        if not status.has_sd_encrypt:
            print(
                "warning: sd-encrypt is not in HOOKS in /etc/mkinitcpio.conf; "
                "the beep won't run unless you add it.",
                file=sys.stderr,
            )
        elif not status.has_hook:
            backup = hooks.apply(status)
            if backup:
                print(f"edited /etc/mkinitcpio.conf (backup at {backup})")
        return _run_mkinitcpio()

    if backend == "dracut":
        if not status.has_conf:
            backup = hooks.apply(status)
            if backup:
                print(
                    f"replaced /etc/dracut.conf.d/cryptsetup-beep.conf "
                    f"(backup at {backup})"
                )
            else:
                print("wrote /etc/dracut.conf.d/cryptsetup-beep.conf")
        print(hooks.post_apply_message())
        return 0

    print("No supported initramfs system found.", file=sys.stderr)
    return 1


def _run_mkinitcpio() -> int:
    print("running mkinitcpio -P")
    result = subprocess.run(["mkinitcpio", "-P"])
    return result.returncode


def _reexec_under_pkexec(extra_args: list[str]) -> int:
    pkexec = shutil.which("pkexec") or "/usr/bin/pkexec"
    self_bin = shutil.which("cryptsetup-beep") or "/usr/bin/cryptsetup-beep"
    result = subprocess.run([pkexec, self_bin] + extra_args)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
