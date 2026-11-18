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
- The extraction stage starts from a copy of the existing frame tree. If one video cannot be accessed or decoded, that row is recorded and processing continues. Existing frames for failed rows are preserved, and their Neon img values are left unchanged. Successful frames are pushed and only their matching img values are updated.
- After all frames are generated, the workflow pushes the images to `main`. Only after that succeeds does it update the database URLs.
- Each manual run regenerates the complete frame set. It is intentionally not scheduled, to avoid repeatedly downloading every video without your instruction.

## Run it

Go to **Actions → Regenerate midpoint frames → Run workflow**. The GitHub Actions job installs FFmpeg and updates the repository and Neon database.

Individual video errors do not stop the batch when at least one frame succeeds. Each failure is logged with its post UUID, processing stage, and a sanitized reason; raw FFmpeg output and source URLs are never logged because they may contain temporary access signatures. Every run writes reports/frame-regeneration-report.json and reports/frame-regeneration-errors.csv, commits those reports to the repository, and uploads them as a GitHub Actions artifact. The run summary also lists failures. When zero frames succeed, no frame or database changes are made, but the report is still committed and uploaded.
