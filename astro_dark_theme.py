"""Shared dark Tk/ttk styling for the standalone Astro utilities."""

from __future__ import annotations

import ctypes
import os
from typing import Any


DARK_BG = "#181a1f"
DARK_SURFACE = "#24272e"
DARK_FIELD = "#111318"
DARK_BORDER = "#3a3f49"
DARK_TEXT = "#e8eaed"
DARK_ACCENT = "#3f86d9"
DARK_ACCENT_ACTIVE = "#4d96eb"
DARK_SELECTION = "#285f9e"
DARK_DISABLED = "#747b86"


def _enable_windows_dark_titlebar(window: Any) -> None:
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                handle,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except (AttributeError, OSError):
        pass


def configure_dark_theme(window: Any) -> Any:
    """Configure a consistent dark ttk palette and return its Style object."""

    from tkinter import ttk

    window.configure(background=DARK_BG)
    style = ttk.Style(window)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=DARK_BG, foreground=DARK_TEXT)
    style.configure("TFrame", background=DARK_BG)
    style.configure("TLabel", background=DARK_BG, foreground=DARK_TEXT)
    style.configure(
        "TButton",
        background=DARK_SURFACE,
        foreground=DARK_TEXT,
        bordercolor=DARK_BORDER,
        focusthickness=1,
        focuscolor=DARK_ACCENT,
        padding=(8, 4),
    )
    style.map(
        "TButton",
        background=[("active", "#30343d"), ("pressed", DARK_FIELD), ("disabled", DARK_BG)],
        foreground=[("disabled", DARK_DISABLED)],
    )
    style.configure(
        "Primary.TButton",
        background=DARK_ACCENT,
        foreground="#ffffff",
        bordercolor=DARK_ACCENT,
    )
    style.map(
        "Primary.TButton",
        background=[("active", DARK_ACCENT_ACTIVE), ("pressed", DARK_SELECTION), ("disabled", DARK_BG)],
        foreground=[("disabled", DARK_DISABLED)],
    )
    for widget_style in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(
            widget_style,
            fieldbackground=DARK_FIELD,
            background=DARK_SURFACE,
            foreground=DARK_TEXT,
            bordercolor=DARK_BORDER,
            insertcolor=DARK_TEXT,
            arrowcolor=DARK_TEXT,
        )
        style.map(
            widget_style,
            fieldbackground=[("readonly", DARK_FIELD), ("disabled", DARK_BG)],
            foreground=[("readonly", DARK_TEXT), ("disabled", DARK_DISABLED)],
            bordercolor=[("focus", DARK_ACCENT)],
        )
    for widget_style in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget_style, background=DARK_BG, foreground=DARK_TEXT, indicatorcolor=DARK_FIELD)
        style.map(
            widget_style,
            background=[("active", DARK_BG)],
            foreground=[("disabled", DARK_DISABLED)],
            indicatorcolor=[("selected", DARK_ACCENT), ("disabled", DARK_SURFACE)],
        )
    style.configure(
        "Treeview",
        background=DARK_FIELD,
        fieldbackground=DARK_FIELD,
        foreground=DARK_TEXT,
        bordercolor=DARK_BORDER,
    )
    style.map("Treeview", background=[("selected", DARK_SELECTION)], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=DARK_SURFACE, foreground=DARK_TEXT, bordercolor=DARK_BORDER)
    style.map("Treeview.Heading", background=[("active", "#30343d")])
    style.configure(
        "TScrollbar",
        background=DARK_SURFACE,
        troughcolor=DARK_FIELD,
        bordercolor=DARK_BORDER,
        arrowcolor=DARK_TEXT,
    )
    style.map("TScrollbar", background=[("active", "#343943")])
    style.configure("TProgressbar", background=DARK_ACCENT, troughcolor=DARK_FIELD, bordercolor=DARK_BORDER)
    _enable_windows_dark_titlebar(window)
    return style