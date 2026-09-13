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
import math

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
BAYER_PATTERNS = {'auto': 0, 'RGGB': 0, 'BGGR': 1, 'GBRG': 2, 'GRBG': 3}
BAYER_ORIENTATIONS = {'auto': 0, 'top-down': 2, 'bottom-up': 3}
BACKGROUND_METHODS = {'off': None, 'linear': '1', 'quadratic': '2', 'rbf': '-rbf'}
DEFAULT_BAYER_PATTERN = 'auto'
DEFAULT_BAYER_ORIENTATION = 'auto'
FITS_CFA_KEYWORDS = {'BAYERPAT', 'ROWORDER', 'INSTRUME'}


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


def _unique_staging_name(source, used_names):
    candidate = source.name
    counter = 2
    while candidate.casefold() in used_names:
        candidate = f"{source.stem}_{counter}{source.suffix}"
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def group_files(root_folder):
    check_cancellation()
    report_progress(0, 3, "Staging source frames")
    source_folder = root_folder
    destination_folder = root_folder / "Lights_sorted"
    if destination_folder.exists() and any(destination_folder.iterdir()):
        raise RuntimeError("Lights_sorted already exists and is not empty.")
    destination_folder.mkdir(exist_ok=True)
    files = discover_light_files(source_folder, (output_dir,))
    random.shuffle(files)
    group_size = math.ceil(len(files) / SubStack_nb)
    for i in range(0, len(files), group_size):
        check_cancellation()
        folder_index = i // group_size + 1
        group_folder = destination_folder / f"group_{folder_index}"
        lights_folder = group_folder / "lights"
        process_folder = group_folder / "process"
        lights_folder.mkdir(parents=True, exist_ok=True)
        process_folder.mkdir(parents=True, exist_ok=True)
        manifest = {}
        staged_files = []
        used_names = set()
        for file in files[i:i+group_size]:
            staging_name = _unique_staging_name(file, used_names)
            manifest[staging_name] = file.relative_to(source_folder).as_posix()
            staged_files.append((file, staging_name))
        (group_folder / SOURCE_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        for file, staging_name in staged_files:
            check_cancellation()
            shutil.move(str(file), str(lights_folder / staging_name))
        check_cancellation()
        print(f"[INFO] Moved {len(files[i:i+group_size])} files to {lights_folder}")
    report_progress(3, 3, "Source staging complete")

def cleanup(group_num, restore_sources=None):
    group_path = workdir / 'Lights_sorted' / f'group_{group_num}'
    process_folder = group_path / "process"
    lights_folder = group_path / "lights"
    master_light_folder = workdir
    for ext in ('*.fit', '*.seq', '*.txt'):
        for f in process_folder.glob(ext):
            f.unlink()
    should_restore_sources = not debug if restore_sources is None else restore_sources
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


def execute_siril(command):
    check_cancellation()
    if app is None:
        raise RuntimeError("Siril is not open.")
    succeeded = app.Execute(command) is not False
    check_cancellation()
    get_data = getattr(app, "GetData", None)
    response = get_data() if callable(get_data) else []
    response = [str(line) for line in response]
    if succeeded:
        return response
    details = "\n".join(str(line) for line in response if str(line).strip())
    message = f"Siril command failed: {command}"
    if details:
        message += f"\n{details}"
    raise RuntimeError(message)


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


def quality_filter_value(percentage):
    if adaptive_quality_filtering:
        return f"{quality_filter_sigma}k"
    return percentage


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
    accepted = re.search(r"total of images processed of (\d+)\)", text)
    if not accepted:
        accepted = re.search(r"(?:^|\n).*?\b(\d+) images processed\.", text)
    if accepted:
        summary["accepted_frames"] = int(accepted.group(1))
        summary["rejected_frames"] = input_frames - int(accepted.group(1))
    return summary


def parse_stack_quality(lines):
    summary = {"pixel_rejection_percent": {}}
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
    dimensions = re.search(r"(\d+) layer\(s\), (\d+)x(\d+) pixels, (\d+) bits", text)
    if dimensions:
        summary["output"] = {
            "channels": int(dimensions.group(1)),
            "width": int(dimensions.group(2)),
            "height": int(dimensions.group(3)),
            "bits_per_channel": int(dimensions.group(4)),
        }
    return summary


def initialize_quality_report(input_frames):
    return {
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_frames": input_frames,
        "settings": {
            "substacks": SubStack_nb,
            "drizzle": drizzle_enabled,
            "drizzle_scale": drizzle_scale,
            "pixel_fraction": pix_frac,
            "bayer_pattern": bayer_pattern,
            "bayer_orientation": bayer_orientation,
            "cosmetic_correction": cosmetic_correction,
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
        },
        "substacks": [],
    }


def write_quality_report(status, error=None):
    if quality_report is None:
        return
    quality_report["status"] = status
    quality_report["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if error is not None:
        quality_report["error"] = str(error)
    quality_report_path().write_text(
        json.dumps(quality_report, indent=2),
        encoding="utf-8",
    )


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
    files = [
        f for f in lights_folder.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_FRAME_SUFFIXES
    ]
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
    phase(0, 0.02, "Preparing")
    configure_debayer()
    execute_siril(f"cd {siril_path(lights_folder)}")
    execute_siril("set32bits")
    phase(0.02, 0.10, "Converting frames")
    execute_siril("convert light -out=../process")
    execute_siril(f"cd {siril_path(process_folder)}")
    input_sequence = "light"
    if cosmetic_correction:
        phase(0.10, 0.16, "Correcting hot pixels")
        execute_siril(
            f"seqfind_cosme_cfa light 0 {cosmetic_hot_sigma} -prefix=cc_"
        )
        input_sequence = "cc_light"
    phase(0.16, 0.28, "Calibrating and debayering")
    execute_siril(
        f"calibrate {input_sequence}"
        + ("" if drizzle_enabled else " -debayer")
    )
    processed_sequence = f"pp_{input_sequence}"
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
    execute_siril(
        f"seqplatesolve {background_sequence} "
        f"-order=3 -nocrop -nocache -force -catalog={cat}"
    )

    # Registration with Drizzle (Lanczos3 droplet)
    registration_command = (
        f"seqapplyreg {background_sequence} "
        f"-filter-bkg={quality_filter_value(filter_bkg)} "
        f"-filter-nbstars={quality_filter_value(filter_nbstars)} "
        f"-filter-round={quality_filter_value(filter_round)} "
        f"-filter-fwhm={quality_filter_value(filter_fwhm)} "
        f"-framing=max"
    )
    if drizzle_enabled:
        registration_command += (
            f" -drizzle -scale={drizzle_scale} "
            f"-pixfrac={pix_frac} -kernel=lanczos3"
        )
    phase(0.58, 0.78, "Registering frames")
    registration_response = execute_siril(registration_command)

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
        substack_report["stack_seconds"] = round(stack_time, 1)
        quality_report["substacks"].append(substack_report)
        write_quality_report("running")
    phase(0.98, 1, "Finalizing substack")
    execute_siril(f"cd {siril_path(group_path)}")
    execute_siril(f"mirrorx_single substack_{group_num}")
    phase(1, 1, "Complete")
    return True

def masterstack(SubStack_nb):
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
        shutil.move(str(substack_file), str(lights_folder))
        for rejection_map in source_group.glob(f"substack_{i}_*rejmap.fit"):
            shutil.move(str(rejection_map), str(rejection_maps_folder))
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
    group_folder = workdir / "substacks"
    lights_folder = group_folder / "lights"
    rejection_maps_folder = group_folder / "rejection_maps"
    substack_output = output_dir / "substacks"
    substack_output.mkdir(parents=True, exist_ok=True)
    for i in range(1, SubStack_nb + 1):
        source = lights_folder / f"substack_{i}.fit"
        destination = substack_output / f"substack_{i}_{run_id}.fit"
        shutil.move(str(source), str(destination))
    for rejection_map in rejection_maps_folder.glob("*.fit"):
        prefix = rejection_map.name.split("_", 2)[:2]
        substack_prefix = "_".join(prefix)
        destination = substack_output / _run_artifact_name(rejection_map.name, substack_prefix)
        shutil.move(str(rejection_map), str(destination))
    remove_tree(workdir / 'Lights_sorted')
    remove_tree(group_folder)
    report_progress(100, 100, "Complete")

def build_parser():
    parser = argparse.ArgumentParser(description="Build a randomized multi-stage Siril mosaic stack.")
    parser.add_argument("--workdir", type=Path, default=workdir)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--siril-exe", type=Path, default=siril_exe)
    parser.add_argument("--cancel-file", type=Path)
    parser.add_argument("--substacks", type=int, default=SubStack_nb)
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
    parser.add_argument("--debug", action="store_true", help="keep intermediate files")
    return parser


def configure(arguments):
    global workdir, output_dir, siril_exe, run_id, quality_report
    global cancel_file
    global SubStack_nb, drizzle_enabled, drizzle_scale, pix_frac
    global bayer_pattern, bayer_orientation, cosmetic_correction, cosmetic_hot_sigma
    global overlap_normalization, adaptive_quality_filtering, quality_filter_sigma
    global background_method, background_samples, background_tolerance
    global filter_bkg, filter_nbstars, filter_round, filter_fwhm
    global stacking_weight, feather_val, rej_low, rej_high, fast_normalization, catalog
    global memory_fraction, cpu_count, max_retries, debug

    workdir = arguments.workdir.expanduser().resolve()
    selected_output = arguments.output_dir or (workdir / "Siril Mosaic Output")
    output_dir = selected_output.expanduser().resolve()
    run_id = choose_run_id(output_dir)
    quality_report = None
    siril_exe = arguments.siril_exe.expanduser().resolve()
    cancel_file = arguments.cancel_file.expanduser().resolve() if arguments.cancel_file else None
    SubStack_nb = arguments.substacks
    drizzle_enabled = arguments.drizzle
    drizzle_scale = str(arguments.drizzle_scale)
    pix_frac = str(arguments.pixel_fraction)
    bayer_pattern = arguments.bayer_pattern
    bayer_orientation = arguments.bayer_orientation
    cosmetic_correction = arguments.cosmetic_correction
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
    debug = arguments.debug


def validate_parameters():
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
    if not siril_exe.is_file():
        raise ValueError("Siril executable does not exist.")
    if SubStack_nb < 1:
        raise ValueError("Substack count must be at least 1.")
    if SubStack_nb > light_count:
        raise ValueError("Substack count cannot exceed the number of light frames.")
    if cpu_count < 1:
        raise ValueError("CPU count must be at least 1.")
    if max_retries < 0:
        raise ValueError("Retries cannot be negative.")
    if float(quality_filter_sigma) <= 0:
        raise ValueError("Quality filter sigma must be greater than 0.")
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
        numeric_values.append(("cosmetic hot sigma", cosmetic_hot_sigma))
    if drizzle_enabled:
        numeric_values.extend((("drizzle scale", drizzle_scale), ("pixel fraction", pix_frac)))
    for label, value in numeric_values:
        try:
            number = float(value)
        except ValueError as error:
            raise ValueError(f"{label.title()} must be a number.") from error
        if number <= 0:
            raise ValueError(f"{label.title()} must be greater than 0.")
    if float(memory_fraction) > 1:
        raise ValueError("Memory fraction cannot exceed 1.")
    if drizzle_enabled and float(pix_frac) > 1:
        raise ValueError("Pixel fraction cannot exceed 1.")
    if drizzle_enabled and float(drizzle_scale) > 3:
        raise ValueError("Drizzle scale cannot exceed 3.")
    if stacking_weight not in STACKING_WEIGHTS:
        raise ValueError(f"Weight must be one of: {', '.join(sorted(STACKING_WEIGHTS))}.")
    for label, value in (("Background filter", filter_bkg), ("Star-count filter", filter_nbstars),
                         ("Roundness filter", filter_round), ("FWHM filter", filter_fwhm)):
        percentage = int(value.removesuffix("%"))
        if not 1 <= percentage <= 100:
            raise ValueError(f"{label} must be from 1 to 100 percent.")
    if float(feather_val) < 0:
        raise ValueError("Feather cannot be negative.")


def run_pipeline(arguments):
    global app, quality_report
    configure(arguments)
    validate_parameters()
    from pysiril.siril import Siril

    app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Run ID: {run_id}")
        input_files = discover_light_files(workdir, (output_dir,))
        input_frame_count = len(input_files)
        quality_report = initialize_quality_report(input_frame_count)
        quality_report["preflight_warnings"] = debayer_preflight_warnings(
            input_files, bayer_pattern, bayer_orientation
        )
        write_quality_report("running")
        print(f"[INFO] Quality report: {quality_report_path()}")
        for warning in quality_report["preflight_warnings"]:
            print(f"[WARNING] {warning}", flush=True)
        group_files(workdir)
        if app.Open() is False:
            raise RuntimeError("Siril failed to open its command pipe.")
        execute_siril("setext fit")
        execute_siril(f"setmem {memory_fraction}")
        execute_siril(f"setcpu {cpu_count}")
        for i in range(1, SubStack_nb + 1):
            retries = 0
            while retries <= max_retries:
                if substack(i):
                    break
                retries += 1
                if retries > max_retries:
                    raise RuntimeError(f"Substack {i} failed after {max_retries + 1} attempts")
                app.Close()
                time.sleep(2)
                app = Siril(siril_exe=str(siril_exe), bStable=False, requires='1.3.6')
                if app.Open() is False:
                    raise RuntimeError("Siril failed to reopen its command pipe.")
                execute_siril("setext fit")
                execute_siril(f"setmem {memory_fraction}")
                execute_siril(f"setcpu {cpu_count}")
            cleanup(i)
        masterstack(SubStack_nb)
        app.Close()
        app = None
        check_cancellation()
        if not debug:
            final_cleanup()
        write_quality_report("complete")
        return 0
    except Exception as error:
        cancelled = isinstance(error, CancellationRequested)
        if cancelled:
            print("[INFO] Cancellation requested; restoring source files...", flush=True)
        prepare_siril_for_cleanup()
        try:
            lights_sorted = workdir / "Lights_sorted"
            if lights_sorted.is_dir():
                for i in range(1, SubStack_nb + 1):
                    if (lights_sorted / f"group_{i}").is_dir():
                        cleanup(i, restore_sources=True)
                if not debug:
                    remove_tree(lights_sorted)
            if cancelled and not debug:
                remove_tree(workdir / "substacks")
        except Exception as rollback_error:
            if app is not None:
                try:
                    app.Close()
                except Exception:
                    pass
                app = None
            write_quality_report("failed", f"{error}; source rollback failed: {rollback_error}")
            raise RuntimeError(f"{error}\nSource rollback also failed: {rollback_error}") from error
        if app is not None:
            try:
                app.Close()
            except Exception:
                pass
            app = None
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
