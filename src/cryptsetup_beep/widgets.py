"""Custom GTK3 widgets — accessibility-aware list boxes.

`FocusManagedListBox` is adapted from polyglot-for-orca/polyglot/config_ui.py
(also Toby's project, MIT-compatible). It owns Tab/Shift+Tab navigation
between widgets in its rows, so Orca and keyboard users get a deterministic
tab order rather than the GtkListBox default (which can be confusing with
multiple focusable children).

`PreviewExitListBox` is a subclass for the cryptsetup-beep wizard's Page 3:
Tab on ANY row jumps directly to a configured "exit forward" widget (the
Preview button) rather than walking widget-by-widget through the list.
Arrow keys still use the GtkListBox default (Up/Down within the list).
"""

from __future__ import annotations

import gi  # type: ignore[import-not-found]

gi.require_version("Atk", "1.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, Gdk, Gtk  # type: ignore[import-not-found]


class FocusManagedListBox(Gtk.ListBox):
    """A ListBox that owns Tab/Shift+Tab focus between row widgets.

    Each row holds one interactive widget; the box steers focus to the next
    or previous one on Tab/Shift+Tab. Arrow keys retain GtkListBox defaults.
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.get_style_context().add_class("frame")
        self.set_can_focus(False)
        self.set_header_func(self._separator_header_func, None)
        self._widgets: list[Gtk.Widget] = []
        self._rows: list[Gtk.ListBoxRow] = []
        self._exiting_backward: list[bool] = [False]

    @staticmethod
    def _separator_header_func(row, before, _user_data) -> None:
        if before is not None:
            row.set_header(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

    def add_row_with_widget(self, row: Gtk.ListBoxRow, widget: Gtk.Widget) -> None:
        widget.connect("key-press-event", self._on_widget_key_press)
        row.connect("focus-in-event", self._on_row_focus_in, widget)
        self.add(row)
        self._rows.append(row)
        self._widgets.append(widget)

    def _focus_next_sensitive_widget(self, widget: Gtk.Widget) -> bool:
        try:
            idx = self._widgets.index(widget)
        except ValueError:
            return False
        for i in range(idx + 1, len(self._widgets)):
            if self._widgets[i].get_sensitive():
                self._widgets[i].grab_focus()
                return True
        return False

    def _focus_prev_sensitive_widget(self, widget: Gtk.Widget) -> bool:
        try:
            idx = self._widgets.index(widget)
        except ValueError:
            return False
        for i in range(idx - 1, -1, -1):
            if self._widgets[i].get_sensitive():
                self._widgets[i].grab_focus()
                return True
        if self._rows:
            self._exiting_backward[0] = True
            self._rows[0].grab_focus()
        return True

    def _on_widget_key_press(self, widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Tab:
            return self._focus_next_sensitive_widget(widget)
        if event.keyval == Gdk.KEY_ISO_Left_Tab:
            return self._focus_prev_sensitive_widget(widget)
        return False

    def _on_row_focus_in(self, _row, _event, widget: Gtk.Widget) -> bool:
        if self._exiting_backward[0]:
            self._exiting_backward[0] = False
            return False
        widget.grab_focus()
        if isinstance(widget, Gtk.Entry):
            widget.set_position(-1)
        return False


class PreviewExitListBox(FocusManagedListBox):
    """Variant where Tab from any row goes straight to a single exit widget.

    Used on the wizard's ALSA-device page: regardless of which row you've
    arrowed into, pressing Tab takes you to the Preview button. Without this
    you'd have to Tab through every device row before reaching Preview.
    """

    def __init__(self) -> None:
        super().__init__()
        self._exit_forward: Gtk.Widget | None = None
        self._exit_backward: Gtk.Widget | None = None

    def set_exit_forward(self, widget: Gtk.Widget) -> None:
        """Set the widget Tab from any row should focus."""
        self._exit_forward = widget

    def set_exit_backward(self, widget: Gtk.Widget) -> None:
        """Set the widget Shift+Tab from any row should focus.

        Optional. If unset, Shift+Tab uses the parent class behaviour.
        """
        self._exit_backward = widget

    def _focus_next_sensitive_widget(self, widget: Gtk.Widget) -> bool:
        if self._exit_forward is not None and self._exit_forward.get_sensitive():
            self._exit_forward.grab_focus()
            return True
        return super()._focus_next_sensitive_widget(widget)

    def _focus_prev_sensitive_widget(self, widget: Gtk.Widget) -> bool:
        if self._exit_backward is not None and self._exit_backward.get_sensitive():
            self._exit_backward.grab_focus()
            return True
        return super()._focus_prev_sensitive_widget(widget)


def set_atk(widget: Gtk.Widget, role: Atk.Role | None = None,
            name: str | None = None, description: str | None = None) -> None:
    """One-liner to attach ATK metadata for Orca."""
    atk = widget.get_accessible()
    if atk is None:
        return
    if role is not None:
        atk.set_role(role)
    if name is not None:
        atk.set_name(name)
    if description is not None:
        atk.set_description(description)
