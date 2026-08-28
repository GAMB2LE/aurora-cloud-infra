#!/usr/bin/env python3
"""Build immutable, public-safe Menapia flight display products.

The authoritative production host scans canonical raw flight bundles and writes
compact one-second JSON plus PNG presentation products.  Raw inputs are never
modified.  Product publication is additive and atomic, and a failed new bundle
cannot replace an already-published flight or latest quicklook.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import fcntl
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import shutil
import statistics
import struct
import sys
import tempfile
from typing import Any, Iterable


UTC = dt.timezone.utc
SCHEMA_VERSION = 1
DEFAULT_CAMPAIGN_START_DAY = dt.date(2026, 8, 25)
PATH_RE = re.compile(
    r"^drone-uploads/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<dock>[^/]+)/(?P<flight>[^/]+)/data_files$"
)
RECORD = struct.Struct(">BBHBBfBfBfBfIBH")
TIMESTAMP_MODULUS = 2**32
CSV_COLUMNS = {
    "temperatureC": "SHT85 - temperature",
    "relativeHumidityPct": "SHT85 - humidity",
    "pressurePa": "ICP10100 - pressure",
    "altitudeM": "Fused_Altitude - Altitude",
}
BOUNDS = {
    "temperatureC": (-80.0, 80.0),
    "relativeHumidityPct": (0.0, 105.0),
    "pressurePa": (30_000.0, 110_000.0),
    "altitudeM": (-500.0, 10_000.0),
}
SENSOR_MEASUREMENTS = {
    (2, 1): "temperatureC",
    (2, 2): "relativeHumidityPct",
    (7, 2): "pressurePa",
}
DRONE_MEASUREMENTS = {(6, 1): "altitudeM"}
SERIES_KEYS = (
    "temperatureC",
    "relativeHumidityPct",
    "pressurePa",
    "altitudeM",
)


class ProductError(RuntimeError):
    """A bundle cannot safely become a display product."""


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> dt.datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith(" UTC"):
        text = f"{text[:-4]}+00:00"
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=False, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        with source.open("rb") as reader:
            shutil.copyfileobj(reader, handle, length=1024 * 1024)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)


def same_file_content(left: Path, right: Path) -> bool:
    """Return true without changing either file when two products are identical."""
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def clean_number(value: object, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


class SecondAggregator:
    """Exact one-second medians, with an opt-in unordered legacy mode."""

    def __init__(
        self,
        metrics: Iterable[str],
        path_day: dt.date,
        *,
        tolerate_out_of_order: bool = False,
    ):
        self.metrics = tuple(metrics)
        self.path_day = path_day
        self.current_second: dt.datetime | None = None
        self.current_values = {metric: [] for metric in self.metrics}
        self.result = {metric: {} for metric in self.metrics}
        self.tolerate_out_of_order = tolerate_out_of_order
        self.unordered_values: dict[str, dict[dt.datetime, list[float]]] = {
            metric: {} for metric in self.metrics
        }
        self.last_timestamp: dt.datetime | None = None
        self.invalid_timestamps = 0
        self.other_day_rows = 0
        self.out_of_bounds = {metric: 0 for metric in self.metrics}

    def invalid_timestamp(self) -> None:
        self.invalid_timestamps += 1

    def add(self, timestamp: dt.datetime, values: dict[str, object]) -> None:
        timestamp = timestamp.astimezone(UTC)
        if (
            not self.tolerate_out_of_order
            and self.last_timestamp is not None
            and timestamp < self.last_timestamp
        ):
            raise ProductError("source timestamps are not non-decreasing")
        self.last_timestamp = timestamp
        if timestamp.date() != self.path_day:
            self.other_day_rows += 1
            return
        second = timestamp.replace(microsecond=0)
        if self.tolerate_out_of_order:
            for metric in self.metrics:
                raw = values.get(metric)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                low, high = BOUNDS[metric]
                if not math.isfinite(value) or not low <= value <= high:
                    self.out_of_bounds[metric] += 1
                    continue
                self.unordered_values[metric].setdefault(second, []).append(value)
            return
        if self.current_second is None:
            self.current_second = second
        elif second != self.current_second:
            if second < self.current_second:
                raise ProductError("source seconds are not non-decreasing")
            self.flush()
            self.current_second = second
        for metric in self.metrics:
            raw = values.get(metric)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            low, high = BOUNDS[metric]
            if not math.isfinite(value) or not low <= value <= high:
                self.out_of_bounds[metric] += 1
                continue
            self.current_values[metric].append(value)

    def flush(self) -> None:
        if self.current_second is None:
            return
        key = iso_utc(self.current_second)
        for metric in self.metrics:
            value = median(self.current_values[metric])
            if value is not None:
                self.result[metric][key] = value
            self.current_values[metric].clear()

    def finish(self) -> dict[str, dict[str, float]]:
        if self.tolerate_out_of_order:
            for metric, seconds in self.unordered_values.items():
                self.result[metric] = {
                    iso_utc(second): float(statistics.median(values))
                    for second, values in sorted(seconds.items())
                    if values
                }
            return self.result
        self.flush()
        return self.result

    def warnings(self, label: str) -> list[str]:
        warnings: list[str] = []
        if self.invalid_timestamps:
            warnings.append(f"{label}: {self.invalid_timestamps} invalid timestamp row(s) excluded")
        invalid_values = sum(self.out_of_bounds.values())
        if invalid_values:
            warnings.append(f"{label}: {invalid_values} out-of-bounds value(s) excluded")
        return warnings


def read_csv_stream(
    path: Path, metrics: tuple[str, ...], path_day: dt.date
) -> tuple[dict[str, dict[str, float]], list[str]]:
    columns = [CSV_COLUMNS[metric] for metric in metrics]
    aggregator = SecondAggregator(metrics, path_day)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = set(reader.fieldnames or ())
        required = {"DateTime", *columns}
        missing = sorted(required - names)
        if missing:
            raise ProductError(f"CSV is missing required column(s): {', '.join(missing)}")
        for row in reader:
            try:
                timestamp = parse_utc(row.get("DateTime"))
            except (TypeError, ValueError):
                aggregator.invalid_timestamp()
                continue
            aggregator.add(
                timestamp,
                {metric: row.get(CSV_COLUMNS[metric]) for metric in metrics},
            )
    result = aggregator.finish()
    if not any(result[metric] for metric in metrics):
        raise ProductError("CSV contains no valid path-day observations")
    return result, aggregator.warnings(path.name)


def parse_binary_header(path: Path) -> tuple[dict[str, Any], int]:
    with path.open("rb") as handle:
        preview = handle.read(65_536)
    try:
        text = preview.decode("latin-1")
        header, end = json.JSONDecoder().raw_decode(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductError(f"binary JSON header is invalid: {exc}") from exc
    while end < len(preview) and preview[end : end + 1] in b"\r\n\t ":
        end += 1
    if not isinstance(header, dict) or "unix_time_ms" not in header:
        raise ProductError("binary JSON header has no unix_time_ms anchor")
    return header, end


def reconstruct_unix_ms(low_ms: int, anchor_ms: int) -> int:
    result = (anchor_ms & ~0xFFFFFFFF) + low_ms
    delta = result - anchor_ms
    if delta > 2**31:
        result -= TIMESTAMP_MODULUS
    elif delta < -(2**31):
        result += TIMESTAMP_MODULUS
    return result


def read_binary_stream(
    path: Path,
    measurement_map: dict[tuple[int, int], str],
    path_day: dt.date,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    header, offset = parse_binary_header(path)
    anchor_ms = int(header["unix_time_ms"])
    payload = path.stat().st_size - offset
    count, trailing = divmod(payload, RECORD.size)
    if count <= 0:
        raise ProductError("binary contains no complete telemetry records")
    metrics = tuple(dict.fromkeys(measurement_map.values()))
    aggregator = SecondAggregator(metrics, path_day, tolerate_out_of_order=True)
    marker_count = 0
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            for index in range(count):
                record = RECORD.unpack_from(raw, offset + index * RECORD.size)
                (
                    marker,
                    _message_type,
                    _device_id,
                    sensor_id,
                    measurement_1,
                    value_1,
                    measurement_2,
                    value_2,
                    measurement_3,
                    value_3,
                    measurement_4,
                    value_4,
                    timestamp_low_ms,
                    _flags,
                    _checksum,
                ) = record
                marker_count += int(marker == 0x08)
                values: dict[str, float] = {}
                for measurement_id, raw_value in (
                    (measurement_1, value_1),
                    (measurement_2, value_2),
                    (measurement_3, value_3),
                    (measurement_4, value_4),
                ):
                    metric = measurement_map.get((sensor_id, measurement_id))
                    if metric is not None:
                        values[metric] = raw_value
                if not values:
                    continue
                unix_ms = reconstruct_unix_ms(timestamp_low_ms, anchor_ms)
                try:
                    timestamp = dt.datetime.fromtimestamp(unix_ms / 1000.0, UTC)
                except (OverflowError, OSError, ValueError):
                    aggregator.invalid_timestamp()
                    continue
                aggregator.add(timestamp, values)
    result = aggregator.finish()
    if not any(result[metric] for metric in metrics):
        raise ProductError("binary contains no valid path-day observations")
    warnings = aggregator.warnings(path.name)
    marker_fraction = marker_count / count
    if marker_fraction < 0.999:
        warnings.append(f"{path.name}: telemetry marker validity is {marker_fraction:.3%}")
    if trailing:
        warnings.append(f"{path.name}: {trailing} trailing byte(s) ignored")
    return result, warnings


def file_candidates(data_files: Path, stream: str) -> tuple[list[Path], list[Path]]:
    if stream == "Drone":
        matches = [path for path in data_files.iterdir() if path.is_file() and "_DRN_" in path.name]
    else:
        needle = f"_{stream}."
        matches = [path for path in data_files.iterdir() if path.is_file() and needle in path.name]
    csv_files = sorted(path for path in matches if path.suffix.lower() == ".csv")
    binary_files = sorted(path for path in matches if path.suffix.lower() == ".bin")
    return csv_files, binary_files


def read_preferred_stream(
    data_files: Path,
    stream: str,
    path_day: dt.date,
) -> tuple[dict[str, dict[str, float]], list[str], list[Path]]:
    csv_files, binary_files = file_candidates(data_files, stream)
    if not csv_files and not binary_files:
        raise ProductError(f"missing {stream} CSV/binary stream")
    metrics = ("altitudeM",) if stream == "Drone" else (
        "temperatureC",
        "relativeHumidityPct",
        "pressurePa",
    )
    measurement_map = DRONE_MEASUREMENTS if stream == "Drone" else SENSOR_MEASUREMENTS
    failures: list[str] = []
    if csv_files:
        try:
            result, warnings = read_csv_stream(csv_files[0], metrics, path_day)
            return result, warnings, [csv_files[0]]
        except (OSError, UnicodeError, csv.Error, ProductError) as exc:
            failures.append(f"{stream} CSV rejected ({exc}); binary fallback used")
    if binary_files:
        result, warnings = read_binary_stream(binary_files[0], measurement_map, path_day)
        return result, [*failures, *warnings], [binary_files[0]]
    raise ProductError(f"{stream} CSV rejected and no binary fallback is available")


def bundle_identity(raw_root: Path, data_files: Path) -> dict[str, Any] | None:
    try:
        relative = data_files.relative_to(raw_root).as_posix()
    except ValueError:
        return None
    match = PATH_RE.fullmatch(relative)
    if not match:
        return None
    groups = match.groupdict()
    try:
        path_day = dt.date(int(groups["year"]), int(groups["month"]), int(groups["day"]))
    except ValueError:
        return None
    bundle_relative = data_files.parent.relative_to(raw_root).as_posix()
    stable_id = hashlib.sha256(bundle_relative.encode("utf-8")).hexdigest()[:20]
    return {
        "id": stable_id,
        "sourceFlightID": groups["flight"],
        "dock": groups["dock"],
        "day": path_day,
        "dayUTC": path_day.isoformat(),
        "relative": bundle_relative,
    }


def discover_bundles(
    raw_root: Path,
    campaign_start_day: dt.date = DEFAULT_CAMPAIGN_START_DAY,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    base = raw_root / "drone-uploads"
    if not base.exists():
        return [], []
    complete: list[tuple[Path, dict[str, Any]]] = []
    incomplete: list[str] = []
    for data_files in sorted(base.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*/*/data_files")):
        if not data_files.is_dir():
            continue
        identity = bundle_identity(raw_root, data_files)
        if identity is None:
            continue
        # The shared bucket contains pre-campaign test bundles with qualifying
        # filenames but no telemetry on their path day. Exclude them before
        # completeness/deferred classification so they cannot create false
        # product failures or backlog counts.
        if identity["day"] < campaign_start_day:
            continue
        available = {
            stream: any(file_candidates(data_files, stream))
            for stream in ("Drone", "SN0122", "SN0123")
        }
        if all(available.values()):
            complete.append((data_files, identity))
        elif any(available.values()):
            incomplete.append(identity["relative"])
    return complete, incomplete


def bundle_fingerprint(data_files: Path) -> tuple[str, str]:
    records = []
    latest_ns = 0
    for stream in ("Drone", "SN0122", "SN0123"):
        csv_files, binary_files = file_candidates(data_files, stream)
        for path in [*csv_files, *binary_files]:
            stat = path.stat()
            latest_ns = max(latest_ns, stat.st_mtime_ns)
            records.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    modified = dt.datetime.fromtimestamp(latest_ns / 1_000_000_000, UTC)
    return hashlib.sha256(encoded).hexdigest(), iso_utc(modified)


def second_range(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    start = start.replace(microsecond=0)
    end = end.replace(microsecond=0)
    count = int((end - start).total_seconds())
    if count < 0:
        raise ProductError("drone window ends before it starts")
    if count > 24 * 60 * 60:
        raise ProductError("drone window exceeds one day")
    return [start + dt.timedelta(seconds=index) for index in range(count + 1)]


def quality(warnings: list[str]) -> dict[str, Any]:
    return {"level": "amber" if warnings else "green", "warnings": warnings}


def decode_bundle(
    data_files: Path,
    identity: dict[str, Any],
    modified_at: str,
) -> dict[str, Any]:
    warnings: list[str] = []
    drone, stream_warnings, _ = read_preferred_stream(data_files, "Drone", identity["day"])
    warnings.extend(stream_warnings)
    sensor_data: dict[str, dict[str, dict[str, float]]] = {}
    for sensor in ("SN0122", "SN0123"):
        decoded, stream_warnings, _ = read_preferred_stream(data_files, sensor, identity["day"])
        sensor_data[sensor] = decoded
        warnings.extend(stream_warnings)

    altitude = drone["altitudeM"]
    if not altitude:
        raise ProductError("drone stream has no valid altitude")
    start = parse_utc(min(altitude))
    end = parse_utc(max(altitude))
    times = second_range(start, end)
    if len(times) < 2:
        raise ProductError("drone window contains fewer than two one-second samples")
    time_strings = [iso_utc(value) for value in times]

    def values(mapping: dict[str, float], digits: int = 4) -> list[float | None]:
        return [clean_number(mapping.get(timestamp), digits) for timestamp in time_strings]

    pressure_122 = {key: value / 100.0 for key, value in sensor_data["SN0122"]["pressurePa"].items()}
    pressure_123 = {key: value / 100.0 for key, value in sensor_data["SN0123"]["pressurePa"].items()}
    series = {
        "timeUTC": time_strings,
        "temperatureC": {
            "SN0122": values(sensor_data["SN0122"]["temperatureC"]),
            "SN0123": values(sensor_data["SN0123"]["temperatureC"]),
        },
        "pressureHpa": {
            "SN0122": values(pressure_122),
            "SN0123": values(pressure_123),
        },
        "relativeHumidityPct": {
            "SN0122": values(sensor_data["SN0122"]["relativeHumidityPct"]),
            "SN0123": values(sensor_data["SN0123"]["relativeHumidityPct"]),
        },
        "altitudeM": values(altitude),
    }
    altitude_values = [value for value in series["altitudeM"] if value is not None]
    metadata = {
        "id": identity["id"],
        "sourceFlightID": identity["sourceFlightID"],
        "dayUTC": identity["dayUTC"],
        "flightNumber": 0,
        "title": "Menapia flight",
        "startTimeUTC": iso_utc(start),
        "endTimeUTC": iso_utc(end),
        "durationSeconds": int((end - start).total_seconds()),
        "samplePeriodSeconds": 1,
        "modifiedAt": modified_at,
        "maximumAltitudeM": max(altitude_values) if altitude_values else None,
        "quality": quality(warnings),
        "detailPath": f"flights/{identity['id']}.json",
        "plotPath": f"plots/{identity['id']}.png",
        "allFlightsPlotPath": f"uas__summary__{identity['dayUTC'].replace('-', '')}.png",
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "flight": metadata,
        "series": series,
        "aggregation": "one-second median; no interpolation",
        "units": {
            "temperatureC": "degC",
            "pressureHpa": "hPa",
            "relativeHumidityPct": "%",
            "altitudeM": "m",
        },
    }


def validate_detail(detail: dict[str, Any]) -> bool:
    try:
        flight = detail["flight"]
        series = detail["series"]
        count = len(series["timeUTC"])
        arrays = [
            series["temperatureC"]["SN0122"],
            series["temperatureC"]["SN0123"],
            series["pressureHpa"]["SN0122"],
            series["pressureHpa"]["SN0123"],
            series["relativeHumidityPct"]["SN0122"],
            series["relativeHumidityPct"]["SN0123"],
            series["altitudeM"],
        ]
        return (
            detail.get("schemaVersion") == SCHEMA_VERSION
            and isinstance(flight.get("id"), str)
            and count >= 2
            and all(len(values) == count for values in arrays)
        )
    except (KeyError, TypeError):
        return False


def _plot_modules():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ProductError(f"PNG rendering dependency unavailable: {exc}") from exc
    return mdates, plt


def _plot_values(axis, times, values, **kwargs) -> None:
    # Preserve explicit missing seconds as line breaks.  Filtering null pairs
    # would visually interpolate across a data gap and violate the product
    # contract even though the JSON still contained nulls.
    plotted = [math.nan if value is None else value for value in values]
    if any(math.isfinite(value) for value in plotted):
        axis.plot(times, plotted, **kwargs)


def sensor_plot_style(metric: str, marker_size: float) -> dict[str, Any]:
    """Make sparse legacy pressure observations visible without filling gaps."""
    return {"marker": ".", "markersize": marker_size} if metric == "pressureHpa" else {}


def source_short_label(source_flight_id: str) -> str:
    """Keep UUID prefixes but use the unique suffix of manual flight IDs."""
    suffix = source_flight_id.rsplit("-", 1)[-1]
    if suffix != source_flight_id and re.fullmatch(r"[0-9A-Fa-f]{8}", suffix):
        return suffix
    return source_flight_id[:8]


def configure_time_axis(axis, mdates, times: list[dt.datetime]) -> None:
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9, tz=UTC)
    axis.xaxis.set_major_locator(locator)
    span_seconds = (max(times) - min(times)).total_seconds() if times else 0
    pattern = "%H:%M:%S" if span_seconds < 20 * 60 else "%H:%M"
    axis.xaxis.set_major_formatter(mdates.DateFormatter(pattern, tz=UTC))


def save_figure_atomic(figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        figure.savefig(temporary, format="png", dpi=160, facecolor="white")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def render_flight_plot(detail: dict[str, Any], path: Path) -> None:
    mdates, plt = _plot_modules()
    flight = detail["flight"]
    series = detail["series"]
    times = [parse_utc(value) for value in series["timeUTC"]]
    figure, axes = plt.subplots(4, 1, figsize=(13, 12), sharex=True, constrained_layout=True)
    figure.suptitle(
        f"{flight['title']} - {source_short_label(flight['sourceFlightID'])}",
        fontsize=17,
    )
    axes[0].set_title(
        f"{flight['dayUTC']} | {flight['startTimeUTC'][11:19]} UTC | "
        f"{flight['durationSeconds'] / 60:.1f} min | one-second medians",
        color="#5f6368",
        fontsize=10,
        pad=8,
    )
    panels = (
        ("temperatureC", "Air temperature\n(°C)"),
        ("pressureHpa", "Air pressure\n(hPa)"),
        ("relativeHumidityPct", "Relative humidity\n(% RH)"),
    )
    for axis, (key, label) in zip(axes[:3], panels, strict=True):
        marker_style = sensor_plot_style(key, 3.5)
        _plot_values(
            axis,
            times,
            series[key]["SN0122"],
            color="#2b6cb0",
            linewidth=1.3,
            label="SN0122",
            **marker_style,
        )
        _plot_values(
            axis,
            times,
            series[key]["SN0123"],
            color="#d97706",
            linewidth=1.3,
            linestyle="--",
            label="SN0123",
            **marker_style,
        )
        axis.set_ylabel(label)
        axis.legend(frameon=False, ncol=2, loc="best")
    _plot_values(axes[3], times, series["altitudeM"], color="#263238", linewidth=1.4)
    axes[3].set_ylabel("Fused drone altitude\n(m)")
    axes[3].set_xlabel("Time (UTC)")
    configure_time_axis(axes[3], mdates, times)
    for axis in axes:
        axis.grid(axis="y", color="#dadce0", linewidth=0.7)
        axis.spines[["top", "right"]].set_visible(False)
    save_figure_atomic(figure, path)
    plt.close(figure)


def render_daily_plot(details: list[dict[str, Any]], path: Path, day: str) -> None:
    mdates, plt = _plot_modules()
    figure, axes = plt.subplots(4, 1, figsize=(15, 13), sharex=True, constrained_layout=True)
    figure.suptitle(f"Menapia UAS flights - {day}", fontsize=17)
    axes[0].set_title(
        "All decoded flights | UTC | one-second medians",
        color="#5f6368",
        fontsize=10,
        pad=8,
    )
    colors = plt.get_cmap("tab10")
    panels = (
        ("temperatureC", "Air temperature\n(°C)"),
        ("pressureHpa", "Air pressure\n(hPa)"),
        ("relativeHumidityPct", "Relative humidity\n(% RH)"),
    )
    all_times: list[dt.datetime] = []
    for flight_index, detail in enumerate(details):
        metadata = detail["flight"]
        series = detail["series"]
        times = [parse_utc(value) for value in series["timeUTC"]]
        all_times.extend(times)
        color = colors(flight_index % 10)
        flight_label = f"Flight {metadata['flightNumber']}"
        for axis, (key, _label) in zip(axes[:3], panels, strict=True):
            marker_style = sensor_plot_style(key, 3.0)
            _plot_values(
                axis,
                times,
                series[key]["SN0122"],
                color=color,
                linewidth=1.15,
                label=f"{flight_label} SN0122",
                **marker_style,
            )
            _plot_values(
                axis,
                times,
                series[key]["SN0123"],
                color=color,
                linewidth=1.15,
                linestyle="--",
                label=f"{flight_label} SN0123",
                **marker_style,
            )
        _plot_values(axes[3], times, series["altitudeM"], color=color, linewidth=1.3, label=flight_label)
    for axis, (_key, label) in zip(axes[:3], panels, strict=True):
        axis.set_ylabel(label)
    axes[3].set_ylabel("Fused drone altitude\n(m)")
    axes[3].set_xlabel("Time (UTC)")
    configure_time_axis(axes[3], mdates, all_times)
    axes[0].legend(frameon=False, ncol=min(4, max(1, len(details) * 2)), fontsize=8, loc="upper center")
    axes[3].legend(frameon=False, ncol=min(4, max(1, len(details))), fontsize=8, loc="best")
    for axis in axes:
        axis.grid(axis="y", color="#dadce0", linewidth=0.7)
        axis.spines[["top", "right"]].set_visible(False)
    save_figure_atomic(figure, path)
    plt.close(figure)


def stage_flight_pair(
    detail: dict[str, Any], detail_path: Path, plot_path: Path
) -> tuple[Path, Path]:
    """Render a JSON/PNG pair without changing either published artifact."""
    nonce = f"{os.getpid()}-{hashlib.sha256(os.urandom(16)).hexdigest()[:8]}"
    staged_detail = detail_path.with_name(f".{detail_path.name}.{nonce}.tmp")
    staged_plot = plot_path.with_name(f".{plot_path.name}.{nonce}.tmp")
    try:
        atomic_json(staged_detail, detail)
        render_flight_plot(detail, staged_plot)
        return staged_detail, staged_plot
    except Exception:
        staged_detail.unlink(missing_ok=True)
        staged_plot.unlink(missing_ok=True)
        raise


def publish_flight_pair(
    detail: dict[str, Any], detail_path: Path, plot_path: Path
) -> None:
    """Stage JSON and PNG fully before replacing either published artifact."""
    staged_detail, staged_plot = stage_flight_pair(detail, detail_path, plot_path)
    try:
        # The catalog is published only after both artifacts exist.  Replace
        # media first so a reader never sees new JSON pointing at no PNG.
        staged_plot.replace(plot_path)
        staged_detail.replace(detail_path)
    finally:
        staged_detail.unlink(missing_ok=True)
        staged_plot.unlink(missing_ok=True)


def metadata_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return dict(detail["flight"])


def build_products(
    raw_root: Path,
    product_root: Path,
    quicklook_root: Path,
    state_path: Path,
    *,
    force: bool = False,
    campaign_start_day: dt.date = DEFAULT_CAMPAIGN_START_DAY,
) -> tuple[dict[str, Any], int]:
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "flights").mkdir(parents=True, exist_ok=True)
    (product_root / "plots").mkdir(parents=True, exist_ok=True)
    quicklook_root.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProductError("another Menapia product build is running") from exc

        state = load_json(state_path)
        state_flights = (
            dict(state.get("flights")) if isinstance(state.get("flights"), dict) else {}
        )
        old_catalog = load_json(product_root / "catalog.json")
        previous_details: dict[str, dict[str, Any]] = {}
        for metadata in old_catalog.get("flights", []):
            if not isinstance(metadata, dict) or not metadata.get("id"):
                continue
            flight_id = str(metadata["id"])
            detail_path = product_root / str(
                metadata.get("detailPath", f"flights/{flight_id}.json")
            )
            plot_path = product_root / str(
                metadata.get("plotPath", f"plots/{flight_id}.png")
            )
            detail = load_json(detail_path)
            if validate_detail(detail) and plot_path.is_file():
                previous_details[flight_id] = detail

        proposals = {key: copy.deepcopy(value) for key, value in previous_details.items()}
        changed_ids: set[str] = set()
        candidate_state: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        bundles, incomplete = discover_bundles(raw_root, campaign_start_day)

        for data_files, identity in bundles:
            flight_id = identity["id"]
            detail_path = product_root / "flights" / f"{flight_id}.json"
            plot_path = product_root / "plots" / f"{flight_id}.png"
            try:
                fingerprint, modified_at = bundle_fingerprint(data_files)
                saved = state_flights.get(flight_id)
                published = load_json(detail_path)
                reusable = (
                    not force
                    and isinstance(saved, dict)
                    and saved.get("fingerprint") == fingerprint
                    and validate_detail(published)
                    and plot_path.is_file()
                )
                if reusable:
                    proposals[flight_id] = published
                else:
                    proposals[flight_id] = decode_bundle(data_files, identity, modified_at)
                    changed_ids.add(flight_id)
                candidate_state[flight_id] = {
                    "fingerprint": fingerprint,
                    "sourceRelativePath": identity["relative"],
                    "updatedAt": utc_now(),
                }
                if force or not detail_path.exists() or not plot_path.exists():
                    changed_ids.add(flight_id)
            except Exception as exc:
                failures.append(f"{identity['sourceFlightID']}: {exc}")
                if flight_id in previous_details:
                    proposals[flight_id] = copy.deepcopy(previous_details[flight_id])
                else:
                    proposals.pop(flight_id, None)

        proposal_days: dict[str, list[dict[str, Any]]] = {}
        for detail in proposals.values():
            proposal_days.setdefault(str(detail["flight"]["dayUTC"]), []).append(detail)
        for details in proposal_days.values():
            details.sort(
                key=lambda item: (
                    item["flight"]["startTimeUTC"],
                    item["flight"]["id"],
                )
            )
            for number, detail in enumerate(details, 1):
                metadata = detail["flight"]
                expected_title = f"Menapia Flight {number}"
                if (
                    metadata.get("flightNumber") != number
                    or metadata.get("title") != expected_title
                ):
                    metadata["flightNumber"] = number
                    metadata["title"] = expected_title
                    changed_ids.add(metadata["id"])

        # Publish one UTC day as a transaction: every changed per-flight pair
        # and the all-flights PNG must render before any artifact for that day
        # replaces its previous published version.
        final_details: dict[str, dict[str, Any]] = {}
        committed_candidate_ids: set[str] = set()
        for day, details in sorted(proposal_days.items()):
            token = day.replace("-", "")
            summary_path = quicklook_root / f"uas__summary__{token}.png"
            staged_pairs: list[tuple[Path, Path, Path, Path, str]] = []
            staged_summary: Path | None = None
            day_ids = {detail["flight"]["id"] for detail in details}
            day_changed = force or any(flight_id in changed_ids for flight_id in day_ids)
            needs_summary = day_changed or not summary_path.is_file()
            try:
                for detail in details:
                    metadata = detail["flight"]
                    flight_id = metadata["id"]
                    detail_path = product_root / metadata["detailPath"]
                    plot_path = product_root / metadata["plotPath"]
                    if force or flight_id in changed_ids or not detail_path.is_file() or not plot_path.is_file():
                        staged_detail, staged_plot = stage_flight_pair(
                            detail, detail_path, plot_path
                        )
                        staged_pairs.append(
                            (staged_detail, staged_plot, detail_path, plot_path, flight_id)
                        )
                if needs_summary:
                    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:8]
                    staged_summary = summary_path.with_name(
                        f".{summary_path.name}.{os.getpid()}-{nonce}.tmp"
                    )
                    render_daily_plot(details, staged_summary, day)

                for staged_detail, staged_plot, detail_path, plot_path, _flight_id in staged_pairs:
                    staged_plot.replace(plot_path)
                    staged_detail.replace(detail_path)
                if staged_summary is not None:
                    staged_summary.replace(summary_path)
                for detail in details:
                    flight_id = detail["flight"]["id"]
                    final_details[flight_id] = detail
                    if flight_id in candidate_state:
                        committed_candidate_ids.add(flight_id)
            except Exception as exc:
                failures.append(f"{day} product transaction: {exc}")
                for flight_id, detail in previous_details.items():
                    if detail["flight"]["dayUTC"] == day:
                        final_details[flight_id] = copy.deepcopy(detail)
            finally:
                for staged_detail, staged_plot, _detail_path, _plot_path, _flight_id in staged_pairs:
                    staged_detail.unlink(missing_ok=True)
                    staged_plot.unlink(missing_ok=True)
                if staged_summary is not None:
                    staged_summary.unlink(missing_ok=True)

        final_days: dict[str, list[dict[str, Any]]] = {}
        for detail in final_details.values():
            final_days.setdefault(str(detail["flight"]["dayUTC"]), []).append(detail)
        sorted_days = sorted(final_days, reverse=True)
        for day in sorted_days:
            source = quicklook_root / f"uas__summary__{day.replace('-', '')}.png"
            if source.exists():
                try:
                    latest = quicklook_root / "uas__summary__latest.png"
                    if not same_file_content(source, latest):
                        atomic_copy(source, latest)
                except OSError as exc:
                    failures.append(f"latest quicklook: {exc}")
                break

        for flight_id in committed_candidate_ids:
            state_flights[flight_id] = candidate_state[flight_id]

        flights = sorted(
            (metadata_from_detail(detail) for detail in final_details.values()),
            key=lambda item: (item["startTimeUTC"], item["id"]),
            reverse=True,
        )
        catalog = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": utc_now(),
            "lastRunState": "partial_failure" if failures else "success",
            "latestFlightID": flights[0]["id"] if flights else None,
            "availableDays": sorted_days,
            "flights": flights,
            "deferredBundleCount": len(incomplete),
            "runWarnings": failures,
        }
        atomic_json(product_root / "catalog.json", catalog)
        atomic_json(
            state_path,
            {
                "schemaVersion": SCHEMA_VERSION,
                "updatedAt": catalog["generatedAt"],
                "flights": state_flights,
            },
        )
        return catalog, 1 if failures else 0


def campaign_day(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("campaign start day must be YYYY-MM-DD") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path("/project/aurora/raw/menapia"))
    parser.add_argument("--product-root", type=Path, default=Path("/data/aurora/products/menapia"))
    parser.add_argument("--quicklook-root", type=Path, default=Path("/data/aurora/products/quicklooks/uas"))
    parser.add_argument("--state-path", type=Path, default=Path("/var/lib/aurora-cloud/menapia-products/state.json"))
    parser.add_argument(
        "--campaign-start-day",
        type=campaign_day,
        default=DEFAULT_CAMPAIGN_START_DAY,
        help="earliest UTC path day eligible for display products (default: 2026-08-25)",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        catalog, status = build_products(
            args.raw_root,
            args.product_root,
            args.quicklook_root,
            args.state_path,
            force=args.force,
            campaign_start_day=args.campaign_start_day,
        )
    except ProductError as exc:
        print(f"Menapia product build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "state": catalog["lastRunState"],
                "flight_count": len(catalog["flights"]),
                "latest_flight_id": catalog["latestFlightID"],
                "available_days": catalog["availableDays"],
                "deferred_bundles": catalog["deferredBundleCount"],
            },
            sort_keys=True,
        )
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
