#!/usr/bin/env python3
"""Create midpoint JPG frames and update public.blog_posts.img after the push."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGES_BASE_URL = "https://usercontent6.github.io/friendly-pancake"
DEFAULT_MANIFEST = Path(tempfile.gettempdir()) / "frame_manifest.json"
MAX_IMAGES_PER_FOLDER = 100
MAX_IMAGE_WIDTH = 1280

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
    with psycopg.connect(require_database_url(), connect_timeout=30, row_factory=dict_row) as conn:
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


def extract_midpoint(source: str, duration: float, output: Path) -> None:
    midpoint = duration / 2.0
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-ss", f"{midpoint:.3f}",
        "-i", source,
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", f"scale='min({MAX_IMAGE_WIDTH},iw)':-2",
        "-q:v", "3",
        "-an",
        str(output),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg timed out while seeking to the midpoint") from exc
    except OSError as exc:
        raise RuntimeError("FFmpeg could not be started") from exc

    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        # Do not print FFmpeg stderr: it may contain a signed video URL.
        raise RuntimeError("FFmpeg could not extract a midpoint frame")


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


def prepare_frames() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and ffprobe must be installed on the runner.")

    rows = fetch_video_rows()
    if not rows:
        raise RuntimeError("No blog_posts rows with a non-empty video_src were found.")

    pages_base = pages_base_url()
    manifest_entries: list[dict[str, str]] = []

    LOGGER.info("Preparing midpoint frames for %s video rows.", len(rows))

    # Build the complete replacement off to the side. Existing frames remain intact
    # unless every row has produced a valid image.
    with tempfile.TemporaryDirectory(prefix=".frames-staging-", dir=ROOT) as temp_dir:
        staging_root = Path(temp_dir)
        staging_frames = staging_root / "frames"

        for index, row in enumerate(rows, start=1):
            post_id = str(uuid.UUID(str(row["id"])))
            source = resolve_video_source(row.get("video_src"))

            duration = probe_duration(source)
            if duration is None:
                duration = parse_duration(row.get("video_duration"))
            if duration is None:
                raise RuntimeError(
                    f"Could not determine video duration for post {post_id}; "
                    "ffprobe and video_duration both failed."
                )

            folder_number = ((index - 1) // MAX_IMAGES_PER_FOLDER) + 1
            relative_path = Path("frames") / f"{folder_number:04d}" / f"{post_id}.jpg"
            staged_output = staging_root / relative_path

            LOGGER.info(
                "Extracting post %s (%s of %s) at %.2f seconds.",
                post_id,
                index,
                len(rows),
                duration / 2.0,
            )
            extract_midpoint(source, duration, staged_output)

            manifest_entries.append({
                "id": post_id,
                "img_url": f"{pages_base}/{relative_path.as_posix()}",
            })

        replace_frames_directory(staging_frames)

    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest_entries, indent=2),
        encoding="utf-8",
    )
    LOGGER.info(
        "Generated %s frames in %s folders. Manifest: %s",
        len(manifest_entries),
        (len(manifest_entries) + MAX_IMAGES_PER_FOLDER - 1) // MAX_IMAGES_PER_FOLDER,
        path,
    )


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
    with psycopg.connect(require_database_url(), connect_timeout=30) as conn:
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
