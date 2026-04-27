"""GTK3 setup wizard for cryptsetup-beep.

Single Gtk.Window holding a Gtk.Stack with five pages, navigated forward and
backward by user choice. Designed for Orca: ATK roles set explicitly, mnemonic
labels on every actionable button, focus moved deterministically on each page
transition, and the high-frequency AT-SPI events suppressed during page swaps
to prevent flooding (the same _EVENTS_TO_SUSPEND list polyglot-for-orca uses).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import gi  # type: ignore[import-not-found]

gi.require_version("Atk", "1.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, GLib, Gtk  # type: ignore[import-not-found]  # noqa: E402

from cryptsetup_beep import audio, hooks, pcspkr
from cryptsetup_beep.config import BeepConfig
from cryptsetup_beep.widgets import (
    FocusManagedListBox,
    PreviewExitListBox,
    set_atk,
)

WAV_PATH = Path("/usr/share/cryptsetup-beep/beep.wav")
PKEXEC = "/usr/bin/pkexec"
SELF_BIN = "/usr/bin/cryptsetup-beep"

_EVENTS_TO_SUSPEND = (
    "object:state-changed:showing",
    "object:state-changed:visible",
    "object:children-changed:add",
    "object:children-changed:remove",
    "object:property-change:accessible-name",
    "object:property-change:accessible-description",
)


def _suspend_events() -> None:
    try:
        from orca import event_manager  # type: ignore[import-not-found]
        manager = event_manager.get_manager()
        for event in _EVENTS_TO_SUSPEND:
            manager.deregister_listener(event)
    except Exception:
        pass


def _resume_events() -> bool:
    try:
        from orca import event_manager  # type: ignore[import-not-found]
        manager = event_manager.get_manager()
        for event in _EVENTS_TO_SUSPEND:
            manager.register_listener(event)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------


def _page(margin: int = 18, spacing: int = 12) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    box.set_margin_top(margin)
    box.set_margin_bottom(margin)
    box.set_margin_start(margin)
    box.set_margin_end(margin)
    return box


def _heading(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    ctx = label.get_style_context()
    ctx.add_class("heading")
    set_atk(label, role=Atk.Role.HEADING, name=text)
    return label


def _body(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.set_line_wrap(True)
    label.set_max_width_chars(60)
    set_atk(label, role=Atk.Role.LABEL, description=text)
    return label


def _button(label: str, mnemonic: bool = True, atk_name: str | None = None) -> Gtk.Button:
    btn = Gtk.Button.new_with_mnemonic(label) if mnemonic else Gtk.Button(label=label)
    set_atk(btn, role=Atk.Role.PUSH_BUTTON, name=atk_name or label.replace("_", ""))
    return btn


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class Wizard:
    PAGE_WELCOME = "welcome"
    PAGE_HEARD = "heard"
    PAGE_ALSA = "alsa"
    PAGE_REVIEW = "review"
    PAGE_DONE = "done"

    def __init__(self) -> None:
        self.config = BeepConfig.from_file()  # start from existing if present

        _suspend_events()

        self.window = Gtk.Window()
        self.window.set_title("cryptsetup-beep setup")
        self.window.set_default_size(560, 420)
        self.window.connect("destroy", Gtk.main_quit)
        set_atk(self.window, role=Atk.Role.FRAME, name="cryptsetup-beep setup")

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.window.add(self.stack)

        self.stack.add_named(self._build_welcome(), self.PAGE_WELCOME)
        self.stack.add_named(self._build_heard(), self.PAGE_HEARD)
        self.stack.add_named(self._build_alsa(), self.PAGE_ALSA)
        self.stack.add_named(self._build_review(), self.PAGE_REVIEW)
        self.stack.add_named(self._build_done(), self.PAGE_DONE)

        # Where Page 4's Back button should jump to (depends on the path taken).
        self._back_target = self.PAGE_WELCOME

        self.window.show_all()
        self._show_page(self.PAGE_WELCOME)
        GLib.timeout_add(500, _resume_events)

    # ---- Page 1: Welcome / PC speaker test --------------------------------

    def _build_welcome(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("cryptsetup-beep setup"), False, False, 0)
        page.pack_start(_body(
            "This wizard configures an audible cue that will play during boot when "
            "your LUKS passphrase prompt appears.\n\nFirst, let's see whether your "
            "machine has a working PC speaker. Press the Test button to play a beep."
        ), False, False, 0)

        self.welcome_status = Gtk.Label()
        self.welcome_status.set_xalign(0)
        page.pack_start(self.welcome_status, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.test_btn = _button("_Test PC speaker", atk_name="Test PC speaker")
        self.test_btn.connect("clicked", self._on_test_pcspkr)
        buttons.pack_start(self.test_btn, False, False, 0)

        self.skip_btn = _button("Skip to _ALSA", atk_name="Skip to ALSA selection")
        self.skip_btn.connect("clicked", lambda *_: self._goto(self.PAGE_ALSA))
        buttons.pack_start(self.skip_btn, False, False, 0)

        page.pack_start(buttons, False, False, 0)
        return page

    def _on_test_pcspkr(self, _btn: Gtk.Button) -> None:
        try:
            pcspkr.test()
        except pcspkr.PcSpkrUnavailable:
            self.welcome_status.set_text(
                "The 'beep' package isn't installed. We'll use ALSA instead."
            )
            set_atk(self.welcome_status, description=self.welcome_status.get_text())
            self._goto(self.PAGE_ALSA)
            return
        self._goto(self.PAGE_HEARD)

    # ---- Page 2: Did you hear it? -----------------------------------------

    def _build_heard(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Did you hear the beep?"), False, False, 0)
        page.pack_start(_body(
            "Did you hear a beep through the PC speaker? If yes, we'll use the PC "
            "speaker for boot beeps. If no, we'll let you pick an ALSA audio device "
            "instead."
        ), False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.yes_btn = _button("_Yes, use PC speaker", atk_name="Yes, use PC speaker")
        self.yes_btn.connect("clicked", self._on_pcspkr_chosen)
        buttons.pack_start(self.yes_btn, False, False, 0)

        self.no_btn = _button("_No, try ALSA", atk_name="No, try ALSA selection")
        self.no_btn.connect("clicked", lambda *_: self._goto(self.PAGE_ALSA))
        buttons.pack_start(self.no_btn, False, False, 0)

        page.pack_start(buttons, False, False, 0)
        return page

    def _on_pcspkr_chosen(self, _btn: Gtk.Button) -> None:
        self.config = replace(self.config, method="pcspkr",
                              codec_match="", codec_modules="")
        self._back_target = self.PAGE_HEARD
        self._goto(self.PAGE_REVIEW)

    # ---- Page 3: ALSA device selection ------------------------------------

    def _build_alsa(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Select an audio device"), False, False, 0)
        page.pack_start(_body(
            "Choose the device the boot beep should play through. Use Up and Down "
            "arrows to navigate the list. Press Tab to reach the Preview button — "
            "Preview plays the beep through the selected device. Press OK to save "
            "your choice and continue."
        ), False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_vexpand(True)

        self.alsa_list = PreviewExitListBox()
        self.alsa_radios: list[Gtk.RadioButton] = []
        self.alsa_devices: list[audio.AlsaDevice] = []
        self._populate_alsa_list()
        scrolled.add(self.alsa_list)
        page.pack_start(scrolled, True, True, 0)

        self.alsa_status = Gtk.Label()
        self.alsa_status.set_xalign(0)
        page.pack_start(self.alsa_status, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.preview_btn = _button("_Preview", atk_name="Preview selected device")
        self.preview_btn.connect("clicked", self._on_alsa_preview)
        buttons.pack_start(self.preview_btn, False, False, 0)

        self.alsa_ok_btn = _button("_OK", atk_name="OK, save selection")
        self.alsa_ok_btn.connect("clicked", self._on_alsa_ok)
        buttons.pack_start(self.alsa_ok_btn, False, False, 0)

        self.alsa_cancel_btn = _button("_Cancel", atk_name="Cancel and quit")
        self.alsa_cancel_btn.connect("clicked", lambda *_: self.window.close())
        buttons.pack_start(self.alsa_cancel_btn, False, False, 0)

        page.pack_start(buttons, False, False, 0)

        self.alsa_list.set_exit_forward(self.preview_btn)
        return page

    def _populate_alsa_list(self) -> None:
        devices = audio.enumerate_alsa_devices()
        if not devices:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.add(Gtk.Label(label="No ALSA devices found via 'aplay -L'."))
            self.alsa_list.add(row)
            self.alsa_devices = []
            return

        first_radio: Gtk.RadioButton | None = None
        for device in devices:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            radio = Gtk.RadioButton.new_with_label_from_widget(first_radio, device.description)
            if first_radio is None:
                first_radio = radio
                radio.set_active(True)
            radio.set_margin_top(6)
            radio.set_margin_bottom(6)
            radio.set_margin_start(12)
            radio.set_margin_end(12)
            atk_desc = f"{device.description}. ALSA spec {device.pcm_spec}."
            if device.codec_string:
                atk_desc += f" Codec {device.codec_string}."
            set_atk(radio, role=Atk.Role.RADIO_BUTTON,
                    name=device.description, description=atk_desc)
            row.add(radio)
            self.alsa_list.add_row_with_widget(row, radio)
            self.alsa_radios.append(radio)
        self.alsa_devices = devices

    def _selected_alsa_device(self) -> audio.AlsaDevice | None:
        for radio, device in zip(self.alsa_radios, self.alsa_devices):
            if radio.get_active():
                return device
        return None

    def _on_alsa_preview(self, _btn: Gtk.Button) -> None:
        device = self._selected_alsa_device()
        if device is None:
            self._set_status(self.alsa_status, "No device selected.")
            return
        try:
            audio.preview_beep(device, WAV_PATH, self.config)
            self._set_status(self.alsa_status, f"Played beep through {device.description}.")
        except subprocess.CalledProcessError as exc:
            self._set_status(self.alsa_status, f"Playback failed (aplay exit {exc.returncode}).")
        except FileNotFoundError:
            self._set_status(self.alsa_status, "aplay or amixer isn't installed.")

    def _on_alsa_ok(self, _btn: Gtk.Button) -> None:
        device = self._selected_alsa_device()
        if device is None:
            self._set_status(self.alsa_status, "No device selected.")
            return
        self.config = replace(
            self.config,
            method="alsa",
            codec_match=device.codec_match,
            codec_modules=" ".join(audio.modules_for_card(device.card_index)),
            pcm_device=device.pcm_device,
        )
        self._back_target = self.PAGE_ALSA
        self._goto(self.PAGE_REVIEW)

    # ---- Page 4: Review ---------------------------------------------------

    def _build_review(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Review and apply"), False, False, 0)
        page.pack_start(_body(
            "Review the configuration. Apply will save it, edit /etc/mkinitcpio.conf "
            "if needed, and rebuild your initramfs. You'll be asked for your password."
        ), False, False, 0)

        self.review_list = FocusManagedListBox()
        page.pack_start(self.review_list, False, False, 0)

        self.hooks_status_label = Gtk.Label()
        self.hooks_status_label.set_xalign(0)
        self.hooks_status_label.set_line_wrap(True)
        page.pack_start(self.hooks_status_label, False, False, 0)

        self.review_status = Gtk.Label()
        self.review_status.set_xalign(0)
        self.review_status.set_line_wrap(True)
        page.pack_start(self.review_status, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.apply_btn = _button("_Apply (admin password required)",
                                 atk_name="Apply, requires admin password")
        self.apply_btn.connect("clicked", self._on_apply)
        buttons.pack_start(self.apply_btn, False, False, 0)

        self.review_back_btn = _button("_Back", atk_name="Back to previous page")
        self.review_back_btn.connect("clicked", lambda *_: self._goto(self._back_target))
        buttons.pack_start(self.review_back_btn, False, False, 0)

        self.review_cancel_btn = _button("_Cancel", atk_name="Cancel and quit")
        self.review_cancel_btn.connect("clicked", lambda *_: self.window.close())
        buttons.pack_start(self.review_cancel_btn, False, False, 0)

        page.pack_start(buttons, False, False, 0)
        return page

    def _refresh_review(self) -> None:
        for child in self.review_list.get_children():
            self.review_list.remove(child)
        self.review_list._widgets.clear()
        self.review_list._rows.clear()

        rows = [
            ("Method", self.config.method),
            ("Codec match", self.config.codec_match or "(none)"),
            ("Codec modules", self.config.codec_modules or "(none)"),
            ("PCM device", str(self.config.pcm_device)),
            ("Master volume", self.config.mixer_master),
            ("Speaker volume", self.config.mixer_speaker),
            ("Headphone volume", self.config.mixer_headphone),
            ("PCM volume", self.config.mixer_pcm),
        ]
        if self.config.method == "pcspkr":
            rows = [r for r in rows if r[0] in ("Method",)] + [
                ("Frequency (Hz)", str(self.config.pcspkr_freq)),
                ("Length (ms)", str(self.config.pcspkr_len)),
            ]

        for label_text, value in rows:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            label = Gtk.Label()
            label.set_xalign(0)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            label.set_margin_start(12)
            label.set_margin_end(12)
            label.set_text(f"{label_text}: {value}")
            label.set_selectable(True)  # selectable labels are focusable for Orca
            set_atk(label, role=Atk.Role.LABEL,
                    name=label_text, description=f"{label_text} is {value}")
            row.add(label)
            self.review_list.add_row_with_widget(row, label)
        self.review_list.show_all()

        try:
            status = hooks.inspect()
            text = hooks.diff_summary(status)
            self.hooks_status_label.set_text(text)
            set_atk(self.hooks_status_label, description=text)
            self.apply_btn.set_sensitive(status.has_sd_encrypt)
        except OSError as exc:
            self.hooks_status_label.set_text(
                f"Could not read /etc/mkinitcpio.conf: {exc}"
            )
            self.apply_btn.set_sensitive(False)

    def _on_apply(self, _btn: Gtk.Button) -> None:
        self.apply_btn.set_sensitive(False)
        self._set_status(self.review_status, "Working — please wait…")
        threading.Thread(target=self._apply_worker, daemon=True).start()

    def _apply_worker(self) -> None:
        # Write a staging file we own, then call ourselves under pkexec to
        # validate, install, and rebuild the initramfs.
        try:
            fd, path = tempfile.mkstemp(prefix="cryptsetup-beep-", suffix=".config")
            os.close(fd)
            staging = Path(path)
            staging.write_text(self.config.to_shell())
            os.chmod(staging, 0o644)
        except OSError as exc:
            GLib.idle_add(self._apply_done, False, f"failed to write staging file: {exc}")
            return

        try:
            result = subprocess.run(
                [PKEXEC, SELF_BIN, "--write-config", str(staging)],
                capture_output=True, text=True,
            )
        except FileNotFoundError:
            GLib.idle_add(self._apply_done, False, f"{PKEXEC} not found")
            staging.unlink(missing_ok=True)
            return
        finally:
            staging.unlink(missing_ok=True)

        if result.returncode == 0:
            GLib.idle_add(self._apply_done, True, result.stdout)
        else:
            stderr = result.stderr.strip() or f"exit code {result.returncode}"
            GLib.idle_add(self._apply_done, False, stderr)

    def _apply_done(self, ok: bool, message: str) -> bool:
        self.apply_btn.set_sensitive(True)
        if ok:
            self._goto(self.PAGE_DONE)
        else:
            self._set_status(self.review_status, f"Apply failed: {message}")
        return False

    # ---- Page 5: Done -----------------------------------------------------

    def _build_done(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Setup complete"), False, False, 0)
        page.pack_start(_body(
            "cryptsetup-beep is configured and your initramfs has been rebuilt. "
            "Reboot to hear the beep at the LUKS passphrase prompt.\n\n"
            "If something doesn't work, run 'cryptsetup-beep --test' to play the "
            "configured beep on the live system, or check 'journalctl -b -u "
            "cryptsetup-beep.service -u cryptsetup-beep.path' after a boot."
        ), False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.done_btn = _button("_Close", atk_name="Close wizard")
        self.done_btn.connect("clicked", lambda *_: self.window.close())
        buttons.pack_start(self.done_btn, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    # ---- Plumbing ---------------------------------------------------------

    def _goto(self, page_name: str) -> None:
        _suspend_events()
        self.stack.set_visible_child_name(page_name)
        if page_name == self.PAGE_REVIEW:
            self._refresh_review()
        self._show_page(page_name)
        GLib.timeout_add(500, _resume_events)

    def _show_page(self, page_name: str) -> None:
        # Set initial focus per page for Orca-friendly navigation.
        first_widget = {
            self.PAGE_WELCOME: lambda: self.test_btn,
            self.PAGE_HEARD:   lambda: self.yes_btn,
            self.PAGE_ALSA:    lambda: (self.alsa_radios[0] if self.alsa_radios else self.preview_btn),
            self.PAGE_REVIEW:  lambda: self.apply_btn,
            self.PAGE_DONE:    lambda: self.done_btn,
        }.get(page_name)
        if first_widget is not None:
            GLib.idle_add(lambda: first_widget().grab_focus() or False)

    @staticmethod
    def _set_status(label: Gtk.Label, text: str) -> None:
        label.set_text(text)
        set_atk(label, description=text)


def run() -> int:
    Wizard()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(run())
