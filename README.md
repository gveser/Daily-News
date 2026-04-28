# News of the Day (personal homepage)

This project builds a static webpage (`dist/index.html`) that shows **today's headlines** in a **4×4 grid** (16 blocks total), pulled from:

- The Economist
- The Guardian
- Der Spiegel
- The Straits Times

Each block contains a headline, source, and (when available) a photo that is downloaded locally to `dist/static/images/`.

## Quick start

1) Create/activate a virtual environment (recommended).

2) Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3) Build the page:

```bash
python build.py
```

4) Open the generated file in your browser:

- `dist/index.html`

## Publish & auto-update via GitHub Pages (no server needed)

If you want the page to update automatically *without* running your laptop, use GitHub Pages.
This repo includes a workflow at `.github/workflows/pages.yml` that:

- runs `python build.py` on a schedule (every 2 hours)
- deploys `dist/` to GitHub Pages

Steps:

1) Push this folder to a GitHub repository.

2) In GitHub, go to **Settings → Pages** and set:
   - **Build and deployment**: “GitHub Actions”

3) Go to the **Actions** tab and run the workflow once (or wait for the schedule).

Then you’ll get a public URL (shown in the Pages settings) that you can open on any device.

## Notes

- News sites occasionally change their RSS formats; this script is written to be resilient, but a broken/missing image usually means the feed item simply didn't include one.
- Images are cached by URL hash so re-building is fast.

