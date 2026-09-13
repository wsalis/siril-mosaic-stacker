"""Dark graphical launcher for sirilmosaic.py."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tkinter as tk
import psutil
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from tkinter import filedialog, messagebox, ttk
from typing import Any

from astro_dark_theme import DARK_BORDER, DARK_FIELD, DARK_TEXT, configure_dark_theme
from sirilmosaic import debayer_preflight_warnings, discover_light_files

PROFILE_FIELDS = (
    "substacks",
    "drizzle",
    "drizzle_scale",
    "pixel_fraction",
    "bayer_pattern",
    "bayer_orientation",
    "cosmetic_correction",
    "cosmetic_cold_sigma",
    "cosmetic_hot_sigma",
    "overlap_normalization",
    "filter_background",
    "filter_stars",
    "filter_roundness",
    "filter_fwhm",
    "adaptive_quality_filtering",
    "quality_filter_sigma",
    "background_method",
    "background_samples",
    "background_tolerance",
    "weight",
    "feather",
    "rejection_low",
    "rejection_high",
    "fast_normalization",
    "catalog",
    "memory",
    "cpus",
    "retries",
    "debug",
)

SIRIL_PROCESS_NAMES = {"siril", "siril.exe", "siril-cli", "siril-cli.exe"}
PROGRESS_MARKER = re.compile(r"^\[PROGRESS\]\s+([0-9.]+)\s+([0-9.]+)\s+(.+)$")
SIRIL_PROGRESS = re.compile(r"^\s*progress:\s*([0-9.]+)%", re.IGNORECASE)


class RunProgressEstimator:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.percent = 0.0
        self.phase_start = 0.0
        self.phase_end = 0.0
        self.phase = "Waiting to start"

    def consume(self, message: str) -> bool:
        marker = PROGRESS_MARKER.match(message)
        if marker:
            requested_start = min(100.0, max(0.0, float(marker.group(1))))
            requested_end = min(100.0, max(requested_start, float(marker.group(2))))
            self.phase_start = max(self.percent, requested_start)
            self.phase_end = max(self.phase_start, requested_end)
            self.percent = self.phase_start
            self.phase = marker.group(3)
            return True
        native = SIRIL_PROGRESS.match(message)
        if native:
            command_percent = min(100.0, max(0.0, float(native.group(1))))
            estimate = self.phase_start + (
                (self.phase_end - self.phase_start) * command_percent / 100
            )
            self.percent = max(self.percent, estimate)
            return True
        return False

    def complete(self) -> None:
        self.percent = 100.0
        self.phase_start = 100.0
        self.phase_end = 100.0
        self.phase = "Complete"


def terminate_siril_descendants(parent_pid: int) -> int:
    try:
        descendants = psutil.Process(parent_pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return 0
    targets = []
    for process in descendants:
        try:
            if process.name().lower() in SIRIL_PROCESS_NAMES:
                targets.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    for process in targets:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    return len(targets)


class SirilMosaicApp:
    def __init__(self, root: tk.Tk, profile_path: Path | None = None) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.log_path: Path | None = None
        self.cancel_path: Path | None = None
        self.quality_report_path: Path | None = None
        self.running = False
        self.cancelling = False
        self.progress_estimator = RunProgressEstimator()
        self.events: Queue[tuple[str, Any]] = Queue()

        root.title("Siril Mosaic Stacker")
        root.geometry("1120x850")
        root.minsize(920, 720)
        configure_dark_theme(root)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close_window)

        profile_root = Path(os.environ.get("APPDATA", Path.home())) / "Siril Mosaic Stacker"
        self.profile_path = profile_path or (profile_root / "profiles.json")
        self.last_paths_path = self.profile_path.with_name("last_paths.json")
        last_paths = self._read_last_paths()

        self.workdir = tk.StringVar(value=last_paths.get("workdir", r"G:\Rosette"))
        self.siril_exe = tk.StringVar(value=r"C:\Program Files\Siril\bin\siril.exe")
        self.substacks = tk.IntVar(value=2)
        self.drizzle = tk.BooleanVar(value=True)
        self.drizzle_scale = tk.DoubleVar(value=2.0)
        self.pixel_fraction = tk.DoubleVar(value=0.8)
        self.bayer_pattern = tk.StringVar(value=last_paths.get("bayer_pattern", "Auto (header)"))
        self.bayer_orientation = tk.StringVar(
            value=last_paths.get("bayer_orientation", "Bottom-up")
        )
        self.cosmetic_correction = tk.BooleanVar(value=True)
        self.cosmetic_cold_sigma = tk.DoubleVar(value=50.0)
        self.cosmetic_hot_sigma = tk.DoubleVar(value=3.0)
        self.overlap_normalization = tk.BooleanVar(value=True)
        self.filter_background = tk.IntVar(value=97)
        self.filter_stars = tk.IntVar(value=97)
        self.filter_roundness = tk.IntVar(value=97)
        self.filter_fwhm = tk.IntVar(value=97)
        self.adaptive_quality_filtering = tk.BooleanVar(value=False)
        self.quality_filter_sigma = tk.DoubleVar(value=3.0)
        self.background_method = tk.StringVar(value="Quadratic")
        self.background_samples = tk.IntVar(value=20)
        self.background_tolerance = tk.DoubleVar(value=1.0)
        self.weight = tk.StringVar(value="wfwhm")
        self.feather = tk.IntVar(value=20)
        self.rejection_low = tk.DoubleVar(value=3.0)
        self.rejection_high = tk.DoubleVar(value=3.0)
        self.fast_normalization = tk.BooleanVar(value=False)
        self.catalog = tk.StringVar(value="localgaia")
        self.memory = tk.DoubleVar(value=0.8)
        self.cpus = tk.IntVar(value=min(28, os.cpu_count() or 1))
        self.retries = tk.IntVar(value=5)
        self.debug = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Choose a folder; FITS and XISF frames in its subfolders are included.")
        self.progress_text = tk.StringVar(value="0.0% estimated - Waiting to start")
        self.drizzle_widgets: list[ttk.Spinbox] = []
        self.cosmetic_widgets: list[ttk.Spinbox] = []
        self.quality_percent_widgets: list[ttk.Spinbox] = []
        self.quality_sigma_widgets: list[ttk.Spinbox] = []
        self.background_widgets: list[ttk.Spinbox] = []
        self.output_dir = tk.StringVar(
            value=last_paths.get("output_dir", r"G:\Rosette\Siril Mosaic Output")
        )
        self.profile_name = tk.StringVar()
        self.profiles = self._read_profiles()

        self._build_profiles()
        self._build_paths()
        self._build_settings()
        self.update_drizzle_controls()
        self.update_cosmetic_controls()
        self.update_quality_filter_controls()
        self.update_background_controls()
        self._build_actions()
        self._build_log()
        root.after(100, self.poll_events)

    def _build_profiles(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Run profile", padding=12)
        frame.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 10))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Profile").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.profile_combo = ttk.Combobox(
            frame,
            textvariable=self.profile_name,
            values=sorted(self.profiles, key=str.casefold),
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Load", command=self.load_profile).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(frame, text="Save", command=self.save_profile).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(frame, text="Delete", command=self.delete_profile).grid(row=0, column=4, padx=(8, 0))

    def _build_paths(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Locations", padding=12)
        frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Input folder (recursive)").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.workdir).grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Choose...", command=self.choose_workdir).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frame, text="Output folder").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(frame, text="Choose...", command=self.choose_output_dir).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(frame, text="Siril executable").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.siril_exe).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(frame, text="Choose...", command=self.choose_siril).grid(row=2, column=2, padx=(8, 0), pady=(8, 0))

    def _spinbox(
        self,
        parent: Any,
        row: int,
        pair: int,
        label: str,
        variable: tk.Variable,
        lower: float,
        upper: float,
        increment: float,
    ) -> ttk.Spinbox:
        column = pair * 2
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 6), pady=4)
        spinbox = ttk.Spinbox(
            parent,
            from_=lower,
            to=upper,
            increment=increment,
            textvariable=variable,
            width=10,
        )
        spinbox.grid(row=row, column=column + 1, sticky="ew", padx=(0, 16), pady=4)
        return spinbox

    def _build_settings(self) -> None:
        frame = ttk.Frame(self.root)
        frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        for column in (0, 1):
            frame.columnconfigure(column, weight=1, uniform="settings")

        def section(title: str, row: int, column: int) -> ttk.LabelFrame:
            group = ttk.LabelFrame(frame, text=title, padding=(10, 6))
            group.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0, 5) if column == 0 else (5, 0),
                pady=(0, 8),
            )
            for value_column in (1, 3):
                group.columnconfigure(value_column, weight=1)
            return group

        capture = section("Capture & CFA", 0, 0)
        self._spinbox(capture, 0, 0, "Substacks", self.substacks, 1, 100, 1)
        ttk.Label(capture, text="Bayer pattern").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            capture,
            textvariable=self.bayer_pattern,
            values=("Auto (header)", "RGGB", "BGGR", "GBRG", "GRBG"),
            state="readonly",
            width=13,
        ).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=4)
        ttk.Label(capture, text="CFA row order").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            capture,
            textvariable=self.bayer_orientation,
            values=("Auto", "Top-down", "Bottom-up"),
            state="readonly",
            width=13,
        ).grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=4)
        ttk.Checkbutton(
            capture,
            text="Enable drizzle",
            variable=self.drizzle,
            command=self.update_drizzle_controls,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=4)
        self.drizzle_widgets.extend((
            self._spinbox(capture, 2, 0, "Drizzle scale", self.drizzle_scale, 0.1, 3.0, 0.1),
            self._spinbox(capture, 2, 1, "Pixel fraction", self.pixel_fraction, 0.1, 1.0, 0.1),
        ))

        cosmetic = section("Cosmetic correction", 0, 1)
        ttk.Checkbutton(
            cosmetic,
            text="Enable cosmetic correction",
            variable=self.cosmetic_correction,
            command=self.update_cosmetic_controls,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=4)
        self.cosmetic_widgets.extend((
            self._spinbox(
                cosmetic, 1, 0, "Cold-pixel sigma", self.cosmetic_cold_sigma, 0.1, 50.0, 0.1
            ),
            self._spinbox(
                cosmetic, 1, 1, "Hot-pixel sigma", self.cosmetic_hot_sigma, 0.1, 20.0, 0.1
            ),
        ))

        selection = section("Frame selection", 1, 0)
        for row, pair, label, variable in (
            (0, 0, "Background filter (%)", self.filter_background),
            (0, 1, "Star-count filter (%)", self.filter_stars),
            (1, 0, "Roundness filter (%)", self.filter_roundness),
            (1, 1, "FWHM filter (%)", self.filter_fwhm),
        ):
            self.quality_percent_widgets.append(
                self._spinbox(selection, row, pair, label, variable, 1, 100, 1)
            )
        ttk.Checkbutton(
            selection,
            text="Adaptive quality filters",
            variable=self.adaptive_quality_filtering,
            command=self.update_quality_filter_controls,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        self.quality_sigma_widgets.append(
            self._spinbox(
                selection, 2, 1, "Quality filter sigma", self.quality_filter_sigma, 0.1, 20.0, 0.1
            )
        )

        integration = section("Integration", 1, 1)
        ttk.Label(integration, text="Weighting").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            integration,
            textvariable=self.weight,
            values=("wfwhm", "noise", "nbstars", "nbstack"),
            state="readonly",
            width=13,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=4)
        self._spinbox(integration, 0, 1, "Feather", self.feather, 0, 1000, 1)
        self._spinbox(integration, 1, 0, "Low rejection", self.rejection_low, 0.1, 20.0, 0.1)
        self._spinbox(integration, 1, 1, "High rejection", self.rejection_high, 0.1, 20.0, 0.1)
        ttk.Checkbutton(
            integration,
            text="Normalize overlaps",
            variable=self.overlap_normalization,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(
            integration,
            text="Fast normalization",
            variable=self.fast_normalization,
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=4)

        background = section("Background & plate solving", 2, 0)
        ttk.Label(background, text="Background extraction").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            background,
            textvariable=self.background_method,
            values=("Off", "Linear", "Quadratic", "RBF"),
            state="readonly",
            width=13,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=4)
        ttk.Label(background, text="Plate catalog").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            background,
            textvariable=self.catalog,
            values=("localgaia", "gaia", "nomad", "apass"),
            width=13,
        ).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=4)
        self.background_widgets.extend((
            self._spinbox(
                background, 1, 0, "Background samples", self.background_samples, 1, 100, 1
            ),
            self._spinbox(
                background,
                1,
                1,
                "Background tolerance",
                self.background_tolerance,
                0.1,
                10.0,
                0.1,
            ),
        ))
        self.background_method.trace_add("write", lambda *_: self.update_background_controls())

        resources = section("Resources", 2, 1)
        self._spinbox(resources, 0, 0, "Memory fraction", self.memory, 0.1, 1.0, 0.1)
        self._spinbox(resources, 0, 1, "CPU count", self.cpus, 1, 256, 1)
        self._spinbox(resources, 1, 0, "Retries", self.retries, 0, 20, 1)
        ttk.Checkbutton(
            resources,
            text="Keep intermediate files",
            variable=self.debug,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=4)

    def _build_actions(self) -> None:
        frame = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        frame.grid(row=3, column=0, sticky="ew")
        frame.columnconfigure(3, weight=1)
        self.start_button = ttk.Button(frame, text="Start Mosaic Stack", command=self.start, style="Primary.TButton")
        self.start_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(frame, text="Cancel Run", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=210)
        self.progress.grid(row=0, column=2, padx=(12, 0))
        ttk.Label(frame, textvariable=self.progress_text).grid(row=0, column=3, sticky="w", padx=(12, 0))
        ttk.Label(frame, textvariable=self.status).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

    def _build_log(self) -> None:
        frame = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        frame.grid(row=4, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(
            frame,
            wrap="word",
            state="disabled",
            background=DARK_FIELD,
            foreground=DARK_TEXT,
            insertbackground=DARK_TEXT,
            selectbackground="#285f9e",
            relief="flat",
            highlightthickness=1,
            highlightbackground=DARK_BORDER,
            font=("Consolas", 9),
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _read_profiles(self) -> dict[str, dict[str, Any]]:
        if not self.profile_path.is_file():
            return {}
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
        return profiles if isinstance(profiles, dict) else {}

    def _read_last_paths(self) -> dict[str, str]:
        if not self.last_paths_path.is_file():
            return {}
        try:
            data = json.loads(self.last_paths_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            name: value
            for name in ("workdir", "output_dir", "bayer_pattern", "bayer_orientation")
            if isinstance((value := data.get(name)), str) and value
        }

    def _write_last_paths(self) -> None:
        self.last_paths_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.last_paths_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "workdir": self.workdir.get().strip(),
                    "output_dir": self.output_dir.get().strip(),
                    "bayer_pattern": self.bayer_pattern.get(),
                    "bayer_orientation": self.bayer_orientation.get(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.last_paths_path)

    def _write_profiles(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.profile_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "profiles": self.profiles}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.profile_path)
        self.profile_combo.configure(values=sorted(self.profiles, key=str.casefold))

    def _profile_values(self) -> dict[str, Any]:
        return {name: getattr(self, name).get() for name in PROFILE_FIELDS}

    def save_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        if not name:
            if notify:
                messagebox.showerror("Run profile", "Enter a profile name.")
            return
        if notify and name in self.profiles and not messagebox.askyesno(
            "Run profile", f"Replace profile '{name}'?"
        ):
            return
        self.profiles[name] = self._profile_values()
        self._write_profiles()
        self.profile_name.set(name)
        self.status.set(f"Saved profile: {name}")

    def load_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        profile = self.profiles.get(name)
        if profile is None:
            if notify:
                messagebox.showerror("Run profile", "Choose an existing profile.")
            return
        for field in PROFILE_FIELDS:
            if field in profile:
                getattr(self, field).set(profile[field])
        self.update_drizzle_controls()
        self.update_cosmetic_controls()
        self.update_quality_filter_controls()
        self.update_background_controls()
        self.status.set(f"Loaded profile: {name}")

    def delete_profile(self, notify: bool = True) -> None:
        name = self.profile_name.get().strip()
        if name not in self.profiles:
            if notify:
                messagebox.showerror("Run profile", "Choose an existing profile.")
            return
        if notify and not messagebox.askyesno("Run profile", f"Delete profile '{name}'?"):
            return
        del self.profiles[name]
        self._write_profiles()
        self.profile_name.set("")
        self.status.set(f"Deleted profile: {name}")

    def choose_workdir(self) -> None:
        selected = filedialog.askdirectory(title="Choose folder to scan recursively for FITS or XISF frames")
        if selected:
            self.workdir.set(selected)
            default_output = Path(selected) / "Siril Mosaic Output"
            self.output_dir.set(str(default_output))
            self._write_last_paths()
            frame_count = len(discover_light_files(Path(selected), (default_output,)))
            self.status.set(f"Found {frame_count} supported frame(s), including subfolders.")

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(title="Choose output folder")
        if selected:
            self.output_dir.set(selected)
            self._write_last_paths()

    def choose_siril(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose Siril executable",
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if selected:
            self.siril_exe.set(selected)

    def update_drizzle_controls(self) -> None:
        state = "normal" if self.drizzle.get() else "disabled"
        for widget in self.drizzle_widgets:
            widget.configure(state=state)

    def update_cosmetic_controls(self) -> None:
        state = "normal" if self.cosmetic_correction.get() else "disabled"
        for widget in self.cosmetic_widgets:
            widget.configure(state=state)

    def update_quality_filter_controls(self) -> None:
        percent_state = "disabled" if self.adaptive_quality_filtering.get() else "normal"
        sigma_state = "normal" if self.adaptive_quality_filtering.get() else "disabled"
        for widget in self.quality_percent_widgets:
            widget.configure(state=percent_state)
        for widget in self.quality_sigma_widgets:
            widget.configure(state=sigma_state)

    def update_background_controls(self) -> None:
        state = "disabled" if self.background_method.get() == "Off" else "normal"
        for widget in self.background_widgets:
            widget.configure(state=state)

    def command(self) -> list[str]:
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        executable = Path(self.siril_exe.get().strip()).expanduser()
        if not workdir.is_dir():
            raise ValueError("Choose an existing input folder.")
        if not self.output_dir.get().strip():
            raise ValueError("Choose an output folder.")
        if output_dir.resolve() == workdir.resolve():
            raise ValueError("Output folder must be different from the input folder.")
        frame_count = len(discover_light_files(workdir, (output_dir,)))
        if frame_count == 0:
            raise ValueError("Input folder contains no supported FITS or XISF files.")
        if not executable.is_file():
            raise ValueError("Choose an existing Siril executable.")
        if not 1 <= self.substacks.get() <= frame_count:
            raise ValueError("Substacks must be between 1 and the number of input frames.")
        if self.drizzle.get():
            if not 0 < self.drizzle_scale.get() <= 3:
                raise ValueError("Drizzle scale must be greater than 0 and no more than 3.")
            if not 0 < self.pixel_fraction.get() <= 1:
                raise ValueError("Pixel fraction must be greater than 0 and no more than 1.")
        for label, value in (
            ("Background filter", self.filter_background.get()),
            ("Star-count filter", self.filter_stars.get()),
            ("Roundness filter", self.filter_roundness.get()),
            ("FWHM filter", self.filter_fwhm.get()),
        ):
            if not 1 <= value <= 100:
                raise ValueError(f"{label} must be from 1 to 100 percent.")
        if self.feather.get() < 0:
            raise ValueError("Feather cannot be negative.")
        if self.rejection_low.get() <= 0 or self.rejection_high.get() <= 0:
            raise ValueError("Rejection thresholds must be greater than 0.")
        if self.cosmetic_correction.get():
            if self.cosmetic_cold_sigma.get() <= 0:
                raise ValueError("Cold-pixel sigma must be greater than 0.")
            if self.cosmetic_hot_sigma.get() <= 0:
                raise ValueError("Hot-pixel sigma must be greater than 0.")
        if self.quality_filter_sigma.get() <= 0:
            raise ValueError("Quality filter sigma must be greater than 0.")
        if self.background_method.get() != "Off":
            if self.background_samples.get() < 1:
                raise ValueError("Background samples must be at least 1.")
            if self.background_tolerance.get() <= 0:
                raise ValueError("Background tolerance must be greater than 0.")
        if not 0 < self.memory.get() <= 1:
            raise ValueError("Memory fraction must be greater than 0 and no more than 1.")
        if self.cpus.get() < 1:
            raise ValueError("CPU count must be at least 1.")
        if self.retries.get() < 0:
            raise ValueError("Retries cannot be negative.")

        script = Path(__file__).with_name("sirilmosaic.py")
        bayer_pattern = "auto" if self.bayer_pattern.get() == "Auto (header)" else self.bayer_pattern.get()
        bayer_orientation = self.bayer_orientation.get().lower()
        command = [
            sys.executable,
            "-u",
            str(script),
            "--workdir", str(workdir),
            "--output-dir", str(output_dir),
            "--siril-exe", str(executable),
            "--substacks", str(self.substacks.get()),
            "--drizzle" if self.drizzle.get() else "--no-drizzle",
            "--drizzle-scale", str(self.drizzle_scale.get()),
            "--pixel-fraction", str(self.pixel_fraction.get()),
            "--bayer-pattern", bayer_pattern,
            "--bayer-orientation", bayer_orientation,
            "--cosmetic-correction" if self.cosmetic_correction.get() else "--no-cosmetic-correction",
            "--cosmetic-cold-sigma", str(self.cosmetic_cold_sigma.get()),
            "--cosmetic-hot-sigma", str(self.cosmetic_hot_sigma.get()),
            "--overlap-normalization" if self.overlap_normalization.get() else "--no-overlap-normalization",
            "--filter-background", str(self.filter_background.get()),
            "--filter-stars", str(self.filter_stars.get()),
            "--filter-roundness", str(self.filter_roundness.get()),
            "--filter-fwhm", str(self.filter_fwhm.get()),
            "--adaptive-quality-filtering" if self.adaptive_quality_filtering.get()
            else "--no-adaptive-quality-filtering",
            "--quality-filter-sigma", str(self.quality_filter_sigma.get()),
            "--background-method", self.background_method.get().lower(),
            "--background-samples", str(self.background_samples.get()),
            "--background-tolerance", str(self.background_tolerance.get()),
            "--weight", self.weight.get(),
            "--feather", str(self.feather.get()),
            "--rejection-low", str(self.rejection_low.get()),
            "--rejection-high", str(self.rejection_high.get()),
            "--fast-normalization" if self.fast_normalization.get() else "--no-fast-normalization",
            "--catalog", self.catalog.get(),
            "--memory", str(self.memory.get()),
            "--cpus", str(self.cpus.get()),
            "--retries", str(self.retries.get()),
        ]
        if self.debug.get():
            command.append("--debug")
        return command

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_progress_display(self) -> None:
        self.progress.configure(value=self.progress_estimator.percent)
        self.progress_text.set(
            f"{self.progress_estimator.percent:.1f}% estimated - {self.progress_estimator.phase}"
        )

    def start(self) -> None:
        try:
            command = self.command()
        except (tk.TclError, ValueError) as error:
            messagebox.showerror("Siril Mosaic Stacker", str(error))
            return
        workdir = Path(self.workdir.get().strip()).expanduser()
        output_dir = Path(self.output_dir.get().strip()).expanduser()
        pattern = "auto" if self.bayer_pattern.get() == "Auto (header)" else self.bayer_pattern.get()
        orientation = self.bayer_orientation.get().lower()
        warnings = debayer_preflight_warnings(
            discover_light_files(workdir, (output_dir,)), pattern, orientation
        )
        confirmation = (
            "Frames in the selected folder will be moved temporarily into randomized substack folders. "
            "Do not interrupt disk operations. Continue?"
        )
        if warnings:
            confirmation = "Debayer preflight warning:\n\n" + "\n".join(
                f"- {warning}" for warning in warnings
            ) + "\n\n" + confirmation
        if not messagebox.askyesno(
            "Start mosaic stack?",
            confirmation,
        ):
            return
        self._write_last_paths()

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_estimator.reset()
        self.update_progress_display()
        self.status.set("Processing...")
        self.running = True
        self.cancelling = False
        self.quality_report_path = None
        log_directory = Path(self.workdir.get().strip()).expanduser() / "siril_mosaic_logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        run_stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        self.log_path = log_directory / f"siril_mosaic_{run_stamp}.log"
        self.cancel_path = log_directory / f"siril_mosaic_{run_stamp}.cancel"
        self.cancel_path.unlink(missing_ok=True)
        command.extend(("--cancel-file", str(self.cancel_path)))
        self.log_path.write_text(
            f"Command: {subprocess.list2cmdline(command)}\n\n",
            encoding="utf-8",
        )
        self.append_log(f"[INFO] Run log: {self.log_path}")

        def worker() -> None:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                assert self.log_path is not None
                with self.log_path.open("a", encoding="utf-8", buffering=1) as log_file:
                    self.process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=creation_flags,
                    )
                    assert self.process.stdout is not None
                    for line in self.process.stdout:
                        message = line.rstrip()
                        log_file.write(message + "\n")
                        self.events.put(("log", message))
                    return_code = self.process.wait()
                    log_file.write(f"\nExit code: {return_code}\n")
                self.events.put(("done", return_code))
            except Exception as error:
                if self.log_path is not None:
                    with self.log_path.open("a", encoding="utf-8") as log_file:
                        log_file.write(f"\nLauncher error: {error}\n")
                self.events.put(("error", error))
            finally:
                self.process = None
                if self.cancel_path is not None:
                    self.cancel_path.unlink(missing_ok=True)

        Thread(target=worker, daemon=True).start()

    def cancel(self) -> None:
        if not self.running or self.cancelling:
            return
        if not messagebox.askyesno(
            "Cancel mosaic stack?",
            "The current Siril operation will stop, then source files will be restored before the run exits.",
        ):
            return
        if self.cancel_path is None:
            messagebox.showerror("Cancel mosaic stack", "The cancellation marker is unavailable.")
            return
        try:
            self.cancel_path.write_text("cancel\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Cancel mosaic stack", f"Could not request cancellation:\n\n{error}")
            return
        self.cancelling = True
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelling safely; restoring sources...")
        self.progress_estimator.phase = "Cancelling safely"
        self.update_progress_display()
        self.append_log("[INFO] Safe cancellation requested; waiting for source rollback...")
        process = self.process
        if process is None:
            return

        def interrupt_siril() -> None:
            try:
                count = terminate_siril_descendants(process.pid)
                self.events.put(("cancel_interrupt", count))
            except Exception as error:
                self.events.put(("cancel_error", error))

        Thread(target=interrupt_siril, daemon=True).start()

    def poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    message = str(payload)
                    self.append_log(message)
                    progress_changed = self.progress_estimator.consume(message)
                    if progress_changed:
                        self.update_progress_display()
                    report_prefix = "[INFO] Quality report: "
                    if message.startswith(report_prefix):
                        self.quality_report_path = Path(message[len(report_prefix):])
                    if message and not progress_changed:
                        self.status.set(message)
                elif event == "done":
                    self.running = False
                    self.cancelling = False
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    if payload == 2:
                        self.status.set("Cancelled; source files restored")
                        self.progress_estimator.phase = "Cancelled"
                        self.update_progress_display()
                        messagebox.showinfo(
                            "Siril Mosaic Stacker",
                            f"Run cancelled safely. Source files were restored.\n\nLog: {self.log_path}",
                        )
                    elif payload == 0:
                        self.progress_estimator.complete()
                        self.update_progress_display()
                        self.status.set("Complete")
                        report_message = (
                            f"\nQuality report: {self.quality_report_path}"
                            if self.quality_report_path is not None else ""
                        )
                        messagebox.showinfo(
                            "Siril Mosaic Stacker",
                            f"Mosaic stack completed successfully.\n\nLog: {self.log_path}{report_message}",
                        )
                    else:
                        self.status.set(f"Failed (exit code {payload})")
                        messagebox.showerror(
                            "Siril Mosaic Stacker",
                            f"Processing failed.\n\nLog: {self.log_path}",
                        )
                elif event == "error":
                    self.running = False
                    self.cancelling = False
                    self.start_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.status.set("Failed")
                    self.progress_estimator.phase = "Failed"
                    self.update_progress_display()
                    self.append_log(f"[ERROR] {payload}")
                    messagebox.showerror("Siril Mosaic Stacker", str(payload))
                elif event == "cancel_interrupt":
                    if payload:
                        self.append_log("[INFO] Siril stopped; backend rollback is running...")
                    else:
                        self.append_log("[INFO] Cancellation queued; waiting for the current safe checkpoint...")
                elif event == "cancel_error":
                    self.append_log(f"[WARNING] Could not stop Siril immediately: {payload}")
        except Empty:
            pass
        self.root.after(100, self.poll_events)

    def close_window(self) -> None:
        if self.running:
            messagebox.showwarning(
                "Siril Mosaic Stacker",
                "Processing is still running. Keep this window open until it finishes.",
            )
            return
        self.root.destroy()


def launch_gui() -> None:
    root = tk.Tk()
    SirilMosaicApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
