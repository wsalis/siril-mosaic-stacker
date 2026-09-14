import argparse
import json
import sys
import os
import re
import time
import random
import shutil
import struct
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Literal, overload
import math
import numpy as np

workdir = Path(r"G:\Rosette")
output_dir = workdir / "Siril Mosaic Output"
siril_exe = Path(r"C:\Program Files\Siril\bin\siril.exe")
cancel_file = None
app = None
run_id = ''
quality_report = None
SUPPORTED_FRAME_SUFFIXES = {'.fit', '.fits', '.fts', '.xisf'}
GENERATED_DIRECTORIES = {'lights_sorted', 'substacks', '__pycache__'}
GENERATED_FILE_PREFIXES = ('master_stack', 'substack_')
SOURCE_MANIFEST = 'source_manifest.json'
STACKING_WEIGHTS = {'noise', 'wfwhm', 'nbstars', 'nbstack'}
SIRIL_MAX_STACK_FRAMES = 8192
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
drizzle_enabled = True
bayer_pattern = DEFAULT_BAYER_PATTERN
bayer_orientation = DEFAULT_BAYER_ORIENTATION
cosmetic_correction = True
cosmetic_cold_sigma = '3.0'
cosmetic_hot_sigma = '3.0'
overlap_normalization = True
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
fast_normalization = False
catalog = 'localgaia'
memory_fraction = '0.8'
cpu_count = 28
max_retries = 5
skip_failed_frames = False
auto_substacks = False
mosaic_aware_star_count = False
test_frame_count = 0
coverage_map_enabled = False
auto_crop_enabled = False
auto_crop_coverage_percent = 50
sky_quality_percent = 100
# ==============================================

def check_cancellation():
    if cancel_file is not None and cancel_file.is_file():
        raise CancellationRequested("Cancellation requested by user.")


def report_progress(start, end, label):
    print(f"[PROGRESS] {start:.2f} {end:.2f} {label}", flush=True)

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


def summarize_frame_cohorts(files):
    cohorts = {}
    for path in files:
        key = frame_cohort_key(path)
        cohort_id = ' | '.join(
            f'{name}={key[name]}'
            for name in ('camera', 'exposure_seconds', 'gain', 'filter', 'dimensions')
        )
        entry = cohorts.setdefault(cohort_id, {'id': cohort_id, 'count': 0, 'metadata': key})
        entry['count'] += 1
    return sorted(cohorts.values(), key=lambda item: (-item['count'], item['id']))


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


def move_replace(source, destination):
    """Replace an existing generated artifact while moving a new one into place."""
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    shutil.move(str(source), str(destination))


def group_files(root_folder, files=None):
    check_cancellation()
    report_progress(0, 3, "Staging source frames")
    source_folder = root_folder
    destination_folder = root_folder / "Lights_sorted"
    if destination_folder.exists() and any(destination_folder.iterdir()):
        raise RuntimeError("Lights_sorted already exists and is not empty.")
    destination_folder.mkdir(exist_ok=True)
    files = list(files) if files is not None else discover_light_files(source_folder, (output_dir,))
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
        (group_folder / SOURCE_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        for file, staging_name in staged_files:
            check_cancellation()
            shutil.move(str(file), str(lights_folder / staging_name))
        check_cancellation()
        print(f"[INFO] Moved {count} files to {lights_folder}")
    report_progress(3, 3, "Source staging complete")

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


class SirilCommandError(RuntimeError):
    pass


def execute_siril(command):
    check_cancellation()
    if app is None:
        raise RuntimeError("Siril is not open.")
    started = time.perf_counter()
    response = []
    status = 'failed'
    try:
        succeeded = app.Execute(command) is not False
        get_data = getattr(app, "GetData", None)
        data = get_data() if callable(get_data) else []
        response = [str(line) for line in data] if isinstance(data, (list, tuple)) else []
        check_cancellation()
        if not succeeded:
            details = "\n".join(line for line in response if line.strip())
            raise RuntimeError(f"Siril command failed: {command}\n{details}")
        status = 'complete'
        return response
    except CancellationRequested:
        status = 'cancelled'
        raise
    except Exception as error:
        raise SirilCommandError(str(error)) from error
    finally:
        elapsed = round(time.perf_counter() - started, 3)
        if quality_report is not None:
            quality_report.setdefault('commands', []).append({
                'command': command, 'seconds': elapsed, 'status': status,
                'substack': quality_report.get('active_substack'),
            })
            if status != 'complete':
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
    return output_dir / f"master_stack_{run_id}.fit"


def _run_artifact_name(filename, prefix):
    return filename.replace(f"{prefix}_", f"{prefix}_{run_id}_", 1)


def quality_report_path():
    return output_dir / f"quality_report_{run_id}.json"


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
        if stages['registered']:
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
                reason = 'Excluded during registration; individual filter or export cause unavailable'
        discarded_frame_reports.append({
            'image_number': image_number,
            'file': manifest.get(source.name, source.name),
            'exposure_seconds': exposure if math.isfinite(exposure) and exposure > 0 else None,
            'status': status,
            'reason': reason,
            'reason_code': reason_code,
            'candidate_filters': failed_filters if reason_code == 'registration_excluded' else [],
            'stages': stages,
        })
    stage_exposure_seconds['stacked'] = (
        stage_exposure_seconds['registered'] if stack_identity_known and stacked_count is not None else None
    )
    return {
        'discarded_frames': discarded_frame_reports,
        'discarded_frame_count': len(discarded_frame_reports),
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
    return {
        "report_schema_version": 2,
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
        "settings": {
            "test_frame_count": test_frame_count,
            "coverage_map": coverage_map_enabled,
            "auto_crop_master": auto_crop_enabled,
            "auto_crop_coverage_percent": auto_crop_coverage_percent,
            "substacks": SubStack_nb,
            "auto_substacks": auto_substacks,
            "max_frames_per_substack": SIRIL_MAX_STACK_FRAMES,
            "drizzle": drizzle_enabled,
            "drizzle_scale": drizzle_scale,
            "pixel_fraction": pix_frac,
            "bayer_pattern": bayer_pattern,
            "bayer_orientation": bayer_orientation,
            "cosmetic_correction": cosmetic_correction,
            "cosmetic_cold_sigma": cosmetic_cold_sigma,
            "cosmetic_hot_sigma": cosmetic_hot_sigma,
            "overlap_normalization": overlap_normalization,
            "filter_background": filter_bkg,
            "filter_stars": filter_nbstars,
            "filter_roundness": filter_round,
            "filter_fwhm": filter_fwhm,
            "adaptive_quality_filtering": adaptive_quality_filtering,
            "quality_filter_sigma": quality_filter_sigma,
            "background_method": background_method,
            "background_samples": background_samples,
            "background_tolerance": background_tolerance,
            "stacking_weight": stacking_weight,
            "rejection_low": rej_low,
            "rejection_high": rej_high,
            "fast_normalization": fast_normalization,
            "skip_failed_frames": skip_failed_frames,
            "mosaic_aware_star_count": mosaic_aware_star_count,
            "sky_quality_percent": sky_quality_percent,
        },
        "substacks": [],
    }


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


def _round_to_int(value):
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _write_coverage_fits(path, coverage, unit='frame'):
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
        'END',
    ]
    header = b''.join(card.ljust(80).encode('ascii') for card in cards)
    header = header.ljust((len(header) + 2879) // 2880 * 2880, b' ')
    data = np.asarray(coverage, dtype='>f4' if floating else '>i4').tobytes(order='C')
    data = data.ljust((len(data) + 2879) // 2880 * 2880, b'\0')
    Path(path).write_bytes(header + data)


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


def generate_coverage_map(process_folder, sequence_name):
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
        if quality_report is not None:
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
        if quality_report is not None:
            quality_report['coverage'] = report
        print(f"[WARNING] {report['reason']} Skipping coverage and autocrop.", flush=True)
        return None
    coverage = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    for (path, record), exposure in zip(placements, exposures):
        array = _read_fits_array(path)
        valid = np.isfinite(array) & (np.abs(array) > 1e-7)
        if array.ndim == 3:
            valid = np.any(valid, axis=0)
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
    coverage_path = output_dir / f'coverage_map_{run_id}.fit'
    integration_path = output_dir / f'integration_time_map_{run_id}.fit'
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
        'integration_basis': 'sum of registered EXPTIME over finite nonzero pixels before per-pixel rejection and stacking weights',
        'width': int(coverage.shape[1]),
        'height': int(coverage.shape[0]),
        'canvas_source': 'Siril maximize framing',
    }
    if auto_crop_enabled:
        bounds = coverage_crop_bounds(coverage, auto_crop_coverage_percent)
        if bounds:
            report['crop_threshold'] = bounds.pop('threshold')
            report['crop_reference_coverage'] = bounds.pop('reference_coverage')
            report['crop_method'] = 'largest rectangle above percentage of median positive integration time'
            report['crop_coverage_percent'] = auto_crop_coverage_percent
            report['crop_bounds'] = bounds
            report['crop_coordinate_system'] = 'FITS array rows'
    if quality_report is not None:
        quality_report['coverage'] = report
    print(
        f"[INFO] Coverage map: {len(placements)} registered frames, "
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
    exact_stacked_exposure = bool(substacks) and all(
        'stage_exposure_seconds' in substack
        and substack['stage_exposure_seconds'].get('stacked') is not None
        for substack in quality_report.get('substacks', [])
    )
    master_count = quality_report.get('master', {}).get('stacked_frames')
    if (len(substacks) > 1 and master_count != len(substacks)) or (
        master_count is not None and master_count != len(substacks)
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
    temporary.write_text(
        json.dumps(quality_report, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


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

    def phase(start_fraction, end_fraction, label):
        report_progress(
            progress_start + (substack_span * start_fraction),
            progress_start + (substack_span * end_fraction),
            f"Substack {group_num}/{SubStack_nb}: {label}",
        )

    print(f"[INFO] Processing Substack {group_num} ({len(files)} frames)")
    if quality_report is not None:
        quality_report['active_substack'] = group_num
    phase(0, 0.02, "Preparing")
    configure_debayer()
    execute_siril(f"cd {siril_path(lights_folder)}")
    execute_siril("set32bits")
    phase(0.02, 0.10, "Converting frames")
    execute_siril("convert light -out=../process")
    execute_siril(f"cd {siril_path(process_folder)}")
    input_sequence = "light"
    if cosmetic_correction:
        phase(0.10, 0.16, "Correcting cosmetic defects")
        execute_siril(
            f"seqfind_cosme_cfa light {cosmetic_cold_sigma} "
            f"{cosmetic_hot_sigma} -prefix=cc_"
        )
        input_sequence = "cc_light"
    phase(0.16, 0.28, "Calibrating and debayering")
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
    if background_method == 'off':
        background_sequence = processed_sequence
        phase(0.28, 0.40, "Background extraction disabled")
    else:
        background_sequence = f"bkg_{processed_sequence}"
        phase(0.28, 0.40, "Removing gradients")
        execute_siril(
            f"seqsubsky {processed_sequence} {BACKGROUND_METHODS[background_method]} "
            f"-samples={background_samples} -tolerance={background_tolerance}"
        )
    phase(0.40, 0.58, "Plate solving")
    plate_solve_response = execute_siril(
        f"seqplatesolve {background_sequence} "
        f"-order=3 -nocrop -nocache -force -catalog={cat}"
    )

    # Registration with Drizzle (Lanczos3 droplet)
    selection_filters = effective_selection_filters()
    print(f"[INFO] Effective frame filters: {selection_filters}", flush=True)
    registration_command = f"seqapplyreg {background_sequence} " + ' '.join(
        f'-filter-{name}={value}' for name, value in selection_filters.items()
    ) + ' -framing=max'
    if drizzle_enabled:
        registration_command += (
            f" -drizzle -scale={drizzle_scale} "
            f"-pixfrac={pix_frac} -kernel=lanczos3"
        )
    phase(0.58, 0.78, "Registering frames")
    registration_response = execute_siril(registration_command)
    if SubStack_nb == 1:
        generate_coverage_map(process_folder, f"r_{background_sequence}")

    stack_start = time.time()
    # Stacking with parameterized Sigma and Feathering
    phase(0.78, 0.98, "Stacking frames")
    stack_response = execute_siril(
        f"stack r_{background_sequence} rej linear {rej_low} {rej_high} "
        f"-weight={stacking_weight} "
        f"-norm=addscale{' -overlap_norm' if overlap_normalization else ''} "
        f"-feather={feather_val} "
        f"-rgb_equal -output_norm -rejmaps -maximize"
        f"{' -fastnorm' if fast_normalization else ''} -out=../substack_{group_num}"
    )

    stack_time = time.time() - stack_start
    print(f"[INFO] Substack {group_num} stack time: {stack_time:.1f}s")
    if quality_report is not None:
        substack_report = parse_registration_quality(registration_response, len(files))
        substack_report["number"] = group_num
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
        substack_report.update(frame_summary)
        substack_report["stack_seconds"] = round(stack_time, 1)
        quality_report["substacks"].append(substack_report)
        print(f"[INFO] Substack {group_num} stage counts: {substack_report['stage_counts']}", flush=True)
        write_quality_report("running")
    phase(0.98, 1, "Finalizing substack")
    execute_siril(f"cd {siril_path(group_path)}")
    execute_siril(f"mirrorx_single substack_{group_num}")
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
            master_name = rejection_map.name.replace("substack_1_", f"master_stack_{run_id}_", 1)
            shutil.copy2(rejection_map, output_dir / master_name)
        if quality_report is not None:
            quality_report["master"] = {
                "method": "single substack copy",
                "path": str(master_stack_path()),
            }
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
    execute_siril("register pp_light -2pass -interp=lanczos4")
    report_progress(95.5, 96, "Applying master registration")
    execute_siril("seqapplyreg pp_light")
    if SubStack_nb > 1 and coverage_map_enabled and quality_report is not None:
        quality_report['coverage'] = {
            'status': 'unavailable',
            'reason': 'Coverage maps for multiple substacks are not combined yet.',
            'frames_seen': 0,
        }

    # Master Stacking with full APP-level parameters
    report_progress(96, 97.5, "Integrating master stack")
    use_master_rejection = SubStack_nb >= 4
    master_rejection = f"rej linear {rej_low} {rej_high}" if use_master_rejection else "rej none"
    master_response = execute_siril(
        f"stack r_pp_light {master_rejection} "
        f"-weight=nbstack "
        f"-norm=addscale{' -overlap_norm' if overlap_normalization else ''} "
        f"-feather={feather_val} "
        f"-rgb_equal -output_norm{' -rejmaps' if use_master_rejection else ''} -maximize"
        f"{' -fastnorm' if fast_normalization else ''} "
        f"{siril_path_option('out', master_stack_path().with_suffix(''))}"
    )
    report_progress(97.5, 98, "Finalizing master stack")
    execute_siril(f"cd {siril_path(output_dir)}")
    execute_siril(f"mirrorx_single {master_stack_path().stem}")
    if quality_report is not None:
        quality_report["master"] = parse_stack_quality(master_response)
        quality_report["master"]["method"] = "registered substack integration"
        quality_report["master"]["path"] = str(master_stack_path())
        quality_report["master"]["weight"] = "nbstack"
        quality_report["master"]["rejection"] = "linear" if use_master_rejection else "none"

def final_cleanup():
    report_progress(98, 100, "Writing final outputs")
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
    cropped_path = output_dir / f"master_stack_{run_id}_cropped.fit"
    execute_siril(f"cd {siril_path(output_dir)}")
    execute_siril(f"load {siril_path(master_stack_path().with_suffix(''))}")
    execute_siril(
        f"boxselect {selection['x']} {selection['y']} "
        f"{selection['width']} {selection['height']}"
    )
    execute_siril("crop")
    execute_siril(f"save {siril_path(cropped_path.with_suffix(''))}")
    quality_report['coverage']['cropped_master_path'] = str(cropped_path)
    print(f"[INFO] Coverage-cropped master: {cropped_path}", flush=True)

def build_parser():
    parser = argparse.ArgumentParser(description="Build a randomized multi-stage Siril mosaic stack.")
    parser.add_argument("--workdir", type=Path, default=workdir)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--siril-exe", type=Path, default=siril_exe)
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
        "--auto-crop-coverage-percent", type=int, default=50,
        help="minimum crop integration time as a percentage of the nonblank-pixel median; lower keeps more field",
    )
    parser.add_argument("--substacks", type=int, default=SubStack_nb)
    parser.add_argument(
        "--auto-substacks",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="calculate the substack count to keep each sequence at or below 8192 frames",
    )
    parser.add_argument("--drizzle", action=argparse.BooleanOptionalAction, default=drizzle_enabled)
    parser.add_argument("--drizzle-scale", default=drizzle_scale)
    parser.add_argument("--pixel-fraction", default=pix_frac)
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
        "--fast-normalization",
        action=argparse.BooleanOptionalAction,
        default=fast_normalization,
    )
    parser.add_argument("--catalog", default=catalog)
    parser.add_argument("--memory", default=memory_fraction)
    parser.add_argument("--cpus", type=int, default=cpu_count)
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
    return parser


def configure(arguments):
    global workdir, output_dir, siril_exe, run_id, quality_report
    global cancel_file
    global SubStack_nb, auto_substacks, drizzle_enabled, drizzle_scale, pix_frac
    global bayer_pattern, bayer_orientation, cosmetic_correction
    global cosmetic_cold_sigma, cosmetic_hot_sigma
    global overlap_normalization, adaptive_quality_filtering, quality_filter_sigma
    global background_method, background_samples, background_tolerance
    global filter_bkg, filter_nbstars, filter_round, filter_fwhm
    global stacking_weight, feather_val, rej_low, rej_high, fast_normalization, catalog
    global memory_fraction, cpu_count, max_retries, skip_failed_frames
    global mosaic_aware_star_count, test_frame_count, coverage_map_enabled
    global auto_crop_enabled, auto_crop_coverage_percent, debug
    global sky_quality_percent

    workdir = arguments.workdir.expanduser().resolve()
    selected_output = arguments.output_dir or (workdir / "Siril Mosaic Output")
    output_dir = selected_output.expanduser().resolve()
    run_id = choose_run_id(output_dir)
    quality_report = None
    siril_exe = arguments.siril_exe.expanduser().resolve()
    cancel_file = arguments.cancel_file.expanduser().resolve() if arguments.cancel_file else None
    SubStack_nb = arguments.substacks
    auto_substacks = arguments.auto_substacks
    drizzle_enabled = arguments.drizzle
    drizzle_scale = str(arguments.drizzle_scale)
    pix_frac = str(arguments.pixel_fraction)
    bayer_pattern = arguments.bayer_pattern
    bayer_orientation = arguments.bayer_orientation
    cosmetic_correction = arguments.cosmetic_correction
    cosmetic_cold_sigma = str(arguments.cosmetic_cold_sigma)
    cosmetic_hot_sigma = str(arguments.cosmetic_hot_sigma)
    overlap_normalization = arguments.overlap_normalization
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
    fast_normalization = arguments.fast_normalization
    catalog = arguments.catalog
    memory_fraction = str(arguments.memory)
    cpu_count = arguments.cpus
    max_retries = arguments.retries
    skip_failed_frames = arguments.skip_failed_frames
    mosaic_aware_star_count = arguments.mosaic_aware_star_count
    test_frame_count = arguments.test_frame_count
    coverage_map_enabled = arguments.coverage_map
    auto_crop_enabled = arguments.auto_crop_master
    auto_crop_coverage_percent = arguments.auto_crop_coverage_percent
    sky_quality_percent = arguments.sky_quality_percent
    debug = arguments.debug


def validate_parameters():
    global SubStack_nb
    if not workdir.is_dir():
        raise ValueError("Input folder does not exist.")
    if output_dir == workdir:
        raise ValueError("Output folder must be different from the input folder.")
    for transient_folder in (workdir / "Lights_sorted", workdir / "substacks"):
        if _is_within(output_dir, transient_folder):
            raise ValueError("Output folder cannot be inside a temporary processing folder.")
    light_count = len(discover_light_files(workdir, (output_dir,)))
    if light_count == 0:
        raise ValueError("Input folder contains no supported FITS or XISF files.")
    if not 0 <= test_frame_count <= light_count:
        raise ValueError("Test frame count must be 0 or no more than the input frame count.")
    if auto_crop_enabled and not coverage_map_enabled:
        raise ValueError("Auto-crop master requires the coverage map.")
    if not 1 <= auto_crop_coverage_percent <= 100:
        raise ValueError("Auto-crop coverage percentage must be from 1 to 100.")
    effective_count = test_frame_count or light_count
    if not siril_exe.is_file():
        raise ValueError("Siril executable does not exist.")
    if auto_substacks:
        SubStack_nb = math.ceil(effective_count / SIRIL_MAX_STACK_FRAMES)
        print(
            f"[INFO] Auto substacks: {effective_count} frame(s) -> "
            f"{SubStack_nb} substack(s), max {SIRIL_MAX_STACK_FRAMES} frames each",
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
    if sky_quality_percent != 100:
        raise ValueError("Sky quality percentage filtering is unsupported; use percentage or adaptive frame filters.")
    effective_selection_filters()
    if background_samples < 1:
        raise ValueError("Background samples must be at least 1.")
    if float(background_tolerance) <= 0:
        raise ValueError("Background tolerance must be greater than 0.")
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
    if stacking_weight not in STACKING_WEIGHTS:
        raise ValueError(f"Weight must be one of: {', '.join(sorted(STACKING_WEIGHTS))}.")
    if float(feather_val) < 0:
        raise ValueError("Feather cannot be negative.")


def run_pipeline(arguments):
    global app, quality_report
    configure(arguments)
    validate_parameters()
    from pysiril.siril import Siril

    app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
    owns_staging = False
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Run ID: {run_id}")
        input_files = discover_light_files(workdir, (output_dir,))
        if test_frame_count:
            input_files = random.sample(input_files, test_frame_count)
            print(
                f"[INFO] Test mode: randomly selected {test_frame_count} frame(s) "
                f"from {len(discover_light_files(workdir, (output_dir,)))} available",
                flush=True,
            )
        input_frame_count = len(input_files)
        input_summary = summarize_input_frames(input_files)
        cohort_summary = summarize_frame_cohorts(input_files)
        quality_report = initialize_quality_report(input_frame_count)
        quality_report["input_summary"] = input_summary
        quality_report["cohorts"] = cohort_summary
        quality_report["preflight_warnings"] = debayer_preflight_warnings(
            input_files, bayer_pattern, bayer_orientation
        )
        if SubStack_nb > 1 and coverage_map_enabled:
            quality_report['preflight_warnings'].append(
                'Coverage maps and autocrop currently require one substack; they will be unavailable.'
            )
        write_quality_report("running")
        print(f"[INFO] Quality report: {quality_report_path()}")
        print(
            f"[INFO] Acquisition cohorts detected: {len(cohort_summary)}",
            flush=True,
        )
        for warning in quality_report["preflight_warnings"]:
            print(f"[WARNING] {warning}", flush=True)
        (workdir / 'Lights_sorted').mkdir()
        owns_staging = True
        group_files(workdir, input_files)
        if app.Open() is False:
            raise RuntimeError("Siril failed to open its command pipe.")
        execute_siril("setext fit")
        execute_siril(f"setmem {memory_fraction}")
        execute_siril(f"setcpu {cpu_count}")
        for i in range(1, SubStack_nb + 1):
            retries = 0
            report_count = len(quality_report['substacks'])
            while retries <= max_retries:
                try:
                    if substack(i):
                        break
                    failure = f'Substack {i} produced no stack'
                except CancellationRequested:
                    raise
                except SirilCommandError as error:
                    failure = str(error)
                retries += 1
                quality_report.setdefault('substack_attempt_failures', []).append({
                    'substack': i, 'attempt': retries, 'error': failure,
                })
                del quality_report['substacks'][report_count:]
                write_quality_report('running')
                if retries > max_retries:
                    raise RuntimeError(f"Substack {i} failed after {max_retries + 1} attempts: {failure}")
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
                if app.Open() is False:
                    raise RuntimeError("Siril failed to reopen its command pipe.")
                execute_siril("setext fit")
                execute_siril(f"setmem {memory_fraction}")
                execute_siril(f"setcpu {cpu_count}")
            cleanup(i)
        masterstack(SubStack_nb)
        create_cropped_master()
        app.Close()
        app = None
        check_cancellation()
        if not debug:
            final_cleanup()
        finalize_integration_report(input_summary)
        write_quality_report("complete")
        return 0
    except Exception as error:
        cancelled = isinstance(error, CancellationRequested)
        if cancelled:
            print("[INFO] Cancellation requested; restoring source files...", flush=True)
        prepare_siril_for_cleanup()
        if app is not None:
            try:
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
                if not debug:
                    remove_tree(lights_sorted)
            if owns_staging and cancelled and not debug:
                remove_tree(workdir / "substacks")
        except Exception as rollback_error:
            write_quality_report("failed", f"{error}; source rollback failed: {rollback_error}")
            raise RuntimeError(f"{error}\nSource rollback also failed: {rollback_error}") from error
        write_quality_report("cancelled" if cancelled else "failed", error)
        if cancelled:
            print("[INFO] Cancellation complete; source files restored.", flush=True)
            return 2
        raise
    finally:
        if cancel_file is not None:
            cancel_file.unlink(missing_ok=True)


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
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
