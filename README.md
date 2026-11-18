# Midpoint video frame generator

This repository stores one generated JPG midpoint frame per video in the Neon \`public.blog_posts\` table. Each successful frame is published through GitHub Pages, then its URL is written to \`public.blog_posts.img\`.

## One-time setup

1. In **Settings → Secrets and variables → Actions**, create or confirm the repository secret named \`DATABASE_URL\`. It must connect to the Neon database containing \`public.blog_posts\`.
2. In **Settings → Pages**, choose **Deploy from a branch**, select \`main\`, and select folder \`/(root)\`.
3. Wait for GitHub Pages to finish its first deployment.
4. Open **Actions → Regenerate midpoint frames → Run workflow** and run it on \`main\`.

The default Pages base URL is:

\`https://usercontent6.github.io/friendly-pancake\`

Override it with an Actions variable named \`PAGES_BASE_URL\` if your Pages URL differs.

### Relative video paths

Absolute \`http://\` and \`https://\` values in \`video_src\` are used directly. If \`video_src\` contains a relative path, configure the Actions variable \`VIDEO_BASE_URL\` with the base URL used to resolve those paths.

The source videos must be reachable from GitHub-hosted runners. Expiring signed URLs may fail if they expire before extraction.

## Frame folder layout

Images are stored in numbered folders with a maximum of 100 images per folder:

\`\`\`text
frames/
  0001/   # ordered video rows 1–100
  0002/   # ordered video rows 101–200
  0003/
\`\`\`

Rows are read in stable order: \`upload_date\` oldest first (null dates last), then \`id\`.

## Batch processing

The workflow no longer tries to process all videos in one large extraction pass.

1. It starts with a **10-video test batch**.
2. After the test batch's frames are committed to the repository and the successful image URLs are updated in Neon, it continues automatically with **50-video batches**.
3. Each completed batch is published before the next batch begins. The progress cursor advances only after the batch's Neon transaction succeeds.
4. If the runner stops during a batch, rerunning resumes that same batch. Valid JPGs already committed to the repository are reused, and the \`img\` update can safely be repeated.
5. Individual video failures (such as HTTP 404, inaccessible relative paths, or invalid media) are written to \`reports/frame-regeneration-errors.csv\` and the workflow continues with the remaining videos. Git, Neon connection, transaction, or publishing failures stop the run without advancing the cursor.

The persistent checkpoint branch is \`frame-regeneration-batches\`. The workflow publishes each batch to both that branch and \`main\`; it does not push workflow-definition changes into the checkpoint branch as part of a frame batch.

## Database safety

- The workflow reads only \`id\`, \`video_src\`, and \`video_duration\` from \`public.blog_posts\`; \`upload_date\` is used only for ordering.
- It updates only the \`img\` column.
- A batch's Neon updates are one transaction. If any update fails, the transaction rolls back and that batch's cursor is not advanced.
- It does not access \`comments\` or other post fields.

## Reports and status

Every batch writes:

- \`reports/frame-regeneration-report.json\`
- \`reports/frame-regeneration-errors.csv\`
- \`frame-progress.json\`

The reports are committed to the repository and uploaded as a GitHub Actions artifact. The CSV contains the video UUID, processing stage, and sanitized reason; raw source URLs and FFmpeg output are not logged because signed URLs may contain temporary credentials.
