#!/usr/bin/env python3
"""Retry only the video UUIDs currently listed in reports/frame-regeneration-errors.csv."""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from regenerate_frames import (
    ROOT,
    extract_midpoint,
    is_valid_jpeg,
    pages_base_url,
    parse_duration,
    probe_duration,
    resolve_video_source,
)

ERROR_CSV = ROOT / "reports" / "frame-regeneration-errors.csv"
REPORT_DIR = ROOT / "reports"
RETRY_REPORT = REPORT_DIR / "frame-regeneration-retry-report.json"
RETRY_HISTORY = REPORT_DIR / "frame-regeneration-retry-history.csv"

CHECKPOINT_BRANCH = "main"
BATCH_SIZE = 50
MAX_IMAGES_PER_FOLDER = 100

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("retry-failed-frames")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is missing.")
    return value


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Git command failed ({' '.join(args[:3])}): {detail[-1800:]}"
        )
    return result


def ensure_branch() -> None:
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != CHECKPOINT_BRANCH:
        raise RuntimeError(
            f"Expected to run directly on main, found branch {branch}."
        )
    run_git(["config", "user.name", "github-actions[bot]"])
    run_git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])


def load_error_csv() -> list[dict[str, str]]:
    if not ERROR_CSV.is_file():
        raise RuntimeError("reports/frame-regeneration-errors.csv does not exist.")
    with ERROR_CSV.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        seen: set[str] = set()
        for raw in reader:
            post_id_raw = str(raw.get("post_id") or "").strip()
            try:
                post_id = str(uuid.UUID(post_id_raw))
            except (ValueError, TypeError):
                # Keep malformed entries visible in the report rather than
                # making a non-video-specific CSV error crash the whole retry.
                post_id = post_id_raw
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            rows.append({
                "post_id": post_id,
                "stage": str(raw.get("stage") or "unknown"),
                "reason": str(raw.get("reason") or "Previous extraction failed."),
                "recorded_at": str(raw.get("recorded_at") or now()),
                "last_attempted_at": str(raw.get("last_attempted_at") or ""),
                "attempt_count": str(raw.get("attempt_count") or "0"),
            })
        return rows


def fetch_only_failed_video_rows(
    error_rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    valid_ids: list[uuid.UUID] = []
    invalid_rows: list[dict[str, str]] = []
    for item in error_rows:
        try:
            valid_ids.append(uuid.UUID(item["post_id"]))
        except (ValueError, TypeError):
            invalid_rows.append({
                **item,
                "stage": "retry_lookup",
                "reason": "The error CSV contains an invalid UUID.",
            })

    if not valid_ids:
        return [], invalid_rows

    # row_num preserves the original numbered-folder placement. Media fields
    # are returned only for UUIDs present in the error CSV.
    sql = """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    ORDER BY upload_date ASC NULLS LAST, id ASC
                ) AS row_num
            FROM public.blog_posts
            WHERE video_src IS NOT NULL
              AND btrim(video_src) <> ''
        )
        SELECT
            p.id::text AS id,
            p.video_src,
            p.video_duration,
            ranked.row_num
        FROM public.blog_posts AS p
        JOIN ranked ON ranked.id = p.id
        WHERE p.id = ANY(%s)
        ORDER BY ranked.row_num ASC
    """

    with psycopg.connect(
        database_url(),
        connect_timeout=30,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (valid_ids,))
            rows = list(cur.fetchall())

    found = {str(row["id"]) for row in rows}
    for item in error_rows:
        if item["post_id"] not in found and item["post_id"] not in {
            x["post_id"] for x in invalid_rows
        }:
            invalid_rows.append({
                **item,
                "stage": "retry_lookup",
                "reason": "No current blog_posts row with a non-empty video_src matched this UUID.",
            })

    return rows, invalid_rows


def write_error_csv(rows: list[dict[str, str]]) -> None:
    fields = [
        "post_id",
        "stage",
        "reason",
        "recorded_at",
        "last_attempted_at",
        "attempt_count",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with ERROR_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in rows:
            writer.writerow({key: item.get(key, "") for key in fields})


def append_history(results: list[dict[str, str]]) -> None:
    fields = [
        "post_id",
        "outcome",
        "stage",
        "reason",
        "previous_reason",
        "attempted_at",
        "img_url",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    exists = RETRY_HISTORY.is_file()
    with RETRY_HISTORY.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for item in results:
            writer.writerow({key: item.get(key, "") for key in fields})


def write_retry_report(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RETRY_REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def commit_and_publish(message: str) -> None:
    run_git([
        "add",
        "--all",
        "--",
        "frames",
        "reports/frame-regeneration-errors.csv",
        "reports/frame-regeneration-retry-report.json",
        "reports/frame-regeneration-retry-history.csv",
    ])
    staged = run_git(["diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        return

    run_git(["commit", "-m", message])

    main_push = run_git(
        ["push", "origin", "HEAD:refs/heads/main"],
        check=False,
    )
    if main_push.returncode != 0:
        detail = (main_push.stderr or main_push.stdout or "").strip()
        raise RuntimeError(
            "Publishing the retry batch to main failed; Neon was not updated. "
            + detail[-1600:]
        )


def update_only_recovered_rows(entries: list[dict[str, str]]) -> int:
    if not entries:
        return 0

    sql = "UPDATE public.blog_posts SET img = %s WHERE id = %s"
    updated = 0
    # One transaction for this retry batch: either every successful frame URL
    # is recorded, or none of this batch's img values are changed.
    with psycopg.connect(
        database_url(),
        connect_timeout=30,
        prepare_threshold=None,
    ) as conn:
        with conn.cursor() as cur:
            for item in entries:
                cur.execute(sql, (item["img_url"], item["id"]))
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"Expected one blog_posts row for {item['id']}; updated {cur.rowcount}."
                    )
                updated += 1
    return updated


def main() -> int:
    ensure_branch()
    pages_base = pages_base_url()
    source_errors = load_error_csv()

    if not source_errors:
        report = {
            "status": "nothing_to_retry",
            "started_at": now(),
            "source": "reports/frame-regeneration-errors.csv",
            "previous_errors": 0,
            "attempted": 0,
            "recovered": 0,
            "still_failing": 0,
            "message": "The error CSV contains no rows.",
        }
        write_retry_report(report)
        commit_and_publish("Record empty failed-frame retry report")
        LOGGER.info("RETRY_COMPLETE no error rows found")
        return 0

    video_rows, lookup_failures = fetch_only_failed_video_rows(source_errors)
    pending_by_id = {item["post_id"]: item for item in source_errors}
    # Keep every CSV row pending until its batch has successfully updated Neon.
    # This preserves rows belonging to later batches when we save intermediate results.
    still_failing: dict[str, dict[str, str]] = {
        item["post_id"]: item for item in source_errors
    }
    for item in lookup_failures:
        still_failing[item["post_id"]] = item

    report: dict[str, Any] = {
        "status": "running",
        "started_at": now(),
        "source": "reports/frame-regeneration-errors.csv",
        "previous_errors": len(source_errors),
        "attempted": 0,
        "recovered": 0,
        "still_failing": len(lookup_failures),
        "batches": [],
    }
    results_history: list[dict[str, str]] = []
    recovered_ids: set[str] = set()

    if lookup_failures:
        results_history.extend({
            "post_id": item["post_id"],
            "outcome": "not_found_in_neon",
            "stage": item["stage"],
            "reason": item["reason"],
            "previous_reason": item.get("reason", ""),
            "attempted_at": now(),
            "img_url": "",
        } for item in lookup_failures)

    LOGGER.info(
        "RETRY_START csv_errors=%s matched_neon_rows=%s lookup_failures=%s batch_size=%s",
        len(source_errors),
        len(video_rows),
        len(lookup_failures),
        BATCH_SIZE,
    )

    for start in range(0, len(video_rows), BATCH_SIZE):
        batch = video_rows[start:start + BATCH_SIZE]
        successes: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        batch_history: list[dict[str, str]] = []
        generated = 0
        reused = 0

        LOGGER.info(
            "RETRY_BATCH_START batch=%s rows=%s-%s size=%s",
            (start // BATCH_SIZE) + 1,
            start + 1,
            start + len(batch),
            len(batch),
        )

        for row in batch:
            post_id = str(uuid.UUID(str(row["id"])))
            previous = pending_by_id.get(post_id, {})
            row_num = int(row["row_num"])
            folder = ((row_num - 1) // MAX_IMAGES_PER_FOLDER) + 1
            relative_path = Path("frames") / f"{folder:04d}" / f"{post_id}.jpg"
            output = ROOT / relative_path
            image_url = f"{pages_base}/{relative_path.as_posix()}"
            attempted_at = now()
            attempt_count = int(previous.get("attempt_count") or 0) + 1

            if is_valid_jpeg(output):
                successes.append({"id": post_id, "img_url": image_url})
                reused += 1
                batch_history.append({
                    "post_id": post_id,
                    "outcome": "reused_existing_frame",
                    "stage": "complete",
                    "reason": "",
                    "previous_reason": previous.get("reason", ""),
                    "attempted_at": attempted_at,
                    "img_url": image_url,
                })
                LOGGER.info("RETRY_FRAME_REUSED post_id=%s", post_id)
                continue

            stage = "source"
            temporary = output.with_name(output.stem + ".retry-partial.jpg")
            try:
                source = resolve_video_source(row.get("video_src"))

                stage = "duration"
                duration = probe_duration(source)
                if duration is None:
                    duration = parse_duration(row.get("video_duration"))
                if duration is None or duration <= 0:
                    raise RuntimeError(
                        "Could not determine a valid duration from ffprobe or video_duration."
                    )

                stage = "ffmpeg"
                output.parent.mkdir(parents=True, exist_ok=True)
                temporary.unlink(missing_ok=True)
                extract_midpoint(source, duration, temporary)
                if not is_valid_jpeg(temporary):
                    raise RuntimeError("FFmpeg output failed JPEG integrity validation.")

                os.replace(temporary, output)
                successes.append({"id": post_id, "img_url": image_url})
                generated += 1
                batch_history.append({
                    "post_id": post_id,
                    "outcome": "frame_extracted",
                    "stage": "complete",
                    "reason": "",
                    "previous_reason": previous.get("reason", ""),
                    "attempted_at": attempted_at,
                    "img_url": image_url,
                })
                LOGGER.info("RETRY_FRAME_OK post_id=%s", post_id)

            except Exception as exc:
                temporary.unlink(missing_ok=True)
                reason = str(exc).strip() or type(exc).__name__
                failure = {
                    "post_id": post_id,
                    "stage": stage,
                    "reason": reason,
                    "recorded_at": previous.get("recorded_at") or attempted_at,
                    "last_attempted_at": attempted_at,
                    "attempt_count": str(attempt_count),
                }
                failed.append(failure)
                batch_history.append({
                    "post_id": post_id,
                    "outcome": "still_failing",
                    "stage": stage,
                    "reason": reason,
                    "previous_reason": previous.get("reason", ""),
                    "attempted_at": attempted_at,
                    "img_url": "",
                })
                LOGGER.error("RETRY_STILL_FAILED post_id=%s stage=%s reason=%s", post_id, stage, reason)

        # Persist successful images before updating Neon. If a database step
        # later fails, another run finds and reuses these validated JPEG files.
        report["batches"].append({
            "batch": (start // BATCH_SIZE) + 1,
            "rows": len(batch),
            "generated": generated,
            "reused": reused,
            "frame_successes": len(successes),
            "video_failures": len(failed),
            "stage": "frames_published_pending_db",
        })
        write_retry_report(report)
        append_history(batch_history)
        commit_and_publish(
            f"Save failed-frame retry batch {(start // BATCH_SIZE) + 1} frames"
        )

        # Only UUIDs from the original error CSV are passed to this UPDATE.
        updated = update_only_recovered_rows(successes)

        report["attempted"] += len(batch)
        report["recovered"] += len(successes)

        for item in successes:
            recovered_ids.add(item["id"])
            still_failing.pop(item["id"], None)

        for item in failed:
            still_failing[item["post_id"]] = item

        report["batches"][-1]["neon_updated"] = updated
        report["batches"][-1]["stage"] = "database_updated"
        report["still_failing"] = len(still_failing)
        report["updated_at"] = now()

        write_error_csv(list(still_failing.values()))
        write_retry_report(report)
        commit_and_publish(
            f"Update failed-frame retry report after batch {(start // BATCH_SIZE) + 1}"
        )

        LOGGER.info(
            "RETRY_BATCH_COMPLETE batch=%s extracted=%s reused=%s neon_updated=%s still_failing=%s",
            (start // BATCH_SIZE) + 1,
            generated,
            reused,
            updated,
            len(still_failing),
        )

    # Keep malformed or missing Neon UUID rows in the error CSV for manual review.
    write_error_csv(list(still_failing.values()))
    report["status"] = "completed"
    report["finished_at"] = now()
    report["recovered"] = len(recovered_ids)
    report["still_failing"] = len(still_failing)
    report["recovered_ids"] = sorted(recovered_ids)
    report["remaining_failed_ids"] = sorted(still_failing)
    write_retry_report(report)
    commit_and_publish("Finalize failed-frame retry report")

    LOGGER.info(
        "RETRY_COMPLETE previous=%s attempted=%s recovered=%s remaining=%s",
        len(source_errors),
        report["attempted"],
        report["recovered"],
        report["still_failing"],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOGGER.error("RETRY_FATAL %s: %s", type(exc).__name__, str(exc).strip())
        raise SystemExit(1)
