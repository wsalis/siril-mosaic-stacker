import argparse
import hashlib
import html
import json
import platform
import sys
import os
import re
import subprocess
import time
import random
import shutil
import struct
import tempfile
import threading
import traceback
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, overload
import math
import numpy as np
import psutil
from collections import Counter

workdir = Path(r"G:\Rosette")
output_dir = workdir / "Siril Mosaic Output"
siril_exe = Path(r"C:\Program Files\Siril\bin\siril.exe")
cancel_file = None
app = None
run_id = ''
quality_report = None
active_master_path = None
active_cohort_id = None
active_cohort_number = None
active_cohort_tag = None
active_cohort_total = None
active_cohort_progress_base = 0.0
active_cohort_progress_span = 100.0
active_run_file_total = None
active_cohort_file_offset = 0
active_cohort_file_total = None
journal_path = None
frame_ledger_path = None
resume_enabled = False
resume_checkpoint_data = None
journal_sequence = 0
command_sequence = 0
active_phase = "Starting"
run_seed = None
journal_warning_emitted = False
run_lock_payload = None
SUPPORTED_FRAME_SUFFIXES = {'.fit', '.fits', '.fts', '.xisf'}
GENERATED_DIRECTORIES = {'lights_sorted', 'substacks', 'rejects', '__pycache__'}
GENERATED_FILE_PREFIXES = ('master_stack', 'substack_')
SOURCE_MANIFEST = 'source_manifest.json'
REJECTS_DIRECTORY = 'rejects'
RUN_CHECKPOINT = 'run_checkpoint.json'
RUN_LOCK = 'run.lock'
INTEGRITY_SCAN_VERSION = 1
MAX_SIRIL_ARTIFACT_PATH = 150
SUPPORTED_CHECKPOINT_SCHEMAS = {1, 2}
CHECKPOINT_PHASES = {
    'pending', 'substacks_running', 'master_written', 'maps_written',
    'crop_pending', 'skipped', 'complete', 'interrupted',
}
FINALIZATION_PHASES = {'master_written', 'maps_written', 'crop_pending', 'complete'}
LEGACY_CHECKPOINT_PHASES = {'running': 'substacks_running'}
STACKING_WEIGHTS = {'noise', 'wfwhm', 'nbstars', 'nbstack'}
STACK_NORMALIZATION_METHODS = ('add', 'mul', 'addscale', 'mulscale')
PLATE_SOLVE_CATALOGS = ('localgaia', 'tycho2', 'nomad', 'gaia', 'ppmxl', 'brightstars', 'apass')
REGISTRATION_TRANSFORMS = ('shift', 'similarity', 'affine', 'homography')
REGISTRATION_INTERPOLATIONS = ('none', 'nearest', 'cubic', 'lanczos4', 'linear', 'area')
DRIZZLE_KERNELS = ('point', 'turbo', 'square', 'gaussian', 'lanczos2', 'lanczos3')
PIXEL_REJECTION_METHODS = (
    'none', 'percentile', 'sigma', 'mad', 'median', 'linear', 'winsorized', 'generalized'
)
REJECTION_FRACTION_METHODS = {'percentile', 'generalized'}
SIRIL_LEGACY_MAX_STACK_FRAMES = 2048
SIRIL_MAX_STACK_FRAMES = 8192
SIRIL_8192_FRAME_VERSION = (1, 4, 0)
BAYER_PATTERNS = {'auto': 0, 'RGGB': 0, 'BGGR': 1, 'GBRG': 2, 'GRBG': 3}
BAYER_ORIENTATIONS = {'auto': 0, 'top-down': 2, 'bottom-up': 3}
BACKGROUND_METHODS = {'off': None, 'linear': '1', 'quadratic': '2', 'rbf': '-rbf'}
DEFAULT_BAYER_PATTERN = 'auto'
DEFAULT_BAYER_ORIENTATION = 'auto'
FITS_CFA_KEYWORDS = {
    'BAYERPAT', 'ROWORDER', 'INSTRUME', 'EXPTIME', 'GAIN', 'FILTER',
    'NAXIS1', 'NAXIS2',
}


class CancellationRequested(RuntimeError):
    pass

# ================= Parameters =================
debug = False
SubStack_nb = 2
drizzle_scale = '2.0'
pix_frac = '0.8'
drizzle_kernel = 'lanczos3'
drizzle_enabled = True
bayer_pattern = DEFAULT_BAYER_PATTERN
bayer_orientation = DEFAULT_BAYER_ORIENTATION
cosmetic_correction = True
cosmetic_cold_sigma = '3.0'
cosmetic_hot_sigma = '3.0'
overlap_normalization = True
stack_normalization = 'addscale'
plate_solve_order = 3
plate_solve_downscale = False
plate_solve_radius = None
plate_solve_limit_mag = None
rbf_smoothing = '0.5'
background_dither = True
registration_transform = 'homography'
registration_minpairs = 0
registration_maxstars = 0
registration_interpolation = 'lanczos4'
filter_bkg = '97%'
filter_nbstars = '97%'
filter_round = '97%'
filter_fwhm = '97%'
adaptive_quality_filtering = False
quality_filter_sigma = '3.0'
background_method = 'quadratic'
background_samples = 20
background_tolerance = '1.0'
stacking_weight = 'wfwhm'
feather_val = '20'  # Blends field rotation edges (S50)
rej_low = '3.0'     # Sigma for dark pixel rejection
rej_high = '3.0'    # Sigma for bright pixel rejection (satellites)
pixel_rejection_method = 'linear'
fast_normalization = False
catalog = 'localgaia'
memory_fraction = '0.8'
cpu_count = 28
siril_open_timeout = 60.0
siril_command_timeout = 14400.0
siril_unresponsive = False
siril_version_text = 'unavailable'
siril_version_tuple = None
siril_max_stack_frames = SIRIL_LEGACY_MAX_STACK_FRAMES
minimum_free_disk_gb = 0.5
max_retries = 5
skip_failed_frames = False
auto_substacks = False
mosaic_aware_star_count = False
test_frame_count = 0
coverage_map_enabled = False
auto_crop_enabled = False
auto_crop_coverage_percent = 50
export_per_cohort = False
COHORT_GROUP_FIELDS = ('camera', 'filter', 'exposure_seconds')
cohort_group_fields = COHORT_GROUP_FIELDS
sky_quality_percent = 100
# ==============================================

def check_cancellation():
    if cancel_file is not None and cancel_file.is_file():
        raise CancellationRequested("Cancellation requested by user.")


def report_progress(start, end, label, file_start=None, file_end=None, file_total=None):
    global active_phase
    if active_cohort_total:
        start = active_cohort_progress_base + (start * active_cohort_progress_span / 100)
        end = active_cohort_progress_base + (end * active_cohort_progress_span / 100)
        label = f"Cohort {active_cohort_number}/{active_cohort_total}: {label}"
    active_phase = label
    file_marker = ""
    if file_total is not None and file_start is not None:
        file_end = file_start if file_end is None else file_end
        file_marker = f" files={max(0, file_start)}-{max(0, file_end)}/{max(0, file_total)}"
    print(f"[PROGRESS] {start:.2f} {end:.2f}{file_marker} {label}", flush=True)


def append_journal_event(event, **fields):
    global journal_sequence, journal_warning_emitted
    if journal_path is None:
        return
    record = {
        'sequence': journal_sequence + 1,
        'timestamp': datetime.now().astimezone().isoformat(timespec='milliseconds'),
        'event': event,
        **fields,
    }
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        journal_sequence = record['sequence']
    except OSError as error:
        if quality_report is not None:
            quality_report['journal_degraded'] = True
            quality_report.setdefault('journal_errors', []).append(str(error))
        if not journal_warning_emitted:
            journal_warning_emitted = True
            print(f"[WARNING] Could not write run journal: {error}", flush=True)


def detect_siril_version():
    candidates = [siril_exe]
    if siril_exe.name.casefold() != 'siril-cli.exe':
        candidates.insert(0, siril_exe.with_name('siril-cli.exe'))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), '--version'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        output = (result.stdout or result.stderr).strip()
        if output:
            return output.splitlines()[0]
    return 'unavailable'


def parse_siril_version(version_text):
    match = re.search(r'(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?', str(version_text))
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def siril_frame_limit_for_version(version_text):
    version = parse_siril_version(version_text)
    if version is not None and version >= SIRIL_8192_FRAME_VERSION:
        return SIRIL_MAX_STACK_FRAMES
    return SIRIL_LEGACY_MAX_STACK_FRAMES

def _is_within(path, folder):
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def discover_light_files(root_folder, excluded_folders=()):
    files = []
    for path in root_folder.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_FRAME_SUFFIXES:
            continue
        if any(_is_within(path, folder) for folder in excluded_folders):
            continue
        relative = path.relative_to(root_folder)
        if any(part.lower() in GENERATED_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if path.stem.lower().startswith(GENERATED_FILE_PREFIXES) or 'rejmap' in path.stem.lower():
            continue
        files.append(path)
    return files


def pending_rejected_files(root_folder=None):
    root_folder = workdir if root_folder is None else Path(root_folder)
    rejects_folder = root_folder / REJECTS_DIRECTORY
    if not rejects_folder.is_dir():
        return []
    return sorted(
        path for path in rejects_folder.rglob('*')
        if path.is_file() and path.suffix.lower() in SUPPORTED_FRAME_SUFFIXES
    )


def input_files_after_reject_restore(root_folder, excluded_folders=()):
    return (
        discover_light_files(root_folder, excluded_folders)
        + pending_rejected_files(root_folder)
    )


def restore_rejected_frames():
    rejects_folder = workdir / REJECTS_DIRECTORY
    if not rejects_folder.is_dir():
        return []
    rejected_files = pending_rejected_files()
    destinations = [workdir / path.relative_to(rejects_folder) for path in rejected_files]
    collisions = [destination for destination in destinations if destination.exists()]
    if collisions:
        formatted = ', '.join(str(path) for path in collisions[:5])
        suffix = '...' if len(collisions) > 5 else ''
        raise RuntimeError(
            f"Cannot restore rejected frames because destination files already exist: {formatted}{suffix}"
        )
    restored = []
    for source, destination in zip(rejected_files, destinations):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        restored.append(destination.relative_to(workdir).as_posix())
    for directory in sorted(
        (path for path in rejects_folder.rglob('*') if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    if rejects_folder.is_dir() and not any(rejects_folder.iterdir()):
        rejects_folder.rmdir()
    if restored:
        print(f"[INFO] Restored {len(restored)} previously rejected frame(s) from {rejects_folder}", flush=True)
        append_journal_event(
            'rejected_frames_restored',
            count=len(restored),
            files=restored,
        )
    return restored


def _clean_fits_value(value):
    value = value.strip()
    if value.startswith("'"):
        value = value[1:].split("'", 1)[0]
    else:
        value = value.split('/', 1)[0]
    return value.strip()


def read_cfa_metadata(path):
    path = Path(path)
    metadata = {}
    try:
        with path.open('rb') as stream:
            if path.suffix.lower() == '.xisf':
                signature = stream.read(8)
                if signature != b'XISF0100':
                    return metadata
                header_length = struct.unpack('<I', stream.read(4))[0]
                stream.read(4)
                root = ET.fromstring(stream.read(header_length))
                for element in root.iter():
                    if element.tag.rsplit('}', 1)[-1] != 'FITSKeyword':
                        continue
                    name = element.attrib.get('name', '').upper()
                    if name in FITS_CFA_KEYWORDS:
                        metadata[name] = _clean_fits_value(element.attrib.get('value', ''))
                return metadata

            while block := stream.read(2880):
                for offset in range(0, len(block), 80):
                    card = block[offset:offset + 80].decode('ascii', errors='replace')
                    name = card[:8].strip().upper()
                    if name == 'END':
                        return metadata
                    if name in FITS_CFA_KEYWORDS and card[8:10] == '= ':
                        metadata[name] = _clean_fits_value(card[10:])
    except (OSError, ET.ParseError, struct.error):
        return {}
    return metadata


def read_fits_layer_count(path):
    path = Path(path)
    if path.suffix.lower() not in {'.fit', '.fits', '.fts'}:
        return None
    header = {}
    try:
        with path.open('rb') as stream:
            while block := stream.read(2880):
                for offset in range(0, len(block), 80):
                    card = block[offset:offset + 80].decode('ascii', errors='replace')
                    name = card[:8].strip().upper()
                    if name == 'END':
                        if header.get('NAXIS') == 2:
                            return 1
                        if header.get('NAXIS') == 3:
                            return header.get('NAXIS3')
                        return None
                    if name in {'NAXIS', 'NAXIS3'} and card[8:10] == '= ':
                        header[name] = int(_clean_fits_value(card[10:]))
    except (OSError, ValueError):
        return None
    return None


def debayer_preflight_warnings(files, pattern, orientation):
    metadata = [read_cfa_metadata(path) for path in files]
    patterns = {item['BAYERPAT'].upper() for item in metadata if item.get('BAYERPAT')}
    row_orders = {
        item['ROWORDER'].strip().lower().replace('_', '-').replace(' ', '-')
        for item in metadata
        if item.get('ROWORDER')
    }
    warnings = []
    if pattern == 'auto' and not patterns:
        warnings.append('No BAYERPAT metadata was found; Auto Bayer pattern may use a Siril fallback.')
    elif len(patterns) > 1:
        warnings.append(f"Input frames contain multiple Bayer patterns: {', '.join(sorted(patterns))}.")
    elif pattern != 'auto' and patterns and pattern.upper() not in patterns:
        warnings.append(
            f"Selected Bayer pattern {pattern} disagrees with header pattern {next(iter(patterns))}."
        )

    missing_row_order = sum(not item.get('ROWORDER') for item in metadata)
    if orientation == 'auto' and missing_row_order:
        warnings.append(
            f'CFA ROWORDER metadata is missing from {missing_row_order}/{len(metadata)} frames; '
            'Auto orientation may use a Siril fallback. Select Top-down or Bottom-up explicitly.'
        )
    elif len(row_orders) > 1:
        warnings.append(f"Input frames contain multiple CFA row orders: {', '.join(sorted(row_orders))}.")
    elif orientation != 'auto' and row_orders and orientation not in row_orders:
        warnings.append(
            f"Selected CFA orientation {orientation} disagrees with header row order "
            f"{next(iter(row_orders))}."
        )
    return warnings


def _cohort_value(metadata, key, default='Unknown'):
    value = metadata.get(key)
    return str(value).strip() if value not in (None, '') else default


def frame_cohort_key(path):
    metadata = read_cfa_metadata(path)
    exposure = read_exposure_seconds(path)
    exposure_value = f'{exposure:g}' if exposure else _cohort_value(metadata, 'EXPTIME')
    dimensions = 'x'.join(
        _cohort_value(metadata, key) for key in ('NAXIS1', 'NAXIS2')
    )
    return {
        'camera': _cohort_value(metadata, 'INSTRUME'),
        'exposure_seconds': exposure_value,
        'gain': _cohort_value(metadata, 'GAIN'),
        'filter': _cohort_value(metadata, 'FILTER'),
        'dimensions': dimensions,
    }


def _cohort_id(key, group_fields=None):
    fields = COHORT_GROUP_FIELDS if group_fields is None else tuple(group_fields)
    if not fields:
        return 'all_frames'
    return ' | '.join(f'{name}={key[name]}' for name in fields)


def summarize_frame_cohorts(files, group_fields=None):
    cohorts = {}
    for path in files:
        key = frame_cohort_key(path)
        cohort_id = _cohort_id(key, group_fields)
        entry = cohorts.setdefault(cohort_id, {
            'id': cohort_id,
            'count': 0,
            'metadata_values': {name: set() for name in key},
        })
        entry['count'] += 1
        for name, value in key.items():
            entry['metadata_values'][name].add(value)
    for entry in cohorts.values():
        values_by_name = entry['metadata_values']
        entry['metadata_values'] = {
            name: sorted(values) for name, values in values_by_name.items()
        }
        entry['metadata'] = {
            name: _summarize_metadata_values(name, values)
            for name, values in entry['metadata_values'].items()
        }
    return sorted(cohorts.values(), key=lambda item: (-item['count'], item['id']))


def partition_frame_cohorts(files, group_fields=None):
    cohorts = {}
    for path in files:
        key = frame_cohort_key(path)
        cohort_id = _cohort_id(key, group_fields)
        cohorts.setdefault(cohort_id, []).append(path)
    return sorted(cohorts.items(), key=lambda item: (-len(item[1]), item[0]))


def _summarize_metadata_values(name, values):
    values = sorted({str(value) for value in values})
    if len(values) == 1:
        return values[0]
    if name == 'exposure_seconds':
        try:
            numbers = sorted(float(value) for value in values)
        except ValueError:
            pass
        else:
            return f'mixed-{numbers[0]:g}-{numbers[-1]:g}'
    return 'mixed'


def cohort_filename_tag(source, directory=None, run_identifier=None):
    key = source if isinstance(source, dict) else frame_cohort_key(source)

    def token(value):
        value = str(value).strip()
        if re.fullmatch(r'-?\d+(?:\.\d+)?', value):
            value = f'{float(value):g}'
        value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
        return value.strip('._-')[:40] or 'unknown'

    parts = (
        f"camera-{token(key['camera'])}",
        f"exp-{token(key['exposure_seconds'])}s",
        f"gain-{token(key['gain'])}",
        f"filter-{token(key['filter'])}",
        f"size-{token(key['dimensions'])}",
    )
    tag = '_'.join(parts)
    if directory is None:
        return tag
    identifier = str(run_identifier or run_id)
    fixed_names = (
        f'master_stack_{identifier}_cohort_000__cropped.fit',
        f'integration_time_map_{identifier}_cohort_000_.fit',
    )
    tag_budget = MAX_SIRIL_ARTIFACT_PATH - len(str(Path(directory))) - 1 - max(
        len(name) for name in fixed_names
    )
    if tag_budget < 13:
        raise ValueError(
            f'Output directory is too long for Siril artifact paths: {directory}'
        )
    if len(tag) > tag_budget:
        digest = hashlib.sha256(tag.encode('utf-8')).hexdigest()[:10]
        metadata_suffix = '_'.join(parts[1:])
        camera_budget = tag_budget - len(metadata_suffix) - 1
        if camera_budget >= 13:
            camera = f"{parts[0][:camera_budget - 11].rstrip('._-')}-{digest}"
            tag = f'{camera}_{metadata_suffix}'
        else:
            tag = f"{tag[:tag_budget - 11].rstrip('._-')}-{digest}"
    return tag


def remove_tree(path, attempts=8):
    if not path.exists():
        return
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


def _atomic_write_bytes(destination, content):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{destination.name}.', suffix='.tmp', dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(str(destination.parent), os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_text(destination, content):
    encoded = str(content).encode('utf-8')
    _atomic_write_bytes(destination, encoded)


def prepare_siril_for_cleanup():
    if app is None:
        return
    try:
        execute_siril("close")
    except Exception:
        pass
    try:
        execute_siril(f"cd {siril_path(output_dir)}")
    except Exception:
        pass


def close_siril_before_staging():
    global app
    siril_app = app
    if siril_app is None:
        return
    try:
        if not siril_unresponsive:
            _run_siril_call(
                lambda: siril_app.Execute(f"cd {siril_path(output_dir)}"),
                'Siril staging handoff',
                siril_command_timeout,
            )
    except Exception:
        pass
    try:
        siril_app.Close()
    except Exception:
        pass
    app = None


def move_replace(source, destination):
    """Replace an existing generated artifact while moving a new one into place."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(source), str(destination))


def group_files(root_folder, files=None, shuffle_files=True):
    check_cancellation()
    source_folder = root_folder
    destination_folder = root_folder / "Lights_sorted"
    if destination_folder.exists() and any(destination_folder.iterdir()):
        raise RuntimeError("Lights_sorted already exists and is not empty.")
    destination_folder.mkdir(exist_ok=True)
    files = list(files) if files is not None else discover_light_files(source_folder, (output_dir,))
    file_total = active_run_file_total or len(files)
    file_offset = active_cohort_file_offset
    report_progress(
        0, 3, "Staging source frames",
        file_start=file_offset,
        file_end=file_offset + len(files),
        file_total=file_total,
    )
    if shuffle_files:
        random.shuffle(files)
    group_size, remainder = divmod(len(files), SubStack_nb)
    offset = 0
    for folder_index in range(1, SubStack_nb + 1):
        check_cancellation()
        count = group_size + (folder_index <= remainder)
        group_folder = destination_folder / f"group_{folder_index}"
        lights_folder = group_folder / "lights"
        process_folder = group_folder / "process"
        lights_folder.mkdir(parents=True, exist_ok=True)
        process_folder.mkdir(parents=True, exist_ok=True)
        manifest = {}
        staged_files = []
        for image_number, file in enumerate(files[offset:offset + count], 1):
            staging_name = f"frame_{image_number:05d}{file.suffix.lower()}"
            manifest[staging_name] = file.relative_to(source_folder).as_posix()
            staged_files.append((file, staging_name))
        offset += count
        _atomic_write_text(group_folder / SOURCE_MANIFEST, json.dumps(manifest, indent=2))
        for file, staging_name in staged_files:
            check_cancellation()
            shutil.move(str(file), str(lights_folder / staging_name))
        check_cancellation()
        print(f"[INFO] Moved {count} files to {lights_folder}")
        report_progress(
            0, 3, "Staging source frames",
            file_start=file_offset + offset,
            file_end=file_offset + offset,
            file_total=file_total,
        )
    report_progress(
        3, 3, "Source staging complete",
        file_start=file_offset + len(files),
        file_end=file_offset + len(files),
        file_total=file_total,
    )


def read_group_manifests(root_folder, group_numbers):
    manifests = {}
    for group_number in group_numbers:
        manifest_path = root_folder / 'Lights_sorted' / f'group_{group_number}' / SOURCE_MANIFEST
        if not manifest_path.is_file():
            raise RuntimeError(f'Missing source manifest for resume group {group_number}.')
        manifests[str(group_number)] = json.loads(manifest_path.read_text(encoding='utf-8'))
    return manifests


def divide_group_assignments(relative_files, substack_count):
    if substack_count < 1:
        raise ValueError('Substack count must be at least one.')
    group_size, remainder = divmod(len(relative_files), substack_count)
    groups = {}
    offset = 0
    for group_number in range(1, substack_count + 1):
        count = group_size + (group_number <= remainder)
        groups[str(group_number)] = list(relative_files[offset:offset + count])
        offset += count
    return groups


def plan_group_assignments(files, root_folder, substack_count):
    planned_files = list(files)
    random.shuffle(planned_files)
    relative_files = [path.relative_to(root_folder).as_posix() for path in planned_files]
    return divide_group_assignments(relative_files, substack_count)


def restore_staged_group_sources(group_number):
    group_path = workdir / 'Lights_sorted' / f'group_{group_number}'
    manifest_path = group_path / SOURCE_MANIFEST
    if not manifest_path.is_file():
        raise RuntimeError(f'Missing source manifest for resume group {group_number}.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    lights_folder = group_path / 'lights'
    rejects_folder = workdir / REJECTS_DIRECTORY
    for staging_name, relative_path in manifest.items():
        source = lights_folder / staging_name
        destination = workdir / relative_path
        rejected = rejects_folder / relative_path
        if source.is_file():
            if destination.exists() or rejected.exists():
                raise RuntimeError(
                    f'Cannot recover staged source because destination already exists: {destination}'
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        elif not destination.is_file() and not rejected.is_file():
            raise RuntimeError(
                f'Resume source is missing from staging, source, and rejects: {relative_path}'
            )


class InterruptedRunRecoveryError(RuntimeError):
    def __init__(self, message, missing_sources=(), orphan_cleanup_allowed=False):
        super().__init__(message)
        self.missing_sources = tuple(missing_sources)
        self.orphan_cleanup_allowed = orphan_cleanup_allowed


def abandon_interrupted_run(workdir_path, output_dir_path, allow_missing=False):
    workdir_path = Path(workdir_path).expanduser().resolve()
    output_dir_path = Path(output_dir_path).expanduser().resolve()
    staging_root = workdir_path / 'Lights_sorted'
    checkpoint_path = output_dir_path / RUN_CHECKPOINT
    if not staging_root.exists() and not checkpoint_path.is_file():
        return {
            'restored': 0,
            'missing': [],
            'removed_staging': False,
            'removed_checkpoint': False,
        }
    if staging_root.exists() and not staging_root.is_dir():
        raise RuntimeError(f'Cannot abandon run because staging path is not a folder: {staging_root}')

    moves = []
    missing_sources = []
    staging_payloads = []
    if staging_root.is_dir():
        staging_payloads = [
            path for path in staging_root.rglob('*')
            if path.is_file() and path.name != SOURCE_MANIFEST
        ]
        group_paths = sorted(path for path in staging_root.glob('group_*') if path.is_dir())
        for group_path in group_paths:
            manifest_path = group_path / SOURCE_MANIFEST
            if not manifest_path.is_file():
                raise RuntimeError(f'Missing source manifest in staged group: {group_path}')
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f'Could not read source manifest: {manifest_path} ({error})') from error
            lights_folder = group_path / 'lights'
            rejects_folder = workdir_path / REJECTS_DIRECTORY
            for staging_name, relative_value in manifest.items():
                relative_path = Path(str(relative_value))
                if relative_path.is_absolute() or '..' in relative_path.parts:
                    raise RuntimeError(f'Unsafe staged source path: {relative_path}')
                source = lights_folder / staging_name
                destination = workdir_path / relative_path
                rejected = rejects_folder / relative_path
                if source.is_file():
                    if destination.exists() or rejected.exists():
                        raise RuntimeError(
                            f'Cannot restore staged source because destination exists: {destination}'
                        )
                    moves.append((source, destination))
                elif not destination.is_file() and not rejected.is_file():
                    missing_sources.append(str(relative_path))
    if missing_sources and not allow_missing:
        sample = ', '.join(missing_sources[:3])
        suffix = '...' if len(missing_sources) > 3 else ''
        raise InterruptedRunRecoveryError(
            f'{len(missing_sources)} staged source file(s) are missing from staging, source, and rejects: '
            f'{sample}{suffix}',
            missing_sources,
            orphan_cleanup_allowed=not checkpoint_path.is_file() and not staging_payloads,
        )
    if allow_missing and missing_sources:
        if checkpoint_path.is_file():
            raise RuntimeError('Cannot clear orphaned recovery state while a checkpoint is still present.')
        if staging_payloads:
            raise RuntimeError('Cannot clear orphaned recovery state while staged payload files are present.')
    for source, destination in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    if staging_root.exists():
        remove_tree(staging_root)
    remove_tree(workdir_path / 'substacks')
    checkpoint_path.unlink(missing_ok=True)
    return {
        'restored': len(moves),
        'missing': missing_sources,
        'removed_staging': True,
        'removed_checkpoint': checkpoint_path.exists() is False,
    }


def stage_resume_group(group_number, relative_files):
    group_path = workdir / 'Lights_sorted' / f'group_{group_number}'
    lights_folder = group_path / 'lights'
    process_folder = group_path / 'process'
    lights_folder.mkdir(parents=True, exist_ok=True)
    process_folder.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for image_number, relative_name in enumerate(relative_files, 1):
        source = workdir / relative_name
        if not source.is_file():
            raise RuntimeError(f'Resume source file is missing: {source}')
        staging_name = f'frame_{image_number:05d}{source.suffix.lower()}'
        manifest[staging_name] = relative_name
        shutil.move(str(source), str(lights_folder / staging_name))
    _atomic_write_text(group_path / SOURCE_MANIFEST, json.dumps(manifest, indent=2))


def prepare_resume_staging(checkpoint):
    completed = {int(number) for number in checkpoint.get('completed_substacks', [])}
    group_files = checkpoint.get('groups', {})
    if not group_files:
        raise RuntimeError('Resume checkpoint has no staged group assignments.')
    lights_sorted = workdir / 'Lights_sorted'
    lights_sorted.mkdir(exist_ok=True)
    for group_number in range(1, int(checkpoint['substack_count']) + 1):
        group_path = lights_sorted / f'group_{group_number}'
        if group_number in completed:
            artifact = group_path / f'substack_{group_number}.fit'
            moved_artifact = workdir / 'substacks' / 'lights' / artifact.name
            if not artifact.is_file() and moved_artifact.is_file():
                group_path.mkdir(parents=True, exist_ok=True)
                move_replace(moved_artifact, artifact)
                moved_maps = workdir / 'substacks' / 'rejection_maps'
                for rejection_map in moved_maps.glob(f'substack_{group_number}_*rejmap.fit'):
                    move_replace(rejection_map, group_path / rejection_map.name)
            if not artifact.is_file():
                raise RuntimeError(
                    f'Completed resume substack artifact is missing: {artifact}'
                )
            continue
        if group_path.is_dir():
            restore_staged_group_sources(group_number)
            remove_tree(group_path)
        stage_resume_group(group_number, group_files.get(str(group_number), []))
    return completed


def cleanup(group_num, restore_sources=None):
    group_path = workdir / 'Lights_sorted' / f'group_{group_num}'
    process_folder = group_path / "process"
    lights_folder = group_path / "lights"
    master_light_folder = workdir
    if not debug:
        for ext in ('*.fit', '*.seq', '*.txt'):
            for path in process_folder.glob(ext):
                path.unlink()
    should_restore_sources = True if restore_sources is None else restore_sources
    if should_restore_sources:
        manifest_path = group_path / SOURCE_MANIFEST
        if not manifest_path.is_file():
            raise RuntimeError(f"Missing source manifest for group {group_num}.")
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for staging_name, relative_path in manifest.items():
            source = lights_folder / staging_name
            destination = master_light_folder / relative_path
            if source.is_file() and destination.exists():
                raise RuntimeError(f"Cannot restore source because destination exists: {destination}")
            if source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            elif not destination.is_file():
                raise RuntimeError(f"Source frame is missing from staging and its original path: {source}")


def restore_staged_cohort_sources():
    for group_number in range(1, SubStack_nb + 1):
        group_path = workdir / 'Lights_sorted' / f'group_{group_number}'
        if group_path.is_dir():
            cleanup(group_number, restore_sources=True)
    remove_tree(workdir / 'Lights_sorted')
    remove_tree(workdir / 'substacks')


def move_frame_selection_rejects(discarded_frames):
    rejected = [
        record for record in discarded_frames
        if record.get('status') == 'rejected'
        and record.get('reason_code') == 'registration_excluded'
    ]
    if not rejected:
        return []
    rejects_folder = workdir / REJECTS_DIRECTORY
    moves = []
    for record in rejected:
        relative_name = Path(str(record['file']))
        if relative_name.is_absolute() or '..' in relative_name.parts:
            raise RuntimeError(f"Rejected frame path is unsafe: {relative_name}")
        source = workdir / relative_name
        destination = rejects_folder / relative_name
        if not source.is_file():
            raise RuntimeError(f"Rejected frame is missing after source restoration: {source}")
        if destination.exists():
            raise RuntimeError(f"Reject destination already exists: {destination}")
        moves.append((source, destination, relative_name.as_posix()))
    for source, destination, relative_name in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    moved = [
        {
            'path': relative_name,
            'rejects_path': f'{REJECTS_DIRECTORY}/{relative_name}',
            'reason_code': 'registration_excluded',
            'reason': 'Excluded by frame-selection filters during registration',
        }
        for _, _, relative_name in moves
    ]
    print(f"[INFO] Moved {len(moved)} frame-selection reject(s) to {rejects_folder}", flush=True)
    append_journal_event(
        'rejected_frames_moved',
        count=len(moved),
        files=moved,
    )
    return moved


class SirilCommandError(RuntimeError):
    pass


class NoPlateSolveFramesError(SirilCommandError):
    def __init__(self, command, response):
        self.command = command
        self.response = tuple(response)
        details = "\n".join(line for line in response if str(line).strip())
        super().__init__(f"No images successfully plate-solved for {command}.\n{details}")


def terminate_siril_processes(parent_pid=None):
    parent = psutil.Process(parent_pid or os.getpid())
    try:
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0
    targets = []
    for process in descendants:
        try:
            if process.name().casefold() in {'siril.exe', 'siril-cli.exe', 'siril'}:
                targets.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
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


def _process_command_line(process):
    try:
        return ' '.join(process.cmdline()).casefold()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return ''


def _same_run_scope(command_line, workdir_path, output_dir_path):
    normalized = command_line.replace('\\', '/').casefold()
    return all(
        token in normalized
        for token in (
            str(Path(workdir_path).expanduser().resolve()).replace('\\', '/').casefold(),
            str(Path(output_dir_path).expanduser().resolve()).replace('\\', '/').casefold(),
        )
    )


def terminate_stale_run_processes(workdir_path, output_dir_path):
    current_pid = os.getpid()
    roots = []
    siril_targets = []
    for process in psutil.process_iter(['pid', 'name', 'ppid']):
        if process.info['pid'] == current_pid:
            continue
        command_line = _process_command_line(process)
        if not _same_run_scope(command_line, workdir_path, output_dir_path):
            continue
        name = (process.info.get('name') or '').casefold()
        if name == 'python.exe' and 'sirilmosaic.py' in command_line and 'sirilmosaic_gui.py' not in command_line:
            roots.append(process)
        elif name in {'siril.exe', 'siril-cli.exe', 'siril'} and ' -p' in command_line:
            siril_targets.append(process)
    targets = set(siril_targets)
    for root in roots:
        try:
            targets.update(root.children(recursive=True))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        targets.add(root)
    targets = [process for process in targets if process.pid != current_pid]
    for process in targets:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = psutil.wait_procs(targets, timeout=5)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    return len(targets)


def _is_siril_pipe_failure(error):
    text = str(error).casefold()
    return any(token in text for token in (
        'broken pipe',
        'pipe instances are busy',
        'cpipe::open',
        'pipeout.write',
        'pysiril is already closed',
    ))


def _plate_solve_success_count(command, response):
    if not command.casefold().startswith('seqplatesolve '):
        return None
    text = '\n'.join(response).casefold()
    match = re.search(r'(\d+)\s+images?\s+successfully\s+platesolved\s+out\s+of', text)
    return int(match.group(1)) if match else None


def _is_partial_plate_solve(command, response):
    if not command.casefold().startswith('seqplatesolve ') or not skip_failed_frames:
        return False
    text = '\n'.join(response).casefold()
    return (
        'sequence processing partially succeeded' in text
        and (_plate_solve_success_count(command, response) or 0) > 0
    )


def _is_all_failed_plate_solve(command, response):
    return (
        skip_failed_frames
        and command.casefold().startswith('seqplatesolve ')
        and _plate_solve_success_count(command, response) == 0
    )


def open_siril_with_recovery(siril_factory):
    global app, siril_unresponsive
    try:
        opened = _run_siril_call(app.Open, 'Siril open', siril_open_timeout)
        if opened is not False:
            siril_unresponsive = False
            return
        error = RuntimeError('Siril failed to open its command pipe.')
    except Exception as error:
        if siril_unresponsive or not _is_siril_pipe_failure(error):
            raise
    current_terminated = terminate_siril_processes()
    stale_terminated = terminate_stale_run_processes(workdir, output_dir)
    print(
        f'[WARNING] Siril pipe was unavailable; terminated {current_terminated + stale_terminated} '
        'stale Siril/backend process(es) and retrying once.',
        flush=True,
    )
    app = siril_factory(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
    opened = _run_siril_call(app.Open, 'Siril open retry', siril_open_timeout)
    if opened is False:
        raise RuntimeError('Siril failed to open its command pipe after stale-process recovery.')
    siril_unresponsive = False


def _run_siril_call(callback, label, timeout):
    global siril_unresponsive
    if timeout <= 0:
        return callback()
    result = {}
    completed = threading.Event()

    def worker():
        try:
            result['value'] = callback()
        except BaseException as error:
            result['error'] = error
        finally:
            completed.set()

    threading.Thread(target=worker, name='siril-watchdog-call', daemon=True).start()
    if not completed.wait(timeout):
        siril_unresponsive = True
        terminated = terminate_siril_processes()
        raise SirilCommandError(
            f'{label} timed out after {timeout:.1f}s; terminated {terminated} Siril process(es).'
        )
    if 'error' in result:
        raise result['error']
    return result.get('value')


def execute_siril(command, allow_partial=False):
    global command_sequence
    check_cancellation()
    siril_app = app
    if siril_app is None:
        raise RuntimeError("Siril is not open.")
    if siril_unresponsive:
        raise SirilCommandError('Siril is marked unresponsive after a watchdog timeout.')
    command_sequence += 1
    command_id = command_sequence
    started = time.perf_counter()
    response = []
    status = 'failed'
    try:
        succeeded = _run_siril_call(
            lambda: siril_app.Execute(command),
            f'Siril command {command!r}',
            siril_command_timeout,
        ) is not False
        get_data = getattr(siril_app, "GetData", None)
        data = (
            _run_siril_call(get_data, f'Siril response for {command!r}', siril_command_timeout)
            if callable(get_data) else []
        )
        response = [str(line) for line in data] if isinstance(data, (list, tuple)) else []
        check_cancellation()
        if not succeeded:
            if allow_partial:
                if _is_all_failed_plate_solve(command, response):
                    status = 'no_usable_frames'
                    print(
                        '[WARNING] Siril plate-solved zero frames; '
                        'the current cohort will be handled as unusable.',
                        flush=True,
                    )
                    raise NoPlateSolveFramesError(command, response)
                if _is_partial_plate_solve(command, response):
                    status = 'partial'
                    print(
                        '[WARNING] Siril reported partial plate-solving success; '
                        'continuing with the successfully solved frames.',
                        flush=True,
                    )
                    return response
            details = "\n".join(line for line in response if line.strip())
            raise RuntimeError(f"Siril command failed: {command}\n{details}")
        status = 'complete'
        return response
    except CancellationRequested:
        status = 'cancelled'
        raise
    except SirilCommandError:
        raise
    except Exception as error:
        raise SirilCommandError(str(error)) from error
    finally:
        elapsed = round(time.perf_counter() - started, 3)
        if quality_report is not None:
            quality_report.setdefault('commands', []).append({
                'id': command_id,
                'command': command,
                'seconds': elapsed,
                'status': status,
                'substack': quality_report.get('active_substack'),
                'phase': active_phase,
                'response_tail': response[-20:],
            })
            if status not in {'complete', 'partial'}:
                quality_report['last_failed_command'] = command
                quality_report['siril_response_tail'] = response[-20:]
                failures = quality_report.setdefault('frame_failures', [])
                for line in response:
                    match = re.search(r'Image\s+(\S+)\s+did not solve', line, re.IGNORECASE)
                    if match:
                        failures.append({
                            'sequence_file': match.group(1),
                            'substack': quality_report.get('active_substack'),
                            'reason_code': 'plate_solving_failed',
                        })
        append_journal_event(
            'siril_command',
            id=command_id,
            command=command,
            seconds=elapsed,
            status=status,
            phase=active_phase,
            cohort=active_cohort_id,
            substack=quality_report.get('active_substack') if quality_report else None,
            response_tail=response[-20:],
        )


def siril_path(path):
    return f'"{Path(path).as_posix()}"'


def siril_path_option(name, path):
    return f'"-{name}={Path(path).as_posix()}"'


def choose_run_id(folder, preferred=None):
    base_id = preferred or datetime.now().strftime('%Y%m%d_%H%M%S')
    candidate = base_id
    counter = 2
    while folder.exists() and any(folder.glob(f"*_{candidate}*")):
        candidate = f"{base_id}_{counter}"
        counter += 1
    return candidate


def master_stack_path():
    return active_master_path or (output_dir / f"master_stack_{run_id}.fit")


def _run_artifact_name(filename, prefix):
    return filename.replace(f"{prefix}_", f"{prefix}_{run_id}_", 1)


def quality_report_path():
    return output_dir / f"quality_report_{run_id}.json"


def frame_ledger_file_path():
    return frame_ledger_path or (output_dir / f"frame_ledger_{run_id}.jsonl")


def run_checkpoint_file_path():
    return output_dir / RUN_CHECKPOINT


def run_lock_file_path(path=None):
    return (Path(path).expanduser().resolve() if path is not None else output_dir) / RUN_LOCK


def inspect_run_lock(path=None):
    destination = run_lock_file_path(path)
    if not destination.is_file():
        return {'status': 'NONE', 'path': str(destination), 'lock': None}
    try:
        lock = json.loads(destination.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        return {'status': 'INVALID', 'path': str(destination), 'lock': None, 'error': str(error)}
    return {
        'status': 'AVAILABLE',
        'path': str(destination),
        'lock': lock,
        'age_seconds': max(0.0, time.time() - destination.stat().st_mtime),
    }


def acquire_run_lock():
    global run_lock_payload
    destination = run_lock_file_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'pid': os.getpid(),
        'host': platform.node(),
        'run_id': run_id,
        'workdir': str(workdir),
        'output_dir': str(output_dir),
        'started_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    try:
        descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        info = inspect_run_lock(destination.parent)
        raise RuntimeError(
            f'Another run owns {destination}. Inspect it with --run-lock-status; '
            'remove it with --break-run-lock only after confirming no run is active. '
            f'Lock details: {info.get("lock") or info.get("error") or "unavailable"}'
        ) from error
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(payload, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    run_lock_payload = payload
    return destination


def release_run_lock():
    global run_lock_payload
    if run_lock_payload is None:
        return
    destination = run_lock_file_path()
    try:
        current = json.loads(destination.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        current = None
    if current == run_lock_payload:
        destination.unlink(missing_ok=True)
    run_lock_payload = None


def break_run_lock(path=None):
    destination = run_lock_file_path(path)
    existed = destination.exists()
    destination.unlink(missing_ok=True)
    return existed


def write_run_checkpoint(checkpoint):
    _atomic_write_text(run_checkpoint_file_path(), json.dumps(checkpoint, indent=2))


def normalize_run_checkpoint(checkpoint):
    normalized = json.loads(json.dumps(checkpoint))
    schema_version = normalized.get('schema_version')
    if schema_version not in SUPPORTED_CHECKPOINT_SCHEMAS:
        raise RuntimeError(f'Unsupported resume checkpoint schema: {schema_version}')
    cohorts = normalized.get('cohorts')
    if schema_version == 2 and not isinstance(cohorts, list):
        raise RuntimeError('Checkpoint schema 2 is missing cohort resume state.')
    state = normalized.get('state', 'interrupted')
    normalized.setdefault('global_phase', LEGACY_CHECKPOINT_PHASES.get(state, state))
    if normalized['global_phase'] not in CHECKPOINT_PHASES:
        raise RuntimeError(f'Unsupported resume checkpoint phase: {normalized["global_phase"]}')
    for cohort in cohorts or []:
        cohort_state = cohort.get('state', 'pending')
        cohort.setdefault('phase', LEGACY_CHECKPOINT_PHASES.get(cohort_state, cohort_state))
        if cohort['phase'] not in CHECKPOINT_PHASES:
            raise RuntimeError(f'Unsupported cohort checkpoint phase: {cohort["phase"]}')
    return normalized


def load_run_checkpoint(path=None):
    source = Path(path) if path is not None else run_checkpoint_file_path()
    if not source.is_file():
        raise FileNotFoundError(f'Resume checkpoint does not exist: {source}')
    try:
        checkpoint = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f'Could not read resume checkpoint: {source} ({error})') from error
    return normalize_run_checkpoint(checkpoint)


def remove_run_checkpoint():
    run_checkpoint_file_path().unlink(missing_ok=True)


def append_frame_ledger(records):
    if not records:
        return
    destination = frame_ledger_file_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('a', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    if quality_report is not None:
        quality_report['frame_ledger_records'] = (
            quality_report.get('frame_ledger_records', 0) + len(records)
        )
    append_journal_event(
        'frame_ledger_appended',
        count=len(records),
        path=str(destination),
    )


def _verification_path(report_path, value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else Path(report_path).parent / path


def verify_run(report_path):
    report_path = Path(report_path).expanduser().resolve()
    checks = []

    def add_check(name, status, detail):
        checks.append({'name': name, 'status': status, 'detail': detail})

    def require_file(name, value, required=True):
        path = _verification_path(report_path, value)
        if path is None:
            add_check(name, 'FAIL' if required else 'WARN', 'No path is recorded.')
            return None
        if not path.is_file():
            add_check(name, 'FAIL' if required else 'WARN', f'Missing: {path}')
            return path
        add_check(name, 'PASS', str(path))
        return path

    if not report_path.is_file():
        return {
            'status': 'FAIL',
            'report_path': str(report_path),
            'checks': [{'name': 'quality_report', 'status': 'FAIL', 'detail': f'Missing: {report_path}'}],
            'counts': {'PASS': 0, 'WARN': 0, 'FAIL': 1},
        }
    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        return {
            'status': 'FAIL',
            'report_path': str(report_path),
            'checks': [{'name': 'quality_report', 'status': 'FAIL', 'detail': str(error)}],
            'counts': {'PASS': 0, 'WARN': 0, 'FAIL': 1},
        }

    status = report.get('status')
    add_check(
        'run_status',
        'PASS' if status == 'complete' else 'FAIL',
        f'Report status: {status or "unknown"}.',
    )
    schema = report.get('report_schema_version')
    add_check(
        'report_schema',
        'PASS' if isinstance(schema, int) and schema >= 3 else 'WARN',
        f'Report schema version: {schema or "unknown"}.',
    )

    inventory = report.get('artifact_inventory')
    if inventory is None:
        add_check(
            'artifact_inventory', 'PASS',
            'Legacy compatibility mode: generated-artifact fingerprints were not recorded.',
        )
    else:
        inventory_ok = True
        details = []
        for entry in inventory:
            path = _verification_path(report_path, entry.get('path'))
            if path is None or not path.is_file():
                inventory_ok = False
                details.append(f'{entry.get("role", "artifact")}: missing')
                continue
            matches = (
                path.stat().st_size == entry.get('bytes')
                and _sha256_file(path) == entry.get('sha256')
            )
            inventory_ok &= matches
            details.append(f'{entry.get("role", "artifact")}: {"match" if matches else "mismatch"}')
        add_check(
            'artifact_inventory', 'PASS' if inventory_ok and inventory else 'FAIL',
            '; '.join(details) if details else 'No generated artifacts were inventoried.',
        )

    journal_path = require_file('run_journal', report.get('journal_path'))
    if journal_path is not None and journal_path.is_file():
        journal_records = []
        journal_error = None
        try:
            for line_number, line in enumerate(
                journal_path.read_text(encoding='utf-8').splitlines(), 1
            ):
                if line.strip():
                    journal_records.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as error:
            journal_error = str(error)
        if journal_error:
            add_check('journal_jsonl', 'FAIL', journal_error)
        else:
            sequences = [record.get('sequence') for record in journal_records]
            valid_sequences = sequences == list(range(1, len(sequences) + 1))
            finished = any(
                record.get('event') == 'run_finished'
                and record.get('status') == 'complete'
                for record in journal_records
            )
            add_check(
                'journal_integrity',
                'PASS' if valid_sequences and finished else 'FAIL',
                f'{len(journal_records)} events; sequences and completion event '
                f'are {"valid" if valid_sequences and finished else "not valid"}.',
            )

    manifest_path = require_file('input_manifest', report.get('input_manifest'))
    manifest = None
    if manifest_path is not None and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            selected_files = manifest.get('selected_files', [])
            expected_frames = report.get('input_frames')
            add_check(
                'manifest_frame_count',
                'PASS' if expected_frames is None or len(selected_files) == expected_frames else 'FAIL',
                f'Manifest lists {len(selected_files)} frame(s); report lists {expected_frames}.',
            )
        except (OSError, json.JSONDecodeError) as error:
            add_check('manifest_json', 'FAIL', str(error))

    ledger_value = report.get('frame_ledger_path')
    if ledger_value:
        ledger_path = require_file('frame_ledger', ledger_value)
    else:
        ledger_path = None
        add_check(
            'frame_ledger',
            'WARN',
            'No frame ledger is recorded; this report predates full-frame ledger support.',
        )
    if ledger_path is not None and ledger_path.is_file():
        ledger_records = []
        ledger_error = None
        try:
            for line in ledger_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    ledger_records.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as error:
            ledger_error = str(error)
        if ledger_error:
            add_check('frame_ledger_jsonl', 'FAIL', ledger_error)
        else:
            ledger_files = [record.get('file') for record in ledger_records]
            unique_files = len(ledger_files) == len(set(ledger_files))
            expected_files = (
                [record.get('path') for record in manifest.get('selected_files', [])]
                if manifest is not None else []
            )
            count_matches = (
                report.get('input_frames') is None
                or len(ledger_records) == report.get('input_frames')
            )
            files_match = not expected_files or set(ledger_files) == set(expected_files)
            statuses = {'stacked', 'registered', 'rejected', 'failed'}
            valid_statuses = all(record.get('status') in statuses for record in ledger_records)
            ledger_ok = unique_files and count_matches and files_match and valid_statuses
            add_check(
                'frame_ledger_integrity',
                'PASS' if ledger_ok else 'FAIL',
                f'{len(ledger_records)} frame records; unique={unique_files}, '
                f'count_matches={count_matches}, files_match={files_match}, '
                f'statuses_valid={valid_statuses}.',
            )

    artifact_entries = []
    masters = report.get('masters') or []
    coverages = report.get('coverages') or []
    if report.get('settings', {}).get('export_per_cohort'):
        expected_cohorts = {
            cohort.get('id') for cohort in report.get('cohorts', []) if cohort.get('id')
        }
        skipped_cohorts = {
            cohort.get('cohort')
            for cohort in report.get('skipped_cohorts', []) if cohort.get('cohort')
        }
        expected_cohorts -= skipped_cohorts

        def verify_cohort_records(name, records):
            record_ids = [record.get('cohort') for record in records]
            present_ids = {cohort_id for cohort_id in record_ids if cohort_id}
            duplicates = sorted({
                cohort_id for cohort_id in present_ids if record_ids.count(cohort_id) > 1
            })
            missing = sorted(expected_cohorts - present_ids)
            unexpected = sorted(present_ids - expected_cohorts)
            valid = not missing and not duplicates and not unexpected
            add_check(
                name,
                'PASS' if valid else 'FAIL',
                f'{len(records)} record(s) for {len(expected_cohorts)} expected cohort(s); '
                f'missing={missing}, duplicates={duplicates}, unexpected={unexpected}.',
            )

        verify_cohort_records('cohort_master_records', masters)
        if report.get('settings', {}).get('coverage_map'):
            verify_cohort_records('cohort_coverage_records', coverages)
    if masters:
        for index, master in enumerate(masters):
            cohort = master.get('cohort')
            coverage = next(
                (item for item in coverages if cohort and item.get('cohort') == cohort),
                coverages[index] if index < len(coverages) else {},
            )
            artifact_entries.append((f'cohort_{index + 1}_master', master, coverage))
    else:
        artifact_entries.append(('master', report.get('master', {}), report.get('coverage', {})))

    for label, master_report, coverage_report in artifact_entries:
        master_path = require_file(f'{label}_artifact', master_report.get('path'))
        coverage_enabled = bool(report.get('settings', {}).get('coverage_map')) or bool(coverage_report)
        if coverage_enabled:
            integration_path = require_file(
                f'{label}_integration_time_map',
                coverage_report.get('integration_time_path', coverage_report.get('path')),
            )
        else:
            integration_path = None
            add_check(
                f'{label}_coverage_artifacts',
                'PASS',
                'Coverage map was disabled for this run; coverage checks are not applicable.',
            )
        if master_path is not None and integration_path is not None:
            try:
                master_width, master_height = _read_fits_dimensions(master_path)
                coverage_width, coverage_height = _read_fits_dimensions(integration_path)
                matches = (master_width, master_height) == (coverage_width, coverage_height)
                add_check(
                    f'{label}_canvas_dimensions',
                    'PASS' if matches else 'FAIL',
                    f'Master {master_width}x{master_height}; map {coverage_width}x{coverage_height}.',
                )
            except (OSError, ValueError) as error:
                add_check(f'{label}_canvas_dimensions', 'FAIL', str(error))
        cropped_path = coverage_report.get('cropped_master_path')
        if cropped_path:
            cropped = require_file(f'{label}_cropped_master', cropped_path)
            if cropped is not None and cropped.is_file() and master_path is not None and master_path.is_file():
                try:
                    cropped_width, cropped_height = _read_fits_dimensions(cropped)
                    master_width, master_height = _read_fits_dimensions(master_path)
                    valid_crop = 0 < cropped_width <= master_width and 0 < cropped_height <= master_height
                    add_check(
                        f'{label}_crop_dimensions',
                        'PASS' if valid_crop else 'FAIL',
                        f'Cropped master {cropped_width}x{cropped_height}; '
                        f'master {master_width}x{master_height}.',
                    )
                    retained_area = (
                        cropped_width * cropped_height / (master_width * master_height)
                    )
                    add_check(
                        f'{label}_crop_retained_area',
                        'PASS' if retained_area >= 0.01 else 'WARN',
                        f'Cropped master retains {retained_area:.2%} of the master area.',
                    )
                except (OSError, ValueError) as error:
                    add_check(f'{label}_crop_dimensions', 'FAIL', str(error))
        elif report.get('settings', {}).get('auto_crop_master'):
            add_check(
                f'{label}_cropped_master',
                'WARN',
                'Auto-crop was enabled but no cropped master path is recorded.',
            )

    rejects_directory = _verification_path(
        report_path,
        report.get('settings', {}).get('rejects_directory'),
    )
    if rejects_directory is not None:
        rejects_directory = rejects_directory.resolve()
    if manifest is not None and rejects_directory is not None:
        missing_sources = []
        collisions = []
        for record in manifest.get('selected_files', []):
            relative = Path(str(record.get('path', '')))
            if relative.is_absolute() or '..' in relative.parts:
                missing_sources.append(str(relative))
                continue
            source = rejects_directory.parent / relative
            rejected = rejects_directory / relative
            if source.is_file() and rejected.is_file():
                collisions.append(str(relative))
            elif not source.is_file() and not rejected.is_file():
                missing_sources.append(str(relative))
        if missing_sources:
            add_check(
                'source_restoration',
                'FAIL',
                f'{len(missing_sources)} manifest frame(s) are missing from source and rejects paths.',
            )
        elif collisions:
            add_check(
                'source_restoration',
                'WARN',
                f'{len(collisions)} frame(s) exist in both source and rejects paths.',
            )
        else:
            add_check(
                'source_restoration',
                'PASS',
                'Every manifest frame exists at its source path or mirrored rejects path.',
            )

        rejected_records = report.get('rejected_files', [])
        missing_rejects = []
        rejects_present = 0
        rejects_restored = 0
        for record in rejected_records:
            relative = Path(str(record.get('path', '')))
            if relative.is_absolute() or '..' in relative.parts:
                missing_rejects.append(str(relative))
                continue
            if (rejects_directory / relative).is_file():
                rejects_present += 1
            elif (rejects_directory.parent / relative).is_file():
                rejects_restored += 1
            else:
                missing_rejects.append(str(relative))
        add_check(
            'rejected_file_agreement',
            'PASS' if not missing_rejects else 'FAIL',
            f'{rejects_present} reported reject(s) are in the rejects directory; '
            f'{rejects_restored} were restored to source; {len(missing_rejects)} are missing.',
        )

    substacks = report.get('substacks', [])
    input_frames = report.get('input_frames')
    if substacks and input_frames is not None:
        substack_input = sum(item.get('input_frames', 0) or 0 for item in substacks)
        add_check(
            'substack_input_accounting',
            'PASS' if substack_input == input_frames else 'FAIL',
            f'Substack inputs sum to {substack_input}; report input is {input_frames}.',
        )
        accounting_failures = []
        for item in substacks:
            item_input = item.get('input_frames')
            accepted = item.get('accepted_frames')
            rejected = item.get('rejected_frames')
            stacked = item.get('stack', {}).get('stacked_frames')
            if item_input is not None and accepted is not None and rejected is not None:
                if item_input != accepted + rejected:
                    accounting_failures.append(item.get('number', '?'))
            if stacked is not None and accepted is not None and stacked > accepted:
                accounting_failures.append(item.get('number', '?'))
        add_check(
            'substack_frame_accounting',
            'PASS' if not accounting_failures else 'FAIL',
            'Accepted, rejected, and stacked frame counts are internally consistent.'
            if not accounting_failures else
            f'Inconsistent substack number(s): {accounting_failures}.',
        )

    counts = {status: sum(check['status'] == status for check in checks) for status in ('PASS', 'WARN', 'FAIL')}
    overall = 'FAIL' if counts['FAIL'] else 'WARN' if counts['WARN'] else 'PASS'
    return {
        'status': overall,
        'report_path': str(report_path),
        'run_id': report.get('run_id'),
        'checks': checks,
        'counts': counts,
    }


def verification_artifact_path(report_path):
    report_path = Path(report_path)
    return report_path.with_name(f'verification_{report_path.stem.removeprefix("quality_report_")}.json')


def write_verification_artifact(report_path):
    verification = verify_run(report_path)
    destination = verification_artifact_path(report_path)
    _atomic_write_text(destination, json.dumps(verification, indent=2))
    return destination, verification


def create_run_bundle(report_path, destination=None):
    report_path = Path(report_path).expanduser().resolve()
    if not report_path.is_file():
        raise FileNotFoundError(f'Quality report does not exist: {report_path}')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    destination = Path(destination).expanduser().resolve() if destination else (
        report_path.parent / f'{report_path.stem}_bundle.zip'
    )
    candidates = [
        report_path,
        _verification_path(report_path, report.get('journal_path')),
        _verification_path(report_path, report.get('input_manifest')),
        _verification_path(report_path, report.get('frame_ledger_path')),
        verification_artifact_path(report_path),
    ]
    verification_path = verification_artifact_path(report_path)
    if not verification_path.is_file():
        write_verification_artifact(report_path)
    settings = report.get('settings') or {}
    log_roots = {report_path.parent / 'siril_mosaic_logs'}
    rejects_directory = settings.get('rejects_directory')
    if rejects_directory:
        log_roots.add(Path(rejects_directory).expanduser().parent / 'siril_mosaic_logs')
    for log_root in log_roots:
        if log_root.is_dir():
            candidates.extend(sorted(log_root.glob('*.log')))
    existing = []
    seen = set()
    for path in candidates:
        if path is not None and path.is_file() and path.resolve() not in seen:
            existing.append(path)
            seen.add(path.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in existing:
            archive.write(path, arcname=path.name)
        archive.writestr(
            f"configuration_{report.get('run_id', report_path.stem)}.json",
            json.dumps({
                'run_id': report.get('run_id'),
                'configuration_hash': report.get('configuration_hash'),
                'seed': report.get('seed'),
                'settings': settings,
                'environment': report.get('environment', {}),
            }, indent=2),
        )
    temporary.replace(destination)
    return destination


def export_html_run_report(report_path, destination=None):
    report_path = Path(report_path).expanduser().resolve()
    if not report_path.is_file():
        raise FileNotFoundError(f'Quality report does not exist: {report_path}')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    verification = verify_run(report_path)
    ledger_path = _verification_path(report_path, report.get('frame_ledger_path'))
    records = read_frame_ledger(ledger_path) if ledger_path and ledger_path.is_file() else []
    status_counts = Counter(record.get('status', 'unknown') for record in records)
    destination = Path(destination).expanduser().resolve() if destination else report_path.with_suffix('.html')
    settings = report.get('settings') or {}
    integration = report.get('integration') or {}
    coverage = report.get('coverage') or {}
    checks_html = ''.join(
        f"<tr><td>{html.escape(str(check['status']))}</td><td>{html.escape(str(check['name']))}</td><td>{html.escape(str(check['detail']))}</td></tr>"
        for check in verification.get('checks', [])
    )
    settings_html = ''.join(
        f"<tr><td>{html.escape(str(key))}</td><td>{html.escape(str(value))}</td></tr>"
        for key, value in sorted(settings.items())
    )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Siril Mosaic Run {html.escape(str(report.get('run_id', report_path.stem)))}</title>
<style>body{{font:14px Segoe UI, sans-serif;background:#17191e;color:#e7e8eb;margin:32px}}h1,h2{{color:#f2c078}}table{{border-collapse:collapse;width:100%;margin:12px 0 24px}}td,th{{border:1px solid #3a3d45;padding:7px;text-align:left;vertical-align:top}}th{{background:#252832}}.PASS{{color:#8fd3a8}}.WARN{{color:#f2c078}}.FAIL{{color:#f28b82}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.card{{border:1px solid #3a3d45;padding:12px;background:#20232a}}code{{color:#b6d7ff}}</style></head>
<body><h1>Siril Mosaic Run {html.escape(str(report.get('run_id', report_path.stem)))}</h1>
<p>Status: <strong class="{html.escape(str(report.get('status', 'unknown')))}">{html.escape(str(report.get('status', 'unknown')))}</strong> | Report: <code>{html.escape(str(report_path))}</code></p>
<div class="grid"><div class="card">Input frames<br><strong>{report.get('input_frames', 0)}</strong></div><div class="card">Stacked frames<br><strong>{status_counts.get('stacked', integration.get('stacked_frames', 0))}</strong></div><div class="card">Rejected frames<br><strong>{status_counts.get('rejected', 0)}</strong></div><div class="card">Integrated hours<br><strong>{integration.get('integrated_hours', 'unavailable')}</strong></div></div>
<h2>Verification</h2><p>Overall: <strong class="{html.escape(str(verification.get('status')))}">{html.escape(str(verification.get('status')))}</strong></p><table><tr><th>Status</th><th>Check</th><th>Detail</th></tr>{checks_html}</table>
<h2>Coverage</h2><table><tr><th>Field</th><th>Value</th></tr><tr><td>Canvas</td><td>{html.escape(str(coverage.get('width', 'unavailable')))} x {html.escape(str(coverage.get('height', 'unavailable')))}</td></tr><tr><td>Maximum integration</td><td>{html.escape(str(coverage.get('maximum_integration_seconds', 'unavailable')))} seconds</td></tr><tr><td>Cropped master</td><td>{html.escape(str(coverage.get('cropped_master_path', 'unavailable')))}</td></tr></table>
<h2>Configuration</h2><table><tr><th>Setting</th><th>Value</th></tr>{settings_html}</table>
<p>Generated by Siril Mosaic Stacker. Raw light frames are not embedded.</p></body></html>"""
    _atomic_write_text(destination, body)
    return destination


def inspect_checkpoint(output_dir, workdir=None):
    path = Path(output_dir).expanduser().resolve() / RUN_CHECKPOINT
    if not path.is_file():
        return {'status': 'NONE', 'path': str(path), 'checkpoint': None}
    try:
        checkpoint = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        return {'status': 'INVALID', 'path': str(path), 'checkpoint': None, 'error': str(error)}
    checks = []
    requested_workdir = Path(workdir).expanduser().resolve() if workdir is not None else None
    checkpoint_workdir = requested_workdir or Path(checkpoint.get('workdir', '')).expanduser().resolve()
    checkpoint_output = Path(checkpoint.get('output_dir', output_dir)).expanduser().resolve()
    if checkpoint.get('schema_version') not in {1, 2}:
        checks.append({'status': 'FAIL', 'name': 'schema', 'detail': 'Unsupported checkpoint schema.'})
    else:
        checks.append({'status': 'PASS', 'name': 'schema', 'detail': f"Schema {checkpoint['schema_version']} is supported."})
    requested_output = Path(output_dir).expanduser().resolve()
    for name, expected in (
        ('workdir', requested_workdir or checkpoint_workdir),
        ('output_dir', requested_output),
    ):
        actual = Path(checkpoint.get(name, '')).expanduser().resolve()
        checks.append({
            'status': 'PASS' if actual == expected else 'FAIL',
            'name': name,
            'detail': str(actual),
        })
    substack_count = checkpoint.get('substack_count')
    try:
        substack_count = int(substack_count)
    except (TypeError, ValueError):
        substack_count = 0
    completed = checkpoint.get('completed_substacks', [])
    valid_completed = all(isinstance(item, int) and 1 <= item <= substack_count for item in completed)
    checks.append({
        'status': 'PASS' if substack_count > 0 and valid_completed else 'FAIL',
        'name': 'substack_state',
        'detail': f'{len(completed)} of {substack_count} substacks marked complete.',
    })
    for name in ('input_manifest', 'quality_report', 'frame_ledger'):
        value = checkpoint.get(name)
        candidate = Path(value).expanduser() if value else Path('')
        if not candidate.is_absolute():
            candidate = checkpoint_output / candidate
        checks.append({
            'status': 'PASS' if value and candidate.is_file() else 'WARN',
            'name': name,
            'detail': str(candidate),
        })
    groups = checkpoint.get('groups', {})
    if checkpoint.get('cohorts'):
        groups = checkpoint.get('cohorts', [{}])[0].get('groups', groups)
    for number in completed:
        artifact = checkpoint_workdir / 'Lights_sorted' / f'group_{number}' / f'substack_{number}.fit'
        checks.append({
            'status': 'PASS' if artifact.is_file() else 'FAIL',
            'name': f'completed_substack_{number}',
            'detail': str(artifact),
        })
    if groups:
        checks.append({'status': 'PASS', 'name': 'group_assignments', 'detail': f'{len(groups)} staged group assignments recorded.'})
    else:
        checks.append({'status': 'FAIL', 'name': 'group_assignments', 'detail': 'No staged group assignments recorded.'})
    status = 'INVALID' if any(check['status'] == 'FAIL' for check in checks) else 'AVAILABLE'
    return {
        'status': status,
        'path': str(path),
        'checkpoint': checkpoint,
        'checks': checks,
        'age_seconds': max(0.0, time.time() - path.stat().st_mtime),
    }


def stale_artifacts(workdir, output_dir):
    workdir = Path(workdir).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    candidates = []
    for path in (
        workdir / 'Lights_sorted',
        workdir / 'substacks',
        output_dir / RUN_CHECKPOINT,
        output_dir / RUN_LOCK,
    ):
        if path.exists():
            candidates.append({
                'path': str(path),
                'kind': (
                    'resume state' if path.name == RUN_CHECKPOINT else
                    'active run lock' if path.name == RUN_LOCK else
                    'temporary processing'
                ),
                'safe_to_delete': path.name not in {'Lights_sorted', RUN_LOCK},
                'age_seconds': max(0.0, time.time() - path.stat().st_mtime),
            })
    candidates.extend({
        'path': str(path),
        'kind': 'temporary report artifact',
                'safe_to_delete': True,
        'age_seconds': max(0.0, time.time() - path.stat().st_mtime),
    } for path in output_dir.glob('*.tmp'))
    for log_path in (workdir / 'siril_mosaic_logs').glob('*.log'):
        candidates.append({
            'path': str(log_path),
            'kind': 'run log',
            'safe_to_delete': True,
            'age_seconds': max(0.0, time.time() - log_path.stat().st_mtime),
        })
    return candidates


def delete_stale_artifacts(workdir, output_dir, paths):
    candidates = {
        Path(item['path']).expanduser().resolve(): item
        for item in stale_artifacts(workdir, output_dir)
    }
    deleted = []
    for value in paths:
        candidate = Path(value).expanduser().resolve()
        item = candidates.get(candidate)
        if item is None or not item.get('safe_to_delete'):
            raise ValueError(f'Cleanup path is not an approved stale artifact: {candidate}')
        if candidate.is_dir():
            remove_tree(candidate)
        elif candidate.exists():
            candidate.unlink()
        deleted.append(str(candidate))
    return deleted


def runtime_settings():
    return {
        'test_frame_count': test_frame_count,
        'coverage_map': coverage_map_enabled,
        'auto_crop_master': auto_crop_enabled,
        'export_per_cohort': export_per_cohort,
        'cohort_group_fields': list(cohort_group_fields),
        'auto_crop_coverage_percent': auto_crop_coverage_percent,
        'substacks': SubStack_nb,
        'auto_substacks': auto_substacks,
        'max_frames_per_substack': siril_max_stack_frames,
        'siril_version': siril_version_text,
        'drizzle': drizzle_enabled,
        'drizzle_scale': drizzle_scale,
        'pixel_fraction': pix_frac,
        'drizzle_kernel': drizzle_kernel,
        'bayer_pattern': bayer_pattern,
        'bayer_orientation': bayer_orientation,
        'cosmetic_correction': cosmetic_correction,
        'cosmetic_cold_sigma': cosmetic_cold_sigma,
        'cosmetic_hot_sigma': cosmetic_hot_sigma,
        'overlap_normalization': overlap_normalization,
        'stack_normalization': stack_normalization,
        'plate_solve_order': plate_solve_order,
        'plate_solve_downscale': plate_solve_downscale,
        'plate_solve_radius': plate_solve_radius,
        'plate_solve_limit_mag': plate_solve_limit_mag,
        'rbf_smoothing': rbf_smoothing,
        'background_dither': background_dither,
        'registration_transform': registration_transform,
        'registration_minpairs': registration_minpairs,
        'registration_maxstars': registration_maxstars,
        'registration_interpolation': registration_interpolation,
        'filter_background': filter_bkg,
        'filter_stars': filter_nbstars,
        'filter_roundness': filter_round,
        'filter_fwhm': filter_fwhm,
        'adaptive_quality_filtering': adaptive_quality_filtering,
        'quality_filter_sigma': quality_filter_sigma,
        'background_method': background_method,
        'background_samples': background_samples,
        'background_tolerance': background_tolerance,
        'stacking_weight': stacking_weight,
        'feather': feather_val,
        'rejection_low': rej_low,
        'rejection_high': rej_high,
        'pixel_rejection_method': pixel_rejection_method,
        'fast_normalization': fast_normalization,
        'skip_failed_frames': skip_failed_frames,
        'mosaic_aware_star_count': mosaic_aware_star_count,
        'sky_quality_percent': sky_quality_percent,
        'catalog': catalog,
        'move_frame_selection_rejects': True,
        'rejects_directory': str(workdir / REJECTS_DIRECTORY),
        'memory_fraction': memory_fraction,
        'cpu_count': cpu_count,
        'siril_open_timeout': siril_open_timeout,
        'siril_command_timeout': siril_command_timeout,
        'minimum_free_disk_gb': minimum_free_disk_gb,
        'max_retries': max_retries,
        'seed': run_seed,
    }


def apply_runtime_settings(settings):
    setting_globals = {
        'test_frame_count': 'test_frame_count', 'coverage_map': 'coverage_map_enabled',
        'auto_crop_master': 'auto_crop_enabled', 'export_per_cohort': 'export_per_cohort',
        'auto_crop_coverage_percent': 'auto_crop_coverage_percent', 'substacks': 'SubStack_nb',
        'auto_substacks': 'auto_substacks', 'drizzle': 'drizzle_enabled',
        'drizzle_scale': 'drizzle_scale', 'pixel_fraction': 'pix_frac',
        'drizzle_kernel': 'drizzle_kernel', 'bayer_pattern': 'bayer_pattern',
        'bayer_orientation': 'bayer_orientation', 'cosmetic_correction': 'cosmetic_correction',
        'cosmetic_cold_sigma': 'cosmetic_cold_sigma', 'cosmetic_hot_sigma': 'cosmetic_hot_sigma',
        'overlap_normalization': 'overlap_normalization', 'stack_normalization': 'stack_normalization',
        'plate_solve_order': 'plate_solve_order', 'plate_solve_downscale': 'plate_solve_downscale',
        'plate_solve_radius': 'plate_solve_radius', 'plate_solve_limit_mag': 'plate_solve_limit_mag',
        'rbf_smoothing': 'rbf_smoothing', 'background_dither': 'background_dither',
        'registration_transform': 'registration_transform', 'registration_minpairs': 'registration_minpairs',
        'registration_maxstars': 'registration_maxstars', 'registration_interpolation': 'registration_interpolation',
        'filter_background': 'filter_bkg', 'filter_stars': 'filter_nbstars',
        'filter_roundness': 'filter_round', 'filter_fwhm': 'filter_fwhm',
        'adaptive_quality_filtering': 'adaptive_quality_filtering', 'quality_filter_sigma': 'quality_filter_sigma',
        'background_method': 'background_method', 'background_samples': 'background_samples',
        'background_tolerance': 'background_tolerance', 'stacking_weight': 'stacking_weight',
        'feather': 'feather_val', 'rejection_low': 'rej_low', 'rejection_high': 'rej_high',
        'pixel_rejection_method': 'pixel_rejection_method', 'fast_normalization': 'fast_normalization',
        'skip_failed_frames': 'skip_failed_frames', 'mosaic_aware_star_count': 'mosaic_aware_star_count',
        'sky_quality_percent': 'sky_quality_percent', 'catalog': 'catalog',
        'memory_fraction': 'memory_fraction', 'cpu_count': 'cpu_count',
        'siril_open_timeout': 'siril_open_timeout', 'siril_command_timeout': 'siril_command_timeout',
        'minimum_free_disk_gb': 'minimum_free_disk_gb', 'max_retries': 'max_retries',
    }
    for setting_name, global_name in setting_globals.items():
        if setting_name in settings:
            globals()[global_name] = settings[setting_name]
    if 'cohort_group_fields' in settings:
        globals()['cohort_group_fields'] = tuple(settings['cohort_group_fields'])
    if 'seed' in settings:
        globals()['run_seed'] = settings['seed']


def configuration_hash(settings=None):
    settings_payload = dict(settings or runtime_settings())
    settings_payload.pop('siril_version', None)
    payload = json.dumps(settings_payload, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _resolve_workdir_relative_path(relative_path):
    relative = Path(str(relative_path))
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError(f'Unsafe workdir-relative input path: {relative_path}')
    root = workdir.expanduser().resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f'Input path escapes workdir: {relative_path}') from error
    return candidate


def write_input_manifest(input_files, cohort_batches):
    manifest = {
        'run_id': run_id,
        'seed': run_seed,
        'configuration_hash': configuration_hash(),
        'selected_files': [],
        'cohorts': [],
    }
    for path in input_files:
        stat = path.stat()
        manifest['selected_files'].append({
            'path': path.relative_to(workdir).as_posix(),
            'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
            'sha256': _sha256_file(path),
        })
    for number, (cohort_id, files) in enumerate(cohort_batches, 1):
        manifest['cohorts'].append({
            'number': number,
            'id': cohort_id,
            'files': [path.relative_to(workdir).as_posix() for path in files],
        })
    destination = output_dir / f'input_manifest_{run_id}.json'
    temporary = destination.with_suffix('.tmp')
    _atomic_write_text(destination, json.dumps(manifest, indent=2))
    return destination


def validate_resume_input_identity(checkpoint):
    manifest_value = checkpoint.get('input_manifest')
    if not manifest_value:
        raise RuntimeError('Resume checkpoint does not record an input manifest.')
    manifest_path = Path(str(manifest_value)).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = output_dir / manifest_path
    if not manifest_path.is_file():
        raise RuntimeError(f'Resume input manifest is missing: {manifest_path}')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f'Could not read resume input manifest: {manifest_path} ({error})') from error
    entries = manifest.get('selected_files')
    selected_files = checkpoint.get('selected_files')
    if not isinstance(entries, list) or not isinstance(selected_files, list):
        raise RuntimeError('Resume input manifest or checkpoint has no selected-file list.')
    manifest_paths = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise RuntimeError('Resume input manifest contains an invalid selected-file record.')
        manifest_paths.append(entry['path'])
    if manifest_paths != selected_files:
        raise RuntimeError('Resume selected-file order does not match the input manifest.')
    if checkpoint.get('input_frames') is not None and checkpoint['input_frames'] != len(entries):
        raise RuntimeError('Resume checkpoint frame count does not match the input manifest.')
    if len(manifest_paths) != len(set(manifest_paths)):
        raise RuntimeError('Resume input manifest contains duplicate paths.')
    if checkpoint.get('cohorts'):
        cohort_paths = []
        for cohort in checkpoint['cohorts']:
            files = cohort.get('files', [])
            if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
                raise RuntimeError('Resume checkpoint contains invalid cohort file assignments.')
            cohort_paths.extend(files)
        if sorted(cohort_paths) != sorted(manifest_paths):
            raise RuntimeError('Resume cohort assignments do not match the input manifest.')
    staged_locations = {}
    lights_sorted = workdir / 'Lights_sorted'
    if lights_sorted.is_dir():
        for staged_manifest_path in lights_sorted.glob('group_*/source_manifest.json'):
            try:
                staged_manifest = json.loads(staged_manifest_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f'Could not read staged resume manifest: {staged_manifest_path} ({error})'
                ) from error
            lights_folder = staged_manifest_path.parent / 'lights'
            for staging_name, relative_name in staged_manifest.items():
                if not isinstance(staging_name, str) or not isinstance(relative_name, str):
                    raise RuntimeError(f'Invalid staged resume manifest entry: {staged_manifest_path}')
                staged_locations.setdefault(relative_name, []).append(lights_folder / staging_name)
    paths = []
    metadata_only = []
    staged_files = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get('path'):
            raise RuntimeError('Resume input manifest contains an invalid selected-file record.')
        logical_path = _resolve_workdir_relative_path(entry['path'])
        candidates = [logical_path, workdir / REJECTS_DIRECTORY / Path(entry['path'])]
        candidates.extend(staged_locations.get(entry['path'], []))
        candidates = [candidate for candidate in candidates if candidate.is_file()]
        if len(candidates) > 1:
            raise RuntimeError(f'Resume input has multiple live locations: {entry["path"]}')
        if not candidates:
            raise RuntimeError(
                f'Resume input file is missing from source, rejects, and staging: {entry["path"]}'
            )
        path = candidates[0]
        stat = path.stat()
        if stat.st_size != entry.get('size') or stat.st_mtime_ns != entry.get('mtime_ns'):
            raise RuntimeError(f'Resume input file changed since manifest creation: {entry["path"]}')
        expected_hash = entry.get('sha256')
        if expected_hash:
            actual_hash = _sha256_file(path)
            if actual_hash != expected_hash:
                raise RuntimeError(f'Resume input file contents changed: {entry["path"]}')
        else:
            metadata_only.append(entry['path'])
        paths.append(logical_path)
        if path != logical_path:
            staged_files.append(entry['path'])
    return paths, manifest_path, {
        'mode': 'metadata_and_sha256' if not metadata_only else 'metadata_only',
        'metadata_only_files': metadata_only,
        'staged_files': staged_files,
    }


def build_selection_filters(adaptive, sigma, percentages, mosaic_aware):
    names = ('bkg', 'nbstars', 'round', 'fwhm')
    if mosaic_aware:
        names = tuple(name for name in names if name != 'nbstars')
    if adaptive:
        value = float(sigma)
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Quality filter sigma must be finite and greater than 0.')
        return {name: f'{value}k' for name in names}
    result = {}
    for name in names:
        value = float(str(percentages[name]).removesuffix('%'))
        if not math.isfinite(value) or not value.is_integer() or not 1 <= value <= 100:
            raise ValueError(f'{name} keep percentage must be an integer from 1 to 100.')
        result[name] = f'{int(value)}%'
    return result


def effective_selection_filters():
    return build_selection_filters(
        adaptive_quality_filtering, quality_filter_sigma,
        {'bkg': filter_bkg, 'nbstars': filter_nbstars, 'round': filter_round, 'fwhm': filter_fwhm},
        mosaic_aware_star_count,
    )


def parse_registration_quality(lines, input_frames):
    summary = {
        "input_frames": input_frames,
        "quality_filters": {},
    }
    criteria = {
        "fwhm": "FWHM",
        "roundness": "roundness",
        "background": "background",
        "stars": "number of stars",
    }
    text = "\n".join(lines)
    for name, label in criteria.items():
        match = re.search(
            rf"{re.escape(label)} (?:lower or equal|higher or equal) than "
            rf"([-+0-9.eE]+) \((\d+)\)",
            text,
        )
        if match:
            summary["quality_filters"][name] = {
                "threshold": float(match.group(1)),
                "passing_frames": int(match.group(2)),
            }
    selected = re.search(r"Using selected images filter \((\d+)/(\d+) of the sequence\)", text)
    if selected:
        summary["plate_solved_frames"] = int(selected.group(1))
        summary["sequence_frames"] = int(selected.group(2))
    quality_selected = re.search(r"total of images processed of (\d+)\)", text)
    if quality_selected:
        summary["quality_selected_frames"] = int(quality_selected.group(1))
    exported = re.search(r"Total: (\d+) failed, (\d+) exported\.", text)
    accepted = re.search(r"(?:^|\n).*?\b(\d+) images processed\.", text)
    if exported:
        summary["registration_failed_frames"] = int(exported.group(1))
        summary["accepted_frames"] = int(exported.group(2))
        summary["rejected_frames"] = input_frames - int(exported.group(2))
        return summary
    if not accepted:
        accepted = quality_selected
    if accepted:
        summary["accepted_frames"] = int(accepted.group(1))
        summary["rejected_frames"] = input_frames - int(accepted.group(1))
    return summary


def parse_stack_quality(lines):
    summary: dict = {"pixel_rejection_percent": {}}
    text = "\n".join(lines)
    for channel, low, high in re.findall(
        r"Pixel rejection in channel #(\d+): ([0-9.]+)% - ([0-9.]+)%",
        text,
    ):
        summary["pixel_rejection_percent"][channel] = {
            "low": float(low),
            "high": float(high),
        }
    stacked = re.search(r"(\d+) images have been stacked", text)
    if stacked:
        summary["stacked_frames"] = int(stacked.group(1))
    dimensions = re.search(
        r"Saving FITS:.*?(\d+) layer\(s\), (\d+)x(\d+) pixels, (\d+) bits", text
    )
    if dimensions:
        summary["output"] = {
            "channels": int(dimensions.group(1)),
            "width": int(dimensions.group(2)),
            "height": int(dimensions.group(3)),
            "bits_per_channel": int(dimensions.group(4)),
        }
    return summary


def count_sequence_files(folder, sequence_name):
    pattern = re.compile(rf'^{re.escape(sequence_name)}_(\d+)\.fit$', re.IGNORECASE)
    return sum(1 for path in Path(folder).glob(f'{sequence_name}_*.fit') if pattern.match(path.name))


def summarize_substack_stages(process_folder, files, input_sequence, processed_sequence, background_sequence, stack_response):
    stack_summary = parse_stack_quality(stack_response)
    return {
        'discovered': len(files),
        'converted': count_sequence_files(process_folder, 'light'),
        'cosmetic_corrected': count_sequence_files(process_folder, 'cc_light') if cosmetic_correction else None,
        'calibrated': count_sequence_files(process_folder, processed_sequence),
        'background_corrected': count_sequence_files(process_folder, background_sequence),
        'registered': count_sequence_files(process_folder, f'r_{background_sequence}'),
        'stacked': stack_summary.get('stacked_frames'),
    }


def _sequence_paths(process_folder, sequence_name, selected_only=False):
    pattern = re.compile(rf'^{re.escape(sequence_name)}_(\d+)\.fit$', re.IGNORECASE)
    paths = {
        int(match.group(1)): path
        for path in Path(process_folder).glob(f'{sequence_name}_*.fit')
        if (match := pattern.match(path.name))
    }
    if selected_only:
        for suffix in ('_.seq', '.seq'):
            sequence_path = Path(process_folder) / f'{sequence_name}{suffix}'
            if sequence_path.is_file():
                included = {
                    int(fields[1]) for line in sequence_path.read_text(encoding='utf-8').splitlines()
                    if (fields := line.split()) and fields[0] == 'I' and len(fields) >= 3
                    and fields[2] == '1'
                }
                return {number: path for number, path in paths.items() if number in included}
    return paths


def _sequence_registration_metrics(process_folder, sequence_name):
    sequence_paths = (
        Path(process_folder) / f'{sequence_name}_.seq',
        Path(process_folder) / f'{sequence_name}.seq',
    )
    sequence_path = next((path for path in sequence_paths if path.is_file()), None)
    if sequence_path is None:
        return {}
    layers = {}
    try:
        lines = sequence_path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return {}
    for line in lines:
        if len(line) < 3 or line[0] != 'R' or line[1] not in '0123456789*':
            continue
        fields = line.split()
        if len(fields) < 8 or fields[7] != 'H':
            continue
        try:
            values = {
                'fwhm': float(fields[1]),
                'weighted_fwhm': float(fields[2]),
                'roundness': float(fields[3]),
                'background': float(fields[5]),
                'stars': int(fields[6]),
            }
        except (IndexError, ValueError):
            continue
        layer = line[1]
        layers.setdefault(layer, []).append(values)
    preferred_layer = next(
        (layer for layer in ('1', '0', '*') if layer in layers),
        None,
    )
    if preferred_layer is None:
        return {}
    return {
        image_number: values
        for image_number, values in enumerate(layers[preferred_layer], 1)
    }


def _filter_metric_details(image_number, filter_names, registration_report, metric_values):
    metric_names = {
        'fwhm': 'fwhm',
        'roundness': 'roundness',
        'background': 'background',
        'stars': 'stars',
    }
    details = {}
    thresholds = registration_report.get('quality_filters', {})
    values = metric_values.get(image_number, {})
    for filter_name in filter_names:
        metric_name = metric_names.get(filter_name)
        threshold_data = thresholds.get(filter_name, {})
        if metric_name is None or metric_name not in values:
            details[filter_name] = {
                'value': None,
                'threshold': threshold_data.get('threshold'),
                'comparison': 'unavailable',
                'status': 'unavailable',
            }
            continue
        value = values[metric_name]
        threshold = threshold_data.get('threshold')
        lower_is_better = filter_name in {'fwhm', 'background'}
        passed = (
            value <= threshold if lower_is_better else value >= threshold
        ) if threshold is not None else None
        details[filter_name] = {
            'value': value,
            'threshold': threshold,
            'comparison': '<=' if lower_is_better else '>=',
            'status': 'passed' if passed else 'failed' if passed is not None else 'unavailable',
        }
    return details


def summarize_substack_frames(
    files,
    process_folder,
    processed_sequence,
    background_sequence,
    registration_report,
    stack_report,
    plate_solve_response,
):
    ordered_files = sorted(files, key=lambda path: path.name.casefold())
    stage_sequences = {
        'converted': 'light',
        'cosmetic_corrected': 'cc_light',
        'calibrated': processed_sequence,
        'background_corrected': background_sequence,
        'registered': f'r_{background_sequence}',
    }
    stage_paths = {
        stage: _sequence_paths(process_folder, sequence, selected_only=stage == 'registered')
        for stage, sequence in stage_sequences.items()
    }
    stage_paths['discovered'] = dict(enumerate(ordered_files, 1))
    if not cosmetic_correction:
        stage_paths['cosmetic_corrected'] = stage_paths['converted']
    invalid_calibrated = {
        number for number, path in stage_paths['calibrated'].items()
        if read_fits_layer_count(path) != (1 if drizzle_enabled else 3)
    }
    stage_paths['calibrated'] = {
        number: path for number, path in stage_paths['calibrated'].items()
        if number not in invalid_calibrated
    }
    manifest_path = Path(process_folder).parent / SOURCE_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    registration_metrics = _sequence_registration_metrics(process_folder, background_sequence)
    registration_placements = {}
    for sequence_path in (
        process_folder / f'r_{background_sequence}_.seq',
        process_folder / f'r_{background_sequence}.seq',
    ):
        if sequence_path.is_file():
            try:
                registration_placements = {}
                for item in _read_siril_sequence_placements(sequence_path):
                    width, height = _read_fits_dimensions(stage_paths['registered'][item['number']])
                    registration_placements[item['number']] = {
                        'h02': item.get('h02'),
                        'h12': item.get('h12'),
                        'width': width,
                        'height': height,
                        'included': item.get('included'),
                    }
            except (OSError, ValueError, KeyError):
                registration_placements = {}
            break

    plate_solve_failures = {
        match.group(1)
        for line in plate_solve_response
        if (match := re.search(r'Image\s+(\S+)\s+did not solve', line, re.IGNORECASE))
    }
    sequence_frames = registration_report.get('sequence_frames', len(ordered_files))
    failed_filters = [
        name for name, values in registration_report.get('quality_filters', {}).items()
        if values.get('passing_frames') is not None
        and values['passing_frames'] < sequence_frames
    ]
    registered_count = len(stage_paths['registered'])
    stacked_count = stack_report.get('stacked_frames')
    stack_identity_known = stacked_count is not None and stacked_count == registered_count
    discarded_frame_reports = []
    frame_ledger_records = []
    stage_exposure_seconds = {}
    stage_known_exposure_seconds = {}
    stage_missing_exposure_frames = {}
    for stage, paths in stage_paths.items():
        exposures = [read_exposure_seconds(path) for path in paths.values()]
        valid = [value for value in exposures if math.isfinite(value) and value > 0]
        missing = len(exposures) - len(valid)
        stage_missing_exposure_frames[stage] = missing
        stage_known_exposure_seconds[stage] = sum(valid)
        stage_exposure_seconds[stage] = None if missing else sum(valid)
    for image_number, source in enumerate(ordered_files, 1):
        exposure = read_exposure_seconds(source)
        stages = {
            stage: image_number in paths
            for stage, paths in stage_paths.items()
        }
        all_filter_names = list(registration_report.get('quality_filters', {}))
        all_filter_metrics = _filter_metric_details(
            image_number,
            all_filter_names,
            registration_report,
            registration_metrics,
        )
        metric_source = (
            'Siril sequence registration data'
            if registration_metrics else 'unavailable: sequence registration records missing'
        )
        base_record = {
            'image_number': image_number,
            'file': manifest.get(source.name, source.name),
            'exposure_seconds': exposure if math.isfinite(exposure) and exposure > 0 else None,
            'filter_metrics': all_filter_metrics,
            'filter_metric_source': metric_source,
            'registration_placement': registration_placements.get(image_number),
            'stages': stages,
        }
        if stages['registered']:
            if stack_identity_known:
                frame_ledger_records.append({
                    **base_record,
                    'status': 'stacked',
                    'reason': None,
                    'reason_code': None,
                    'stack_membership': 'confirmed',
                })
            else:
                frame_ledger_records.append({
                    **base_record,
                    'status': 'registered',
                    'reason': 'Registered frame stack membership unavailable',
                    'reason_code': 'stack_membership_unknown',
                    'stack_membership': 'unknown',
                })
            continue
        status = 'failed'
        reason = None
        reason_code = None
        if not stages['converted']:
            reason_code, reason = 'conversion_failed', 'conversion failed'
        elif cosmetic_correction and not stages['cosmetic_corrected']:
            reason_code, reason = 'cosmetic_failed', 'cosmetic correction failed'
        elif not stages['calibrated']:
            reason_code, reason = 'calibration_failed', 'calibration or debayering failed'
        elif not stages['background_corrected']:
            reason_code, reason = 'background_failed', 'background extraction failed'
        else:
            process_name = f'{background_sequence}_{image_number:05d}'
            if process_name in plate_solve_failures:
                reason_code, reason = 'plate_solving_failed', 'plate solving failed'
            else:
                status = 'rejected'
                reason_code = 'registration_excluded'
                reason = 'Excluded during registration; individual filter metric unavailable'
        candidate_filters = failed_filters if reason_code == 'registration_excluded' else []
        metric_filters = (
            all_filter_names if reason_code == 'registration_excluded' else []
        )
        filter_metrics = _filter_metric_details(
            image_number,
            metric_filters,
            registration_report,
            registration_metrics,
        )
        if reason_code == 'registration_excluded' and filter_metrics:
            reason = 'Excluded during registration; see filter_metrics for measured values'
        discarded_record = {
            'image_number': image_number,
            'file': manifest.get(source.name, source.name),
            'exposure_seconds': exposure if math.isfinite(exposure) and exposure > 0 else None,
            'status': status,
            'reason': reason,
            'reason_code': reason_code,
            'candidate_filters': candidate_filters,
            'filter_metrics': filter_metrics,
            'filter_metric_source': (
                'Siril sequence registration data'
                if registration_metrics else 'unavailable: sequence registration records missing'
            ),
            'stages': stages,
        }
        discarded_frame_reports.append(discarded_record)
        frame_ledger_records.append({
            **discarded_record,
            'stack_membership': 'not_registered',
            'filter_metrics': all_filter_metrics,
            'filter_metric_source': metric_source,
        })
    stage_exposure_seconds['stacked'] = (
        stage_exposure_seconds['registered'] if stack_identity_known and stacked_count is not None else None
    )
    return {
        'discarded_frames': discarded_frame_reports,
        'discarded_frame_count': len(discarded_frame_reports),
        'frame_ledger_records': frame_ledger_records,
        'stage_exposure_seconds': {
            stage: round(seconds, 3) if seconds is not None else None
            for stage, seconds in stage_exposure_seconds.items()
        },
        'stack_identity_known': stack_identity_known,
        'unresolved_stack_membership_frames': 0 if stack_identity_known else registered_count,
        'stage_missing_exposure_frames': stage_missing_exposure_frames,
        'stage_known_exposure_seconds': stage_known_exposure_seconds,
    }


def calculate_sky_condition_score(substack_reports, include_star_count=True):
    if not substack_reports:
        return None
    weighted_scores = []
    weights = {
        'background': 0.40,
        'stars': 0.20,
        'fwhm': 0.15,
        'roundness': 0.15,
        'plate_solved': 0.10,
    }
    for report in substack_reports:
        sequence_frames = report.get('sequence_frames') or report.get('input_frames', 0)
        if not sequence_frames:
            continue
        quality_filters = report.get('quality_filters', {})
        values = {}
        metric_names = ('background', 'stars', 'fwhm', 'roundness')
        if not include_star_count:
            metric_names = tuple(name for name in metric_names if name != 'stars')
        for name in metric_names:
            passing = quality_filters.get(name, {}).get('passing_frames')
            if passing is not None:
                values[name] = max(0.0, min(1.0, passing / sequence_frames))
        plate_solved = report.get('plate_solved_frames')
        if plate_solved is not None:
            values['plate_solved'] = max(0.0, min(1.0, plate_solved / sequence_frames))
        available_weight = sum(weights[name] for name in values)
        if not available_weight:
            continue
        score = sum(values[name] * weights[name] for name in values) / available_weight * 100
        weighted_scores.append((score, sequence_frames))
    if not weighted_scores:
        return None
    score = sum(value * frames for value, frames in weighted_scores) / sum(
        frames for _, frames in weighted_scores
    )
    if score >= 85:
        classification = 'Excellent'
    elif score >= 70:
        classification = 'Good'
    elif score >= 50:
        classification = 'Fair'
    else:
        classification = 'Poor'
    component_scores = {}
    for name in weights:
        values = []
        for report in substack_reports:
            sequence_frames = report.get('sequence_frames') or report.get('input_frames', 0)
            if not sequence_frames:
                continue
            if name == 'plate_solved':
                passing = report.get('plate_solved_frames')
            else:
                passing = report.get('quality_filters', {}).get(name, {}).get('passing_frames')
            if passing is not None:
                values.append((max(0.0, min(1.0, passing / sequence_frames)), sequence_frames))
        if values:
            component_scores[name] = round(
                sum(value * frames for value, frames in values) / sum(frames for _, frames in values) * 100,
                1,
            )
    return {
        'score': round(score, 1),
        'classification': classification,
        'basis': 'relative quality metrics from this run; not a Bortle classification',
        'star_count_normalized': not include_star_count,
        'components': component_scores,
    }


def initialize_quality_report(input_frames):
    settings = runtime_settings()
    return {
        "report_schema_version": 3,
        "python_version": sys.version.split()[0],
        "selection": {
            "mode": "adaptive_sigma" if adaptive_quality_filtering else "keep_percentage",
            "effective_filters": effective_selection_filters(),
            "combination": "intersection with successfully plate-solved frames",
            "star_count_enabled": not mosaic_aware_star_count,
        },
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_frames": input_frames,
        "settings": settings,
        "configuration_hash": configuration_hash(settings),
        "seed": run_seed,
        "journal_path": str(journal_path) if journal_path else None,
        "frame_ledger_path": str(frame_ledger_file_path()),
        "frame_ledger_records": 0,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "siril_executable": str(siril_exe),
            "siril_version": siril_version_text,
            "siril_max_stack_frames": siril_max_stack_frames,
            "siril_requires": "1.3.6",
        },
        "restored_rejected_files": [],
        "rejected_files": [],
            "substacks": [],
            }


def initialize_cohort_report_collections(report):
    report.setdefault('masters', [])
    report.setdefault('coverages', [])


def upsert_cohort_report_record(report, collection_name, record):
    records = report.setdefault(collection_name, [])
    cohort_id = record.get('cohort')
    for index, existing in enumerate(records):
        if existing.get('cohort') == cohort_id:
            records[index] = record
            return
    records.append(record)


def cohort_report_record(report, collection_name, cohort_id):
    return next(
        (
            record for record in report.get(collection_name, [])
            if record.get('cohort') == cohort_id
        ),
        None,
    )


def reusable_report_artifact(record, key, expected_path, label):
    if not record or not record.get(key):
        return False
    recorded_path = Path(record[key]).expanduser().resolve()
    if recorded_path != Path(expected_path).expanduser().resolve():
        return False
    try:
        current = require_siril_artifact(recorded_path, label)
    except SirilCommandError:
        return False
    recorded = next(
        (
            artifact for artifact in (quality_report or {}).get('artifact_postconditions', [])
            if Path(artifact.get('path', '')).expanduser().resolve() == recorded_path
        ),
        None,
    )
    if recorded and recorded.get('sha256') and recorded['sha256'] != current['sha256']:
        return False
    return True


def read_exposure_seconds(path):
    path = Path(path)
    if path.suffix.lower() == '.xisf':
        metadata = read_cfa_metadata(path)
        try:
            return float(metadata.get('EXPTIME', 0.0))
        except (TypeError, ValueError):
            return 0.0
    try:
        with path.open('rb') as stream:
            while block := stream.read(2880):
                for offset in range(0, len(block), 80):
                    card = block[offset:offset + 80].decode('ascii', errors='replace')
                    name = card[:8].strip().upper()
                    if name == 'END':
                        return 0.0
                    if name == 'EXPTIME' and card[8:10] == '= ':
                        return float(_clean_fits_value(card[10:]))
    except (OSError, ValueError):
        return 0.0
    return 0.0


def summarize_input_frames(files):
    exposures = [read_exposure_seconds(path) for path in files]
    valid = [value for value in exposures if math.isfinite(value) and value > 0]
    return {
        'frames': len(files),
        'bytes': sum(path.stat().st_size for path in files if path.is_file()),
        'total_exposure_seconds': sum(valid),
        'known_exposure_frames': len(valid),
        'missing_exposure_frames': len(exposures) - len(valid),
    }


def estimate_peak_storage_bytes(input_bytes, drizzle):
    return input_bytes * (18 if drizzle else 8)


def check_runtime_disk_headroom():
    required = int(minimum_free_disk_gb * 1024 ** 3)
    if required <= 0:
        return None
    work_root = workdir
    output_root = output_dir
    work_free = shutil.disk_usage(work_root if work_root.exists() else work_root.parent).free
    output_free = shutil.disk_usage(output_root if output_root.exists() else output_root.parent).free
    available = min(work_free, output_free)
    if available < required:
        raise RuntimeError(
            f'Runtime disk headroom is too low: {available} bytes available, '
            f'{required} bytes required by --minimum-free-disk-gb.'
        )
    return available


def check_preflight_storage_headroom(input_bytes):
    estimated_peak = estimate_peak_storage_bytes(input_bytes, drizzle_enabled)
    emergency_reserve = int(minimum_free_disk_gb * 1024 ** 3)
    required = max(estimated_peak, emergency_reserve)
    work_root = workdir if workdir.exists() else workdir.parent
    output_root = output_dir if output_dir.exists() else output_dir.parent
    available = min(shutil.disk_usage(work_root).free, shutil.disk_usage(output_root).free)
    if available < required:
        raise RuntimeError(
            f'Preflight storage estimate requires {required} bytes, but only '
            f'{available} bytes are available. No input frames were staged.'
        )
    return {'required_bytes': required, 'available_bytes': available}


def build_failure_context(error):
    return {
        'error': str(error),
        'exception_type': type(error).__name__,
        'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
        'phase': active_phase,
        'cohort': active_cohort_id,
        'substack': quality_report.get('active_substack') if quality_report else None,
        'last_siril_command': quality_report.get('last_failed_command') if quality_report else None,
        'siril_response_tail': quality_report.get('siril_response_tail', []) if quality_report else [],
        'recovery_action': 'Inspect Checkpoint Status, then use Resume Run if all checks pass.',
    }


def _read_jsonl_records(path):
    path = Path(path).expanduser()
    records = []
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f'Invalid JSONL at line {line_number}: {error}') from error
    return records


def read_frame_ledger(path):
    return _read_jsonl_records(path)


def _ledger_coverage_impact(records, selected_files):
    placements = [
        record.get('registration_placement')
        for record in records
        if record.get('file') in selected_files and record.get('registration_placement')
    ]
    all_placements = [record.get('registration_placement') for record in records if record.get('registration_placement')]
    if not placements or not all_placements:
        return {
            'available': False,
            'reason': 'Registered placement data is not present in the frame ledger.',
        }
    if any(
        not item.get('width') or not item.get('height')
        for item in all_placements
    ):
        return {
            'available': False,
            'reason': 'Registered frame dimensions are not present in the frame ledger.',
        }
    substacks = {record.get('substack') for record in records if record.get('registration_placement')}
    if len(substacks) > 1:
        return {
            'available': False,
            'reason': 'A global footprint estimate for multiple substacks requires final-canvas placement data.',
        }

    def footprint(selected):
        if not selected:
            return {'width': 0, 'height': 0, 'area_percent': 0.0}
        min_x = min(float(item['h02']) for item in selected)
        min_y = min(float(item['h12']) for item in selected)
        max_x = max(float(item['h02']) + int(item.get('width') or 0) for item in selected)
        max_y = max(float(item['h12']) + int(item.get('height') or 0) for item in selected)
        width = max(1, _round_to_int(max_x - min_x))
        height = max(1, _round_to_int(max_y - min_y))
        grid_width = min(256, width)
        grid_height = min(256, height)
        mask = np.zeros((grid_height, grid_width), dtype=bool)
        for item in selected:
            left = int(np.clip((_round_to_int(float(item['h02']) - min_x) / width) * grid_width, 0, grid_width - 1))
            top = int(np.clip((_round_to_int(float(item['h12']) - min_y) / height) * grid_height, 0, grid_height - 1))
            right = int(np.clip(((_round_to_int(float(item['h02']) - min_x) + int(item.get('width') or 0)) / width) * grid_width, left + 1, grid_width))
            bottom = int(np.clip(((_round_to_int(float(item['h12']) - min_y) + int(item.get('height') or 0)) / height) * grid_height, top + 1, grid_height))
            mask[top:bottom, left:right] = True
        return {
            'width': width,
            'height': height,
            'area_percent': round(float(mask.mean() * 100), 3),
        }

    current_files = {
        record.get('file') for record in records
        if record.get('status') in {'stacked', 'registered'}
    }
    return {
        'available': True,
        'basis': 'coarse union of registered placement rectangles; not pixel-valid coverage',
        'current': footprint([
            record.get('registration_placement')
            for record in records if record.get('file') in current_files and record.get('registration_placement')
        ]),
        'predicted': footprint(placements),
        'predicted_selected_frames_with_placement': len(placements),
    }


def replay_frame_records(records, percentages=None, ledger_path=None):
    ledger_path = Path(ledger_path).expanduser().resolve() if ledger_path else None
    percentages = percentages or {
        'background': 80,
        'roundness': 80,
        'fwhm': 80,
        'stars': 80,
    }
    metric_directions = {
        'fwhm': 'lower',
        'background': 'lower',
        'roundness': 'higher',
        'stars': 'higher',
    }
    thresholds = {}
    unavailable_metrics = []
    for metric, requested_percent in percentages.items():
        requested_percent = float(requested_percent)
        if not math.isfinite(requested_percent) or not 1 <= requested_percent <= 100:
            raise ValueError(f'{metric} keep percentage must be from 1 to 100.')
        values = []
        for record in records:
            detail = record.get('filter_metrics', {}).get(metric, {})
            value = detail.get('value')
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        if not values:
            unavailable_metrics.append(metric)
            continue
        percentile = requested_percent if metric_directions.get(metric) == 'lower' else 100 - requested_percent
        thresholds[metric] = {
            'keep_percent': requested_percent,
            'comparison': '<=' if metric_directions.get(metric) == 'lower' else '>=',
            'threshold': float(np.percentile(values, percentile)),
            'measured_values': len(values),
        }
    if not thresholds:
        raise ValueError('The ledger contains no measured metrics that can be replayed.')

    status_counts = Counter()
    cohort_counts = {}
    predicted_exposure = 0.0
    predicted_known_exposure_frames = 0
    decisions = []
    for record in records:
        measured = record.get('filter_metrics', {})
        failures = []
        missing = []
        for metric, threshold_data in thresholds.items():
            value = measured.get(metric, {}).get('value')
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            if value is None or not math.isfinite(value):
                missing.append(metric)
                continue
            if threshold_data['comparison'] == '<=' and value > threshold_data['threshold']:
                failures.append(metric)
            elif threshold_data['comparison'] == '>=' and value < threshold_data['threshold']:
                failures.append(metric)
        if missing:
            predicted_status = 'unknown'
        elif failures:
            predicted_status = 'excluded'
        else:
            predicted_status = 'selected'
        status_counts[predicted_status] += 1
        cohort = record.get('cohort') or 'Unassigned'
        cohort_summary = cohort_counts.setdefault(
            cohort, {'total': 0, 'selected': 0, 'excluded': 0, 'unknown': 0, 'exposure_seconds': 0.0}
        )
        cohort_summary['total'] += 1
        cohort_summary[predicted_status] += 1
        exposure = record.get('exposure_seconds')
        try:
            exposure = float(exposure)
        except (TypeError, ValueError):
            exposure = 0.0
        if predicted_status == 'selected' and math.isfinite(exposure) and exposure > 0:
            predicted_exposure += exposure
            predicted_known_exposure_frames += 1
            cohort_summary['exposure_seconds'] += exposure
        decisions.append({
            'file': record.get('file'),
            'cohort': cohort,
            'status': predicted_status,
            'failed_metrics': failures,
            'missing_metrics': missing,
        })
    current_status_counts = dict(Counter(record.get('status', 'unknown') for record in records))
    current_exposure = sum(
        float(record.get('exposure_seconds'))
        for record in records
        if record.get('status') in {'stacked', 'registered'}
        and record.get('exposure_seconds') not in (None, '')
    )
    current_cohort_counts = {}
    for record in records:
        cohort = record.get('cohort') or 'Unassigned'
        summary = current_cohort_counts.setdefault(cohort, {'current_selected': 0, 'total': 0})
        summary['total'] += 1
        if record.get('status') in {'stacked', 'registered'}:
            summary['current_selected'] += 1
    cohort_comparison = {}
    for cohort, summary in cohort_counts.items():
        current_selected = current_cohort_counts.get(cohort, {}).get('current_selected', 0)
        proposed_selected = summary['selected']
        retention = proposed_selected / current_selected if current_selected else None
        cohort_comparison[cohort] = {
            'current_selected': current_selected,
            'proposed_selected': proposed_selected,
            'selected_delta': proposed_selected - current_selected,
            'retention_percent': round(retention * 100, 3) if retention is not None else None,
            'warning': bool(current_selected and proposed_selected / current_selected < 0.75),
        }
    selected_files = {
        decision['file'] for decision in decisions if decision['status'] == 'selected'
    }
    return {
        'ledger_path': str(ledger_path) if ledger_path else '<in-memory ledger>',
        'record_count': len(records),
        'thresholds': thresholds,
        'unavailable_metrics': unavailable_metrics,
        'predicted': {
            'selected_frames': status_counts['selected'],
            'excluded_frames': status_counts['excluded'],
            'unknown_frames': status_counts['unknown'],
            'integrated_exposure_seconds': round(predicted_exposure, 3),
            'integrated_hours': round(predicted_exposure / 3600, 3),
            'known_exposure_frames': predicted_known_exposure_frames,
        },
        'current_status_counts': current_status_counts,
        'comparison': {
            'current_selected_frames': sum(
                current_status_counts.get(status, 0) for status in ('stacked', 'registered')
            ),
            'current_exposure_seconds': round(current_exposure, 3),
            'selected_frame_delta': status_counts['selected'] - sum(
                current_status_counts.get(status, 0) for status in ('stacked', 'registered')
            ),
            'exposure_delta_seconds': round(predicted_exposure - current_exposure, 3),
        },
        'cohorts': cohort_counts,
        'cohort_comparison': cohort_comparison,
        'decisions': decisions,
        'coverage_impact': _ledger_coverage_impact(records, selected_files),
    }


def replay_frame_ledger(ledger_path, percentages=None):
    ledger_path = Path(ledger_path).expanduser().resolve()
    if not ledger_path.is_file():
        raise FileNotFoundError(f'Frame ledger does not exist: {ledger_path}')
    return replay_frame_records(
        read_frame_ledger(ledger_path),
        percentages,
        ledger_path,
    )


def _sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def build_artifact_inventory(report):
    candidates = []
    masters = report.get('masters') or ([report.get('master', {})] if report.get('master') else [])
    coverages = report.get('coverages') or ([report.get('coverage', {})] if report.get('coverage') else [])
    for master in masters:
        candidates.append(('master', master.get('path'), master.get('cohort')))
    for coverage in coverages:
        cohort = coverage.get('cohort')
        candidates.extend((
            ('coverage_map', coverage.get('path'), cohort),
            ('integration_time_map', coverage.get('integration_time_path'), cohort),
            ('cropped_master', coverage.get('cropped_master_path'), cohort),
        ))
    inventory = []
    seen = set()
    for role, value, cohort in candidates:
        if not value:
            continue
        path = Path(value)
        key = str(path.resolve())
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        width, height = _read_fits_dimensions(path)
        entry = {
            'role': role,
            'path': str(path),
            'bytes': path.stat().st_size,
            'width': width,
            'height': height,
            'sha256': _sha256_file(path),
        }
        if cohort:
            entry['cohort'] = cohort
        inventory.append(entry)
    return inventory


def scan_input_integrity(
    root_folder,
    output_dir=None,
    files=None,
    bayer_pattern='auto',
    orientation='auto',
    drizzle=False,
    use_cache=True,
    deep_payload=False,
):
    root_folder = Path(root_folder).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve() if output_dir else None
    excluded = (output_dir,) if output_dir else ()
    files = list(files) if files is not None else input_files_after_reject_restore(root_folder, excluded)
    cache_path = output_dir / 'integrity_scan_cache.json' if output_dir else None
    cache_payload = {
        'scan_version': INTEGRITY_SCAN_VERSION,
        'root_folder': str(root_folder),
        'output_dir': str(output_dir) if output_dir else None,
        'bayer_pattern': bayer_pattern,
        'orientation': orientation,
        'drizzle': bool(drizzle),
        'deep_payload': bool(deep_payload),
        'files': [],
    }
    for path in sorted(files, key=lambda item: item.as_posix().casefold()):
        try:
            stat = path.stat()
            cache_payload['files'].append({
                'path': path.relative_to(root_folder).as_posix(),
                'size': stat.st_size,
                'mtime_ns': stat.st_mtime_ns,
            })
        except OSError:
            cache_payload['files'].append({'path': str(path), 'missing': True})
    cache_key = hashlib.sha256(
        json.dumps(cache_payload, sort_keys=True).encode('utf-8')
    ).hexdigest()
    if use_cache and cache_path is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if cached.get('cache_key') == cache_key:
                result = cached.get('result', {})
                result['cached'] = True
                result['cache_path'] = str(cache_path)
                return result
        except (OSError, json.JSONDecodeError):
            pass
    checks = []
    details = []
    dimensions = Counter()
    exposures_missing = 0
    hash_groups = {}
    unreadable = []
    deep_failures = []
    total_bytes = 0

    for path in sorted(files, key=lambda item: item.as_posix().casefold()):
        detail = {'path': path.relative_to(root_folder).as_posix(), 'suffix': path.suffix.lower()}
        try:
            size = path.stat().st_size
            total_bytes += size
            detail['bytes'] = size
            if size <= 0:
                raise ValueError('file is empty')
            if path.suffix.lower() in {'.fit', '.fits', '.fts'}:
                detail['dimensions'] = list(_read_fits_dimensions(path))
                detail['layers'] = read_fits_layer_count(path)
                if detail['layers'] is None:
                    raise ValueError('FITS layer count is unavailable')
                if deep_payload:
                    detail['payload'] = validate_fits_payload(path)
                dimensions[tuple(detail['dimensions'])] += 1
            elif path.suffix.lower() == '.xisf':
                with path.open('rb') as stream:
                    if stream.read(8) != b'XISF0100':
                        raise ValueError('invalid XISF signature')
                metadata = read_cfa_metadata(path)
                detail['dimensions'] = [
                    int(metadata.get('NAXIS1', 0) or 0),
                    int(metadata.get('NAXIS2', 0) or 0),
                ]
            exposure = read_exposure_seconds(path)
            detail['exposure_seconds'] = exposure if math.isfinite(exposure) and exposure > 0 else None
            if detail['exposure_seconds'] is None:
                exposures_missing += 1
            detail['sha256'] = _sha256_file(path)
            hash_groups.setdefault(detail['sha256'], []).append(detail['path'])
            detail['status'] = 'readable'
        except (OSError, ValueError, struct.error, ET.ParseError) as error:
            detail['status'] = 'unreadable'
            detail['error'] = str(error)
            unreadable.append(detail['path'])
            if deep_payload and path.suffix.lower() in {'.fit', '.fits', '.fts'}:
                deep_failures.append(detail['path'])
        details.append(detail)

    duplicate_groups = [paths for paths in hash_groups.values() if len(paths) > 1]
    checks.append({
        'name': 'input_files',
        'status': 'PASS' if files else 'FAIL',
        'detail': f'{len(files)} supported input frame(s) discovered.',
    })
    checks.append({
        'name': 'readability',
        'status': 'PASS' if not unreadable else 'FAIL',
        'detail': 'All inputs were readable.' if not unreadable else f'{len(unreadable)} unreadable input(s).',
        'files': unreadable,
    })
    checks.append({
        'name': 'deep_payload',
        'status': 'FAIL' if deep_failures else 'PASS',
        'detail': (
            'Deep FITS payload validation was not requested.' if not deep_payload else
            'All FITS payloads contain the bytes declared by their headers.'
            if not deep_failures else
            f'{len(deep_failures)} FITS payload(s) are truncated or invalid.'
        ),
        'files': deep_failures,
    })
    checks.append({
        'name': 'duplicates',
        'status': 'PASS' if not duplicate_groups else 'WARN',
        'detail': 'No exact duplicate files found.' if not duplicate_groups else f'{len(duplicate_groups)} exact duplicate group(s).',
        'groups': duplicate_groups,
    })
    checks.append({
        'name': 'dimensions',
        'status': 'PASS' if len(dimensions) <= 1 else 'WARN',
        'detail': f'{len(dimensions)} known dimension group(s): {dict(dimensions)}.',
    })
    checks.append({
        'name': 'exposure_metadata',
        'status': 'PASS' if exposures_missing == 0 else 'WARN',
        'detail': 'Every input has positive EXPTIME.' if exposures_missing == 0 else f'{exposures_missing} input(s) lack positive EXPTIME.',
    })
    cfa_warnings = debayer_preflight_warnings(files, bayer_pattern, orientation)
    checks.append({
        'name': 'cfa_metadata',
        'status': 'PASS' if not cfa_warnings else 'WARN',
        'detail': 'No CFA metadata conflicts detected.' if not cfa_warnings else ' | '.join(cfa_warnings),
    })
    work_candidate = root_folder
    while not work_candidate.exists() and work_candidate != work_candidate.parent:
        work_candidate = work_candidate.parent
    output_candidate = output_dir or root_folder
    while not output_candidate.exists() and output_candidate != output_candidate.parent:
        output_candidate = output_candidate.parent
    estimate = estimate_peak_storage_bytes(total_bytes, drizzle)
    work_free = shutil.disk_usage(work_candidate).free
    output_free = shutil.disk_usage(output_candidate).free
    available = min(work_free, output_free)
    disk_status = 'PASS' if available >= estimate else 'FAIL'
    checks.append({
        'name': 'disk_space',
        'status': disk_status,
        'detail': f'Estimated peak {estimate} bytes; available minimum {available} bytes.',
        'estimated_peak_bytes': estimate,
        'available_bytes': available,
    })
    counts = {status: sum(item['status'] == status for item in checks) for status in ('PASS', 'WARN', 'FAIL')}
    overall = 'FAIL' if counts['FAIL'] else 'WARN' if counts['WARN'] else 'PASS'
    result = {
        'status': overall,
        'root_folder': str(root_folder),
        'output_dir': str(output_dir) if output_dir else None,
        'input_count': len(files),
        'total_bytes': total_bytes,
        'checks': checks,
        'files': details,
        'counts': counts,
        'cached': False,
        'cache_path': str(cache_path) if cache_path else None,
        'deep_payload': bool(deep_payload),
    }
    if use_cache and cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix('.tmp')
            _atomic_write_text(
                cache_path,
                json.dumps({'cache_key': cache_key, 'result': result}, indent=2),
            )
        except OSError:
            pass
    return result


@overload
def _read_fits_array(path, include_header: Literal[False] = False) -> np.ndarray:
    ...


@overload
def _read_fits_array(path, include_header: Literal[True]) -> tuple[np.ndarray, dict]:
    ...


def _read_fits_array(path, include_header=False):
    bitpix_types = {8: '>u1', 16: '>i2', 32: '>i4', -32: '>f4', -64: '>f8'}
    header = {}
    header_bytes = 0
    with Path(path).open('rb') as stream:
        while True:
            block = stream.read(2880)
            if not block:
                raise ValueError(f'FITS header is incomplete: {path}')
            header_bytes += len(block)
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode('ascii', errors='replace')
                name = card[:8].strip().upper()
                if name == 'END':
                    break
                if card[8:10] == '= ' and name in {
                    'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3',
                    'XORGSUBF', 'YORGSUBF', 'XOFFSET', 'YOFFSET',
                }:
                    try:
                        header[name] = int(_clean_fits_value(card[10:]))
                    except ValueError:
                        pass
            else:
                continue
            break
        if header.get('NAXIS') not in (2, 3):
            raise ValueError(f'Unsupported FITS dimensions in {path}')
        shape = tuple(header[f'NAXIS{axis}'] for axis in range(header['NAXIS'], 0, -1))
        count = math.prod(shape)
        dtype = bitpix_types.get(header.get('BITPIX', 0))
        if dtype is None:
            raise ValueError(f'Unsupported FITS bit depth in {path}')
        array = np.fromfile(stream, dtype=dtype, count=count)
    if array.size != count:
        raise ValueError(f'FITS data is incomplete: {path}')
    array = array.reshape(shape)
    return (array, header) if include_header else array


def _read_fits_dimensions(path):
    dimensions = {}
    with Path(path).open('rb') as stream:
        while True:
            block = stream.read(2880)
            if not block:
                raise ValueError(f'FITS header is incomplete: {path}')
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode('ascii', errors='replace')
                name = card[:8].strip().upper()
                if name == 'END':
                    if 'NAXIS1' not in dimensions or 'NAXIS2' not in dimensions:
                        raise ValueError(f'FITS dimensions are missing: {path}')
                    return dimensions['NAXIS1'], dimensions['NAXIS2']
                if card[8:10] == '= ' and name in {'NAXIS1', 'NAXIS2'}:
                    dimensions[name] = int(_clean_fits_value(card[10:]))


def require_siril_artifact(path, label):
    path = Path(path)
    try:
        size = path.stat().st_size
        width, height = _read_fits_dimensions(path)
    except (OSError, ValueError, struct.error) as error:
        raise SirilCommandError(f'{label} is missing or invalid: {path} ({error})') from error
    if size <= 0 or width < 1 or height < 1:
        raise SirilCommandError(f'{label} is empty or has invalid dimensions: {path}')
    return {
        'path': str(path),
        'bytes': size,
        'width': width,
        'height': height,
        'sha256': _sha256_file(path),
    }


def validate_fits_payload(path):
    path = Path(path)
    header = {}
    header_bytes = 0
    with path.open('rb') as stream:
        while True:
            block = stream.read(2880)
            if len(block) != 2880:
                raise ValueError(f'FITS header is incomplete: {path}')
            header_bytes += len(block)
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode('ascii', errors='replace')
                name = card[:8].strip().upper()
                if name == 'END':
                    naxis = int(header.get('NAXIS', 0))
                    if naxis not in (2, 3):
                        raise ValueError(f'Unsupported FITS axis count in {path}')
                    bitpix = int(header.get('BITPIX', 0))
                    if bitpix == 0:
                        raise ValueError(f'FITS BITPIX is missing in {path}')
                    dimensions = [int(header.get(f'NAXIS{axis}', 0)) for axis in range(1, naxis + 1)]
                    if any(value < 1 for value in dimensions):
                        raise ValueError(f'FITS dimensions are incomplete in {path}')
                    data_bytes = math.prod(dimensions) * (abs(bitpix) // 8)
                    minimum_size = header_bytes + data_bytes
                    actual_size = path.stat().st_size
                    if actual_size < minimum_size:
                        raise ValueError(
                            f'FITS payload is truncated: {actual_size} bytes, '
                            f'{minimum_size} required'
                        )
                    return {
                        'header_bytes': header_bytes,
                        'expected_data_bytes': data_bytes,
                        'actual_bytes': actual_size,
                    }
                if card[8:10] == '= ':
                    try:
                        header[name] = _clean_fits_value(card[10:])
                    except ValueError:
                        continue


def read_fits_preview(path, region=None, max_width=480, max_height=300):
    bitpix_types = {8: '>u1', 16: '>i2', 32: '>i4', -32: '>f4', -64: '>f8'}
    if max_width < 1 or max_height < 1:
        raise ValueError('Preview dimensions must be positive.')
    header = {}
    with Path(path).open('rb') as stream:
        while True:
            block = stream.read(2880)
            if not block:
                raise ValueError(f'FITS header is incomplete: {path}')
            end_found = False
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode('ascii', errors='replace')
                name = card[:8].strip().upper()
                if name == 'END':
                    end_found = True
                    break
                if card[8:10] != '= ' or name not in {
                    'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3', 'BSCALE', 'BZERO'
                }:
                    continue
                value = _clean_fits_value(card[10:])
                try:
                    header[name] = float(value) if name in {'BSCALE', 'BZERO'} else int(value)
                except ValueError:
                    pass
            if end_found:
                break
        axis_count = header.get('NAXIS')
        if axis_count not in (2, 3):
            raise ValueError(f'Unsupported FITS dimensions in {path}')
        width = int(header['NAXIS1'])
        height = int(header['NAXIS2'])
        channels = int(header.get('NAXIS3', 1)) if axis_count == 3 else 1
        dtype = bitpix_types.get(header.get('BITPIX', 0))
        if dtype is None:
            raise ValueError(f'Unsupported FITS bit depth in {path}')
        item_size = np.dtype(dtype).itemsize
        data_offset = stream.tell()
        if region is None:
            x0, y0, region_width, region_height = 0, 0, width, height
        else:
            x0 = max(0, min(width - 1, int(region['x'])))
            y0 = max(0, min(height - 1, int(region['y'])))
            region_width = max(1, min(width - x0, int(region['width'])))
            region_height = max(1, min(height - y0, int(region['height'])))
        stride = max(
            1,
            int(math.ceil(region_width / max_width)),
            int(math.ceil(region_height / max_height)),
        )
        columns = np.arange(x0, x0 + region_width, stride, dtype=np.int64)
        rows = np.arange(y0, y0 + region_height, stride, dtype=np.int64)
        sampled = np.empty((min(channels, 3), len(rows), len(columns)), dtype=np.float32)
        scale = float(header.get('BSCALE', 1.0))
        zero = float(header.get('BZERO', 0.0))
        for channel in range(sampled.shape[0]):
            plane_offset = data_offset + channel * width * height * item_size
            for output_row, row in enumerate(rows):
                stream.seek(plane_offset + int(row) * width * item_size + x0 * item_size)
                values = np.fromfile(stream, dtype=dtype, count=region_width)
                if values.size != region_width:
                    raise ValueError(f'FITS data is incomplete: {path}')
                sampled[channel, output_row] = values[::stride] * scale + zero
    if sampled.shape[0] == 1:
        sampled = np.repeat(sampled, 3, axis=0)
    display = []
    for channel in sampled[:3]:
        finite = channel[np.isfinite(channel)]
        if finite.size == 0:
            display.append(np.zeros(channel.shape, dtype=np.uint8))
            continue
        low, high = np.percentile(finite, (1.0, 99.5))
        if not math.isfinite(low) or not math.isfinite(high) or high <= low:
            low = float(np.min(finite))
            high = float(np.max(finite))
        if high <= low:
            display.append(np.zeros(channel.shape, dtype=np.uint8))
            continue
        normalized = np.clip((channel - low) / (high - low), 0, 1)
        normalized[~np.isfinite(normalized)] = 0
        display.append(np.asarray(normalized * 255, dtype=np.uint8))
    return np.stack(display, axis=-1)


def visual_quality_check(master_path, coverage_path=None, crop_path=None):
    checks = []

    def check(name, status, detail, **fields):
        checks.append({'name': name, 'status': status, 'detail': detail, **fields})

    master_path = Path(master_path).expanduser()
    if not master_path.is_file():
        check('master_artifact', 'FAIL', f'Missing master: {master_path}')
        return {'status': 'FAIL', 'checks': checks}
    try:
        master_preview = read_fits_preview(master_path, max_width=640, max_height=420)
        width, height = _read_fits_dimensions(master_path)
        check('master_artifact', 'PASS', f'{width}x{height}, {master_path.stat().st_size} bytes')
    except (OSError, ValueError, struct.error) as error:
        check('master_artifact', 'FAIL', str(error))
        return {'status': 'FAIL', 'checks': checks}

    finite = bool(np.isfinite(master_preview).all())
    check('preview_finite', 'PASS' if finite else 'FAIL', 'Preview contains only finite pixels.' if finite else 'Preview contains nonfinite pixels.')
    grayscale = master_preview.mean(axis=2)
    dynamic_range = float(np.max(grayscale) - np.min(grayscale)) if grayscale.size else 0.0
    check('dynamic_range', 'PASS' if dynamic_range > 10 else 'WARN', f'Preview dynamic range: {dynamic_range:.2f}.', dynamic_range=dynamic_range)
    edge = np.concatenate((grayscale[0, :], grayscale[-1, :], grayscale[:, 0], grayscale[:, -1]))
    inner = grayscale[grayscale.shape[0] // 5:-grayscale.shape[0] // 5 or None, grayscale.shape[1] // 5:-grayscale.shape[1] // 5 or None]
    edge_mean = float(np.mean(edge)) if edge.size else 0.0
    inner_mean = float(np.mean(inner)) if inner.size else edge_mean
    edge_ratio = edge_mean / inner_mean if inner_mean > 0 else 0.0
    check('edge_blackness', 'WARN' if inner_mean > 5 and edge_ratio < 0.15 else 'PASS', f'Edge/interior brightness ratio: {edge_ratio:.3f}.', edge_ratio=edge_ratio)
    clipped_fraction = float(np.mean((grayscale <= 1) | (grayscale >= 254))) if grayscale.size else 1.0
    check('clipping', 'WARN' if clipped_fraction > 0.35 else 'PASS', f'Preview clipped/black fraction: {clipped_fraction * 100:.1f}%.', clipped_fraction=clipped_fraction)
    horizontal_delta = abs(float(np.mean(grayscale[:, :grayscale.shape[1] // 2])) - float(np.mean(grayscale[:, grayscale.shape[1] // 2:])))
    vertical_delta = abs(float(np.mean(grayscale[:grayscale.shape[0] // 2, :])) - float(np.mean(grayscale[grayscale.shape[0] // 2:, :])))
    gradient_delta = max(horizontal_delta, vertical_delta)
    check('large_scale_gradient', 'WARN' if gradient_delta > 35 else 'PASS', f'Half-frame brightness delta: {gradient_delta:.2f}.', horizontal_delta=horizontal_delta, vertical_delta=vertical_delta)

    if coverage_path:
        coverage_path = Path(coverage_path).expanduser()
        if not coverage_path.is_file():
            check('coverage_artifact', 'WARN', f'Missing coverage map: {coverage_path}')
        else:
            try:
                coverage_preview = read_fits_preview(coverage_path, max_width=640, max_height=420)
                coverage_gray = coverage_preview.mean(axis=2)
                holes = float(np.mean(coverage_gray <= 1)) if coverage_gray.size else 1.0
                check('coverage_preview', 'WARN' if holes > 0.55 else 'PASS', f'Coverage preview black fraction: {holes * 100:.1f}%.', black_fraction=holes)
            except (OSError, ValueError, struct.error) as error:
                check('coverage_preview', 'FAIL', str(error))
    if crop_path:
        crop_path = Path(crop_path).expanduser()
        if not crop_path.is_file():
            check('crop_artifact', 'WARN', f'Missing crop: {crop_path}')
        else:
            try:
                crop_width, crop_height = _read_fits_dimensions(crop_path)
                check('crop_artifact', 'PASS', f'{crop_width}x{crop_height}.', width=crop_width, height=crop_height)
            except (OSError, ValueError, struct.error) as error:
                check('crop_artifact', 'FAIL', str(error))
    counts = {status: sum(item['status'] == status for item in checks) for status in ('PASS', 'WARN', 'FAIL')}
    return {'status': 'FAIL' if counts['FAIL'] else 'WARN' if counts['WARN'] else 'PASS', 'checks': checks, 'counts': counts}


def _read_siril_sequence_placements(path):
    images = []
    registrations = {}
    current_layer = None
    variable_size = False
    with path.open('r', encoding='utf-8') as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if line.startswith('S '):
                match = re.match(r"^S\s+'[^']*'\s+(.+)$", line)
                if not match:
                    raise ValueError(f'Unsupported Siril sequence header: {path}')
                fields = match.group(1).split()
                if len(fields) < 7:
                    raise ValueError(f'Incomplete Siril sequence header: {path}')
                variable_size = bool(int(fields[6]))
            elif line.startswith('I '):
                fields = line.split()
                width = height = None
                if variable_size:
                    width, height = (int(value) for value in fields[3].split(',', 1))
                images.append({
                    'number': int(fields[1]),
                    'included': bool(int(fields[2])),
                    'width': width,
                    'height': height,
                })
            elif len(line) > 2 and line[0] == 'R' and line[2] == ' ':
                layer = line[1]
                if current_layer is None:
                    current_layer = layer
                if layer != current_layer:
                    continue
                fields = line.split()
                try:
                    homography = fields.index('H')
                    registrations.setdefault(layer, []).append({
                        'h02': float(fields[homography + 3]),
                        'h12': float(fields[homography + 6]),
                    })
                except (ValueError, IndexError) as exc:
                    raise ValueError(f'Invalid registration record in {path}') from exc
    shifts = registrations.get(current_layer, [])
    if not images or len(shifts) != len(images):
        raise ValueError(f'Incomplete registration data in {path}')
    return [dict(image, **shift) for image, shift in zip(images, shifts)]


def create_registered_map_sequence(source_path, destination_path, sequence_name):
    source_lines = Path(source_path).read_text(encoding='utf-8').splitlines()
    registration_layers = [
        match.group(1)
        for line in source_lines
        if (match := re.match(r'^R(\d+)\s', line))
    ]
    if not registration_layers:
        raise ValueError(f'No registration records found in {source_path}')
    registration_layer = registration_layers[0]
    output_lines = []
    header_replaced = False
    for line in source_lines:
        if line.startswith('S '):
            line, replacements = re.subn(
                r"^(S\s+)'[^']*'", rf"\1'{sequence_name}'", line, count=1
            )
            if replacements != 1:
                raise ValueError(f'Unsupported Siril sequence header: {source_path}')
            header_replaced = True
        elif line.startswith('L '):
            line = 'L 1'
        registration = re.match(r'^R(\d+)(\s.*)$', line)
        if registration:
            if registration.group(1) != registration_layer:
                continue
            line = f'R0{registration.group(2)}'
        statistics = re.match(r'^M(\d+)(-.*)$', line)
        if statistics:
            if statistics.group(1) != registration_layer:
                continue
            line = f'M0{statistics.group(2)}'
        output_lines.append(line)
    if not header_replaced:
        raise ValueError(f'Missing Siril sequence header: {source_path}')
    _atomic_write_text(destination_path, '\n'.join(output_lines) + '\n')


def register_substack_coverage_maps(process_folder, local_dir, substack_count):
    source_sequence = process_folder / 'pp_light_.seq'
    sequence_name = 'coverage_source_'
    sequence_path = local_dir / f'{sequence_name}.seq'
    source_lines = source_sequence.read_text(encoding='utf-8').splitlines()
    header = next((line for line in source_lines if line.startswith('S ')), None)
    match = re.match(r"^S\s+'[^']*'\s+(.+)$", header or '')
    if not match:
        raise ValueError(f'Unsupported Siril sequence header: {source_sequence}')
    fields = match.group(1).split()
    fixed_length = int(fields[3])
    scales = {}
    for number in range(1, substack_count + 1):
        source = local_dir / f'integration_time_map_substack_{number}.fit'
        destination = local_dir / f'{sequence_name}{number:0{fixed_length}d}.fit'
        coverage = np.asarray(_read_fits_array(source), dtype=np.float32)
        scale = float(np.nanmax(coverage)) if coverage.size else 0.0
        if not math.isfinite(scale) or scale < 0:
            raise ValueError(f'Invalid coverage values in {source}')
        scales[number] = scale
        _write_coverage_fits(
            destination,
            coverage / scale if scale else coverage,
            unit='relative',
        )
    create_registered_map_sequence(source_sequence, sequence_path, sequence_name)
    try:
        execute_siril(f'cd {siril_path(local_dir)}')
        execute_siril(
            'seqapplyreg coverage_source -prefix=registered_ '
            '-framing=max -interp=nearest'
        )
    except Exception:
        try:
            execute_siril(f'cd {siril_path(process_folder)}')
        except Exception:
            pass
        raise
    else:
        execute_siril(f'cd {siril_path(process_folder)}')
    output_sequence = local_dir / f'registered_{sequence_name}.seq'
    records = [
        record for record in _read_siril_sequence_placements(output_sequence)
        if record['included'] and 1 <= record['number'] <= substack_count
    ]
    if len(records) != substack_count:
        raise ValueError(
            f'Expected {substack_count} registered coverage maps, found {len(records)}'
        )
    registered = {
        record['number']: _read_fits_array(
            local_dir
            / f'registered_{sequence_name}{record["number"]:0{fixed_length}d}.fit'
        ) * scales[record['number']]
        for record in records
    }
    return registered, records


def _round_to_int(value):
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _read_fits_header_cards(path):
    cards = []
    with Path(path).open('rb') as stream:
        while True:
            block = stream.read(2880)
            if not block:
                raise ValueError(f'FITS header is incomplete: {path}')
            for offset in range(0, len(block), 80):
                card = block[offset:offset + 80].decode('ascii', errors='replace')
                if card[:8].strip().upper() == 'END':
                    return cards
                cards.append(card)


def _celestial_wcs_cards(path):
    scalar_names = {'WCSAXES', 'RADESYS', 'RADECSYS', 'EQUINOX', 'EPOCH', 'LONPOLE', 'LATPOLE'}
    axis_pattern = re.compile(r'^(CTYPE|CUNIT|CRPIX|CRVAL|CDELT|CROTA)[12]$')
    matrix_pattern = re.compile(r'^(CD|PC)[12]_[12]$')
    distortion_pattern = re.compile(r'^(A|B|AP|BP)_(ORDER|\d+_\d+)$|^(PV|PS)[12]_\d+$')
    return [
        card for card in _read_fits_header_cards(path)
        if (
            card[8:10] == '= '
            and (
                card[:8].strip().upper() in scalar_names
                or axis_pattern.match(card[:8].strip().upper())
                or matrix_pattern.match(card[:8].strip().upper())
                or distortion_pattern.match(card[:8].strip().upper())
            )
        )
    ]


def _write_coverage_fits(path, coverage, unit='frame', extra_cards=None):
    height, width = coverage.shape
    floating = np.issubdtype(coverage.dtype, np.floating)
    cards = [
        'SIMPLE  =                    T',
        f'BITPIX  = {(-32 if floating else 32):20d}',
        'NAXIS   =                    2',
        f'NAXIS1  = {width:20d}',
        f'NAXIS2  = {height:20d}',
        'BZERO   =                    0',
        'BSCALE  =                    1',
        f"BUNIT   = '{unit}'",
        'EXTEND  =                    T',
    ]
    cards.extend(extra_cards or [])
    cards.append('END')
    header = b''.join(card.ljust(80).encode('ascii') for card in cards)
    header = header.ljust((len(header) + 2879) // 2880 * 2880, b' ')
    data = np.asarray(coverage, dtype='>f4' if floating else '>i4').tobytes(order='C')
    data = data.ljust((len(data) + 2879) // 2880 * 2880, b'\0')
    _atomic_write_bytes(path, header + data)


def finalize_coverage_maps(master_path, coverage_report):
    if not coverage_report or coverage_report.get('status') == 'unavailable':
        return coverage_report
    master_path = Path(master_path)
    integration_path = Path(coverage_report['integration_time_path'])
    coverage_path = Path(coverage_report['path'])
    master = _read_fits_array(master_path)
    integration = np.asarray(_read_fits_array(integration_path), dtype=np.float32)
    master_support = np.isfinite(master) & (np.abs(master) > 1e-7)
    if master.ndim == 3:
        master_support = np.any(master_support, axis=0)
    if integration.shape != master_support.shape:
        raise ValueError(
            f'Coverage canvas {integration.shape[1]}x{integration.shape[0]} does not match '
            f'master canvas {master_support.shape[1]}x{master_support.shape[0]}.'
        )
    footprint = _fill_drizzle_footprint(master_support) if drizzle_enabled else master_support
    integration[~footprint] = 0
    covered = np.isfinite(integration) & (integration > 0)
    signal_outside = int(np.count_nonzero(master_support & ~covered))
    coverage_outside = int(np.count_nonzero(covered & ~footprint))
    if signal_outside:
        raise ValueError(
            f'Coverage map misses {signal_outside} nonzero master pixel(s); refusing to finalize.'
        )
    maximum = float(integration.max())
    wcs_cards = _celestial_wcs_cards(master_path)
    _write_coverage_fits(integration_path, integration, unit='s', extra_cards=wcs_cards)
    _write_coverage_fits(
        coverage_path,
        integration / maximum if maximum else integration,
        unit='relative',
        extra_cards=wcs_cards,
    )
    coverage_report.update({
        'normalization_seconds': maximum,
        'maximum_coverage': maximum,
        'maximum_integration_seconds': maximum,
        'master_signal_pixels': int(np.count_nonzero(master_support)),
        'coverage_footprint_pixels': int(np.count_nonzero(covered)),
        'master_signal_outside_coverage_pixels': signal_outside,
        'coverage_outside_master_footprint_pixels': coverage_outside,
        'footprint_model': 'filled drizzle detector footprint' if drizzle_enabled else 'finite nonzero master pixels',
        'wcs_status': 'copied' if wcs_cards else 'unavailable',
        'wcs_source': str(master_path),
    })
    if quality_report is not None:
        postconditions = quality_report.setdefault('artifact_postconditions', [])
        for path, label in (
            (integration_path, 'Integration-time map artifact'),
            (coverage_path, 'Coverage map artifact'),
        ):
            artifact = require_siril_artifact(path, label)
            postconditions[:] = [
                item for item in postconditions
                if Path(item.get('path', '')).expanduser().resolve() != path.resolve()
            ]
            postconditions.append(artifact)
    return coverage_report


def _compose_coverage_arrays(local_maps, placements):
    if not local_maps or not placements:
        raise ValueError('No local coverage maps or final placements were provided.')
    for record in placements:
        if record['width'] is None or record['height'] is None:
            height, width = np.asarray(local_maps[record['number']]).shape[-2:]
            record['width'] = width
            record['height'] = height
    min_x = min(record['h02'] for record in placements)
    min_y = min(record['h12'] for record in placements)
    max_x = max(record['h02'] + record['width'] for record in placements)
    max_y = max(record['h12'] + record['height'] for record in placements)
    canvas_width = math.ceil(max_x) - math.floor(min_x) + 1
    canvas_height = math.ceil(max_y) - math.floor(min_y) + 1
    composed = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    for record in placements:
        local = np.asarray(local_maps[record['number']], dtype=np.float32)
        if local.shape != (record['height'], record['width']):
            raise ValueError(
                f"Local coverage dimensions {local.shape[1]}x{local.shape[0]} do not match "
                f"substack {record['number']} placement {record['width']}x{record['height']}"
            )
        local_internal = np.flipud(local)
        x0 = _round_to_int(record['h02'] - int(min_x))
        shift_y = _round_to_int(int(min_y) - record['h12'])
        y0 = canvas_height - record['height'] + shift_y
        composed[y0:y0 + record['height'], x0:x0 + record['width']] += local_internal
    return np.flipud(composed)


def coverage_crop_bounds(coverage, percent):
    if coverage.ndim != 2 or not math.isfinite(percent) or not 0 < percent <= 100:
        raise ValueError('Coverage must be two-dimensional and crop percentage must be in (0, 100].')
    positive = coverage[np.isfinite(coverage) & (coverage > 0)]
    if not positive.size:
        return None
    reference = float(np.median(positive))
    threshold = reference * percent / 100
    if np.issubdtype(coverage.dtype, np.integer):
        threshold = max(1, math.ceil(threshold))
    del positive
    heights = np.zeros(coverage.shape[1], dtype=np.int32)
    best_area = 0
    best_rectangle = None
    for row_index, row in enumerate(coverage):
        heights = np.where(np.isfinite(row) & (row >= threshold), heights + 1, 0)
        stack = []
        for column, height in enumerate(heights.tolist() + [0]):
            start = column
            while stack and stack[-1][1] > height:
                left, previous_height = stack.pop()
                width = column - left
                area = width * previous_height
                if area > best_area:
                    best_area = area
                    best_rectangle = (left, row_index - previous_height + 1, width, previous_height)
                start = left
            if height and (not stack or stack[-1][1] < height):
                stack.append((start, height))
    if best_rectangle is None:
        return None
    left, top, width, height = best_rectangle
    return {
        'threshold': threshold,
        'reference_coverage': reference,
        'x': left,
        'y': top,
        'width': width,
        'height': height,
    }


def build_crop_plan(master_path, integration_time_path, percent):
    master_path = Path(master_path)
    integration_time_path = Path(integration_time_path)
    if not master_path.is_file():
        raise FileNotFoundError(f'Master file does not exist: {master_path}')
    if not integration_time_path.is_file():
        raise FileNotFoundError(
            f'Integration-time map does not exist: {integration_time_path}'
        )
    coverage = _read_fits_array(integration_time_path)
    if coverage.ndim != 2:
        raise ValueError('Integration-time map must be a two-dimensional FITS image.')
    master_width, master_height = _read_fits_dimensions(master_path)
    if coverage.shape != (master_height, master_width):
        raise ValueError(
            f'Coverage canvas {coverage.shape[1]}x{coverage.shape[0]} does not match '
            f'master canvas {master_width}x{master_height}.'
        )
    bounds = coverage_crop_bounds(coverage, float(percent))
    if not bounds:
        raise ValueError('No contiguous crop rectangle meets the requested coverage depth.')
    bounds = dict(bounds)
    threshold = bounds.pop('threshold')
    reference_coverage = bounds.pop('reference_coverage')
    selection = dict(bounds, y=master_height - bounds['y'] - bounds['height'])
    return {
        'master_path': str(master_path),
        'integration_time_path': str(integration_time_path),
        'crop_coverage_percent': float(percent),
        'crop_threshold': threshold,
        'crop_reference_coverage': reference_coverage,
        'crop_bounds': bounds,
        'crop_siril_selection': selection,
        'master_width': master_width,
        'master_height': master_height,
        'cropped_width': bounds['width'],
        'cropped_height': bounds['height'],
        'area_percent': round(
            100 * bounds['width'] * bounds['height'] / (master_width * master_height),
            3,
        ),
    }


def crop_master_with_siril(
    master_path,
    integration_time_path,
    percent,
    output_path,
    executable,
):
    master_path = Path(master_path)
    integration_time_path = Path(integration_time_path)
    output_path = Path(output_path)
    executable = Path(executable)
    if not executable.is_file():
        raise FileNotFoundError(f'Siril executable does not exist: {executable}')
    if output_path.suffix.lower() != '.fit':
        raise ValueError('Workbench output must use the .fit extension.')
    if output_path.resolve() in {master_path.resolve(), integration_time_path.resolve()}:
        raise ValueError('Workbench output must be different from the master and coverage map.')
    if output_path.exists():
        raise FileExistsError(f'Workbench output already exists: {output_path}')
    plan = build_crop_plan(master_path, integration_time_path, percent)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from pysiril.siril import Siril

    session = Siril(siril_exe=str(executable), bStable=False, requires='1.3.6')
    try:
        if session.Open() is False:
            raise RuntimeError('Siril failed to open for the cropping workbench.')
        commands = (
            f'cd {siril_path(output_path.parent)}',
            f'load {siril_path(master_path.with_suffix(""))}',
            (
                f'boxselect {plan["crop_siril_selection"]["x"]} '
                f'{plan["crop_siril_selection"]["y"]} '
                f'{plan["crop_siril_selection"]["width"]} '
                f'{plan["crop_siril_selection"]["height"]}'
            ),
            'crop',
            f'save {siril_path(output_path.with_suffix(""))}',
        )
        for command in commands:
            succeeded = session.Execute(command) is not False
            if not succeeded:
                get_data = getattr(session, 'GetData', None)
                raw_response = get_data() if callable(get_data) else []
                response = raw_response if isinstance(raw_response, (list, tuple)) else []
                details = '\n'.join(str(line) for line in response)
                raise RuntimeError(f'Siril command failed: {command}\n{details}')
    finally:
        try:
            session.Close()
        except Exception:
            pass
    if not output_path.is_file():
        raise RuntimeError(f'Siril did not create the cropped master: {output_path}')
    plan['output_path'] = str(output_path)
    return plan


def generate_coverage_map(
    process_folder,
    sequence_name,
    destination_dir=None,
    artifact_stem=None,
    update_report=True,
):
    if not coverage_map_enabled:
        return None
    pattern = re.compile(rf'^{re.escape(sequence_name)}_(\d+)\.fit$', re.IGNORECASE)
    registered = []
    for path in process_folder.glob(f'{sequence_name}_*.fit'):
        match = pattern.match(path.name)
        if match:
            registered.append((int(match.group(1)), path))
    registered.sort()
    if not registered:
        raise RuntimeError(f'No registered frames found for coverage map: {sequence_name}')
    sequence_path = process_folder / f'{sequence_name}_.seq'
    if not sequence_path.is_file():
        sequence_path = process_folder / f'{sequence_name}.seq'
    try:
        sequence_records = _read_siril_sequence_placements(sequence_path)
        registered_paths = dict(registered)
        placements = [
            (registered_paths[record['number']], record)
            for record in sequence_records if record['included']
        ]
        if not placements:
            raise ValueError('No selected registered frames')
    except (OSError, ValueError, KeyError) as exc:
        message = (
            f"Coverage map unavailable: could not read registered placement data from "
            f"{sequence_path.name} ({exc}); continuing without coverage output."
        )
        if quality_report is not None and update_report:
            quality_report['coverage'] = {
                'status': 'unavailable',
                'reason': message,
                'frames_seen': len(registered),
            }
            write_quality_report('running')
        print(f"[WARNING] {message}", flush=True)
        return None
    if any(record['width'] is None for _, record in placements):
        first_array = _read_fits_array(placements[0][0])
        fixed_height, fixed_width = first_array.shape[-2:]
        for _, record in placements:
            record['width'] = fixed_width
            record['height'] = fixed_height
    min_x = min(record['h02'] for _, record in placements)
    min_y = min(record['h12'] for _, record in placements)
    max_x = max(record['h02'] + record['width'] for _, record in placements)
    max_y = max(record['h12'] + record['height'] for _, record in placements)
    canvas_width = int(max_x) - int(min_x) + 1
    canvas_height = int(max_y) - int(min_y) + 1
    exposures = [read_exposure_seconds(path) for path, _ in placements]
    missing_exposures = sum(not math.isfinite(value) or value <= 0 for value in exposures)
    if missing_exposures:
        report = {
            'status': 'unavailable',
            'reason': f'Integration-time coverage requires positive EXPTIME in every registered frame; {missing_exposures} missing or invalid.',
            'frames_seen': len(placements),
        }
        if quality_report is not None and update_report:
            quality_report['coverage'] = report
        print(f"[WARNING] {report['reason']} Skipping coverage and autocrop.", flush=True)
        return None
    coverage = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    for (path, record), exposure in zip(placements, exposures):
        array = _read_fits_array(path)
        valid = np.isfinite(array) & (np.abs(array) > 1e-7)
        if array.ndim == 3:
            valid = np.any(valid, axis=0)
        if drizzle_enabled:
            valid = _fill_drizzle_footprint(valid)
        height, width = valid.shape
        if (height, width) != (record['height'], record['width']):
            raise ValueError(
                f'Sequence dimensions do not match registered frame {path.name}'
            )
        x0 = _round_to_int(record['h02'] - int(min_x))
        shift_y = _round_to_int(int(min_y) - record['h12'])
        y0 = canvas_height - height + shift_y
        coverage[y0:y0 + valid.shape[0], x0:x0 + valid.shape[1]] += valid * np.float32(exposure)
    coverage = np.flipud(coverage)
    destination_dir = Path(destination_dir or output_dir)
    if artifact_stem is None:
        cohort_suffix = f'_{active_cohort_tag}' if active_cohort_tag else ''
        artifact_stem = f'{run_id}{cohort_suffix}'
    coverage_path = destination_dir / f'coverage_map_{artifact_stem}.fit'
    integration_path = destination_dir / f'integration_time_map_{artifact_stem}.fit'
    destination_dir.mkdir(parents=True, exist_ok=True)
    maximum = float(coverage.max())
    _write_coverage_fits(integration_path, coverage, unit='s')
    _write_coverage_fits(coverage_path, coverage / maximum if maximum else coverage, unit='relative')
    report = {
        'path': str(coverage_path),
        'integration_time_path': str(integration_path),
        'map_unit': 'relative (0 to 1)',
        'crop_unit': 's',
        'normalization_seconds': maximum,
        'frames_counted': len(placements),
        'maximum_coverage': maximum,
        'maximum_coverage_unit': 's',
        'maximum_integration_seconds': maximum,
        'exposure_range_seconds': [min(exposures), max(exposures)],
        'integration_basis': (
            'sum of registered EXPTIME over transformed detector footprints; '
            'interior drizzle sampling holes filled'
            if drizzle_enabled else
            'sum of registered EXPTIME over finite nonzero pixels before per-pixel rejection and stacking weights'
        ),
        'width': int(coverage.shape[1]),
        'height': int(coverage.shape[0]),
        'canvas_source': 'Siril maximize framing',
    }
    if active_cohort_id is not None:
        report['cohort'] = active_cohort_id
    if auto_crop_enabled:
        bounds = coverage_crop_bounds(coverage, auto_crop_coverage_percent)
        if bounds:
            report['crop_threshold'] = bounds.pop('threshold')
            report['crop_reference_coverage'] = bounds.pop('reference_coverage')
            report['crop_method'] = 'largest rectangle above percentage of median positive integration time'
            report['crop_coverage_percent'] = auto_crop_coverage_percent
            report['crop_bounds'] = bounds
            report['crop_coordinate_system'] = 'FITS array rows'
    if quality_report is not None and update_report:
        quality_report['coverage'] = report
    print(
        f"[INFO] Coverage map: {len(placements)} registered frames, "
        f"peak integration {maximum:g}s; normalized view and seconds map saved",
        flush=True,
    )
    return report


def _fill_drizzle_footprint(valid):
    valid = np.asarray(valid, dtype=bool)
    populated_rows = np.flatnonzero(np.any(valid, axis=1))
    if not populated_rows.size:
        return valid
    left_edges = np.argmax(valid[populated_rows], axis=1)
    right_edges = valid.shape[1] - 1 - np.argmax(valid[populated_rows, ::-1], axis=1)
    rows = np.arange(populated_rows[0], populated_rows[-1] + 1)
    left = np.rint(np.interp(rows, populated_rows, left_edges)).astype(int)
    right = np.rint(np.interp(rows, populated_rows, right_edges)).astype(int)
    footprint = np.zeros_like(valid)
    for row, left_edge, right_edge in zip(rows, left, right):
        footprint[row, left_edge:right_edge + 1] = True
    return footprint


def compose_substack_coverage_maps(process_folder, local_dir, substack_count):
    try:
        registered_maps, records = register_substack_coverage_maps(
            process_folder, Path(local_dir), substack_count
        )
        local_reports = quality_report.get('substack_coverages', {}) if quality_report else {}
        coverage = _compose_coverage_arrays(registered_maps, records)
    except (OSError, ValueError, KeyError) as exc:
        report = {
            'status': 'unavailable',
            'reason': f'Could not compose substack coverage maps: {exc}',
            'frames_seen': 0,
        }
        if quality_report is not None:
            quality_report['coverage'] = report
            write_quality_report('running')
        print(f"[WARNING] {report['reason']}", flush=True)
        return None

    destination_dir = Path(output_dir)
    cohort_suffix = f'_{active_cohort_tag}' if active_cohort_tag else ''
    coverage_path = destination_dir / f'coverage_map_{run_id}{cohort_suffix}.fit'
    integration_path = destination_dir / f'integration_time_map_{run_id}{cohort_suffix}.fit'
    destination_dir.mkdir(parents=True, exist_ok=True)
    maximum = float(coverage.max())
    _write_coverage_fits(integration_path, coverage, unit='s')
    _write_coverage_fits(coverage_path, coverage / maximum if maximum else coverage, unit='relative')
    local_report_values = list(local_reports.values())
    frame_counts = [report.get('frames_counted', 0) for report in local_report_values]
    exposure_ranges = [report.get('exposure_range_seconds') for report in local_report_values]
    valid_ranges = [item for item in exposure_ranges if item]
    report = {
        'path': str(coverage_path),
        'integration_time_path': str(integration_path),
        'map_unit': 'relative (0 to 1)',
        'crop_unit': 's',
        'normalization_seconds': maximum,
        'frames_counted': sum(frame_counts),
        'maximum_coverage': maximum,
        'maximum_coverage_unit': 's',
        'maximum_integration_seconds': maximum,
        'exposure_range_seconds': [
            min(item[0] for item in valid_ranges),
            max(item[1] for item in valid_ranges),
        ] if valid_ranges else [],
        'integration_basis': 'sum of per-substack integration-time maps placed on the final master registration canvas',
        'width': int(coverage.shape[1]),
        'height': int(coverage.shape[0]),
        'canvas_source': 'substack coverage maps transformed by final Siril registration',
        'substack_count': substack_count,
    }
    if active_cohort_id is not None:
        report['cohort'] = active_cohort_id
    if quality_report is not None:
        quality_report['coverage'] = report
        write_quality_report('running')
    print(
        f"[INFO] Composed coverage map: {substack_count} substack maps, "
        f"peak integration {maximum:g}s; normalized view and seconds map saved",
        flush=True,
    )
    return report


def finalize_integration_report(input_summary):
    if quality_report is None:
        return
    stacked_frames = sum(
        substack.get('stack', {}).get('stacked_frames', 0)
        for substack in quality_report.get('substacks', [])
    )
    total_exposure = input_summary['total_exposure_seconds']
    stacked_exposure = sum(
        substack.get('stage_exposure_seconds', {}).get('stacked', 0.0) or 0.0
        for substack in quality_report.get('substacks', [])
    )
    substacks = quality_report.get('substacks', [])
    per_cohort = bool(quality_report.get('settings', {}).get('export_per_cohort'))
    exact_stacked_exposure = bool(substacks) and all(
        'stage_exposure_seconds' in substack
        and substack['stage_exposure_seconds'].get('stacked') is not None
        for substack in quality_report.get('substacks', [])
    )
    if quality_report.get('masters') and not per_cohort:
        master_count = sum(
            master.get('stacked_frames', 0) or 0
            for master in quality_report['masters']
        )
    elif not per_cohort:
        master_count = quality_report.get('master', {}).get('stacked_frames')
    else:
        master_count = None
    if not per_cohort and (
        (len(substacks) > 1 and master_count != len(substacks)) or
        (master_count is not None and master_count != len(substacks))
    ):
        exact_stacked_exposure = False
    quality_report['integration'] = {
        'total_input_hours': round(total_exposure / 3600, 3),
        'integrated_exposure_seconds': round(stacked_exposure, 3) if exact_stacked_exposure else None,
        'integrated_hours': round(stacked_exposure / 3600, 3) if exact_stacked_exposure else None,
        'estimated_integrated_hours': round(stacked_exposure / 3600, 3) if exact_stacked_exposure else None,
        'exposure_complete': exact_stacked_exposure,
        'integrated_exposure_basis': (
            'sum of EXPTIME for frames that reached stacking'
            if exact_stacked_exposure else
            'unavailable: missing exposure metadata or unresolved stack membership'
        ),
        'stacked_frames': stacked_frames,
        'known_exposure_frames': input_summary['known_exposure_frames'],
    }
    sky_condition = calculate_sky_condition_score(
        quality_report.get('substacks', []),
        include_star_count=not mosaic_aware_star_count,
    )
    if sky_condition is not None:
        quality_report['sky_condition'] = sky_condition
    hours = quality_report['integration']['integrated_hours']
    display_hours = f'{hours:.2f} hours' if hours is not None else 'unavailable (see report)'
    print(f"[INFO] Integrated data: {display_hours} ({stacked_frames} stacked frames)", flush=True)
    if sky_condition is not None:
        print(
            f"[INFO] Sky condition score: {sky_condition['score']:.1f}/100 "
            f"({sky_condition['classification']})",
            flush=True,
        )


def write_quality_report(status, error=None):
    if quality_report is None:
        return
    quality_report["status"] = status
    quality_report["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if error is not None:
        quality_report["error"] = str(error)
    destination = quality_report_path()
    temporary = destination.with_suffix('.tmp')
    _atomic_write_text(destination, json.dumps(quality_report, indent=2))
    append_journal_event(
        'quality_report_written',
        status=status,
        path=str(destination),
    )


def skip_invalid_sequence_frames(process_folder, sequence_name, expected_layers):
    sequence_files = []
    pattern = re.compile(rf'^{re.escape(sequence_name)}_(\d+)\.fit$', re.IGNORECASE)
    for path in process_folder.glob(f'{sequence_name}_*.fit'):
        match = pattern.match(path.name)
        if match:
            sequence_files.append((int(match.group(1)), path))
    sequence_files.sort()
    invalid = [
        (image_number, path)
        for image_number, path in sequence_files
        if read_fits_layer_count(path) != expected_layers
    ]
    if not invalid or not skip_failed_frames:
        return
    execute_siril(f'select {sequence_name} 1 {sequence_files[-1][0]}')
    for image_number, path in invalid:
        layer_count = read_fits_layer_count(path)
        execute_siril(f'unselect {sequence_name} {image_number} {image_number}')
        skipped = {
            'stage': 'calibration/debayering',
            'sequence': sequence_name,
            'image_number': image_number,
            'file': path.name,
            'reason': f'expected {expected_layers} layer(s), found {layer_count}',
        }
        if quality_report is not None:
            quality_report.setdefault('skipped_frames', []).append(skipped)
        print(f"[WARNING] Skipping failed frame {path.name}: {skipped['reason']}", flush=True)
    if quality_report is not None:
        write_quality_report('running')
    if len(sequence_files) == len(invalid):
        raise RuntimeError(f'All frames failed in sequence {sequence_name}.')


def configure_debayer():
    use_header = bayer_pattern == 'auto'
    execute_siril(f"set debayer.use_bayer_header={'true' if use_header else 'false'}")
    execute_siril(f"set debayer.pattern={BAYER_PATTERNS[bayer_pattern]}")
    execute_siril(f"set debayer.orientation={BAYER_ORIENTATIONS[bayer_orientation]}")


def build_background_command(sequence_name):
    method = BACKGROUND_METHODS[background_method]
    command = (
        f"seqsubsky {sequence_name} {method} -samples={background_samples} "
        f"-tolerance={background_tolerance}"
    )
    if background_method == 'rbf':
        command += f" -smooth={rbf_smoothing}"
    if not background_dither:
        command += " -nodither"
    return command


def build_plate_solve_command(sequence_name, cat):
    command = (
        f"seqplatesolve {sequence_name} -order={plate_solve_order} "
        f"-nocrop -nocache -force -catalog={cat}"
    )
    if plate_solve_downscale:
        command += " -downscale"
    if plate_solve_radius is not None:
        command += f" -radius={plate_solve_radius:g}"
    if plate_solve_limit_mag is not None:
        command += f" -limitmag={plate_solve_limit_mag}"
    return command


def build_registration_command(sequence_name, selection_filters):
    return (
        f"seqapplyreg {sequence_name} "
        + ' '.join(f'-filter-{name}={value}' for name, value in selection_filters.items())
        + f" -framing=max -interp={registration_interpolation}"
    )


def build_master_registration_command():
    command = (
        f"register pp_light -2pass -transf={registration_transform} "
        f"-interp={registration_interpolation}"
    )
    if registration_minpairs:
        command += f" -minpairs={registration_minpairs}"
    if registration_maxstars:
        command += f" -maxstars={registration_maxstars}"
    return command


def substack(group_num, cat=None):
    if cat is None:
        cat = catalog
    group_path = workdir / 'Lights_sorted' / f'group_{group_num}'
    lights_folder = group_path / 'lights'
    process_folder = group_path / 'process'
    files = sorted([
        f for f in lights_folder.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_FRAME_SUFFIXES
    ], key=lambda path: path.name.casefold())
    if not files:
        print(f"[ERROR] No frames found for Substack {group_num}")
        return False

    substack_span = 87 / SubStack_nb
    progress_start = 3 + ((group_num - 1) * substack_span)
    cohort_file_total = active_cohort_file_total or len(files)
    group_size, remainder = divmod(cohort_file_total, SubStack_nb)
    group_file_offset = active_cohort_file_offset + sum(
        group_size + (index <= remainder) for index in range(1, group_num)
    )
    file_total = active_run_file_total or len(files)

    def phase(start_fraction, end_fraction, label, track_files=False, file_current=None):
        progress_label = f"Substack {group_num}/{SubStack_nb}: {label}"
        progress_start_value = progress_start + (substack_span * start_fraction)
        progress_end_value = progress_start + (substack_span * end_fraction)
        if track_files:
            current = group_file_offset if file_current is None else file_current
            report_progress(
                progress_start_value,
                progress_end_value,
                progress_label,
                file_start=current,
                file_end=(group_file_offset + len(files) if file_current is None else current),
                file_total=file_total,
            )
        else:
            report_progress(progress_start_value, progress_end_value, progress_label)

    print(f"[INFO] Processing Substack {group_num} ({len(files)} frames)")
    if quality_report is not None:
        quality_report['active_substack'] = group_num
    phase(0, 0.02, "Preparing")
    configure_debayer()
    execute_siril(f"cd {siril_path(lights_folder)}")
    execute_siril("set32bits")
    phase(0.02, 0.10, "Converting frames", track_files=True)
    execute_siril("convert light -out=../process")
    phase(
        0.10, 0.10, "Converting frames", track_files=True,
        file_current=group_file_offset + count_sequence_files(process_folder, "light"),
    )
    execute_siril(f"cd {siril_path(process_folder)}")
    input_sequence = "light"
    if cosmetic_correction:
        phase(0.10, 0.16, "Correcting cosmetic defects", track_files=True)
        execute_siril(
            f"seqfind_cosme_cfa light {cosmetic_cold_sigma} "
            f"{cosmetic_hot_sigma} -prefix=cc_"
        )
        phase(
            0.16, 0.16, "Correcting cosmetic defects", track_files=True,
            file_current=group_file_offset + count_sequence_files(process_folder, "cc_light"),
        )
        input_sequence = "cc_light"
    phase(0.16, 0.28, "Calibrating and debayering", track_files=True)
    execute_siril(
        f"calibrate {input_sequence}"
        + ("" if drizzle_enabled else " -debayer")
    )
    processed_sequence = f"pp_{input_sequence}"
    skip_invalid_sequence_frames(
        process_folder,
        processed_sequence,
        1 if drizzle_enabled else 3,
    )
    phase(
        0.28, 0.28, "Calibrating and debayering", track_files=True,
        file_current=group_file_offset + count_sequence_files(process_folder, processed_sequence),
    )
    if background_method == 'off':
        background_sequence = processed_sequence
        phase(0.28, 0.40, "Background extraction disabled")
    else:
        background_sequence = f"bkg_{processed_sequence}"
        phase(0.28, 0.40, "Removing gradients", track_files=True)
        execute_siril(build_background_command(processed_sequence))
        phase(
            0.40, 0.40, "Removing gradients", track_files=True,
            file_current=group_file_offset + count_sequence_files(process_folder, background_sequence),
        )
    phase(0.40, 0.58, "Plate solving", track_files=True)
    plate_solve_response = execute_siril(
        build_plate_solve_command(background_sequence, cat),
        allow_partial=skip_failed_frames,
    )
    phase(
        0.58, 0.58, "Plate solving", track_files=True,
        file_current=group_file_offset + count_sequence_files(process_folder, background_sequence),
    )

    # Registration with Drizzle
    selection_filters = effective_selection_filters()
    print(f"[INFO] Effective frame filters: {selection_filters}", flush=True)
    registration_command = build_registration_command(background_sequence, selection_filters)
    if drizzle_enabled:
        registration_command += (
            f" -drizzle -scale={drizzle_scale} "
            f"-pixfrac={pix_frac} -kernel={drizzle_kernel}"
        )
    phase(0.58, 0.78, "Registering frames", track_files=True)
    registration_response = execute_siril(registration_command)
    phase(
        0.78, 0.78, "Registering frames", track_files=True,
        file_current=group_file_offset + count_sequence_files(
            process_folder, f"r_{background_sequence}"
        ),
    )
    if coverage_map_enabled:
        if SubStack_nb == 1:
            generate_coverage_map(process_folder, f"r_{background_sequence}")
        else:
            local_coverage_dir = workdir / 'substacks' / 'coverage_maps'
            local_report = generate_coverage_map(
                process_folder,
                f"r_{background_sequence}",
                destination_dir=local_coverage_dir,
                artifact_stem=f"substack_{group_num}",
                update_report=False,
            )
            if quality_report is not None and local_report is not None:
                quality_report.setdefault('substack_coverages', {})[str(group_num)] = local_report

    stack_start = time.time()
    # Stacking with the selected rejection method and feathering
    phase(0.78, 0.98, "Stacking frames", track_files=True)
    rejection_command = (
        "rej none" if pixel_rejection_method == 'none'
        else f"rej {pixel_rejection_method} {rej_low} {rej_high}"
    )
    stack_response = execute_siril(
        f"stack r_{background_sequence} {rejection_command} "
        f"-weight={stacking_weight} "
        f"-norm={stack_normalization}{' -overlap_norm' if overlap_normalization else ''} "
        f"-feather={feather_val} "
        f"-rgb_equal -output_norm"
        f"{' -rejmaps' if pixel_rejection_method != 'none' else ''} -maximize"
        f"{' -fastnorm' if fast_normalization else ''} -out=../substack_{group_num}"
    )
    stacked_count = parse_stack_quality(stack_response).get("stacked_frames", len(files))
    phase(
        0.98, 0.98, "Stacking frames", track_files=True,
        file_current=group_file_offset + min(len(files), stacked_count),
    )

    stack_time = time.time() - stack_start
    print(f"[INFO] Substack {group_num} stack time: {stack_time:.1f}s")
    if quality_report is not None:
        substack_report = parse_registration_quality(registration_response, len(files))
        substack_report["number"] = group_num
        if active_cohort_id is not None:
            substack_report["cohort"] = active_cohort_id
        substack_report["stack"] = parse_stack_quality(stack_response)
        substack_report["stage_counts"] = summarize_substack_stages(
            process_folder,
            files,
            input_sequence,
            processed_sequence,
            background_sequence,
            stack_response,
        )
        frame_summary = summarize_substack_frames(
            files,
            process_folder,
            processed_sequence,
            background_sequence,
            substack_report,
            substack_report["stack"],
            plate_solve_response,
        )
        ledger_records = frame_summary.pop('frame_ledger_records')
        for record in ledger_records:
            record['run_id'] = run_id
            record['substack'] = group_num
            record['cohort'] = active_cohort_id
        append_frame_ledger(ledger_records)
        substack_report.update(frame_summary)
        substack_report['frame_ledger_count'] = len(ledger_records)
        substack_report["stack_seconds"] = round(stack_time, 1)
        quality_report["substacks"].append(substack_report)
        print(f"[INFO] Substack {group_num} stage counts: {substack_report['stage_counts']}", flush=True)
        write_quality_report("running")
    phase(0.98, 1, "Finalizing substack")
    execute_siril(f"cd {siril_path(group_path)}")
    execute_siril(f"mirrorx_single substack_{group_num}")
    artifact = require_siril_artifact(
        group_path / f'substack_{group_num}.fit',
        f'Substack {group_num} artifact',
    )
    if quality_report is not None:
        quality_report.setdefault('artifact_postconditions', []).append(artifact)
    phase(1, 1, "Complete")
    return True

def masterstack(SubStack_nb):
    if quality_report is not None:
        quality_report.pop('active_substack', None)
    group_folder = workdir / "substacks"
    lights_folder = group_folder / "lights"
    process_folder = group_folder / "process"
    rejection_maps_folder = group_folder / "rejection_maps"
    output_dir.mkdir(parents=True, exist_ok=True)
    lights_folder.mkdir(parents=True, exist_ok=True)
    process_folder.mkdir(parents=True, exist_ok=True)
    rejection_maps_folder.mkdir(parents=True, exist_ok=True)
    print("[INFO] Starting Master Stack")
    report_progress(90, 91, "Collecting substacks")
    execute_siril(f"cd {siril_path(lights_folder)}")
    for i in range(1, SubStack_nb + 1):
        source_group = workdir / f'Lights_sorted/group_{i}'
        substack_file = source_group / f'substack_{i}.fit'
        move_replace(substack_file, lights_folder / substack_file.name)
        for rejection_map in source_group.glob(f"substack_{i}_*rejmap.fit"):
            move_replace(rejection_map, rejection_maps_folder / rejection_map.name)
        if not debug:
            remove_tree(source_group)

    if SubStack_nb == 1:
        report_progress(91, 98, "Creating master stack")
        shutil.copy2(lights_folder / "substack_1.fit", master_stack_path())
        for rejection_map in rejection_maps_folder.glob("substack_1_*rejmap.fit"):
            master_name = rejection_map.name.replace(
                "substack_1_", f"{master_stack_path().stem}_", 1
            )
            shutil.copy2(rejection_map, output_dir / master_name)
        artifact = require_siril_artifact(master_stack_path(), 'Master stack artifact')
        if quality_report is not None:
            quality_report["master"] = {
                "stacked_frames": 1,
                "output": {
                    "width": artifact["width"],
                    "height": artifact["height"],
                },
                "method": "single substack copy",
                "path": str(master_stack_path()),
                "weight": stacking_weight,
                "rejection": pixel_rejection_method,
            }
            quality_report.setdefault('artifact_postconditions', []).append(artifact)
            finalize_coverage_maps(master_stack_path(), quality_report.get('coverage'))
        report_progress(98, 98, "Master stack complete")
        return

    report_progress(91, 92, "Preparing master integration")
    execute_siril("set32bits")
    report_progress(92, 93, "Converting substacks")
    execute_siril("convert light -out=../process")
    execute_siril(f"cd {siril_path(process_folder)}")
    report_progress(93, 94, "Calibrating substacks")
    execute_siril("calibrate light")

    # Master Registration with Lanczos4 (Highest Quality interpolation)
    report_progress(94, 95.5, "Registering substacks")
    execute_siril(build_master_registration_command())
    report_progress(95.5, 96, "Applying master registration")
    execute_siril(
        f"seqapplyreg pp_light -framing=max -interp={registration_interpolation}"
    )
    if SubStack_nb > 1 and coverage_map_enabled:
        compose_substack_coverage_maps(
            process_folder,
            workdir / 'substacks' / 'coverage_maps',
            SubStack_nb,
        )

    # Master Stacking with full APP-level parameters
    report_progress(96, 97.5, "Integrating master stack")
    use_master_rejection = SubStack_nb >= 4
    if use_master_rejection and pixel_rejection_method != 'none':
        master_rejection = f"rej {pixel_rejection_method} {rej_low} {rej_high}"
    else:
        master_rejection = "rej none"
    master_response = execute_siril(
        f"stack r_pp_light {master_rejection} "
        f"-weight=nbstack "
        f"-norm={stack_normalization}{' -overlap_norm' if overlap_normalization else ''} "
        f"-feather={feather_val} "
        f"-rgb_equal -output_norm"
        f"{' -rejmaps' if use_master_rejection and pixel_rejection_method != 'none' else ''} -maximize"
        f"{' -fastnorm' if fast_normalization else ''} "
        f"{siril_path_option('out', master_stack_path().with_suffix(''))}"
    )
    report_progress(97.5, 98, "Finalizing master stack")
    execute_siril(f"cd {siril_path(output_dir)}")
    execute_siril(f"mirrorx_single {master_stack_path().stem}")
    artifact = require_siril_artifact(master_stack_path(), 'Master stack artifact')
    if quality_report is not None:
        quality_report["master"] = parse_stack_quality(master_response)
        quality_report["master"]["method"] = "registered substack integration"
        quality_report["master"]["path"] = str(master_stack_path())
        quality_report["master"]["weight"] = "nbstack"
        quality_report["master"]["rejection"] = (
            pixel_rejection_method if use_master_rejection else "none"
        )
        quality_report.setdefault('artifact_postconditions', []).append(artifact)
        finalize_coverage_maps(master_stack_path(), quality_report.get('coverage'))

def final_cleanup():
    report_progress(98, 100, "Writing final outputs")
    if app is not None and not siril_unresponsive:
        execute_siril(f"cd {siril_path(output_dir)}")
    remove_tree(workdir / 'Lights_sorted')
    remove_tree(workdir / 'substacks')
    report_progress(100, 100, "Complete")


def create_cropped_master():
    if quality_report is None or not quality_report.get('coverage'):
        return
    coverage_report = quality_report['coverage']
    if coverage_report.get('status') == 'unavailable' or not coverage_report.get('path'):
        print(
            "[WARNING] Auto-cropped master unavailable because the coverage map was not created.",
            flush=True,
        )
        return
    coverage_path = Path(coverage_report.get('integration_time_path', coverage_report['path']))
    if not auto_crop_enabled:
        return
    coverage = _read_fits_array(coverage_path)
    master_width, master_height = _read_fits_dimensions(master_stack_path())
    if coverage.shape != (master_height, master_width):
        message = (
            f"Auto-cropped master unavailable: coverage canvas {coverage.shape[1]}x"
            f"{coverage.shape[0]} does not match master canvas "
            f"{master_width}x{master_height}."
        )
        quality_report['coverage']['crop_status'] = 'unavailable'
        quality_report['coverage']['crop_reason'] = message
        print(f"[WARNING] {message}", flush=True)
        return
    bounds = coverage_crop_bounds(coverage, auto_crop_coverage_percent)
    if not bounds:
        return
    threshold = bounds.pop('threshold')
    quality_report['coverage']['crop_threshold'] = threshold
    quality_report['coverage']['crop_reference_coverage'] = bounds.pop('reference_coverage')
    quality_report['coverage']['crop_method'] = (
        'largest rectangle above percentage of median positive integration time'
        if coverage_report.get('integration_time_path') else
        'largest rectangle above percentage of median positive coverage'
    )
    quality_report['coverage']['crop_coverage_percent'] = auto_crop_coverage_percent
    quality_report['coverage']['crop_bounds'] = bounds
    quality_report['coverage']['crop_coordinate_system'] = 'FITS array rows'
    selection = dict(bounds, y=master_height - bounds['y'] - bounds['height'])
    quality_report['coverage']['crop_siril_selection'] = selection
    cropped_path = output_dir / f"{master_stack_path().stem}_cropped.fit"
    if reusable_report_artifact(
        coverage_report, 'cropped_master_path', cropped_path, 'Cropped master artifact'
    ):
        print(f"[INFO] Resuming: reusing coverage-cropped master: {cropped_path}", flush=True)
        return
    execute_siril(f"cd {siril_path(output_dir)}")
    execute_siril(f"load {siril_path(master_stack_path().with_suffix(''))}")
    execute_siril(
        f"boxselect {selection['x']} {selection['y']} "
        f"{selection['width']} {selection['height']}"
    )
    execute_siril("crop")
    execute_siril(f"save {siril_path(cropped_path.with_suffix(''))}")
    artifact = require_siril_artifact(cropped_path, 'Cropped master artifact')
    quality_report.setdefault('artifact_postconditions', []).append(artifact)
    quality_report['coverage']['cropped_master_path'] = str(cropped_path)
    print(f"[INFO] Coverage-cropped master: {cropped_path}", flush=True)

def build_parser():
    parser = argparse.ArgumentParser(description="Build a randomized multi-stage Siril mosaic stack.")
    parser.add_argument("--workdir", type=Path, default=workdir)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--siril-exe", type=Path, default=siril_exe)
    parser.add_argument("--siril-open-timeout", type=float, default=siril_open_timeout)
    parser.add_argument("--siril-command-timeout", type=float, default=siril_command_timeout)
    parser.add_argument("--cancel-file", type=Path)
    parser.add_argument(
        "--test-frame-count",
        type=int,
        default=0,
        help="randomly sample this many input frames; 0 processes all frames",
    )
    parser.add_argument(
        "--coverage-map",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write full-mosaic integration-time FITS maps: normalized view and seconds per pixel",
    )
    parser.add_argument(
        "--auto-crop-master",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write a second master cropped to the requested coverage threshold",
    )
    parser.add_argument(
        "--export-per-cohort",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="write one independent master for each acquisition cohort",
    )
    parser.add_argument(
        "--cohort-group-by",
        choices=COHORT_GROUP_FIELDS,
        action="append",
        default=None,
        help="cohort grouping field; repeat for camera, filter, or exposure_seconds",
    )
    parser.add_argument(
        "--auto-crop-coverage-percent", type=int, default=50,
        help="minimum crop integration time as a percentage of the nonblank-pixel median; lower keeps more field",
    )
    parser.add_argument("--substacks", type=int, default=SubStack_nb)
    parser.add_argument(
        "--auto-substacks",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="calculate the substack count using the detected Siril frame limit",
    )
    parser.add_argument("--drizzle", action=argparse.BooleanOptionalAction, default=drizzle_enabled)
    parser.add_argument("--drizzle-scale", default=drizzle_scale)
    parser.add_argument("--pixel-fraction", default=pix_frac)
    parser.add_argument("--drizzle-kernel", choices=DRIZZLE_KERNELS, default=drizzle_kernel)
    parser.add_argument("--bayer-pattern", choices=BAYER_PATTERNS, default=DEFAULT_BAYER_PATTERN)
    parser.add_argument("--bayer-orientation", choices=BAYER_ORIENTATIONS, default=DEFAULT_BAYER_ORIENTATION)
    parser.add_argument(
        "--cosmetic-correction",
        action=argparse.BooleanOptionalAction,
        default=cosmetic_correction,
    )
    parser.add_argument("--cosmetic-cold-sigma", default=cosmetic_cold_sigma)
    parser.add_argument("--cosmetic-hot-sigma", default=cosmetic_hot_sigma)
    parser.add_argument(
        "--overlap-normalization",
        action=argparse.BooleanOptionalAction,
        default=overlap_normalization,
    )
    parser.add_argument(
        "--stack-normalization",
        choices=STACK_NORMALIZATION_METHODS,
        default=stack_normalization,
        help="normalization mode used when integrating substacks and the final master",
    )
    parser.add_argument("--plate-solve-order", type=int, default=plate_solve_order)
    parser.add_argument(
        "--plate-solve-downscale",
        action=argparse.BooleanOptionalAction,
        default=plate_solve_downscale,
    )
    parser.add_argument("--plate-solve-radius", type=float, default=plate_solve_radius)
    parser.add_argument("--plate-solve-limit-mag", default=plate_solve_limit_mag)
    parser.add_argument("--rbf-smoothing", default=rbf_smoothing)
    parser.add_argument(
        "--background-dither",
        action=argparse.BooleanOptionalAction,
        default=background_dither,
    )
    parser.add_argument(
        "--registration-transform",
        choices=REGISTRATION_TRANSFORMS,
        default=registration_transform,
    )
    parser.add_argument("--registration-minpairs", type=int, default=registration_minpairs)
    parser.add_argument("--registration-maxstars", type=int, default=registration_maxstars)
    parser.add_argument(
        "--registration-interpolation",
        choices=REGISTRATION_INTERPOLATIONS,
        default=registration_interpolation,
    )
    parser.add_argument("--filter-background", type=int, default=97)
    parser.add_argument("--filter-stars", type=int, default=97)
    parser.add_argument("--filter-roundness", type=int, default=97)
    parser.add_argument("--filter-fwhm", type=int, default=97)
    parser.add_argument("--sky-quality-percent", type=int, default=100)
    parser.add_argument(
        "--adaptive-quality-filtering",
        action=argparse.BooleanOptionalAction,
        default=adaptive_quality_filtering,
    )
    parser.add_argument("--quality-filter-sigma", type=float, default=float(quality_filter_sigma))
    parser.add_argument("--background-method", choices=BACKGROUND_METHODS, default=background_method)
    parser.add_argument("--background-samples", type=int, default=background_samples)
    parser.add_argument(
        "--background-tolerance",
        type=float,
        default=float(background_tolerance),
    )
    parser.add_argument("--weight", default=stacking_weight)
    parser.add_argument("--feather", default=feather_val)
    parser.add_argument("--rejection-low", default=rej_low)
    parser.add_argument("--rejection-high", default=rej_high)
    parser.add_argument(
        "--rejection-method", choices=PIXEL_REJECTION_METHODS, default=pixel_rejection_method
    )
    parser.add_argument(
        "--fast-normalization",
        action=argparse.BooleanOptionalAction,
        default=fast_normalization,
    )
    parser.add_argument("--catalog", choices=PLATE_SOLVE_CATALOGS, default=catalog)
    parser.add_argument("--memory", default=memory_fraction)
    parser.add_argument("--cpus", type=int, default=cpu_count)
    parser.add_argument("--minimum-free-disk-gb", type=float, default=minimum_free_disk_gb)
    parser.add_argument("--retries", type=int, default=max_retries)
    parser.add_argument(
        "--skip-failed-frames",
        action=argparse.BooleanOptionalAction,
        default=skip_failed_frames,
        help="exclude malformed calibrated frames and record them in the log/report",
    )
    parser.add_argument(
        "--mosaic-aware-star-count",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="do not globally reject frames based on raw star count",
    )
    parser.add_argument("--debug", action="store_true", help="keep intermediate files")
    parser.add_argument("--seed", type=int, default=None, help="reproducibility seed")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate settings and print the processing plan without moving files or starting Siril",
    )
    parser.add_argument(
        "--verify-run",
        type=Path,
        help="verify a completed quality report and its recorded artifacts without modifying files",
    )
    parser.add_argument(
        "--bundle-report",
        type=Path,
        help="export a completed report and its evidence files as a ZIP bundle",
    )
    parser.add_argument("--bundle-output", type=Path)
    parser.add_argument("--html-report", type=Path, help="export a completed report as a self-contained HTML file")
    parser.add_argument("--html-output", type=Path)
    parser.add_argument("--checkpoint-status", action="store_true")
    parser.add_argument("--discard-checkpoint", action="store_true")
    parser.add_argument("--run-lock-status", action="store_true")
    parser.add_argument("--break-run-lock", action="store_true")
    parser.add_argument("--cleanup-review", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the latest interrupted run from run_checkpoint.json",
    )
    parser.add_argument(
        "--replay-ledger",
        type=Path,
        help="replay quality thresholds against a completed frame ledger without invoking Siril",
    )
    parser.add_argument("--replay-background", type=float, default=80)
    parser.add_argument("--replay-roundness", type=float, default=80)
    parser.add_argument("--replay-fwhm", type=float, default=80)
    parser.add_argument("--replay-stars", type=float, default=80)
    parser.add_argument(
        "--integrity-scan",
        action="store_true",
        help="scan input files, metadata, duplicates, dimensions, and disk headroom without processing",
    )
    parser.add_argument(
        "--deep-integrity-scan",
        action="store_true",
        help="validate FITS payload lengths in addition to header readability",
    )
    parser.add_argument("--integrity-scan-json", type=Path)
    parser.add_argument(
        "--crop-workbench",
        action="store_true",
        help="crop an existing master from an integration-time map without rerunning the stack",
    )
    parser.add_argument("--crop-workbench-master", type=Path)
    parser.add_argument("--crop-workbench-coverage", type=Path)
    parser.add_argument("--crop-workbench-percent", type=float)
    parser.add_argument("--crop-workbench-output", type=Path)
    return parser


def configure(arguments):
    global workdir, output_dir, siril_exe, run_id, quality_report
    global cancel_file
    global SubStack_nb, auto_substacks, drizzle_enabled, drizzle_scale, pix_frac, drizzle_kernel
    global bayer_pattern, bayer_orientation, cosmetic_correction
    global cosmetic_cold_sigma, cosmetic_hot_sigma
    global overlap_normalization, stack_normalization
    global plate_solve_order, plate_solve_downscale, plate_solve_radius, plate_solve_limit_mag
    global rbf_smoothing, background_dither, registration_transform
    global registration_minpairs, registration_maxstars, registration_interpolation
    global adaptive_quality_filtering, quality_filter_sigma
    global background_method, background_samples, background_tolerance
    global filter_bkg, filter_nbstars, filter_round, filter_fwhm
    global stacking_weight, feather_val, rej_low, rej_high, pixel_rejection_method
    global fast_normalization, catalog
    global memory_fraction, cpu_count, max_retries, skip_failed_frames
    global minimum_free_disk_gb
    global mosaic_aware_star_count, test_frame_count, coverage_map_enabled
    global auto_crop_enabled, auto_crop_coverage_percent, debug
    global export_per_cohort
    global cohort_group_fields
    global sky_quality_percent, run_seed, journal_path, frame_ledger_path
    global resume_enabled, resume_checkpoint_data
    global journal_sequence, command_sequence
    global active_phase, journal_warning_emitted
    global siril_open_timeout, siril_command_timeout, siril_unresponsive
    global siril_version_text, siril_version_tuple, siril_max_stack_frames

    workdir = arguments.workdir.expanduser().resolve()
    selected_output = arguments.output_dir or (workdir / "Siril Mosaic Output")
    output_dir = selected_output.expanduser().resolve()
    resume_enabled = arguments.resume
    resume_checkpoint_data = load_run_checkpoint() if resume_enabled else None
    if resume_checkpoint_data is not None:
        if Path(resume_checkpoint_data.get('workdir', '')).resolve() != workdir:
            raise ValueError('Resume checkpoint belongs to a different input folder.')
        if Path(resume_checkpoint_data.get('output_dir', '')).resolve() != output_dir:
            raise ValueError('Resume checkpoint belongs to a different output folder.')
        run_id = resume_checkpoint_data['run_id']
    else:
        run_id = choose_run_id(output_dir)
    quality_report = None
    run_seed = (
        resume_checkpoint_data['seed']
        if resume_checkpoint_data is not None else
        arguments.seed if arguments.seed is not None else random.SystemRandom().randrange(2**32)
    )
    journal_path = output_dir / f"run_events_{run_id}.jsonl"
    frame_ledger_path = output_dir / f"frame_ledger_{run_id}.jsonl"
    journal_sequence = 0
    if resume_checkpoint_data is not None and journal_path.is_file():
        try:
            journal_sequence = max(
                (
                    json.loads(line).get('sequence', 0)
                    for line in journal_path.read_text(encoding='utf-8').splitlines()
                    if line.strip()
                ),
                default=0,
            )
        except (OSError, json.JSONDecodeError):
            journal_sequence = 0
    command_sequence = 0
    active_phase = "Starting"
    journal_warning_emitted = False
    siril_unresponsive = False
    siril_exe = arguments.siril_exe.expanduser().resolve()
    siril_version_text = 'unavailable'
    siril_version_tuple = None
    siril_max_stack_frames = SIRIL_LEGACY_MAX_STACK_FRAMES
    cancel_file = arguments.cancel_file.expanduser().resolve() if arguments.cancel_file else None
    SubStack_nb = arguments.substacks
    auto_substacks = arguments.auto_substacks
    drizzle_enabled = arguments.drizzle
    drizzle_scale = str(arguments.drizzle_scale)
    pix_frac = str(arguments.pixel_fraction)
    drizzle_kernel = arguments.drizzle_kernel
    bayer_pattern = arguments.bayer_pattern
    bayer_orientation = arguments.bayer_orientation
    cosmetic_correction = arguments.cosmetic_correction
    cosmetic_cold_sigma = str(arguments.cosmetic_cold_sigma)
    cosmetic_hot_sigma = str(arguments.cosmetic_hot_sigma)
    overlap_normalization = arguments.overlap_normalization
    stack_normalization = arguments.stack_normalization
    plate_solve_order = arguments.plate_solve_order
    plate_solve_downscale = arguments.plate_solve_downscale
    plate_solve_radius = arguments.plate_solve_radius
    plate_solve_limit_mag = arguments.plate_solve_limit_mag
    rbf_smoothing = str(arguments.rbf_smoothing)
    background_dither = arguments.background_dither
    registration_transform = arguments.registration_transform
    registration_minpairs = arguments.registration_minpairs
    registration_maxstars = arguments.registration_maxstars
    registration_interpolation = arguments.registration_interpolation
    filter_bkg = f"{arguments.filter_background}%"
    filter_nbstars = f"{arguments.filter_stars}%"
    filter_round = f"{arguments.filter_roundness}%"
    filter_fwhm = f"{arguments.filter_fwhm}%"
    adaptive_quality_filtering = arguments.adaptive_quality_filtering
    quality_filter_sigma = str(arguments.quality_filter_sigma)
    background_method = arguments.background_method
    background_samples = arguments.background_samples
    background_tolerance = str(arguments.background_tolerance)
    stacking_weight = arguments.weight
    feather_val = str(arguments.feather)
    rej_low = str(arguments.rejection_low)
    rej_high = str(arguments.rejection_high)
    pixel_rejection_method = arguments.rejection_method
    fast_normalization = arguments.fast_normalization
    catalog = arguments.catalog
    memory_fraction = str(arguments.memory)
    cpu_count = arguments.cpus
    siril_open_timeout = float(arguments.siril_open_timeout)
    siril_command_timeout = float(arguments.siril_command_timeout)
    minimum_free_disk_gb = float(arguments.minimum_free_disk_gb)
    max_retries = arguments.retries
    skip_failed_frames = arguments.skip_failed_frames
    mosaic_aware_star_count = arguments.mosaic_aware_star_count
    test_frame_count = arguments.test_frame_count
    coverage_map_enabled = arguments.coverage_map
    auto_crop_enabled = arguments.auto_crop_master
    export_per_cohort = arguments.export_per_cohort
    cohort_group_fields = tuple(arguments.cohort_group_by or COHORT_GROUP_FIELDS)
    auto_crop_coverage_percent = arguments.auto_crop_coverage_percent
    sky_quality_percent = arguments.sky_quality_percent
    debug = arguments.debug
    if resume_checkpoint_data is not None and resume_checkpoint_data.get('settings'):
        saved_settings = resume_checkpoint_data['settings']
        if configuration_hash(saved_settings) != resume_checkpoint_data.get('configuration_hash'):
            raise ValueError('Resume checkpoint settings are internally inconsistent.')
        apply_runtime_settings(saved_settings)


def validate_parameters():
    global SubStack_nb, siril_version_text, siril_version_tuple, siril_max_stack_frames
    if not workdir.is_dir():
        raise ValueError("Input folder does not exist.")
    if output_dir == workdir:
        raise ValueError("Output folder must be different from the input folder.")
    if not resume_enabled:
        checkpoint_path = output_dir / RUN_CHECKPOINT
        staging_path = workdir / "Lights_sorted"
        if checkpoint_path.is_file() or staging_path.exists():
            raise ValueError(
                "An interrupted run checkpoint or Lights_sorted staging folder already exists. "
                "Use Resume Run to continue it, or inspect and clear the recovery state before starting fresh."
            )
    for transient_folder in (workdir / "Lights_sorted", workdir / "substacks"):
        if _is_within(output_dir, transient_folder):
            raise ValueError("Output folder cannot be inside a temporary processing folder.")
    light_count = len(input_files_after_reject_restore(workdir, (output_dir,)))
    checkpoint_count = (
        int(resume_checkpoint_data.get('input_frames', 0))
        if resume_enabled and resume_checkpoint_data else 0
    )
    if light_count == 0 and not resume_enabled:
        raise ValueError("Input folder contains no supported FITS or XISF files.")
    if not resume_enabled and not 0 <= test_frame_count <= light_count:
        raise ValueError("Test frame count must be 0 or no more than the input frame count.")
    if auto_crop_enabled and not coverage_map_enabled:
        raise ValueError("Auto-crop master requires the coverage map.")
    if export_per_cohort and debug:
        raise ValueError("Debug mode is unavailable when exporting one master per cohort.")
    if export_per_cohort and not cohort_group_fields:
        raise ValueError("At least one cohort grouping field is required when exporting per cohort.")
    if not 1 <= auto_crop_coverage_percent <= 100:
        raise ValueError("Auto-crop coverage percentage must be from 1 to 100.")
    effective_count = checkpoint_count if resume_enabled else (test_frame_count or light_count)
    if not siril_exe.is_file():
        raise ValueError("Siril executable does not exist.")
    siril_version_text = detect_siril_version()
    siril_version_tuple = parse_siril_version(siril_version_text)
    siril_max_stack_frames = siril_frame_limit_for_version(siril_version_text)
    if auto_substacks:
        if siril_version_tuple is None:
            print(
                f"[WARNING] Could not parse Siril version {siril_version_text!r}; "
                f"using the conservative {siril_max_stack_frames}-frame limit.",
                flush=True,
            )
        else:
            print(
                f"[INFO] Siril {siril_version_text}; auto substack limit: "
                f"{siril_max_stack_frames} frame(s)",
                flush=True,
            )
        SubStack_nb = math.ceil(effective_count / siril_max_stack_frames)
        print(
            f"[INFO] Auto substacks: {effective_count} frame(s) -> "
            f"{SubStack_nb} substack(s), max {siril_max_stack_frames} frames each",
            flush=True,
        )
    if SubStack_nb < 1:
        raise ValueError("Substack count must be at least 1.")
    if SubStack_nb > effective_count:
        raise ValueError("Substack count cannot exceed the number of light frames.")
    if cpu_count < 1:
        raise ValueError("CPU count must be at least 1.")
    if max_retries < 0:
        raise ValueError("Retries cannot be negative.")
    for name, value in (
        ('Siril open timeout', siril_open_timeout),
        ('Siril command timeout', siril_command_timeout),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative (0 disables the watchdog).")
    if not math.isfinite(minimum_free_disk_gb) or minimum_free_disk_gb < 0:
        raise ValueError("Minimum free disk space must be finite and non-negative.")
    if sky_quality_percent != 100:
        raise ValueError("Sky quality percentage filtering is unsupported; use percentage or adaptive frame filters.")
    effective_selection_filters()
    if background_samples < 1:
        raise ValueError("Background samples must be at least 1.")
    if float(background_tolerance) <= 0:
        raise ValueError("Background tolerance must be greater than 0.")
    if not 1 <= plate_solve_order <= 5:
        raise ValueError("Plate-solve order must be from 1 to 5.")
    if plate_solve_radius is not None and (
        not math.isfinite(plate_solve_radius) or plate_solve_radius <= 0
    ):
        raise ValueError("Plate-solve radius must be finite and greater than 0.")
    if plate_solve_limit_mag is not None:
        try:
            if not math.isfinite(float(plate_solve_limit_mag)):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError("Plate-solve limit magnitude must be numeric.") from error
    try:
        smoothing = float(rbf_smoothing)
    except ValueError as error:
        raise ValueError("RBF smoothing must be a number.") from error
    if not math.isfinite(smoothing) or smoothing <= 0:
        raise ValueError("RBF smoothing must be finite and greater than 0.")
    if registration_minpairs < 0:
        raise ValueError("Registration minimum pairs cannot be negative.")
    if registration_maxstars and not 100 <= registration_maxstars <= 2000:
        raise ValueError("Registration maximum stars must be 0 or from 100 to 2000.")
    if catalog not in PLATE_SOLVE_CATALOGS:
        raise ValueError(f"Catalog must be one of: {', '.join(PLATE_SOLVE_CATALOGS)}.")
    numeric_values = [
        ("memory", memory_fraction),
        ("low rejection", rej_low),
        ("high rejection", rej_high),
    ]
    if cosmetic_correction:
        numeric_values.append(("cosmetic cold sigma", cosmetic_cold_sigma))
        numeric_values.append(("cosmetic hot sigma", cosmetic_hot_sigma))
    if drizzle_enabled:
        numeric_values.extend((("drizzle scale", drizzle_scale), ("pixel fraction", pix_frac)))
    for label, value in numeric_values:
        try:
            number = float(value)
        except ValueError as error:
            raise ValueError(f"{label.title()} must be a number.") from error
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{label.title()} must be finite and greater than 0.")
    if float(memory_fraction) > 1:
        raise ValueError("Memory fraction cannot exceed 1.")
    if drizzle_enabled and float(pix_frac) > 1:
        raise ValueError("Pixel fraction cannot exceed 1.")
    if drizzle_enabled and float(drizzle_scale) > 3:
        raise ValueError("Drizzle scale cannot exceed 3.")
    if drizzle_kernel not in DRIZZLE_KERNELS:
        raise ValueError(f"Drizzle kernel must be one of: {', '.join(DRIZZLE_KERNELS)}.")
    if pixel_rejection_method not in PIXEL_REJECTION_METHODS:
        raise ValueError(
            f"Rejection method must be one of: {', '.join(PIXEL_REJECTION_METHODS)}."
        )
    if pixel_rejection_method in REJECTION_FRACTION_METHODS and (
        float(rej_low) > 1 or float(rej_high) > 1
    ):
        raise ValueError(
            f"{pixel_rejection_method.title()} rejection thresholds cannot exceed 1."
        )
    if stacking_weight not in STACKING_WEIGHTS:
        raise ValueError(f"Weight must be one of: {', '.join(sorted(STACKING_WEIGHTS))}.")
    if stack_normalization not in STACK_NORMALIZATION_METHODS:
        raise ValueError(
            f"Stack normalization must be one of: {', '.join(STACK_NORMALIZATION_METHODS)}."
        )
    if float(feather_val) < 0:
        raise ValueError("Feather cannot be negative.")


def run_pipeline(arguments):
    global app, quality_report, active_master_path, active_cohort_id
    global active_cohort_number, active_cohort_tag, active_cohort_total
    global active_cohort_progress_base, active_cohort_progress_span, SubStack_nb
    global active_run_file_total, active_cohort_file_offset, active_cohort_file_total
    global siril_unresponsive
    configure(arguments)
    checkpoint_state: dict[str, Any] | None = None
    if arguments.dry_run and resume_enabled:
        raise ValueError('Resume cannot be combined with dry-run.')
    validate_parameters()
    if resume_enabled:
        checkpoint_state = resume_checkpoint_data
        assert checkpoint_state is not None
        if export_per_cohort and not checkpoint_state.get('cohorts'):
            raise ValueError('This checkpoint predates per-cohort resume support; start a new cohort run.')
        if not checkpoint_state.get('settings') and checkpoint_state.get('configuration_hash') != configuration_hash():
            raise ValueError(
                'Resume settings do not match the interrupted run. '
                'Use the same processing settings and seed.'
            )
    if arguments.dry_run:
        current_input_files = discover_light_files(workdir, (output_dir,))
        pending_rejects = pending_rejected_files()
        input_files = current_input_files + pending_rejects
        random.seed(run_seed)
        if test_frame_count:
            input_files = random.sample(input_files, test_frame_count)
        cohort_batches = partition_frame_cohorts(input_files, cohort_group_fields) if export_per_cohort else [(None, input_files)]
        print("[DRY RUN] No files will be moved, no Siril process will be started, and no outputs will be written.")
        print(
            f"[DRY RUN] Input frames: {len(input_files)} "
            f"({len(current_input_files)} current + "
            f"{len(pending_rejects)} prior rejects to restore)"
        )
        print(f"[DRY RUN] Acquisition cohorts: {len(summarize_frame_cohorts(input_files, cohort_group_fields))}")
        print(f"[DRY RUN] Substacks: {SubStack_nb}{' (automatic)' if auto_substacks else ''}")
        print(f"[DRY RUN] Configuration hash: {configuration_hash()}")
        print(f"[DRY RUN] Seed: {run_seed}")
        print(f"[DRY RUN] Output directory: {output_dir}")
        print(
            "[DRY RUN] Expected outputs: master stack, quality report, run journal, "
            "input manifest, and optional coverage/crop/rejection maps."
        )
        for number, (cohort_id, files) in enumerate(cohort_batches, 1):
            print(
                f"[DRY RUN] Cohort {number}: {len(files)} frame(s)"
                + (f" - {cohort_id}" if export_per_cohort else "")
            )
        return 0
    run_lock_acquired = False
    try:
        acquire_run_lock()
        run_lock_acquired = True
        restored_rejected_files = restore_rejected_frames()
        from pysiril.siril import Siril
        app = None
    except Exception:
        if run_lock_acquired:
            release_run_lock()
        raise
    owns_staging = False
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Run ID: {run_id}")
        if resume_enabled:
            assert checkpoint_state is not None
            input_files, input_manifest_path, resume_identity = validate_resume_input_identity(
                checkpoint_state
            )
            input_frame_count = len(input_files)
            active_run_file_total = input_frame_count
            if checkpoint_state.get('cohorts'):
                cohort_batches = [
                    (
                        cohort.get('id'),
                        [workdir / relative for relative in cohort.get('files', [])],
                    )
                    for cohort in checkpoint_state['cohorts']
                ]
            else:
                cohort_batches = [(None, input_files)]
            report_path = quality_report_path()
            if not report_path.is_file():
                raise RuntimeError(f'Resume quality report is missing: {report_path}')
            quality_report = json.loads(report_path.read_text(encoding='utf-8'))
            quality_report['status'] = 'running'
            quality_report.pop('error', None)
            quality_report['restored_rejected_files'] = sorted(set(
                quality_report.get('restored_rejected_files', []) + restored_rejected_files
            ))
            quality_report['resume_input_identity'] = resume_identity
            input_summary = quality_report.get('input_summary') or summarize_input_frames(input_files)
            cohort_summary = quality_report.get('cohorts') or summarize_frame_cohorts(input_files)
            write_quality_report('running')
            append_journal_event(
                'run_resumed',
                checkpoint=str(run_checkpoint_file_path()),
                completed_substacks=checkpoint_state.get('completed_substacks', []),
                input_identity=resume_identity,
            )
        else:
            input_files = discover_light_files(workdir, (output_dir,))
            random.seed(run_seed)
            if test_frame_count:
                input_files = random.sample(input_files, test_frame_count)
                print(
                    f"[INFO] Test mode: randomly selected {test_frame_count} frame(s) "
                    f"from {len(discover_light_files(workdir, (output_dir,)))} available",
                    flush=True,
                )
            input_frame_count = len(input_files)
            active_run_file_total = input_frame_count
            cohort_batches = partition_frame_cohorts(input_files, cohort_group_fields) if export_per_cohort else [(None, input_files)]
            input_manifest_path = write_input_manifest(input_files, cohort_batches)
            _atomic_write_text(frame_ledger_file_path(), '')
            input_summary = summarize_input_frames(input_files)
            storage_preflight = check_preflight_storage_headroom(input_summary['bytes'])
            cohort_summary = summarize_frame_cohorts(input_files, cohort_group_fields)
            quality_report = initialize_quality_report(input_frame_count)
            quality_report["input_summary"] = input_summary
            quality_report["storage_preflight"] = storage_preflight
            quality_report["cohorts"] = cohort_summary
            quality_report["restored_rejected_files"] = restored_rejected_files
            quality_report["input_manifest"] = str(input_manifest_path)
            quality_report["preflight_warnings"] = debayer_preflight_warnings(
                input_files, bayer_pattern, bayer_orientation
            )
            write_quality_report("running")
            checkpoint_cohorts = []
            if export_per_cohort:
                for cohort_id, cohort_files in cohort_batches:
                    cohort_substack_count = (
                        math.ceil(len(cohort_files) / siril_max_stack_frames)
                        if auto_substacks else min(SubStack_nb, len(cohort_files))
                    )
                    checkpoint_cohorts.append({
                        'id': cohort_id,
                        'files': [path.relative_to(workdir).as_posix() for path in cohort_files],
                        'substack_count': max(1, cohort_substack_count),
                        'groups': plan_group_assignments(
                            cohort_files, workdir, max(1, cohort_substack_count)
                        ),
                        'completed_substacks': [],
                        'state': 'pending',
                        'phase': 'pending',
                    })
            checkpoint_state = {
                'schema_version': 2 if export_per_cohort else 1,
                'state': 'running',
                'global_phase': 'substacks_running',
                'run_id': run_id,
                'workdir': str(workdir),
                'output_dir': str(output_dir),
                'configuration_hash': configuration_hash(),
                'settings': runtime_settings(),
                'seed': run_seed,
                'input_frames': input_frame_count,
                'selected_files': [path.relative_to(workdir).as_posix() for path in input_files],
                'input_manifest': str(input_manifest_path),
                'quality_report': str(quality_report_path()),
                'frame_ledger': str(frame_ledger_file_path()),
                'substack_count': SubStack_nb,
                'groups': {},
                'completed_substacks': [],
                'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            }
            if checkpoint_cohorts:
                checkpoint_state['cohorts'] = checkpoint_cohorts
            write_run_checkpoint(checkpoint_state)
        assert checkpoint_state is not None
        print(f"[INFO] Quality report: {quality_report_path()}")
        print(f"[INFO] Run journal: {journal_path}")
        print(f"[INFO] Reproducibility seed: {run_seed}")
        append_journal_event(
            'run_started',
            workdir=str(workdir),
            output_dir=str(output_dir),
            input_frames=input_frame_count,
            input_manifest=str(input_manifest_path),
            seed=run_seed,
            configuration_hash=quality_report['configuration_hash'],
        )
        print(
            f"[INFO] Acquisition cohorts detected: {len(cohort_summary)}",
            flush=True,
        )
        for warning in quality_report.get("preflight_warnings", []):
            print(f"[WARNING] {warning}", flush=True)
        if not resume_enabled:
            app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
            open_siril_with_recovery(Siril)
            execute_siril("setext fit")
            execute_siril(f"setmem {memory_fraction}")
            execute_siril(f"setcpu {cpu_count}")
        active_cohort_total = len(cohort_batches) if export_per_cohort else None
        if export_per_cohort:
            initialize_cohort_report_collections(quality_report)
        requested_substacks = SubStack_nb
        cohort_file_offset = 0
        completed_substacks = set()
        for cohort_number, (cohort_id, cohort_files) in enumerate(cohort_batches, 1):
            cohort_state = None
            if checkpoint_state.get('cohorts'):
                cohort_state = checkpoint_state['cohorts'][cohort_number - 1]
            if resume_enabled:
                close_siril_before_staging()
            calculated_substacks = (
                math.ceil(len(cohort_files) / siril_max_stack_frames)
                if auto_substacks else min(requested_substacks, len(cohort_files))
            )
            SubStack_nb = int(
                cohort_state.get('substack_count', calculated_substacks)
                if cohort_state is not None else calculated_substacks
            )
            if resume_enabled and cohort_state is not None and cohort_state.get('state') in {'complete', 'skipped'}:
                print(
                    f'[INFO] Resuming: cohort {cohort_number} is already {cohort_state.get("state")}.',
                    flush=True,
                )
                cohort_file_offset += len(cohort_files)
                continue
            completed_substacks = set()
            active_cohort_id = cohort_id
            active_cohort_number = cohort_number if export_per_cohort else None
            active_cohort_file_offset = cohort_file_offset
            active_cohort_file_total = len(cohort_files)
            if export_per_cohort:
                assert active_cohort_total is not None
                active_cohort_progress_base = ((cohort_number - 1) * 100) / active_cohort_total
                active_cohort_progress_span = 100 / active_cohort_total
            cohort_metadata = next(
                (
                    cohort.get('metadata') for cohort in cohort_summary
                    if cohort.get('id') == cohort_id
                ),
                None,
            )
            active_cohort_tag = (
                f"cohort_{cohort_number:03d}_{cohort_filename_tag(cohort_metadata or cohort_files[0], output_dir, run_id)}"
                if export_per_cohort else None
            )
            active_master_path = (
                output_dir / f"master_stack_{run_id}_{active_cohort_tag}.fit"
                if export_per_cohort else None
            )
            print(
                f"[INFO] Processing cohort {cohort_number}/{len(cohort_batches)}: "
                f"{len(cohort_files)} frame(s)" if export_per_cohort else "[INFO] Processing all frames",
                flush=True,
            )
            if resume_enabled:
                if cohort_state is not None:
                    if not cohort_state.get('groups'):
                        if cohort_state.get('completed_substacks'):
                            raise RuntimeError(
                                'Resume checkpoint has no staged group assignments for '
                                'completed substacks.'
                            )
                        cohort_state['groups'] = divide_group_assignments(
                            cohort_state.get('files', []), SubStack_nb
                        )
                        checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                        write_run_checkpoint(checkpoint_state)
                    checkpoint_state['substack_count'] = SubStack_nb
                    checkpoint_state['groups'] = cohort_state.get('groups', {})
                    checkpoint_state['completed_substacks'] = cohort_state.get('completed_substacks', [])
                completed_substacks = prepare_resume_staging(checkpoint_state)
                owns_staging = True
                app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
                open_siril_with_recovery(Siril)
                execute_siril("setext fit")
                execute_siril(f"setmem {memory_fraction}")
                execute_siril(f"setcpu {cpu_count}")
            else:
                (workdir / 'Lights_sorted').mkdir()
                owns_staging = True
                planned_files = None
                if cohort_state is not None:
                    planned_files = [
                        workdir / relative
                        for group_number in range(1, SubStack_nb + 1)
                        for relative in cohort_state['groups'][str(group_number)]
                    ]
                group_files(
                    workdir,
                    planned_files if planned_files is not None else cohort_files,
                    shuffle_files=planned_files is None,
                )
                checkpoint_state['groups'] = {
                    str(group_number): list(manifest.values())
                    for group_number, manifest in read_group_manifests(
                        workdir, range(1, SubStack_nb + 1)
                    ).items()
                }
                if cohort_state is not None:
                    cohort_state['substack_count'] = SubStack_nb
                    cohort_state['groups'] = checkpoint_state['groups']
                    cohort_state['state'] = 'running'
                    cohort_state['phase'] = 'substacks_running'
                checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                write_run_checkpoint(checkpoint_state)
            cohort_skipped = False
            for i in range(1, SubStack_nb + 1):
                if i in completed_substacks:
                    print(f'[INFO] Resuming: substack {i} is already complete.', flush=True)
                    continue
                retries = 0
                report_count = len(quality_report['substacks'])
                while retries <= max_retries:
                    try:
                        if substack(i):
                            break
                        failure = f'Substack {i} produced no stack'
                    except CancellationRequested:
                        raise
                    except NoPlateSolveFramesError as error:
                        if not export_per_cohort:
                            raise RuntimeError(
                                f'Cohort {active_cohort_id or cohort_number} has no plate-solvable frames.'
                            ) from error
                        skipped_cohort = {
                            'number': cohort_number,
                            'cohort': active_cohort_id,
                            'input_frames': len(cohort_files),
                            'successful_plate_solve_frames': 0,
                            'reason_code': 'no_plate_solve_frames',
                            'reason': str(error),
                        }
                        quality_report.setdefault('skipped_cohorts', []).append(skipped_cohort)
                        restore_staged_cohort_sources()
                        owns_staging = False
                        if cohort_state is not None:
                            cohort_state['state'] = 'skipped'
                            cohort_state['phase'] = 'skipped'
                            cohort_state['groups'] = {}
                            cohort_state['completed_substacks'] = []
                            cohort_state['error'] = str(error)
                        checkpoint_state['groups'] = {}
                        checkpoint_state['completed_substacks'] = []
                        checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                        write_run_checkpoint(checkpoint_state)
                        write_quality_report('running')
                        append_journal_event(
                            'cohort_skipped',
                            cohort=active_cohort_id,
                            cohort_number=cohort_number,
                            input_frames=len(cohort_files),
                            reason_code='no_plate_solve_frames',
                        )
                        print(
                            f'[WARNING] Skipping cohort {cohort_number}: no frames successfully plate-solved.',
                            flush=True,
                        )
                        active_cohort_id = None
                        active_cohort_number = None
                        active_cohort_tag = None
                        active_cohort_file_total = None
                        active_cohort_progress_base = 0.0
                        active_cohort_progress_span = 100.0
                        cohort_file_offset += len(cohort_files)
                        cohort_skipped = True
                        break
                    except SirilCommandError as error:
                        failure = str(error)
                    retries += 1
                    quality_report.setdefault('substack_attempt_failures', []).append({
                        'substack': i, 'attempt': retries, 'error': failure,
                        'cohort': active_cohort_id,
                    })
                    del quality_report['substacks'][report_count:]
                    write_quality_report('running')
                    if retries > max_retries:
                        raise RuntimeError(f'Substack {i} failed after {max_retries + 1} attempts: {failure}')
                    print(f'[WARNING] Retrying substack {i} after attempt {retries}: {failure}', flush=True)
                    app.Close()
                    app = None
                    group_path = workdir / 'Lights_sorted' / f'group_{i}'
                    remove_tree(group_path / 'process')
                    (group_path / 'process').mkdir()
                    for artifact in group_path.glob(f'substack_{i}*.fit'):
                        artifact.unlink()
                    check_cancellation()
                    app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
                    open_siril_with_recovery(Siril)
                    execute_siril("setext fit")
                    execute_siril(f"setmem {memory_fraction}")
                    execute_siril(f"setcpu {cpu_count}")
                cleanup(i)
                latest_substack = quality_report.get('substacks', [])[-1:] if quality_report else []
                moved_rejects = move_frame_selection_rejects(
                    latest_substack[0].get('discarded_frames', []) if latest_substack else []
                )
                if moved_rejects:
                    quality_report['rejected_files'].extend(moved_rejects)
                    write_quality_report('running')
                completed_substacks.add(i)
                checkpoint_state['completed_substacks'] = sorted(completed_substacks)
                if cohort_state is not None:
                    cohort_state['completed_substacks'] = sorted(completed_substacks)
                checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                write_run_checkpoint(checkpoint_state)
                check_runtime_disk_headroom()
            if cohort_skipped:
                continue
            reusable_master = None
            if (
                resume_enabled
                and cohort_state is not None
                and cohort_state.get('phase') in FINALIZATION_PHASES
            ):
                reusable_master = cohort_report_record(
                    quality_report, 'masters', active_cohort_id
                )
                if not reusable_report_artifact(
                    reusable_master, 'path', master_stack_path(), 'Master stack artifact'
                ):
                    reusable_master = None
            if reusable_master is None:
                masterstack(SubStack_nb)
                if export_per_cohort:
                    master = quality_report.pop('master')
                    master['cohort'] = active_cohort_id
                    upsert_cohort_report_record(quality_report, 'masters', master)
                if cohort_state is not None:
                    cohort_state['state'] = 'running'
                    cohort_state['phase'] = 'master_written'
                    checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                    write_quality_report('running')
                    write_run_checkpoint(checkpoint_state)
            else:
                print(
                    f'[INFO] Resuming: reusing validated master for cohort {cohort_number}.',
                    flush=True,
                )
            if coverage_map_enabled:
                if reusable_master is not None:
                    reusable_coverage = cohort_report_record(
                        quality_report, 'coverages', active_cohort_id
                    )
                    coverage_path = (
                        reusable_coverage.get('integration_time_path')
                        if reusable_coverage else None
                    )
                    expected_coverage_path = output_dir / (
                        f'integration_time_map_{run_id}_{active_cohort_tag}.fit'
                    )
                    if not coverage_path or not reusable_report_artifact(
                        reusable_coverage,
                        'integration_time_path',
                        expected_coverage_path,
                        'Integration-time map artifact',
                    ):
                        raise RuntimeError(
                            f'Resume master exists for cohort {cohort_number}, but its '
                            'integration-time map is missing or invalid.'
                        )
                    assert reusable_coverage is not None
                    quality_report['coverage'] = dict(reusable_coverage)
                if export_per_cohort and quality_report.get('coverage'):
                    upsert_cohort_report_record(
                        quality_report, 'coverages', quality_report['coverage']
                    )
                if cohort_state is not None:
                    cohort_state['phase'] = 'maps_written'
                    checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                    write_quality_report('running')
                    write_run_checkpoint(checkpoint_state)
                if auto_crop_enabled and cohort_state is not None:
                    cohort_state['phase'] = 'crop_pending'
                    checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                    write_run_checkpoint(checkpoint_state)
                create_cropped_master()
                if export_per_cohort and quality_report.get('coverage'):
                    upsert_cohort_report_record(
                        quality_report, 'coverages', quality_report['coverage']
                    )
            check_cancellation()
            if not debug:
                final_cleanup()
            if cohort_state is not None:
                cohort_state['state'] = 'complete'
                cohort_state['phase'] = 'complete'
                cohort_state['completed_substacks'] = sorted(completed_substacks)
                checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                write_run_checkpoint(checkpoint_state)
            active_cohort_id = None
            active_cohort_number = None
            active_cohort_tag = None
            active_cohort_progress_base = 0.0
            active_cohort_progress_span = 100.0
            cohort_file_offset += len(cohort_files)
            owns_staging = False
        app.Close()
        app = None
        active_master_path = None
        active_cohort_total = None
        active_run_file_total = None
        active_cohort_file_offset = 0
        active_cohort_file_total = None
        report_progress(100, 100, "Complete")
        finalize_integration_report(input_summary)
        quality_report['artifact_inventory'] = build_artifact_inventory(quality_report)
        quality_report['verification_path'] = str(
            verification_artifact_path(quality_report_path())
        )
        write_quality_report("complete")
        append_journal_event('run_finished', status='complete')
        verification_exit_code = 0
        try:
            verification_path, verification = write_verification_artifact(quality_report_path())
            quality_report['verification_status'] = verification['status']
            write_quality_report('complete')
            print(
                f"[INFO] Run verification: {verification['status']} "
                f"({verification['counts']['PASS']} pass, "
                f"{verification['counts']['WARN']} warn, {verification['counts']['FAIL']} fail)",
                flush=True,
            )
            if verification['status'] == 'FAIL':
                verification_exit_code = 1
        except Exception as verification_error:
            verification_exit_code = 1
            quality_report['verification_status'] = 'ERROR'
            quality_report['verification_error'] = str(verification_error)
            write_quality_report('complete')
            append_journal_event('verification_failed', error=str(verification_error))
            print(f"[WARNING] Automatic run verification failed: {verification_error}", flush=True)
        remove_run_checkpoint()
        return verification_exit_code
    except Exception as error:
        cancelled = isinstance(error, CancellationRequested)
        failure_context = build_failure_context(error)
        if quality_report is not None:
            quality_report['failure'] = failure_context
        if cancelled:
            print("[INFO] Cancellation requested; restoring source files...", flush=True)
        prepare_siril_for_cleanup()
        if app is not None:
            try:
                if not siril_unresponsive:
                    app.Close()
            except Exception:
                pass
            app = None
        try:
            lights_sorted = workdir / "Lights_sorted"
            if owns_staging and lights_sorted.is_dir():
                for i in range(1, SubStack_nb + 1):
                    if (lights_sorted / f"group_{i}").is_dir():
                        cleanup(i, restore_sources=True)
                if not debug and not run_checkpoint_file_path().is_file():
                    remove_tree(lights_sorted)
            if owns_staging and cancelled and not debug:
                remove_tree(workdir / "substacks")
        except Exception as rollback_error:
            write_quality_report("failed", f"{error}; source rollback failed: {rollback_error}")
            raise RuntimeError(f"{error}\nSource rollback also failed: {rollback_error}") from error
        write_quality_report("cancelled" if cancelled else "failed", error)
        if checkpoint_state is not None:
            checkpoint_state['state'] = 'interrupted'
            checkpoint_state['global_phase'] = 'interrupted'
            checkpoint_state['error'] = str(error)
            checkpoint_state['failure'] = failure_context
            checkpoint_state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            write_run_checkpoint(checkpoint_state)
        append_journal_event(
            'run_finished',
            status='cancelled' if cancelled else 'failed',
            error=str(error),
            failure=failure_context,
        )
        if cancelled:
            print("[INFO] Cancellation complete; source files restored.", flush=True)
            return 2
        raise
    finally:
        if cancel_file is not None:
            cancel_file.unlink(missing_ok=True)
        if run_lock_acquired:
            release_run_lock()


def run_crop_workbench(arguments):
    required = (
        arguments.crop_workbench_master,
        arguments.crop_workbench_coverage,
        arguments.crop_workbench_percent,
        arguments.crop_workbench_output,
    )
    if any(value is None for value in required):
        raise ValueError(
            'Cropping workbench requires master, integration-time map, percentage, and output.'
        )
    plan = crop_master_with_siril(
        arguments.crop_workbench_master.expanduser().resolve(),
        arguments.crop_workbench_coverage.expanduser().resolve(),
        arguments.crop_workbench_percent,
        arguments.crop_workbench_output.expanduser().resolve(),
        arguments.siril_exe.expanduser().resolve(),
    )
    print(
        f'[CROP] {plan["master_width"]}x{plan["master_height"]} master -> '
        f'{plan["cropped_width"]}x{plan["cropped_height"]} '
        f'({plan["area_percent"]:.3f}% of pixels)',
        flush=True,
    )
    print(f'[CROP] Output: {plan["output_path"]}', flush=True)
    return 0


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.bundle_report:
            bundle = create_run_bundle(arguments.bundle_report, arguments.bundle_output)
            print(f"[BUNDLE] {bundle}", flush=True)
            return 0
        if arguments.html_report:
            html_path = export_html_run_report(arguments.html_report, arguments.html_output)
            print(f"[HTML] {html_path}", flush=True)
            return 0
        if (
            arguments.checkpoint_status
            or arguments.discard_checkpoint
            or arguments.run_lock_status
            or arguments.break_run_lock
            or arguments.cleanup_review
        ):
            selected_output = (arguments.output_dir or (arguments.workdir / "Siril Mosaic Output")).expanduser().resolve()
            if arguments.checkpoint_status or arguments.discard_checkpoint:
                checkpoint = inspect_checkpoint(selected_output, arguments.workdir)
                print(json.dumps(checkpoint, indent=2), flush=True)
                if arguments.discard_checkpoint and checkpoint['status'] != 'NONE':
                    Path(checkpoint['path']).unlink(missing_ok=True)
                    print(f"[CHECKPOINT] Discarded {checkpoint['path']}", flush=True)
            if arguments.run_lock_status or arguments.break_run_lock:
                lock = inspect_run_lock(selected_output)
                print(json.dumps(lock, indent=2), flush=True)
                if arguments.break_run_lock and lock['status'] != 'NONE':
                    break_run_lock(selected_output)
                    print(f"[RUN LOCK] Discarded {lock['path']}", flush=True)
            if arguments.cleanup_review:
                print(json.dumps(stale_artifacts(arguments.workdir, selected_output), indent=2), flush=True)
            return 0
        if arguments.replay_ledger:
            replay = replay_frame_ledger(arguments.replay_ledger, {
                'background': arguments.replay_background,
                'roundness': arguments.replay_roundness,
                'fwhm': arguments.replay_fwhm,
                'stars': arguments.replay_stars,
            })
            print(json.dumps(replay, indent=2), flush=True)
            return 0
        if arguments.integrity_scan or arguments.deep_integrity_scan:
            scan = scan_input_integrity(
                arguments.workdir,
                arguments.output_dir,
                bayer_pattern=arguments.bayer_pattern,
                orientation=arguments.bayer_orientation,
                drizzle=arguments.drizzle,
                deep_payload=arguments.deep_integrity_scan,
            )
            if arguments.integrity_scan_json:
                destination = arguments.integrity_scan_json.expanduser().resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix('.tmp')
                _atomic_write_text(destination, json.dumps(scan, indent=2))
            print(
                f"[INTEGRITY] {scan['status']} "
                f"({scan['counts']['PASS']} pass, {scan['counts']['WARN']} warn, "
                f"{scan['counts']['FAIL']} fail; {scan['input_count']} input(s))",
                flush=True,
            )
            for check in scan['checks']:
                print(f"[INTEGRITY] {check['status']}: {check['name']} - {check['detail']}", flush=True)
            return 1 if scan['status'] == 'FAIL' else 0
        if arguments.verify_run:
            verification = verify_run(arguments.verify_run)
            print(
                f"[VERIFY] {verification['status']} "
                f"({verification['counts']['PASS']} pass, "
                f"{verification['counts']['WARN']} warn, "
                f"{verification['counts']['FAIL']} fail)",
                flush=True,
            )
            print(f"[VERIFY] Artifact: {verification_artifact_path(arguments.verify_run)}", flush=True)
            for check in verification['checks']:
                print(
                    f"[VERIFY] {check['status']}: {check['name']} - {check['detail']}",
                    flush=True,
                )
            return 1 if verification['status'] == 'FAIL' else 0
        if arguments.crop_workbench:
            return run_crop_workbench(arguments)
        return run_pipeline(arguments)
    except Exception as error:
        print(f"\n**** ERROR *** {error}\n", flush=True)
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 1:
        from sirilmosaic_gui import launch_gui
        launch_gui()
    else:
        raise SystemExit(main())
