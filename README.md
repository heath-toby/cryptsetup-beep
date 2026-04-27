# cryptsetup-beep

An audible cue that plays the moment `systemd-cryptsetup` is ready to accept
a LUKS passphrase in the initramfs. Configurable for either the PC speaker
(via `beep(1)`) or any ALSA card, with an Orca-friendly GTK3 setup wizard.

Designed for blind and low-vision users who can't see a passphrase prompt
appear on screen, but useful for anyone who'd like a clear audio signal that
the system is ready for their input.

## Features

- **Distinct tones for distinct prompts.** A single 440Hz beep on the
  initial passphrase prompt, two 440Hz beeps on a passphrase retry, a
  single 880Hz beep (one octave up) on an initial token PIN prompt, and
  two 880Hz beeps on a PIN retry — so the audio tells you both *what*
  is being asked and *whether* you've already gotten it wrong once.
- Works with **passphrase** unlocks and with **systemd-cryptenroll** tokens
  (FIDO2 / TPM2): the prompt for a PIN or "please tap your token" is
  delivered through the same ask-password path.
- One small initramfs hook, two systemd units (`.path` + `.service`),
  generalised configuration in `/etc/cryptsetup-beep/config`.

## Installation

```sh
paru -S cryptsetup-beep        # AUR
# or
pacman -U ./cryptsetup-beep-*.pkg.tar.zst
```

After install, run the setup wizard:

```sh
cryptsetup-beep --init
```

The wizard:
1. Tests your PC speaker. If it works, you're done — choose "PC speaker".
2. If you didn't hear anything (most modern laptops), it lists ALSA devices
   and lets you Preview each before confirming.
3. Saves the configuration to `/etc/cryptsetup-beep/config`, edits
   `HOOKS=` in `/etc/mkinitcpio.conf` (with a backup) if the hook isn't
   already there, and rebuilds your initramfs.

Reboot. You should hear the beep just before the passphrase prompt.

## Requirements

- mkinitcpio with the `sd-encrypt` hook (this is the standard systemd-based
  initramfs path; `cryptsetup-beep` does not work with the legacy `encrypt`
  hook).
- `alsa-utils` (for `aplay` and `amixer` in ALSA mode).
- `beep` (for PC-speaker mode; optional otherwise).
- `python`, `python-gobject`, `gtk3` for the setup wizard.
- `polkit` for the wizard's privileged save step.

## Commands

```
cryptsetup-beep                # alias for --init
cryptsetup-beep --init         # interactive setup wizard
cryptsetup-beep --test         # play the configured beep on the live system
cryptsetup-beep --regen        # re-run mkinitcpio (asks for password)
```

## Troubleshooting

If the beep doesn't fire at boot:

1. **Check the journal.** From the running system after a boot:
   ```sh
   journalctl -b -u cryptsetup-beep.service -u cryptsetup-beep.path
   ```
   - **No entries** = the unit was never loaded. Verify
     `cryptsetup-beep` is in `HOOKS=` in `/etc/mkinitcpio.conf` and that
     the config file exists.
   - **Service ran but nothing was heard** = ALSA mixer state in cold-boot
     initramfs is different from your running system. Check the mixer
     values in `/etc/cryptsetup-beep/config` are sensible for your card.
   - **`start-limit-hit`** = the service is being re-fired in a loop
     before it can complete; report a bug with the journal output.

2. **Check the config.** `cat /etc/cryptsetup-beep/config` and confirm
   `METHOD`, `CODEC_MATCH`, and `CODEC_MODULES` look reasonable.

3. **Test on the live system.** `cryptsetup-beep --test` plays the
   configured beep without rebooting. If this doesn't work, neither will
   the boot-time version.

4. **Verify the initramfs.** `lsinitcpio /boot/...img | grep cryptsetup-beep`
   should show the script, units, WAV, and binaries.

## Uninstalling

`pacman -Rns cryptsetup-beep` cleans up after itself: a pre-transaction hook
removes `cryptsetup-beep` from `HOOKS=` in `/etc/mkinitcpio.conf` (writing a
timestamped backup beside it), and mkinitcpio's own post-remove hook then
rebuilds the initramfs without the now-deleted entry. `/etc/cryptsetup-beep/`
is treated as a config directory and left in place — remove it manually if
you don't intend to reinstall.

## How it works

Two systemd units run inside the initramfs:

- `cryptsetup-beep.path` watches `/run/systemd/ask-password/` for files
  matching `ask.*`. systemd-cryptsetup creates one of these files whenever
  it asks the user for input — passphrase, FIDO2 PIN, or token-tap prompt.
- `cryptsetup-beep.service` runs `play-beep.sh`, which sources
  `/etc/cryptsetup-beep/config`, plays the configured beep, and waits for
  the ask-password file to be consumed before exiting (so the .path unit
  doesn't re-trigger).

The wizard captures your hardware specifics — which ALSA card, which codec
to match in `/proc/asound/`, which kernel modules to bake into the
initramfs — and writes them to the config file. The mkinitcpio install hook
reads the config at build time and includes only what's needed.

For the *prompt-specific* tones, `play-beep.sh` reads the most recent
`/run/systemd/ask-password/ask.*` file's `Message=` line and matches it
against the strings systemd-cryptsetup uses (`Incorrect passphrase, try
again!`, `Please enter security token PIN:`, etc.). Token-PIN retries
reuse the same `Message=` as the initial PIN prompt, so the script keeps
a one-byte state file at `/run/cryptsetup-beep/pin-seen` to know whether
this is the first PIN ask of the boot or a follow-up. `/run` is tmpfs so
that file is naturally wiped at every reboot.

## Authors and acknowledgements

- **Design and project ownership:** Toby Heath ([@heath-toby](https://github.com/heath-toby))
- **Co-author / implementation collaborator:** Claude (Anthropic), via Claude Code

This project was developed in a multi-session collaboration. Toby drove the
design — the wizard flow, the accessibility requirements, the tradeoffs at
every decision point (PIN+tap vs tap-only, Headphone at 70%, the in-place
sysfs walk for module detection). Claude did most of the typing and the
routine engineering, plus a number of non-obvious diagnostic threads:

- The original "one typo halts boot" symptom turned out to be TTY line-buffer
  contamination during the cryptsetup Argon2 window, not a `tries=1` limit.
- `systemd-cryptsetup`'s ask-password path covers FIDO2 PIN prompts as well
  as passphrase prompts, so a single watcher unit handles both unlock flows
  without extra plumbing.
- The sd-encrypt initramfs is more minimal than commonly assumed — `tr`
  isn't present, and `add_module` rejects built-in kernel modules — both
  bugs we hit and fixed during this collaboration.

If you find this useful, the credit (and any blame) belongs to both
contributors.

## License

MIT.
