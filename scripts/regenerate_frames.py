#!/usr/bin/env python3
"""Create midpoint JPG frames and update public.blog_posts.img after the push."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGES_BASE_URL = "https://usercontent6.github.io/friendly-pancake"
DEFAULT_MANIFEST = Path(tempfile.gettempdir()) / "frame_manifest.json"
MAX_IMAGES_PER_FOLDER = 100
MAX_IMAGE_WIDTH = 1280
REPORT_DIR = ROOT / "reports"
REPORT_JSON = REPORT_DIR / "frame-regeneration-report.json"
REPORT_CSV = REPORT_DIR / "frame-regeneration-errors.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("frame-regenerator")


def require_database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError(
            "DATABASE_URL is missing. Add it under repository Settings → "
            "Secrets and variables → Actions."
        )
    return value


def pages_base_url() -> str:
    value = os.getenv("PAGES_BASE_URL", "").strip() or DEFAULT_PAGES_BASE_URL
    value = value.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("PAGES_BASE_URL must be a complete HTTPS base URL.")
    return value


def manifest_path() -> Path:
    return Path(os.getenv("FRAME_MANIFEST", str(DEFAULT_MANIFEST))).expanduser()


def fetch_video_rows() -> list[dict[str, Any]]:
    """Read only frame inputs and the ordering column; never fetch post content."""
    sql = """
        SELECT id::text AS id, video_src, video_duration
        FROM public.blog_posts
        WHERE video_src IS NOT NULL
          AND btrim(video_src) <> ''
        ORDER BY upload_date ASC NULLS LAST, id ASC
    """
    with psycopg.connect(
        require_database_url(),
        connect_timeout=30,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())


def resolve_video_source(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        raise ValueError("video_src is empty")

    parsed = urlparse(source)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return source

    if parsed.scheme:
        raise ValueError("video_src uses an unsupported URL scheme")

    base_url = os.getenv("VIDEO_BASE_URL", "").strip()
    if not base_url:
        raise ValueError(
            "video_src is a relative path; configure the Actions variable VIDEO_BASE_URL"
        )

    base = base_url.rstrip("/") + "/"
    resolved = urljoin(base, source.lstrip("/"))
    resolved_parts = urlparse(resolved)
    if resolved_parts.scheme not in ("http", "https") or not resolved_parts.netloc:
        raise ValueError("VIDEO_BASE_URL did not produce a valid HTTP(S) URL")
    return resolved


def parse_duration(value: Any) -> float | None:
    """Parse seconds, MM:SS, HH:MM:SS, or simple h/m/s strings."""
    if value is None:
        return None

    raw = str(value).strip().lower()
    if not raw:
        return None

    try:
        numeric = float(raw)
        return numeric if numeric > 0 else None
    except ValueError:
        pass

    if re.fullmatch(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?){1,2}", raw):
        parts = [float(part) for part in raw.split(":")]
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        else:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        return seconds if seconds > 0 else None

    # Accept values such as "1h 2m 3s", "10m", or "45s".
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", raw)
    if matches:
        seconds = sum(
            float(number) * {"h": 3600, "m": 60, "s": 1}[unit]
            for number, unit in matches
        )
        return seconds if seconds > 0 else None

    return None


def probe_duration(source: str) -> float | None:
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        source,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    try:
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (TypeError, ValueError):
        return None


def classify_media_error(stderr: str, returncode: int) -> str:
    """Return a safe diagnostic without logging a video URL or query string."""
    message = (stderr or "").lower()
    match = re.search(r"(?:http error|server returned)\s*[: ]?\s*(\d{3})", message)
    if match:
        status = int(match.group(1))
        if status in (401, 403):
            return f"Video host returned HTTP {status}; access may be denied or the signed URL may have expired."
        if status == 404:
            return "Video host returned HTTP 404; source file was not found."
        if status == 429:
            return "Video host returned HTTP 429; request was rate limited."
        if status >= 500:
            return f"Video host returned HTTP {status}; server-side error."
        return f"Video host returned HTTP {status}."
    if "moov atom not found" in message:
        return "MP4 metadata is missing; file may be incomplete or invalid."
    if "invalid data found" in message or "could not find codec parameters" in message:
        return "FFmpeg could not recognize or decode the video stream."
    if "connection timed out" in message or "operation timed out" in message:
        return "Video host timed out while FFmpeg read the source."
    if "network is unreachable" in message or "connection refused" in message:
        return "Video host could not be reached from the GitHub runner."
    if "end of file" in message:
        return "Source ended before FFmpeg could read the midpoint frame."
    return f"FFmpeg exited with code {returncode}; raw output suppressed to protect signed video URLs."


def extract_midpoint(source: str, duration: float, output: Path) -> None:
    midpoint = duration / 2.0
    output.parent.mkdir(parents=True, exist_ok=True)
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-rw_timeout", "20000000"]

    # Fast seek first. Some remote origins do not support HTTP range requests,
    # so retry once with accurate input-first seeking when the error is not HTTP auth/not-found.
    fast_command = base + [
        "-ss", f"{midpoint:.3f}", "-i", source,
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale='min({MAX_IMAGE_WIDTH},iw)':-2",
        "-q:v", "3", "-an", str(output),
    ]
    try:
        result = subprocess.run(fast_command, check=False, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg timed out during fast midpoint seek") from exc
    except OSError as exc:
        raise RuntimeError(f"FFmpeg could not start ({type(exc).__name__})") from exc

    if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
        return

    fast_error = classify_media_error(result.stderr, result.returncode)
    if re.search(r"HTTP (401|403|404|429|5\d\d)", fast_error):
        raise RuntimeError(fast_error)

    output.unlink(missing_ok=True)
    accurate_command = base + [
        "-i", source, "-ss", f"{midpoint:.3f}",
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale='min({MAX_IMAGE_WIDTH},iw)':-2",
        "-q:v", "3", "-an", str(output),
    ]
    try:
        retry = subprocess.run(accurate_command, check=False, capture_output=True, text=True, timeout=360)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg timed out during accurate-seek retry") from exc
    except OSError as exc:
        raise RuntimeError(f"FFmpeg retry could not start ({type(exc).__name__})") from exc

    if retry.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        retry_error = classify_media_error(retry.stderr, retry.returncode)
        raise RuntimeError(f"Fast seek: {fast_error} Accurate seek: {retry_error}")


def replace_frames_directory(staging_frames: Path) -> None:
    destination = ROOT / "frames"
    backup = ROOT / ".frames-backup"

    if backup.exists():
        shutil.rmtree(backup)

    had_previous = destination.exists()
    if had_previous:
        destination.rename(backup)

    try:
        staging_frames.rename(destination)
    except Exception:
        if had_previous and backup.exists():
            backup.rename(destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def new_report() -> dict[str, Any]:
    return {
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "status": "running",
        "total_rows": 0,
        "generated": 0,
        "failures": [],
        "database": {"status": "pending", "updated": 0, "failed": 0},
        "notes": [],
    }


def add_failure(report: dict[str, Any], post_id: str, stage: str, reason: str, previous_frame_kept: bool) -> None:
    item = {
        "post_id": post_id,
        "stage": stage,
        "reason": reason,
        "previous_frame_kept": previous_frame_kept,
    }
    report["failures"].append(item)
    LOGGER.error(
        "FRAME_FAILED post_id=%s stage=%s previous_frame_kept=%s reason=%s",
        post_id, stage, previous_frame_kept, reason,
    )


def write_report(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "partial" if report.get("generated", 0) and report.get("failures") else (
        "success" if report.get("generated", 0) else "failed"
    )
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["post_id", "stage", "reason", "previous_frame_kept"])
        writer.writeheader()
        for item in report.get("failures", []):
            writer.writerow({key: item.get(key, "") for key in writer.fieldnames})


def write_step_summary(report: dict[str, Any], heading: str) -> None:
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    database = report.get("database", {})
    lines = [
        f"## {heading}",
        "",
        f"- **Status:** {report.get('status', 'unknown')}",
        f"- **Rows considered:** {report.get('total_rows', 0)}",
        f"- **Frames generated:** {report.get('generated', 0)}",
        f"- **Errors recorded:** {len(report.get('failures', []))}",
        f"- **Neon updates:** {database.get('updated', 0)}",
        f"- **Neon update failures:** {database.get('failed', 0)}",
        "",
    ]
    failures = report.get("failures", [])
    if failures:
        lines.extend(["### Errors", "", "| Post UUID | Stage | Reason |", "|---|---|---|"])
        for item in failures[:50]:
            post_id = str(item.get("post_id", "")).replace("|", "\\|")
            stage = str(item.get("stage", "")).replace("|", "\\|")
            reason = str(item.get("reason", "")).replace("|", "\\|")
            lines.append(f"| {post_id} | {stage} | {reason} |")
        if len(failures) > 50:
            lines.append(f"\nSee the CSV report for all {len(failures)} errors.")
    else:
        lines.append("No errors recorded.")
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def write_manifest(entries: list[dict[str, str]]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def prepare_frames() -> bool:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe must be installed on the runner.")

    rows = fetch_video_rows()
    if not rows:
        raise RuntimeError("No blog_posts rows with a non-empty video_src were found.")

    report = new_report()
    report["total_rows"] = len(rows)
    pages_base = pages_base_url()
    manifest_entries: list[dict[str, str]] = []
    LOGGER.info("Preparing midpoint frames for %s video rows.", len(rows))

    # Copy the previous frame tree first. Failed rows retain their old image,
    # and their database img value is intentionally not changed.
    with tempfile.TemporaryDirectory(prefix=".frames-staging-", dir=ROOT) as temp_dir:
        staging_root = Path(temp_dir)
        staging_frames = staging_root / "frames"
        existing_frames = ROOT / "frames"
        if existing_frames.is_dir():
            shutil.copytree(existing_frames, staging_frames, dirs_exist_ok=True)
        else:
            staging_frames.mkdir(parents=True, exist_ok=True)

        for index, row in enumerate(rows, start=1):
            post_id = str(row.get("id") or f"row-{index}")
            try:
                post_id = str(uuid.UUID(post_id))
            except (ValueError, TypeError):
                add_failure(report, post_id, "row_validation", "Invalid post UUID.", False)
                continue

            folder_number = ((index - 1) // MAX_IMAGES_PER_FOLDER) + 1
            relative_path = Path("frames") / f"{folder_number:04d}" / f"{post_id}.jpg"
            staged_output = staging_root / relative_path
            previous_frame_kept = (existing_frames / relative_path.relative_to("frames")).is_file()
            temp_output = staged_output.with_name(staged_output.stem + ".new.jpg")
            stage = "source"

            try:
                source = resolve_video_source(row.get("video_src"))
                stage = "duration"
                duration = probe_duration(source)
                if duration is None:
                    duration = parse_duration(row.get("video_duration"))
                if duration is None or duration <= 0:
                    raise RuntimeError("Could not determine a valid duration from ffprobe or video_duration.")

                stage = "ffmpeg"
                LOGGER.info(
                    "Extracting midpoint post_id=%s item=%s/%s at %.2f seconds.",
                    post_id, index, len(rows), duration / 2.0,
                )
                temp_output.parent.mkdir(parents=True, exist_ok=True)
                temp_output.unlink(missing_ok=True)
                extract_midpoint(source, duration, temp_output)

                # Replace only after extraction produced a valid non-empty image.
                os.replace(temp_output, staged_output)
                manifest_entries.append({
                    "id": post_id,
                    "img_url": f"{pages_base}/{relative_path.as_posix()}",
                })
                report["generated"] += 1
                LOGGER.info("FRAME_OK post_id=%s item=%s/%s", post_id, index, len(rows))

            except Exception as exc:
                temp_output.unlink(missing_ok=True)
                reason = str(exc).strip() or type(exc).__name__
                add_failure(report, post_id, stage, reason, previous_frame_kept)
                continue

        if report["generated"]:
            replace_frames_directory(staging_frames)
        else:
            report["notes"].append("No new frames were generated; the existing frames directory was left unchanged.")

    write_manifest(manifest_entries)
    report["database"]["status"] = "pending" if manifest_entries else "not_started"
    write_report(report)
    write_step_summary(report, "Midpoint frame extraction")

    LOGGER.info(
        "FRAME_BATCH_COMPLETE total=%s generated=%s failed=%s",
        report["total_rows"], report["generated"], len(report["failures"]),
    )
    # Partial success is allowed. Zero successes fails the job after preserving
    # the report so the workflow can upload it.
    return report["generated"] > 0


def update_database_images() -> None:
    path = manifest_path()
    if not path.is_file():
        raise RuntimeError(f"Frame manifest not found: {path}")

    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Frame manifest is empty or invalid.")

    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Frame manifest contains an invalid entry.")
        post_id = str(uuid.UUID(str(entry.get("id", ""))))
        image_url = str(entry.get("img_url", "")).strip()
        parsed = urlparse(image_url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith(".jpg"):
            raise RuntimeError(f"Invalid Pages image URL for post {post_id}.")
        if post_id in seen_ids:
            raise RuntimeError(f"Duplicate post ID in frame manifest: {post_id}.")
        seen_ids.add(post_id)

    sql = "UPDATE public.blog_posts SET img = %s WHERE id = %s"
    with psycopg.connect(
        require_database_url(),
        connect_timeout=30,
        prepare_threshold=None,
    ) as conn:
        with conn.cursor() as cur:
            for entry in entries:
                cur.execute(sql, (entry["img_url"], entry["id"]))
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"Expected one updated blog_posts row for {entry['id']}; "
                        f"updated {cur.rowcount}."
                    )

    LOGGER.info("Updated blog_posts.img for %s posts.", len(entries))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-db",
        action="store_true",
        help="Update blog_posts.img from the manifest after the frames have been pushed.",
    )
    args = parser.parse_args()

    if args.update_db:
        update_database_images()
    else:
        prepare_frames()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc
