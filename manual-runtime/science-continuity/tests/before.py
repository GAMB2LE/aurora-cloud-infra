#!/opt/aurora-cloud-dashboard/venv/bin/python
"""Publish bounded, independent direct-field science display generations.

Reads ASS files only. Never edits normal mirror products, source data, archive
state, or retention. A failed stream leaves its last published generation intact.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

UTC = timezone.utc
GIB = 1024 ** 3
OWNER = "aurora-science-continuity-v1-20261021"
DEFAULT_ROOT = "/data/aurora/dev-products/science-continuity"
APP = Path(os.environ.get("CONTINUITY_APP", "/opt/aurora-cloud-dashboard"))
HOST = "100.124.55.22"
STREAMS = {
    "cl61": ("/home/aurora/data/cl61", "cl61.zarr"),
    "radar": ("/home/aurora/data/rpgfmcw94", "cloud_radar.zarr"),
    "hatpro": ("/home/aurora/data/hatprog5", "hatpro.zarr"),
    "wxcam": ("/home/aurora/data/wxcam", "wxcam_catalog.sqlite"),
}

REMOTE_MANIFEST = r'''
import datetime,json,pathlib,re,sys,time
base=pathlib.Path(sys.argv[1]); stream=sys.argv[2]; floor=float(sys.argv[3]); now=time.time()
utc=datetime.timezone.utc
days=[]; day=datetime.datetime.fromtimestamp(floor-3600,utc).replace(hour=0,minute=0,second=0,microsecond=0)
while day.timestamp()<=now:
    days.append(day); day+=datetime.timedelta(days=1)
if stream=="cl61":
    candidates=base.glob("gamb2le_depolarisation_lidar_ceilometer_aurora_*.nc")
    regex=r"_(\d{8})_(\d{6})\.nc$"; fmt="%Y%m%d%H%M%S"
elif stream in ("radar","hatpro"):
    pattern="*LV1.NC" if stream=="radar" else "*.NC"
    candidates=(p for d in days for p in (base/d.strftime("Y%Y/M%m/D%d")).glob(pattern))
    regex=r"_(\d{6})_(\d{6})"; fmt="%y%m%d%H%M%S"
else:
    candidates=(p for kind in ("FISH","PANO") for d in days for pattern in ("*/HDR_*.jpg","HDR_*.mp4") for p in (base/kind/d.strftime("%Y%m%d")).glob(pattern))
    regex=r"HDR_(\d{8})_(\d{6})(?:_PANO)?\.(?:jpg|mp4)$"; fmt="%Y%m%d%H%M%S"
rows=[]
for p in candidates:
    if p.is_symlink() or not p.is_file(): continue
    match=re.search(regex,p.name)
    if not match: continue
    stamp=datetime.datetime.strptime(''.join(match.groups()),fmt).replace(tzinfo=utc).timestamp()
    # Include the preceding hour for science files that straddle the cutoff.
    if stamp < floor-(0 if stream=="wxcam" else 3600) or stamp>now-60: continue
    st=p.stat()
    grace=600 if p.suffix==".mp4" else 60
    if not st.st_size or st.st_mtime>now-grace: continue
    rows.append({"path":str(p.relative_to(base)),"size":st.st_size,"mtime_ns":st.st_mtime_ns,"filename_epoch":stamp})
print(json.dumps(sorted(rows,key=lambda r:r["path"])))
'''

SEED_PRODUCT = r'''
import pathlib,shutil,sys
import dask,numpy as np,xarray as xr
source,destination=map(pathlib.Path,sys.argv[1:3]); floor=np.datetime64(sys.argv[3]); chunk=int(sys.argv[4])
with xr.open_zarr(source,chunks={}) as ds:
    if ds.time.values[0]>=floor:
        ds.close()
        shutil.copytree(source,destination)
    else:
        window=ds.sel(time=slice(floor,None))
        if window.sizes.get("time",0):
            window=window.chunk({"time":chunk})
            with dask.config.set(scheduler="single-threaded"):
                window.to_zarr(destination,mode="w",consolidated=True)
'''


def utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def ssh_args() -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "IdentityFile=none",
            "-o", "PubkeyAuthentication=no", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=/home/aurora/.ssh/known_hosts",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]


def validate_rows(rows: object, stream: str) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"No stable {stream} files in the requested recent window")
    for row in rows:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts or any(c in str(relative) for c in "\x00\n\r"):
            raise ValueError("Unsafe source manifest path")
        if stream == "radar" and not relative.name.endswith("LV1.NC"):
            raise ValueError("Radar continuity accepts LV1.NC only")
        if not isinstance(row["size"], int) or row["size"] <= 0:
            raise ValueError("Invalid source manifest size")
    return rows


def remote_manifest(stream: str, floor: float) -> list[dict]:
    command = shlex.join(["python3", "-c", REMOTE_MANIFEST, STREAMS[stream][0], stream, str(floor)])
    result = subprocess.run([*ssh_args(), f"aurora@{HOST}", command], check=True,
                            text=True, capture_output=True, timeout=90)
    return validate_rows(json.loads(result.stdout), stream)


def tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def capacity(root: Path, reserve: int, budget: int, required: int = 0) -> None:
    free = shutil.disk_usage(root).free
    if free < reserve + required:
        raise RuntimeError(f"Insufficient free disk: {free}; need {reserve}+{required} bytes")
    if tree_bytes(root) + required > budget:
        raise RuntimeError("Science continuity display-cache budget exceeded")


def bounded_run(argv: list[str], root: Path, reserve: int, budget: int, timeout: int = 1200, env: dict | None = None) -> None:
    print("Running:", shlex.join(argv[:4]), flush=True)
    process = subprocess.Popen(argv, cwd=APP, start_new_session=True, env=env)
    started = time.monotonic()
    try:
        while process.poll() is None:
            if time.monotonic() - started > timeout:
                raise TimeoutError("Science continuity subprocess timed out")
            capacity(root, reserve, budget)
            time.sleep(2)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, argv)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise


def builder_command(stream: str, raw: Path, destination: Path, floor: datetime, cl61_batch_size: int = 12) -> list[str]:
    cutoff = floor.isoformat().replace("+00:00", "Z")
    if stream == "cl61":
        return [sys.executable, str(APP / "append_new_netcdf_to_zarr.py"), "--input-dir", str(raw),
                "--zarr", str(destination), "--chunk-time", "30", "--from-time", cutoff,
                "--batch-size", str(cl61_batch_size)]
    if stream == "radar":
        return [sys.executable, str(APP / "append_new_cloud_radar_to_zarr.py"), "--root", str(raw),
                "--zarr", str(destination), "--chunk-time", "200", "--from-time", cutoff]
    if stream == "hatpro":
        return [sys.executable, str(APP / "hatpro_to_zarr.py"), "--root", str(raw), "--zarr", str(destination),
                "--chunk-time", "300", "--chunk-range", "48", "--from-time", cutoff]
    return [sys.executable, str(APP / "index_wxcam_catalog.py"), "--root", str(raw), "--catalog", str(destination)]


def reusable_seed(previous: Path | None, rows: list[dict], floor: datetime) -> bool:
    """Reuse only our bounded cache when every overlapping raw input is unchanged."""
    if previous is None:
        return False
    prior = json.loads((previous / "generation.json").read_text())
    frontier = datetime.fromisoformat(prior["product"]["last_sample"].replace("Z", "+00:00"))
    if frontier <= floor:
        return False
    for row in rows:
        old = previous / "raw" / row["path"]
        if old.exists():
            stat = old.stat()
            if stat.st_size != row["size"] or int(stat.st_mtime) != row["mtime_ns"] // 1_000_000_000:
                return False
        elif row["filename_epoch"] <= frontier.timestamp():
            # A backfilled input must be fully rebuilt, not silently skipped by append.
            return False
    return True


def inspect_product(path: Path, floor: datetime) -> dict:
    import numpy as np
    import xarray as xr
    with xr.open_zarr(path, chunks={}) as ds:
        values = np.asarray(ds.time.values).astype("datetime64[ns]")
        if not values.size or np.isnat(values).any() or (np.diff(values.astype("int64")) <= 0).any():
            raise ValueError("Invalid, empty, duplicated or unsorted product time axis")
        if values[0] < np.datetime64(floor.replace(tzinfo=None), "ns"):
            raise ValueError("Product exceeds the configured recent display window")
        if values[-1] > np.datetime64(datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5), "ns"):
            raise ValueError("Future-dated product samples")
        return {"samples": int(values.size), "first_sample": str(values[0]) + "Z",
                "last_sample": str(values[-1]) + "Z", "variables": len(ds.data_vars)}


def inspect_wxcam(path: Path, expected: int) -> dict:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("WXcam catalog integrity check failed")
        count, first, last = conn.execute("SELECT COUNT(*), MIN(time_utc), MAX(time_utc) FROM images").fetchone()
        unreadable = conn.execute("SELECT COUNT(*) FROM unreadable_media").fetchone()[0]
        if count != expected or unreadable:
            raise ValueError(f"WXcam catalog incomplete: {count}/{expected} images; {unreadable} unreadable")
        missing = [p for (p,) in conn.execute("SELECT raw_path FROM images") if not Path(p).is_file()]
        if missing:
            raise ValueError("WXcam catalog references missing frame files")
    return {"samples": count, "first_sample": first.replace(" ", "T") + "Z",
            "last_sample": last.replace(" ", "T") + "Z", "unreadable": unreadable}


def seed_wx_media(previous: Path | None, candidate: Path, rows: list[dict]) -> None:
    if previous is None or not (previous / "media").is_dir():
        return
    media = candidate / "media"
    shutil.copytree(previous / "media", media)
    days = {Path(row["path"]).parts[1] for row in rows}
    names = {("fish_hdr" if row["path"].startswith("FISH/") else "pano_hdr", Path(row["path"]).name)
             for row in rows if row["path"].endswith(".jpg")}
    for path in (media / "hourly_thumbnails").glob("*/*/*.jpg"):
        if path.parent.name not in days or (path.parents[1].name, path.name) not in names:
            path.unlink()
    for path in (media / "daily_videos").glob("*/*"):
        token = path.name.split(".")[0]
        if re.fullmatch(r"\d{8}", token) and token not in days:
            path.unlink()
    # The builder compares file path, byte count and mtime. Only the generation
    # prefix changes; added/changed/expired clips still force an authentic rebuild.
    old_prefix, new_prefix = str(previous / "raw") + "/", str(candidate / "raw") + "/"
    for signature in (media / "daily_videos").glob("*/*.inputs"):
        content = signature.read_text()
        if all(line.startswith(old_prefix) for line in content.splitlines()):
            signature.write_text(content.replace(old_prefix, new_prefix))
        else:
            signature.unlink()


def render_artifacts(stream: str, candidate: Path, product: Path, rows: list[dict], root: Path, reserve: int, budget: int,
                     previous: Path | None = None) -> dict:
    from PIL import Image
    quicklooks = candidate / "quicklooks"
    quicklooks.mkdir()
    env = dict(os.environ)
    env.update(MPLBACKEND="Agg", MPLCONFIGDIR="/tmp/aurora-science-mpl",
               CLOUD_RADAR_RAW_ROOT=str(candidate / "raw"))
    if stream != "wxcam":
        script = {"cl61": "plot_ceilometer_zarr_last24h.py", "radar": "plot_cloud_radar_last24h.py",
                  "hatpro": "plot_hatpro_last24h.py"}[stream]
        output = quicklooks / "latest.png"
        bounded_run([sys.executable, str(APP / script), str(product), str(output)], root, reserve, budget, env=env)
        with Image.open(output) as image:
            image.verify()
        return {"latest_quicklook": str(output), "bytes": output.stat().st_size}

    seed_wx_media(previous, candidate, rows)
    media = candidate / "media"
    bounded_run([sys.executable, str(APP / "build_wxcam_daily_videos.py"),
                 "--raw-root", str(candidate / "raw"), "--output-root", str(media / "daily_videos"),
                 "--thumbnail-root", str(media / "hourly_thumbnails"), "--quicklook-root", str(quicklooks),
                 "--catalog-path", str(product)], root, reserve, budget, env=env)
    outputs = {}
    for image_type, source_dir in (("fish_hdr", "FISH"), ("pano_hdr", "PANO")):
        frames = [r for r in rows if r["path"].startswith(source_dir + "/") and r["path"].endswith(".jpg")]
        clips = [r for r in rows if r["path"].startswith(source_dir + "/") and r["path"].endswith(".mp4")]
        if not frames or not clips:
            raise ValueError(f"No settled {source_dir} frames or source clips for WXcam presentation")
        day = datetime.fromtimestamp(max(r["filename_epoch"] for r in clips), UTC).strftime("%Y%m%d")
        video = media / "daily_videos" / image_type / f"{day}.mp4"
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
                               check=True, capture_output=True, text=True, timeout=30)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not 0 < duration < 86400:
            raise ValueError("Invalid WXcam video duration")
        thumbs = list((media / "hourly_thumbnails" / image_type).glob("*/*.jpg"))
        if not thumbs:
            raise ValueError(f"No settled hourly thumbnails generated for {image_type}")
        for thumb in thumbs:
            with Image.open(thumb) as image:
                image.verify()
        outputs[image_type] = {"day": day, "video": str(video), "duration_seconds": duration, "thumbnails": len(thumbs)}
    newest = max((r for r in rows if r["path"].startswith("FISH/") and r["path"].endswith(".jpg")), key=lambda r: r["filename_epoch"])
    shutil.copyfile(candidate / "raw" / newest["path"], quicklooks / "latest.jpg")
    shutil.copyfile(quicklooks / "latest.jpg", quicklooks / "wxcam__latest.jpg")
    return outputs


def check_source_frontier(stream: str, rows: list[dict], actual: dict) -> None:
    relevant = rows
    if stream == "hatpro":
        relevant = [row for row in rows if row["path"].endswith((".LWP.NC", ".IWV.NC", ".IRT.NC", ".TPC.NC", ".TPB.NC"))]
    if not relevant:
        raise ValueError("No supported scientific source files in selected window")
    latest = max(row["filename_epoch"] for row in relevant)
    measured = datetime.fromisoformat(actual["last_sample"].replace("Z", "+00:00")).timestamp()
    if measured < latest - 60:
        raise ValueError("Product has not reached the newest selected source file")
    actual["latest_source_filename_time"] = datetime.fromtimestamp(latest, UTC).isoformat()


def checked_root(value: str) -> Path:
    root = Path(value)
    permitted = Path("/data/aurora/dev-products")
    if not root.is_absolute() or root == permitted or permitted not in root.parents or root.is_symlink():
        raise ValueError("Root must be a dedicated directory below /data/aurora/dev-products")
    root.mkdir(parents=True, exist_ok=True)
    if permitted.resolve() not in root.resolve().parents:
        raise ValueError("Root resolves outside the development product tree")
    return root


def cleanup_generations(directory: Path, keep: set[Path]) -> None:
    for path in directory.iterdir():
        if path.resolve() not in keep and re.fullmatch(r"\d{8}T\d{6}Z-\d+", path.name) and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def acquire_runner_lock(lock, stream: str, wait_seconds: float) -> bool:
    """Wait in the kernel lock queue with a finite deadline; never claim a skip succeeded."""
    started = time.monotonic()
    print(json.dumps({"stream": stream, "state": "waiting", "queued_at": utc(),
                      "lock_wait_limit_seconds": wait_seconds}), flush=True)

    def expired(_signum, _frame):
        raise TimeoutError("Science continuity lock wait timed out")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, wait_seconds)
    try:
        # A blocking waiter stays queued behind the current publisher instead
        # of losing every timer race to a longer imagery build.
        fcntl.flock(lock, fcntl.LOCK_EX)
    except TimeoutError:
        print(json.dumps({"stream": stream, "state": "failed", "failed_at": utc(),
                          "reason": "lock_wait_timeout", "exit_code": 75,
                          "waited_seconds": time.monotonic() - started}), flush=True)
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    print(json.dumps({"stream": stream, "state": "lock_acquired", "acquired_at": utc(),
                      "waited_seconds": time.monotonic() - started}), flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=STREAMS, required=True)
    parser.add_argument("--root", default=os.environ.get("SCIENCE_CONTINUITY_ROOT", DEFAULT_ROOT))
    parser.add_argument("--window-hours", type=float, default=48)
    parser.add_argument("--reserve-gib", type=float, default=8)
    parser.add_argument("--budget-gib", type=float, default=24)
    parser.add_argument("--max-source-gib", type=float, default=4)
    parser.add_argument("--lock-wait-seconds", type=float, default=1200)
    parser.add_argument("--cl61-append-timeout-seconds", type=int, default=2400)
    parser.add_argument("--cl61-batch-size", type=int, default=12)
    parser.add_argument("--check", action="store_true", help="Read source manifest only; create no files")
    args = parser.parse_args()
    if not 1 <= args.window_hours <= 48 or min(args.reserve_gib, args.budget_gib, args.max_source_gib) <= 0:
        raise ValueError("Require a 1-48 hour window and positive disk limits")
    if not 0 < args.lock_wait_seconds <= 1200:
        raise ValueError("Require a lock wait greater than zero and at most 1200 seconds")
    if not 1 <= args.cl61_append_timeout_seconds <= 2400:
        raise ValueError("Require a CL61 append timeout between 1 and 2400 seconds")
    if not 1 <= args.cl61_batch_size <= 24:
        raise ValueError("Require a CL61 batch size between 1 and 24 files")
    floor = datetime.now(UTC) - timedelta(hours=args.window_hours)
    if args.check:
        rows = remote_manifest(args.stream, floor.timestamp())
        total = sum(r["size"] for r in rows)
        print(json.dumps({"stream": args.stream, "source_files": len(rows), "source_bytes": total,
                          "window_start": floor.isoformat(), "latest_filename_epoch": max(r["filename_epoch"] for r in rows)}, indent=2))
        return 0
    if not APP.is_dir():
        raise ValueError("Missing deployed dashboard checkout")
    root = checked_root(args.root)
    lock = (root / ".runner.lock").open("a")
    if not acquire_runner_lock(lock, args.stream, args.lock_wait_seconds):
        lock.close()
        return 75
    # Select current generation and source window only after waiting; a queue
    # delay must not freeze the read frontier at the timer start time.
    floor = datetime.now(UTC) - timedelta(hours=args.window_hours)
    marker = root / ".science-continuity-owner.json"
    if marker.exists():
        if json.loads(marker.read_text()).get("owner") != OWNER:
            raise ValueError("Refusing another owner's continuity directory")
    elif any(p.name != ".runner.lock" for p in root.iterdir()):
        raise ValueError("Refusing to adopt a nonempty directory")
    else:
        atomic_json(marker, {"owner": OWNER, "created_at": utc()})
    stream_root = root / args.stream
    stream_root.mkdir(exist_ok=True)
    if stream_root.is_symlink():
        raise ValueError("Stream root cannot be a symlink")
    generations = stream_root / "generations"
    generations.mkdir(exist_ok=True)
    if generations.is_symlink():
        raise ValueError("Generations directory cannot be a symlink")
    current = stream_root / "current"
    previous = current.resolve(strict=True) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise ValueError("Current generation must be a symlink")
    if previous and previous.parent != generations.resolve():
        raise ValueError("Current generation resolves outside the owned directory")
    attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    candidate = generations / attempt
    status = {"owner": OWNER, "stream": args.stream, "state": "running", "started_at": utc(),
              "window_hours": args.window_hours, "source_host": HOST, "source_path": STREAMS[args.stream][0],
              "data_role": "non_authoritative_direct_field_continuity", "normal_mirror_unchanged": True,
              "last_complete_generation": str(previous) if previous else None}
    atomic_json(stream_root / "status.json", status)
    reserve, budget = int(args.reserve_gib * GIB), int(args.budget_gib * GIB)
    try:
        rows = remote_manifest(args.stream, floor.timestamp())
        total = sum(r["size"] for r in rows)
        if total > int(args.max_source_gib * GIB):
            raise RuntimeError(f"Selected source files exceed the per-stream budget: {total} bytes")
        cleanup_generations(generations, {previous} if previous else set())
        raw = stream_root / "raw"
        raw.mkdir(exist_ok=True)
        if raw.is_symlink():
            raise ValueError("Raw display cache cannot be a symlink")
        capacity(root, reserve, budget, total + GIB // 4)
        transfer_list = stream_root / "transfer.files"
        transfer_list.write_bytes(b"".join(r["path"].encode() + b"\0" for r in rows))
        bounded_run(["rsync", "-rtz", "--compress-level=3", "--partial", "--protect-args", "--delay-updates", "--timeout=90", "--from0",
                     f"--files-from={transfer_list}", "-e", shlex.join(ssh_args()),
                     f"aurora@{HOST}:{STREAMS[args.stream][0]}/", str(raw) + "/"], root, reserve, budget)
        transfer_list.unlink()
        for row in rows:
            file = raw / row["path"]
            if file.is_symlink() or not file.is_file() or file.stat().st_size != row["size"]:
                raise ValueError(f"Source changed during transfer or file missing: {row['path']}")
        # Exclude old cached files from this candidate without changing served data.
        candidate.mkdir()
        inputs = candidate / "raw"
        inputs.mkdir()
        for row in rows:
            destination = inputs / row["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(raw / row["path"], destination)
        destination = candidate / STREAMS[args.stream][1]
        capacity(root, reserve, budget, total * (0 if args.stream == "wxcam" else 2))
        seeded = args.stream != "wxcam" and reusable_seed(previous, rows, floor)
        if seeded:
            chunk = {"cl61": 30, "radar": 200, "hatpro": 300}[args.stream]
            bounded_run([sys.executable, "-c", SEED_PRODUCT, str(previous / STREAMS[args.stream][1]),
                         str(destination), floor.replace(tzinfo=None).isoformat(), str(chunk)], root, reserve, budget)
        append_timeout = args.cl61_append_timeout_seconds if args.stream == "cl61" else 1200
        if args.stream == "cl61":
            print(json.dumps({"stream": "cl61", "phase": "append", "started_at": utc(),
                              "batch_size": args.cl61_batch_size,
                              "timeout_seconds": append_timeout}), flush=True)
        bounded_run(builder_command(args.stream, inputs, destination, floor, args.cl61_batch_size),
                    root, reserve, budget, timeout=append_timeout)
        actual = inspect_wxcam(destination, len(rows)) if args.stream == "wxcam" else inspect_product(destination, floor)
        check_source_frontier(args.stream, rows, actual)
        artifacts = render_artifacts(args.stream, candidate, destination, rows, root, reserve, budget, previous)
        if previous:
            prior = json.loads((previous / "generation.json").read_text())
            if actual["last_sample"] < prior["product"]["last_sample"]:
                raise ValueError("Candidate product would move the served sample frontier backwards")
        generation = {"owner": OWNER, "stream": args.stream, "completed_at": utc(), "window_start": floor.isoformat(),
                      "source_files": len(rows), "source_bytes": total, "product": actual, "artifacts": artifacts,
                      "build_mode": "bounded_previous_generation_append" if seeded else "recent_raw_rebuild",
                      "source_manifest_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
        atomic_json(candidate / "generation.json", generation)
        # Product readers only see a generation after every verification above succeeds.
        link = stream_root / f".current-{os.getpid()}"
        link.symlink_to(candidate.relative_to(stream_root))
        link.replace(current)
        status.update(state="complete", completed_at=utc(), last_complete_generation=str(candidate), product=actual,
                      source_files=len(rows), source_bytes=total, artifacts=artifacts)
        atomic_json(stream_root / "status.json", status)
        selected = {r["path"] for r in rows}
        for path in raw.rglob("*"):
            if path.is_file() and not path.is_symlink() and str(path.relative_to(raw)) not in selected:
                path.unlink()
        print(json.dumps(status, sort_keys=True))
        return 0
    except Exception as exc:
        status.update(state="failed", failed_at=utc(), error=str(exc))
        atomic_json(stream_root / "status.json", status)
        if candidate.exists() and candidate.resolve() != current.resolve():
            shutil.rmtree(candidate)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
