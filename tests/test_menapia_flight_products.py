from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "roles"
    / "menapia_products"
    / "files"
    / "aurora-menapia-flight-products.py"
)
TASKS = SCRIPT.parents[1] / "tasks/main.yml"
SERVICE = SCRIPT.parents[1] / "templates/aurora-menapia-flight-products.service.j2"
TIMER = SCRIPT.parents[1] / "templates/aurora-menapia-flight-products.timer.j2"
PLAYBOOK = ROOT / "playbooks/menapia_products.yml"
INVENTORY = ROOT / "inventory/group_vars/aurora_cloud.yml"
STANDBY_VARS = ROOT / "roles/standby_replication/vars/main.yml"
SPEC = importlib.util.spec_from_file_location("menapia_flight_products", SCRIPT)
assert SPEC and SPEC.loader
products = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(products)


DAY = dt.date(2026, 8, 27)
UTC = dt.timezone.utc


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def make_bundle(
    raw_root: Path,
    *,
    flight: str = "flight-alpha",
    dock: str = "dock-1",
) -> tuple[Path, dict[str, Path]]:
    data_files = (
        raw_root
        / "drone-uploads/2026/08/27"
        / dock
        / flight
        / "data_files"
    )
    drone_path = data_files / "payload_DRN_unknown.csv"
    write_rows(
        drone_path,
        ["DateTime", "Fused_Altitude - Altitude"],
        [
            {"DateTime": "2026-08-26T23:59:59Z", "Fused_Altitude - Altitude": 999},
            {"DateTime": "2026-08-27T12:00:00.100Z", "Fused_Altitude - Altitude": 0},
            {"DateTime": "2026-08-27T12:00:00.900Z", "Fused_Altitude - Altitude": 10},
            {"DateTime": "2026-08-27T12:00:02Z", "Fused_Altitude - Altitude": 20},
        ],
    )
    sensor_paths: dict[str, Path] = {}
    sensor_fields = [
        "DateTime",
        "SHT85 - temperature",
        "SHT85 - humidity",
        "ICP10100 - pressure",
    ]
    for offset, sensor in enumerate(("SN0122", "SN0123")):
        sensor_path = data_files / f"payload_{sensor}.csv"
        sensor_paths[sensor] = sensor_path
        write_rows(
            sensor_path,
            sensor_fields,
            [
                {
                    "DateTime": "2026-08-26T23:59:59Z",
                    "SHT85 - temperature": 7 + offset,
                    "SHT85 - humidity": 40 + offset,
                    "ICP10100 - pressure": 99_900,
                },
                {
                    "DateTime": "2026-08-27T11:59:59Z",
                    "SHT85 - temperature": 8 + offset,
                    "SHT85 - humidity": 42 + offset,
                    "ICP10100 - pressure": 99_950,
                },
                {
                    "DateTime": "2026-08-27T12:00:00.100Z",
                    "SHT85 - temperature": 10 + offset,
                    "SHT85 - humidity": 50 + offset,
                    "ICP10100 - pressure": 100_000 + offset * 100,
                },
                {
                    "DateTime": "2026-08-27T12:00:00.900Z",
                    "SHT85 - temperature": 14 + offset,
                    "SHT85 - humidity": 54 + offset,
                    "ICP10100 - pressure": 100_100 + offset * 100,
                },
                {
                    "DateTime": "2026-08-27T12:00:01Z",
                    "SHT85 - temperature": 999,
                    "SHT85 - humidity": 200,
                    "ICP10100 - pressure": 200_000,
                },
                {
                    "DateTime": "2026-08-27T12:00:02Z",
                    "SHT85 - temperature": 16 + offset,
                    "SHT85 - humidity": 56 + offset,
                    "ICP10100 - pressure": 100_200 + offset * 100,
                },
                {
                    "DateTime": "2026-08-27T12:00:03Z",
                    "SHT85 - temperature": 18 + offset,
                    "SHT85 - humidity": 58 + offset,
                    "ICP10100 - pressure": 100_300 + offset * 100,
                },
            ],
        )
    return data_files, {"Drone": drone_path, **sensor_paths}


def append_sensor_row(path: Path) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["2026-08-27T12:00:04Z", 19, 59, 100_400])


def fake_flight_plot(detail: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = detail["flight"]
    path.write_bytes(
        f"flight:{metadata['id']}:{metadata['modifiedAt']}".encode("utf-8")
    )


def fake_daily_plot(details: list[dict], path: Path, day: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    versions = ",".join(detail["flight"]["modifiedAt"] for detail in details)
    path.write_bytes(f"daily:{day}:{versions}".encode("utf-8"))


def product_paths(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    product_root = root / "products/menapia"
    quicklook_root = root / "products/quicklooks/uas"
    state_path = root / "state/menapia-products/state.json"
    return (
        root / "raw/menapia",
        product_root,
        quicklook_root,
        state_path,
        product_root / "catalog.json",
    )


class MenapiaDecoderTests(unittest.TestCase):
    def test_embedded_flight_day_deduplicates_late_upstream_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "raw/menapia"
            manual, _paths = make_bundle(
                raw_root,
                flight="260827-120000-deadbeef",
            )
            duplicate = (
                raw_root
                / "drone-uploads/2026/08/28/unknown_dock"
                / "deadbeef-0000-4000-8000-260827120000/data_files"
            )
            unique = (
                raw_root
                / "drone-uploads/2026/08/28/unknown_dock"
                / "feedface-0000-4000-8000-260827120000/data_files"
            )
            shutil.copytree(manual, duplicate)
            shutil.copytree(manual, unique)

            duplicate_identity = products.bundle_identity(raw_root, duplicate)
            unique_identity = products.bundle_identity(raw_root, unique)
            assert duplicate_identity is not None
            assert unique_identity is not None
            found, deferred = products.discover_bundles(raw_root)
            detail = products.decode_bundle(
                unique,
                unique_identity,
                "2026-08-28T10:30:00Z",
            )

        self.assertEqual(duplicate_identity["dayUTC"], "2026-08-27")
        self.assertEqual(duplicate_identity["pathDayUTC"], "2026-08-28")
        self.assertEqual(
            duplicate_identity["canonicalFlightKey"],
            "260827120000:deadbeef",
        )
        self.assertEqual([path for path, _identity in found], [manual, unique])
        self.assertEqual(deferred, [])
        self.assertEqual(detail["flight"]["dayUTC"], "2026-08-27")
        self.assertEqual(detail["flight"]["startTimeUTC"], "2026-08-27T12:00:00Z")

    def test_campaign_lower_bound_excludes_aug24_before_bundle_classification(self) -> None:
        def named_bundle(
            raw_root: Path,
            day: dt.date,
            flight: str,
            streams: tuple[str, ...],
        ) -> Path:
            data_files = (
                raw_root
                / "drone-uploads"
                / day.strftime("%Y/%m/%d")
                / "dock-1"
                / flight
                / "data_files"
            )
            data_files.mkdir(parents=True)
            names = {
                "Drone": "payload_DRN_unknown.bin",
                "SN0122": "payload_SN0122.bin",
                "SN0123": "payload_SN0123.bin",
            }
            for stream in streams:
                (data_files / names[stream]).touch()
            return data_files

        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "raw/menapia"
            old_complete = named_bundle(
                raw_root,
                dt.date(2026, 8, 24),
                "pre-campaign-complete",
                ("Drone", "SN0122", "SN0123"),
            )
            named_bundle(
                raw_root,
                dt.date(2026, 8, 24),
                "pre-campaign-incomplete",
                ("SN0122",),
            )
            first_campaign = named_bundle(
                raw_root,
                dt.date(2026, 8, 25),
                "campaign-first-day",
                ("Drone", "SN0122", "SN0123"),
            )
            later_campaign = named_bundle(
                raw_root,
                dt.date(2026, 8, 27),
                "campaign-later-day",
                ("Drone", "SN0122", "SN0123"),
            )

            found, deferred = products.discover_bundles(raw_root)
            found_from_27, deferred_from_27 = products.discover_bundles(
                raw_root, dt.date(2026, 8, 27)
            )

        self.assertNotIn(old_complete, [path for path, _identity in found])
        self.assertEqual(
            [path for path, _identity in found],
            [first_campaign, later_campaign],
        )
        self.assertEqual(deferred, [])
        self.assertEqual(
            [path for path, _identity in found_from_27],
            [later_campaign],
        )
        self.assertEqual(deferred_from_27, [])

    def test_exported_csv_utc_timestamp_format_is_accepted(self) -> None:
        self.assertEqual(
            products.parse_utc("2026-08-27 11:12:11.425 UTC"),
            dt.datetime(2026, 8, 27, 11, 12, 11, 425_000, tzinfo=UTC),
        )

    def test_csv_bundle_is_path_day_filtered_windowed_and_one_second_median(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "raw/menapia"
            data_files, _paths = make_bundle(raw_root)
            identity = products.bundle_identity(raw_root, data_files)
            assert identity is not None
            detail = products.decode_bundle(
                data_files,
                identity,
                "2026-08-27T12:00:04Z",
            )

        series = detail["series"]
        self.assertEqual(
            series["timeUTC"],
            [
                "2026-08-27T12:00:00Z",
                "2026-08-27T12:00:01Z",
                "2026-08-27T12:00:02Z",
            ],
        )
        self.assertEqual(series["altitudeM"], [5.0, None, 20.0])
        self.assertEqual(series["temperatureC"]["SN0122"], [12.0, None, 16.0])
        self.assertEqual(series["pressureHpa"]["SN0122"], [1000.5, None, 1002.0])
        self.assertEqual(series["relativeHumidityPct"]["SN0123"], [53.0, None, 57.0])
        self.assertEqual(detail["aggregation"], "one-second median; no interpolation")
        self.assertEqual(detail["units"]["pressureHpa"], "hPa")
        self.assertEqual(detail["flight"]["samplePeriodSeconds"], 1)
        self.assertEqual(detail["flight"]["quality"]["level"], "amber")
        self.assertTrue(products.validate_detail(detail))

    def test_discovery_requires_drone_and_both_science_sensors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "raw/menapia"
            complete, _paths = make_bundle(raw_root, flight="complete")
            incomplete = (
                raw_root
                / "drone-uploads/2026/08/27/dock-1/incomplete/data_files"
            )
            write_rows(
                incomplete / "payload_SN0122.csv",
                ["DateTime", "SHT85 - temperature", "SHT85 - humidity", "ICP10100 - pressure"],
                [],
            )
            m350 = raw_root / "drone-uploads/2026/08/27/dock-1/m350-only/data_files"
            (m350 / "payload_M350.bin").parent.mkdir(parents=True)
            (m350 / "payload_M350.bin").write_bytes(b"not-a-target-stream")
            revision = (
                raw_root
                / "_upstream_revisions/drone-uploads/2026/08/27/dock-1/revision/data_files"
            )
            revision.mkdir(parents=True)
            for name in (
                "payload_DRN_unknown.csv",
                "payload_SN0122.csv",
                "payload_SN0123.csv",
            ):
                (revision / name).write_text("ignored\n", encoding="utf-8")

            found, deferred = products.discover_bundles(raw_root)

        self.assertEqual([path for path, _identity in found], [complete])
        self.assertEqual(len(deferred), 1)
        self.assertIn("incomplete", deferred[0])

    def test_csv_is_preferred_over_an_invalid_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary) / "raw/menapia"
            data_files, _paths = make_bundle(raw_root)
            (data_files / "payload_SN0122.bin").write_bytes(b"invalid-binary")
            result, warnings, selected = products.read_preferred_stream(
                data_files, "SN0122", DAY
            )

        self.assertTrue(result["temperatureC"])
        self.assertEqual(selected[0].suffix, ".csv")
        self.assertFalse(any("binary fallback" in warning for warning in warnings))

    def test_legacy_binary_target_records_may_arrive_out_of_order(self) -> None:
        base = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        anchor_ms = int(base.timestamp() * 1000)

        def record(second: int, value: float, *, sensor: int = 2, measurement: int = 1) -> bytes:
            timestamp_ms = anchor_ms + second * 1000
            return products.RECORD.pack(
                0x08,
                0,
                1,
                sensor,
                measurement,
                value,
                0,
                0.0,
                0,
                0.0,
                0,
                0.0,
                timestamp_ms & 0xFFFFFFFF,
                0,
                0,
            )

        payload = b"".join(
            [
                record(2, 16.0),
                record(0, 10.0),
                record(-20, 999.0, sensor=99, measurement=99),
                record(0, 14.0),
                record(1, 15.0),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload_SN0122.bin"
            path.write_bytes(
                json.dumps({"unix_time_ms": anchor_ms}).encode("ascii") + b"\n" + payload
            )
            result, warnings = products.read_binary_stream(
                path, products.SENSOR_MEASUREMENTS, DAY
            )

        self.assertEqual(
            list(result["temperatureC"]),
            [
                "2026-08-27T12:00:00Z",
                "2026-08-27T12:00:01Z",
                "2026-08-27T12:00:02Z",
            ],
        )
        self.assertEqual(list(result["temperatureC"].values()), [12.0, 15.0, 16.0])
        self.assertEqual(warnings, [])


class MenapiaPlotContractTests(unittest.TestCase):
    def test_plot_title_uses_uuid_prefix_or_manual_id_unique_suffix(self) -> None:
        self.assertEqual(
            products.source_short_label("01de6a9e-446f-4e67-9bcd-173909283bf8"),
            "01de6a9e",
        )
        self.assertEqual(
            products.source_short_label("260827-141526-077e1d1e"),
            "077e1d1e",
        )

    def test_null_seconds_are_passed_to_the_plot_as_nan_breaks(self) -> None:
        class Axis:
            def __init__(self) -> None:
                self.calls: list[tuple[list, list, dict]] = []

            def plot(self, times, values, **kwargs) -> None:
                self.calls.append((times, values, kwargs))

        axis = Axis()
        times = [1, 2, 3]
        products._plot_values(axis, times, [10.0, None, 12.0], color="blue")

        self.assertEqual(len(axis.calls), 1)
        plotted_times, plotted_values, kwargs = axis.calls[0]
        self.assertEqual(plotted_times, times)
        self.assertEqual(len(plotted_values), 3)
        self.assertTrue(math.isnan(plotted_values[1]))
        self.assertEqual(kwargs["color"], "blue")

    def test_sparse_pressure_uses_visible_markers_but_other_series_do_not(self) -> None:
        self.assertEqual(
            products.sensor_plot_style("pressureHpa", 3.5),
            {"marker": ".", "markersize": 3.5},
        )
        self.assertEqual(products.sensor_plot_style("temperatureC", 3.5), {})
        self.assertEqual(products.sensor_plot_style("relativeHumidityPct", 3.5), {})


class MenapiaPublicationTests(unittest.TestCase):
    def test_catalog_products_quicklooks_and_repeat_run_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, product_root, quicklook_root, state_path, catalog_path = product_paths(root)
            _data_files, _paths = make_bundle(raw)
            raw_snapshot = {
                path.relative_to(raw).as_posix(): path.read_bytes()
                for path in raw.rglob("*")
                if path.is_file()
            }
            with (
                mock.patch.object(products, "render_flight_plot", side_effect=fake_flight_plot) as flight_render,
                mock.patch.object(products, "render_daily_plot", side_effect=fake_daily_plot) as daily_render,
            ):
                catalog, status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )
                flight_id = catalog["flights"][0]["id"]
                detail_path = product_root / f"flights/{flight_id}.json"
                plot_path = product_root / f"plots/{flight_id}.png"
                daily_path = quicklook_root / "uas__summary__20260827.png"
                latest_path = quicklook_root / "uas__summary__latest.png"
                mtimes = {
                    path: path.stat().st_mtime_ns
                    for path in (
                        catalog_path,
                        detail_path,
                        plot_path,
                        daily_path,
                        latest_path,
                    )
                }
                second_catalog, second_status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )

            current_raw = {
                path.relative_to(raw).as_posix(): path.read_bytes()
                for path in raw.rglob("*")
                if path.is_file()
            }
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(status, 0)
            self.assertEqual(second_status, 0)
            self.assertEqual(catalog["schemaVersion"], 1)
            self.assertEqual(catalog["lastRunState"], "success")
            self.assertEqual(catalog["availableDays"], ["2026-08-27"])
            self.assertEqual(catalog["latestFlightID"], flight_id)
            self.assertEqual(second_catalog["latestFlightID"], flight_id)
            self.assertEqual(second_catalog["generatedAt"], catalog["generatedAt"])
            self.assertEqual(catalog["flights"][0]["detailPath"], f"flights/{flight_id}.json")
            self.assertEqual(catalog["flights"][0]["plotPath"], f"plots/{flight_id}.png")
            self.assertEqual(
                catalog["flights"][0]["allFlightsPlotPath"],
                "uas__summary__20260827.png",
            )
            self.assertTrue(products.validate_detail(detail))
            self.assertEqual(len(state["flights"]), 1)
            self.assertEqual(flight_render.call_count, 1)
            self.assertEqual(daily_render.call_count, 1)
            self.assertEqual(
                {path: path.stat().st_mtime_ns for path in mtimes},
                mtimes,
            )
            self.assertEqual(daily_path.read_bytes(), latest_path.read_bytes())
            self.assertEqual(raw_snapshot, current_raw)
            self.assertEqual(list(product_root.rglob("*.tmp")), [])
            self.assertEqual(list(quicklook_root.rglob("*.tmp")), [])
            self.assertTrue(catalog_path.is_file())

    def test_flight_render_failure_preserves_previous_pair_day_and_catalog_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, product_root, quicklook_root, state_path, catalog_path = product_paths(root)
            _data_files, paths = make_bundle(raw)
            with (
                mock.patch.object(products, "render_flight_plot", side_effect=fake_flight_plot),
                mock.patch.object(products, "render_daily_plot", side_effect=fake_daily_plot),
            ):
                baseline, status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )
            self.assertEqual(status, 0)
            flight_id = baseline["latestFlightID"]
            artifacts = [
                product_root / f"flights/{flight_id}.json",
                product_root / f"plots/{flight_id}.png",
                quicklook_root / "uas__summary__20260827.png",
                quicklook_root / "uas__summary__latest.png",
            ]
            before = {path: path.read_bytes() for path in artifacts}
            before_fingerprint = json.loads(state_path.read_text())["flights"][flight_id]["fingerprint"]
            append_sensor_row(paths["SN0122"])

            with mock.patch.object(
                products,
                "render_flight_plot",
                side_effect=products.ProductError("synthetic render failure"),
            ):
                failed_catalog, failed_status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )

            after_fingerprint = json.loads(state_path.read_text())["flights"][flight_id]["fingerprint"]
            self.assertEqual(failed_status, 1)
            self.assertEqual(failed_catalog["lastRunState"], "partial_failure")
            self.assertTrue(any("synthetic render failure" in warning for warning in failed_catalog["runWarnings"]))
            self.assertEqual(
                failed_catalog["flights"][0]["modifiedAt"],
                baseline["flights"][0]["modifiedAt"],
            )
            self.assertEqual({path: path.read_bytes() for path in artifacts}, before)
            self.assertEqual(after_fingerprint, before_fingerprint)
            self.assertEqual(list(product_root.rglob("*.tmp")), [])
            self.assertEqual(list(quicklook_root.rglob("*.tmp")), [])
            self.assertEqual(
                json.loads(catalog_path.read_text())["lastRunState"],
                "partial_failure",
            )

    def test_daily_render_failure_does_not_publish_staged_flight_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, product_root, quicklook_root, state_path, _catalog_path = product_paths(root)
            _data_files, paths = make_bundle(raw)
            with (
                mock.patch.object(products, "render_flight_plot", side_effect=fake_flight_plot),
                mock.patch.object(products, "render_daily_plot", side_effect=fake_daily_plot),
            ):
                baseline, status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )
            self.assertEqual(status, 0)
            flight_id = baseline["latestFlightID"]
            artifacts = [
                product_root / f"flights/{flight_id}.json",
                product_root / f"plots/{flight_id}.png",
                quicklook_root / "uas__summary__20260827.png",
                quicklook_root / "uas__summary__latest.png",
            ]
            before = {path: path.read_bytes() for path in artifacts}
            append_sensor_row(paths["SN0122"])

            with (
                mock.patch.object(products, "render_flight_plot", side_effect=fake_flight_plot),
                mock.patch.object(
                    products,
                    "render_daily_plot",
                    side_effect=products.ProductError("synthetic daily failure"),
                ),
            ):
                failed_catalog, failed_status = products.build_products(
                    raw, product_root, quicklook_root, state_path
                )

            self.assertEqual(failed_status, 1)
            self.assertEqual(failed_catalog["lastRunState"], "partial_failure")
            self.assertEqual({path: path.read_bytes() for path in artifacts}, before)
            self.assertEqual(
                failed_catalog["flights"][0]["modifiedAt"],
                baseline["flights"][0]["modifiedAt"],
            )
            self.assertEqual(list(product_root.rglob("*.tmp")), [])
            self.assertEqual(list(quicklook_root.rglob("*.tmp")), [])


class MenapiaInfrastructureTests(unittest.TestCase):
    def test_focused_producer_has_one_hardened_thirty_minute_timer_authority(self) -> None:
        tasks = TASKS.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        playbook = PLAYBOOK.read_text(encoding="utf-8")
        inventory = INVENTORY.read_text(encoding="utf-8")

        self.assertIn("OnCalendar=*:00/30", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("aurora_writer_timers_enabled", tasks)
        self.assertIn("roles:\n    - menapia_products", playbook)
        self.assertNotIn("dashboard_services", playbook)
        self.assertNotIn("mobile_api", playbook)
        self.assertNotIn("menapia_flight_products", inventory)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ProtectHome=true", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertIn("Environment=MPLCONFIGDIR=", service)
        self.assertIn("ReadOnlyPaths={{ aurora_menapia_product_raw_root }}", service)
        self.assertIn("{{ aurora_menapia_product_state_root }}", service)
        self.assertIn(
            "--campaign-start-day {{ aurora_menapia_product_campaign_start_day }}",
            service,
        )
        self.assertIn(
            'aurora_menapia_product_campaign_start_day: "2026-08-25"',
            inventory,
        )
        self.assertNotIn("restart", tasks.lower())

    def test_cli_campaign_start_day_is_exact_and_configurable(self) -> None:
        self.assertEqual(
            products.parse_args([]).campaign_start_day,
            dt.date(2026, 8, 25),
        )
        self.assertEqual(
            products.parse_args(
                ["--campaign-start-day", "2026-08-27"]
            ).campaign_start_day,
            dt.date(2026, 8, 27),
        )
        with self.assertRaises(SystemExit):
            products.parse_args(["--campaign-start-day", "27-08-2026"])

    def test_standby_replication_has_a_separate_menapia_product_stage(self) -> None:
        standby = STANDBY_VARS.read_text(encoding="utf-8")
        self.assertIn("name: product-menapia", standby)
        self.assertIn("source: /data/aurora/products/menapia/", standby)
        self.assertIn("destination: /data/aurora/products/menapia/", standby)


if __name__ == "__main__":
    unittest.main()
