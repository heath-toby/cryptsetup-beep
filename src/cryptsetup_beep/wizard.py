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
from cryptsetup_beep.config import BeepConfig, DeviceEntry
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


def _device_in_use_by_pipewire(card_index: int) -> bool:
    """Best-effort: is the ALSA card currently held by PipeWire (or PulseAudio)?

    Live-system device tests fail with aplay -EBUSY when the daemon has the
    card open exclusively. We detect it via `pactl list sinks short` matching
    an alsa_output sink name that mentions the card id. If pactl isn't
    installed, return False and let the caller fall back to the generic error."""
    try:
        out = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if out.returncode != 0:
        return False
    # Resolve card index to card id (sinks name their card by id, not index).
    try:
        card_id = Path(f"/proc/asound/card{card_index}/id").read_text().strip()
    except OSError:
        return False
    for line in out.stdout.splitlines():
        # Lines look like:
        #  <id> alsa_output.usb-CalDigit...-Rear-00.analog-stereo PipeWire ... RUNNING
        if "alsa_output" in line and card_id in line and "RUNNING" in line:
            return True
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
    PAGE_MULTI = "multi"
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
        self.stack.add_named(self._build_multi(), self.PAGE_MULTI)
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

        # Advanced path: multiple fallback devices.
        advanced = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.multi_entry_btn = _button(
            "Set up _multiple fallback devices (advanced)",
            atk_name="Set up multiple fallback devices (advanced)",
        )
        self.multi_entry_btn.connect("clicked", lambda *_: self._goto(self.PAGE_MULTI))
        advanced.pack_start(self.multi_entry_btn, False, False, 0)
        page.pack_start(advanced, False, False, 0)

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

    # ---- Page 3b: Multiple fallback devices (advanced) --------------------

    def _build_multi(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Multiple fallback devices"), False, False, 0)
        page.pack_start(_body(
            "Pick one or more audio devices for the boot beep. Add your "
            "always-available fallback first — the device you want when "
            "nothing else is connected. Then add any devices you'd prefer "
            "when they're available; each new addition takes top priority "
            "over the ones already in the list. For example: add internal "
            "speakers first, then dock headphones — the beep plays through "
            "the dock when the dock is connected, otherwise through the "
            "speakers. Use Move up / Move down to re-order if needed. If no "
            "configured device is available at boot, the beep is silent "
            "and boot continues normally."
        ), False, False, 0)

        # --- Candidates (devices available on this machine) ---------------
        page.pack_start(_body("Available devices (choose one, then Add):"),
                        False, False, 0)
        cand_scrolled = Gtk.ScrolledWindow()
        cand_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        cand_scrolled.set_min_content_height(120)
        cand_scrolled.set_vexpand(True)

        self.multi_cand_list = FocusManagedListBox()
        self.multi_cand_radios: list[Gtk.RadioButton] = []
        self.multi_cand_devices: list[audio.DeviceInfo] = []
        self._populate_multi_candidates()
        cand_scrolled.add(self.multi_cand_list)
        page.pack_start(cand_scrolled, True, True, 0)

        # Volume row — slider sets the level that will be applied to the
        # device's common controls (Master/PCM/Headphone/Speaker) at boot
        # time. "Capture current" reads the device's current level from
        # ALSA so you can level-match live and freeze that in. Default 70
        # is a middle-of-the-road starting point; different devices need
        # different levels (dock headphones vs internal speakers can be
        # 30 dB apart at the same nominal percent).
        vol_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        vol_row.pack_start(Gtk.Label(label="Volume:"), False, False, 0)
        self.multi_vol_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 1,
        )
        self.multi_vol_scale.set_value(70)
        self.multi_vol_scale.set_hexpand(True)
        self.multi_vol_scale.set_digits(0)
        self.multi_vol_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.multi_vol_scale.get_accessible().set_name("Device volume percent")
        vol_row.pack_start(self.multi_vol_scale, True, True, 0)
        self.multi_vol_capture_btn = _button(
            "_Capture current",
            atk_name="Capture volume currently set on the selected device",
        )
        self.multi_vol_capture_btn.connect("clicked", self._on_multi_vol_capture)
        vol_row.pack_start(self.multi_vol_capture_btn, False, False, 0)
        page.pack_start(vol_row, False, False, 0)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.multi_add_btn = _button("_Add selected (takes top priority)",
                                     atk_name="Add selected device as top priority")
        self.multi_add_btn.connect("clicked", self._on_multi_add)
        add_row.pack_start(self.multi_add_btn, False, False, 0)
        page.pack_start(add_row, False, False, 0)

        # --- Chosen devices in priority order -----------------------------
        page.pack_start(_body("Current fallback list (tried in order):"),
                        False, False, 0)
        chosen_scrolled = Gtk.ScrolledWindow()
        chosen_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        chosen_scrolled.set_min_content_height(120)
        chosen_scrolled.set_vexpand(True)

        self.multi_chosen_list = FocusManagedListBox()
        self.multi_chosen_radios: list[Gtk.RadioButton] = []
        self.multi_chosen_entries: list[DeviceEntry] = list(self.config.devices)
        self._populate_multi_chosen()
        chosen_scrolled.add(self.multi_chosen_list)
        page.pack_start(chosen_scrolled, True, True, 0)

        self.multi_status = Gtk.Label()
        self.multi_status.set_xalign(0)
        self.multi_status.set_line_wrap(True)
        page.pack_start(self.multi_status, False, False, 0)

        row_ops = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.multi_up_btn = _button("Move _up", atk_name="Move selected device up")
        self.multi_up_btn.connect("clicked", self._on_multi_up)
        row_ops.pack_start(self.multi_up_btn, False, False, 0)
        self.multi_down_btn = _button("Move _down", atk_name="Move selected device down")
        self.multi_down_btn.connect("clicked", self._on_multi_down)
        row_ops.pack_start(self.multi_down_btn, False, False, 0)
        self.multi_remove_btn = _button("_Remove", atk_name="Remove selected device")
        self.multi_remove_btn.connect("clicked", self._on_multi_remove)
        row_ops.pack_start(self.multi_remove_btn, False, False, 0)
        self.multi_test_btn = _button("_Test", atk_name="Test selected device")
        self.multi_test_btn.connect("clicked", self._on_multi_test)
        row_ops.pack_start(self.multi_test_btn, False, False, 0)
        page.pack_start(row_ops, False, False, 0)

        row_main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.multi_ok_btn = _button("_OK (save fallback list)",
                                    atk_name="OK, save fallback list")
        self.multi_ok_btn.connect("clicked", self._on_multi_ok)
        row_main.pack_start(self.multi_ok_btn, False, False, 0)
        self.multi_back_btn = _button("_Back", atk_name="Back to single-device page")
        self.multi_back_btn.connect("clicked", lambda *_: self._goto(self.PAGE_ALSA))
        row_main.pack_start(self.multi_back_btn, False, False, 0)
        self.multi_cancel_btn = _button("_Cancel", atk_name="Cancel and quit")
        self.multi_cancel_btn.connect("clicked", lambda *_: self.window.close())
        row_main.pack_start(self.multi_cancel_btn, False, False, 0)
        page.pack_start(row_main, False, False, 0)
        return page

    def _populate_multi_candidates(self) -> None:
        devices = audio.discover_all_output_devices()
        self.multi_cand_devices = devices
        self.multi_cand_radios = []
        for child in self.multi_cand_list.get_children():
            self.multi_cand_list.remove(child)
        self.multi_cand_list._widgets.clear()
        self.multi_cand_list._rows.clear()

        if not devices:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.add(Gtk.Label(label="No output devices found in /proc/asound."))
            self.multi_cand_list.add(row)
            return

        first: Gtk.RadioButton | None = None
        for dev in devices:
            label = self._candidate_label(dev)
            radio = Gtk.RadioButton.new_with_label_from_widget(first, label)
            if first is None:
                first = radio
                radio.set_active(True)
            radio.set_margin_top(4)
            radio.set_margin_bottom(4)
            radio.set_margin_start(12)
            radio.set_margin_end(12)
            atk_desc = (
                f"{label}. Suggested match {dev.suggested_match() or 'none'}. "
                f"PCM {dev.pcm_devices[0] if dev.pcm_devices else '?'}."
            )
            set_atk(radio, role=Atk.Role.RADIO_BUTTON,
                    name=label, description=atk_desc)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.add(radio)
            self.multi_cand_list.add_row_with_widget(row, radio)
            self.multi_cand_radios.append(radio)
        self.multi_cand_list.show_all()

    def _populate_multi_chosen(self) -> None:
        self.multi_chosen_radios = []
        for child in self.multi_chosen_list.get_children():
            self.multi_chosen_list.remove(child)
        self.multi_chosen_list._widgets.clear()
        self.multi_chosen_list._rows.clear()

        if not self.multi_chosen_entries:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.add(Gtk.Label(label="(no devices added yet)"))
            self.multi_chosen_list.add(row)
            self.multi_chosen_list.show_all()
            return

        first: Gtk.RadioButton | None = None
        for i, entry in enumerate(self.multi_chosen_entries, start=1):
            vol_part = f", volume {entry.volume}%" if entry.volume >= 0 else ""
            label = f"{i}. {entry.name} ({entry.match}, PCM {entry.pcm}{vol_part})"
            radio = Gtk.RadioButton.new_with_label_from_widget(first, label)
            if first is None:
                first = radio
                radio.set_active(True)
            radio.set_margin_top(4)
            radio.set_margin_bottom(4)
            radio.set_margin_start(12)
            radio.set_margin_end(12)
            set_atk(radio, role=Atk.Role.RADIO_BUTTON,
                    name=label, description=label)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.add(radio)
            self.multi_chosen_list.add_row_with_widget(row, radio)
            self.multi_chosen_radios.append(radio)
        self.multi_chosen_list.show_all()

    @staticmethod
    def _candidate_label(dev: audio.DeviceInfo) -> str:
        parts = [dev.card_name]
        if dev.codec_name:
            parts.append(f"codec {dev.codec_name}")
        if dev.usb_vendor_product:
            parts.append(f"USB {dev.usb_vendor_product}")
        return " — ".join(parts)

    def _selected_candidate(self) -> audio.DeviceInfo | None:
        for radio, dev in zip(self.multi_cand_radios, self.multi_cand_devices):
            if radio.get_active():
                return dev
        return None

    def _selected_chosen_index(self) -> int | None:
        for i, radio in enumerate(self.multi_chosen_radios):
            if radio.get_active():
                return i
        return None

    def _on_multi_add(self, _btn: Gtk.Button) -> None:
        dev = self._selected_candidate()
        if dev is None:
            self._set_status(self.multi_status, "No candidate selected.")
            return
        match = dev.suggested_match()
        if not match:
            self._set_status(
                self.multi_status,
                f"Can't suggest a MATCH for {dev.card_name} — no codec or USB id.",
            )
            return
        pcm = dev.pcm_devices[0] if dev.pcm_devices else 0
        name = dev.card_name
        if dev.codec_name:
            name = dev.codec_name
        elif dev.usb_vendor_product:
            name = dev.card_id or dev.card_name
        volume = int(self.multi_vol_scale.get_value())
        entry = DeviceEntry(
            name=name, match=match, pcm=pcm, mixer="", volume=volume,
        )
        # Insert at position 0 so each new add becomes the new top priority
        # (DEVICE_1). Previously-added devices shift down the list to make
        # room, which matches the "add fallback first, add preferred device
        # next and it takes over" mental model.
        self.multi_chosen_entries.insert(0, entry)
        self._populate_multi_chosen()
        self._set_status(
            self.multi_status,
            f"Added {entry.name} ({entry.match}, volume {volume}%) "
            f"at top of priority list.",
        )

    def _on_multi_vol_capture(self, _btn: Gtk.Button) -> None:
        """Read the selected candidate's current ALSA volume into the slider.
        Lets the user adjust volume live (via their normal tools), hear it,
        then freeze that level into the config with one click."""
        dev = self._selected_candidate()
        if dev is None:
            self._set_status(
                self.multi_status,
                "No candidate selected — select a device first, then Capture current.",
            )
            return
        vol = audio.get_current_volume(dev.card_index)
        if vol is None:
            self._set_status(
                self.multi_status,
                f"Could not read current volume on {dev.card_name} "
                f"(no Master/PCM/Headphone/Speaker control responded).",
            )
            return
        self.multi_vol_scale.set_value(vol)
        self._set_status(
            self.multi_status,
            f"Captured {vol}% from {dev.card_name}.",
        )

    def _on_multi_up(self, _btn: Gtk.Button) -> None:
        idx = self._selected_chosen_index()
        if idx is None or idx == 0:
            return
        entries = self.multi_chosen_entries
        entries[idx - 1], entries[idx] = entries[idx], entries[idx - 1]
        self._populate_multi_chosen()
        # Re-select the moved entry (now at idx-1).
        if 0 <= idx - 1 < len(self.multi_chosen_radios):
            self.multi_chosen_radios[idx - 1].set_active(True)

    def _on_multi_down(self, _btn: Gtk.Button) -> None:
        idx = self._selected_chosen_index()
        if idx is None or idx >= len(self.multi_chosen_entries) - 1:
            return
        entries = self.multi_chosen_entries
        entries[idx + 1], entries[idx] = entries[idx], entries[idx + 1]
        self._populate_multi_chosen()
        if 0 <= idx + 1 < len(self.multi_chosen_radios):
            self.multi_chosen_radios[idx + 1].set_active(True)

    def _on_multi_remove(self, _btn: Gtk.Button) -> None:
        idx = self._selected_chosen_index()
        if idx is None:
            return
        removed = self.multi_chosen_entries.pop(idx)
        self._populate_multi_chosen()
        self._set_status(self.multi_status, f"Removed {removed.name}.")

    def _on_multi_test(self, _btn: Gtk.Button) -> None:
        idx = self._selected_chosen_index()
        if idx is None:
            self._set_status(self.multi_status, "No chosen device selected.")
            return
        entry = self.multi_chosen_entries[idx]
        dev = self._resolve_entry_to_alsa_device(entry)
        if dev is None:
            self._set_status(
                self.multi_status,
                f"Could not resolve {entry.match} to a card right now.",
            )
            return
        try:
            # Apply the device's own volume BEFORE calling preview_beep so
            # the test reproduces exactly what the boot runtime will do
            # (preview_beep itself applies the single-device mixer values,
            # which is fine as a baseline — our per-device volume lands on
            # top via the common controls).
            if entry.volume >= 0:
                audio.apply_volume_to_card(dev.card_index, entry.volume)
            audio.preview_beep(dev, WAV_PATH, self.config)
            self._set_status(self.multi_status, f"Played beep through {entry.name}.")
        except subprocess.CalledProcessError as exc:
            # aplay fails most commonly with -EBUSY when PipeWire is holding
            # the device exclusively. That's a live-system issue only — in
            # initramfs there's no PipeWire, so the device will still work
            # at boot. Give a helpful hint so the user doesn't think the
            # device is broken.
            hint = ""
            if _device_in_use_by_pipewire(dev.card_index):
                hint = (
                    " PipeWire is currently using this device — the test "
                    "can't open it exclusively. This usually works at boot "
                    "though, when PipeWire isn't running yet."
                )
            self._set_status(
                self.multi_status,
                f"Playback failed on {entry.name} (aplay exit {exc.returncode}).{hint}",
            )
        except FileNotFoundError:
            self._set_status(self.multi_status, "aplay or amixer isn't installed.")

    @staticmethod
    def _resolve_entry_to_alsa_device(entry: DeviceEntry) -> audio.AlsaDevice | None:
        """Find the live card index matching entry.match and build an
        AlsaDevice suitable for audio.preview_beep()."""
        kind, _, spec = entry.match.partition(":")
        for info in audio.discover_all_output_devices():
            if kind == "codec" and info.codec_name:
                if spec.lower() in info.codec_name.lower():
                    return audio.AlsaDevice(
                        pcm_spec=f"plughw:{info.card_index},{entry.pcm}",
                        card_index=info.card_index,
                        pcm_device=entry.pcm,
                        description=f"{info.card_name} (codec {info.codec_name})",
                        codec_string=info.codec_name,
                    )
            if kind == "usb" and info.usb_vendor_product:
                if spec.lower() == info.usb_vendor_product.lower():
                    return audio.AlsaDevice(
                        pcm_spec=f"plughw:{info.card_index},{entry.pcm}",
                        card_index=info.card_index,
                        pcm_device=entry.pcm,
                        description=f"{info.card_name} (USB {info.usb_vendor_product})",
                    )
        return None

    def _on_multi_ok(self, _btn: Gtk.Button) -> None:
        if not self.multi_chosen_entries:
            self._set_status(self.multi_status, "Add at least one device first.")
            return
        # Collect modules for every chosen device so the dracut/initcpio
        # hook knows what to bake in (snd_hda_intel for HDA, snd_usb_audio
        # for USB — the hooks also auto-add snd_usb_audio when they see a
        # usb: match, but populating CODEC_MODULES too is belt-and-braces).
        modules: list[str] = []
        seen: set[str] = set()
        for entry in self.multi_chosen_entries:
            dev = self._resolve_entry_to_alsa_device(entry)
            if dev is None:
                continue
            for m in audio.modules_for_card(dev.card_index):
                if m not in seen:
                    seen.add(m)
                    modules.append(m)

        self.config = replace(
            self.config,
            method="alsa",
            devices=list(self.multi_chosen_entries),
            codec_modules=" ".join(modules) or self.config.codec_modules,
        )
        if not self.config.probe_timeout:
            self.config = replace(self.config, probe_timeout=5)
        self._back_target = self.PAGE_MULTI
        self._goto(self.PAGE_REVIEW)

    # ---- Page 4: Review ---------------------------------------------------

    def _build_review(self) -> Gtk.Widget:
        page = _page()
        page.pack_start(_heading("Review and apply"), False, False, 0)
        page.pack_start(_body(
            "Review the configuration. Apply will save it, edit your initramfs "
            "configuration if needed, and (on mkinitcpio) rebuild the initramfs. "
            "On dracut you'll be prompted to rebuild afterwards. "
            "You'll be asked for your password."
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

        if self.config.devices:
            rows = [
                ("Method", self.config.method),
                ("Codec modules", self.config.codec_modules or "(none)"),
                ("Probe timeout (s)", str(self.config.probe_timeout)),
                ("Fallback devices", f"{len(self.config.devices)} configured"),
            ]
            for i, entry in enumerate(self.config.devices, start=1):
                rows.append(
                    (f"Device {i}", f"{entry.name} ({entry.match}, PCM {entry.pcm})")
                )
        else:
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
            self.apply_btn.set_sensitive(hooks.can_apply(status))
        except (OSError, RuntimeError) as exc:
            self.hooks_status_label.set_text(
                f"Could not read initramfs configuration: {exc}"
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
            self.PAGE_MULTI:   lambda: (self.multi_cand_radios[0] if self.multi_cand_radios else self.multi_add_btn),
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
