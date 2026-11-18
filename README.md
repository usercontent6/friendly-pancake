# Midpoint video frame generator

This repository stores one generated JPG frame per video in the Neon `public.blog_posts` table. The frame is captured at the midpoint of the video and its GitHub Pages URL is written back to `blog_posts.img`.

## One-time setup

1. In **Settings → Secrets and variables → Actions**, create or confirm the repository secret named `DATABASE_URL`. It must connect to the Neon project whose database contains `public.blog_posts`.
2. In **Settings → Pages**, choose **Deploy from a branch**, select branch `main`, and select folder `/(root)`. Save.
3. Wait for GitHub Pages to finish its first deployment.
4. Open **Actions → Regenerate midpoint frames → Run workflow** and run it on `main`.

The default Pages base URL is:

`https://usercontent6.github.io/friendly-pancake`

You can override it by creating an Actions **variable** named `PAGES_BASE_URL` with your actual Pages base URL (no trailing slash required).

### Relative video paths

The script accepts absolute `http://` and `https://` video URLs. If `video_src` contains a relative path (for example, `videos/example.mp4`), add an Actions **variable** named `VIDEO_BASE_URL` containing the base URL used to resolve those paths.

The workflow needs the video URLs to be reachable by GitHub-hosted runners. Expiring signed URLs may fail if they expire before the frame can be extracted.

## Folder layout

Frames are written as:

```text
frames/
  0001/   # rows 1–100
    <post-uuid>.jpg
  0002/   # rows 101–200
    <post-uuid>.jpg
  0003/
    ...
```

No numbered folder receives more than 100 generated images. Rows are ordered by `upload_date` (oldest first, null dates last) and then by `id`, so newly uploaded posts normally append to the end.

## Safety and update behavior

- The workflow reads only `id`, `video_src`, and `video_duration` from `public.blog_posts`; it uses `upload_date` only to order rows.
- It updates only the `img` column. It does not access `comments` or other post fields.
- Duration is detected with `ffprobe`; `video_duration` is the fallback if the video metadata does not provide a usable duration.
- The workflow uses a persistent `frame-regeneration-progress` branch. It commits extracted frames to that branch every 25 database rows. If a runner stops or a final push fails, rerunning the workflow checks out the checkpoint branch, validates existing JPEG files, skips completed videos, and resumes. Only the final publication step merges the checkpoint branch with the latest `main` and pushes it to `main`. If that final push still fails, completed frames remain stored on the checkpoint branch.
- After all frames are generated, the workflow pushes the images to `main`. Only after that succeeds does it update the database URLs.
- Each manual run regenerates the complete frame set. It is intentionally not scheduled, to avoid repeatedly downloading every video without your instruction.

## Run it

Go to **Actions → Regenerate midpoint frames → Run workflow**. The GitHub Actions job installs FFmpeg and updates the repository and Neon database.

Individual video errors do not stop the batch. Completed frames are checkpointed every 25 rows and reused on later runs. Each failure is logged with its post UUID, processing stage, and a sanitized reason; raw FFmpeg output and source URLs are never logged because they may contain temporary access signatures. Every run writes reports/frame-regeneration-report.json and reports/frame-regeneration-errors.csv and uploads them as a GitHub Actions artifact. The run summary also lists failures. When zero frames succeed, no frame or database changes are made, but the report is still uploaded.
