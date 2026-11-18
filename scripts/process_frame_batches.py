#!/usr/bin/env python3
"""Run midpoint extraction in a 10-row test batch, then 50-row batches.

Each batch is saved to GitHub, published to main, and only then written to
public.blog_posts.img. Individual video failures are recorded and skipped.
Infrastructure, Git, or database failures stop the run without advancing the
batch cursor, so the same batch can be resumed safely.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from regenerate_frames import (
    ROOT,
    fetch_video_rows,
    is_valid_jpeg,
    pages_base_url,
    parse_duration,
    probe_duration,
    resolve_video_source,
    extract_midpoint,
)

CHECKPOINT_BRANCH = "frame-regeneration-batches"
STATE_PATH = ROOT / "frame-progress.json"
REPORT_DIR = ROOT / "reports"
REPORT_JSON = REPORT_DIR / "frame-regeneration-report.json"
REPORT_CSV = REPORT_DIR / "frame-regeneration-errors.csv"
PENDING_MANIFEST = REPORT_DIR / "pending-frame-batch.json"

INITIAL_BATCH_SIZE = 10
SUBSEQUENT_BATCH_SIZE = 50
MAX_IMAGES_PER_FOLDER = 100

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("frame-batches")


def utc_now() -> str:
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
        # Git may echo remote URLs. Remove embedded credentials before logging.
        detail = detail[-1800:]
        raise RuntimeError(f"Git command failed ({' '.join(args[:3])}): {detail}")
    return result


def ensure_branch() -> None:
    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if current != CHECKPOINT_BRANCH:
        raise RuntimeError(
            f"Expected branch {CHECKPOINT_BRANCH}, but checked out {current}."
        )

    run_git(["config", "user.name", "github-actions[bot]"])
    run_git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])


def load_state(total_rows: int) -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "version": 1,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "total_rows": total_rows,
            "next_index": 0,
            "batches_completed": 0,
            "generated": 0,
            "resumed": 0,
            "video_failures": [],
            "history": [],
            "status": "running",
        }

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read {STATE_PATH.name}: {type(exc).__name__}") from exc

    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError("The saved frame progress file has an unsupported format.")

    if int(state.get("total_rows", -1)) != total_rows:
        raise RuntimeError(
            "The number of source video rows changed since the last batch. "
            "The saved cursor was not advanced; review frame-progress.json before continuing."
        )

    next_index = int(state.get("next_index", 0))
    if next_index < 0 or next_index > total_rows:
        raise RuntimeError("The saved frame progress cursor is outside the available rows.")

    return state


def write_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def record_video_failure(
    state: dict[str, Any], post_id: str, stage: str, reason: str
) -> None:
    failures = state.setdefault("video_failures", [])
    key = (post_id, stage, reason)
    if not any(
        (item.get("post_id"), item.get("stage"), item.get("reason")) == key
        for item in failures
    ):
        failures.append({
            "post_id": post_id,
            "stage": stage,
            "reason": reason,
            "recorded_at": utc_now(),
        })
    LOGGER.error("VIDEO_SKIPPED post_id=%s stage=%s reason=%s", post_id, stage, reason)


def write_reports(state: dict[str, Any], last_batch: dict[str, Any] | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "updated_at": utc_now(),
        "status": state.get("status", "running"),
        "total_rows": state.get("total_rows", 0),
        "next_index": state.get("next_index", 0),
        "batches_completed": state.get("batches_completed", 0),
        "generated": state.get("generated", 0),
        "resumed": state.get("resumed", 0),
        "video_failures": len(state.get("video_failures", [])),
        "last_batch": last_batch,
        "history": state.get("history", []),
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["post_id", "stage", "reason", "recorded_at"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in state.get("video_failures", []):
            writer.writerow({field: item.get(field, "") for field in fields})


def push_to_github() -> None:
    """Persist the current commit to the checkpoint branch and then to Pages main."""
    branch_push = run_git(
        ["push", "origin", f"HEAD:refs/heads/{CHECKPOINT_BRANCH}"],
        check=False,
    )
    if branch_push.returncode != 0:
        detail = (branch_push.stderr or branch_push.stdout or "").strip()
        raise RuntimeError(
            "Checkpoint branch push failed; batch was not marked complete. "
            + detail[-1600:]
        )

    main_push = run_git(
        ["push", "origin", "HEAD:refs/heads/main"],
        check=False,
    )
    if main_push.returncode != 0:
        detail = (main_push.stderr or main_push.stdout or "").strip()
        raise RuntimeError(
            "Checkpoint branch was saved, but publishing to main failed. "
            "No Neon updates or cursor advance were made for this batch. "
            + detail[-1600:]
        )


def commit_and_publish(message: str) -> bool:
    run_git(["add", "--all", "--", "frames", "reports", STATE_PATH.name])
    staged = run_git(["diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        return False
    run_git(["commit", "-m", message])
    push_to_github()
    return True


def update_neon(entries: list[dict[str, str]]) -> int:
    if not entries:
        return 0

    sql = "UPDATE public.blog_posts SET img = %s WHERE id = %s"
    updated = 0

    # A single transaction is intentional. If any database row fails, the
    # whole batch rolls back and the saved cursor remains unchanged.
    with psycopg.connect(
        database_url(),
        connect_timeout=30,
        prepare_threshold=None,
    ) as conn:
        with conn.cursor() as cur:
            for entry in entries:
                post_id = str(uuid.UUID(entry["id"]))
                image_url = entry["img_url"]
                cur.execute(sql, (image_url, post_id))
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"Neon update for {post_id} matched {cur.rowcount} rows; expected exactly one."
                    )
                updated += 1

    return updated


def build_image_url(relative_path: Path) -> str:
    return f"{pages_base_url()}/{relative_path.as_posix()}"


def process_one_batch(
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    start: int,
    end: int,
) -> None:
    batch_number = int(state.get("batches_completed", 0)) + 1
    batch_size = end - start
    initial_test = batch_number == 1
    successes: list[dict[str, str]] = []
    generated_now = 0
    resumed_now = 0
    failed_now = 0

    LOGGER.info(
        "BATCH_START batch=%s type=%s rows=%s-%s size=%s total=%s",
        batch_number,
        "initial-test" if initial_test else "standard",
        start + 1,
        end,
        batch_size,
        len(rows),
    )

    for zero_index in range(start, end):
        row = rows[zero_index]
        display_index = zero_index + 1
        raw_id = str(row.get("id") or f"row-{display_index}")

        try:
            post_id = str(uuid.UUID(raw_id))
        except (ValueError, TypeError):
            failed_now += 1
            record_video_failure(state, raw_id, "row_validation", "Invalid post UUID.")
            continue

        folder_number = (zero_index // MAX_IMAGES_PER_FOLDER) + 1
        relative_path = Path("frames") / f"{folder_number:04d}" / f"{post_id}.jpg"
        output = ROOT / relative_path

        # A valid image already in the checkpoint branch is safe to reuse.
        if is_valid_jpeg(output):
            successes.append({"id": post_id, "img_url": build_image_url(relative_path)})
            resumed_now += 1
            LOGGER.info("FRAME_RESUMED post_id=%s item=%s/%s", post_id, display_index, len(rows))
            continue

        stage = "source"
        temporary = output.with_name(output.stem + ".partial.jpg")
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
            LOGGER.info(
                "Extracting midpoint post_id=%s item=%s/%s at %.2f seconds",
                post_id,
                display_index,
                len(rows),
                duration / 2.0,
            )
            extract_midpoint(source, duration, temporary)

            if not is_valid_jpeg(temporary):
                raise RuntimeError("FFmpeg output failed JPEG integrity validation.")

            os.replace(temporary, output)
            successes.append({"id": post_id, "img_url": build_image_url(relative_path)})
            generated_now += 1
            LOGGER.info("FRAME_OK post_id=%s item=%s/%s", post_id, display_index, len(rows))

        except Exception as exc:
            temporary.unlink(missing_ok=True)
            failed_now += 1
            reason = str(exc).strip() or type(exc).__name__
            record_video_failure(state, post_id, stage, reason)

    # Save the images and a manifest before touching Neon. If a later step
    # fails, the next run reuses these valid JPEGs instead of re-extracting.
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_MANIFEST.write_text(
        json.dumps({
            "batch_number": batch_number,
            "start_index": start,
            "end_index": end,
            "entries": successes,
        }, indent=2),
        encoding="utf-8",
    )

    state["generated"] = int(state.get("generated", 0)) + generated_now
    state["resumed"] = int(state.get("resumed", 0)) + resumed_now
    # Create the cursor file before the first image commit. The saved cursor
    # intentionally remains at "start" until Neon confirms the batch.
    write_state(state)
    last_batch = {
        "batch_number": batch_number,
        "start_index": start,
        "end_index": end,
        "rows": batch_size,
        "generated": generated_now,
        "resumed": resumed_now,
        "video_failures": failed_now,
        "db_updated": 0,
        "started_at": utc_now(),
    }
    write_reports(state, last_batch)

    # This commit contains only frame/report files, not workflow definitions.
    commit_and_publish(
        f"Publish frame batch {batch_number} ({start + 1}-{end})"
    )

    # Neon is updated only after the image files are on main/Pages source.
    updated = update_neon(successes)
    last_batch["db_updated"] = updated
    last_batch["completed_at"] = utc_now()

    # Advance the durable cursor only after the complete batch transaction
    # succeeds. Re-running a batch is safe because img updates are idempotent.
    state["next_index"] = end
    state["batches_completed"] = batch_number
    state.setdefault("history", []).append(last_batch)
    state["status"] = "running" if end < len(rows) else "completed"

    PENDING_MANIFEST.unlink(missing_ok=True)
    write_state(state)
    write_reports(state, last_batch)
    commit_and_publish(f"Record completed frame batch {batch_number}")

    LOGGER.info(
        "BATCH_COMPLETE batch=%s generated=%s resumed=%s video_failures=%s neon_updated=%s cursor=%s/%s",
        batch_number,
        generated_now,
        resumed_now,
        failed_now,
        updated,
        end,
        len(rows),
    )


def main() -> int:
    ensure_branch()
    rows = fetch_video_rows()
    if not rows:
        raise RuntimeError("No public.blog_posts rows with a non-empty video_src were found.")

    state = load_state(len(rows))
    LOGGER.info(
        "BATCH_RUN_START rows=%s next_index=%s batches_completed=%s initial_batch=%s subsequent_batch=%s",
        len(rows),
        state["next_index"],
        state["batches_completed"],
        INITIAL_BATCH_SIZE,
        SUBSEQUENT_BATCH_SIZE,
    )

    while int(state["next_index"]) < len(rows):
        start = int(state["next_index"])
        batch_size = INITIAL_BATCH_SIZE if start == 0 else SUBSEQUENT_BATCH_SIZE
        end = min(start + batch_size, len(rows))
        process_one_batch(rows, state, start, end)

    state["status"] = "completed"
    write_state(state)
    write_reports(state)
    commit_and_publish("Mark all midpoint frame batches complete")

    LOGGER.info(
        "ALL_BATCHES_COMPLETE rows=%s batches=%s generated=%s resumed=%s video_failures=%s",
        len(rows),
        state["batches_completed"],
        state["generated"],
        state["resumed"],
        len(state.get("video_failures", [])),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOGGER.error("BATCH_RUN_FATAL %s: %s", type(exc).__name__, str(exc).strip())
        raise SystemExit(1)
