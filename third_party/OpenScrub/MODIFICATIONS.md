# Modifications to OpenScrub

**We ship these files UNMODIFIED.**

This directory vendors the OpenScrub engine so that `easy-video-prep` can run
blurring **in-process** instead of shelling out to a prebuilt `openscrub.exe`.

## What is vendored

| File | Purpose |
|---|---|
| `openscrub.py` | The engine (detection + render). Called via `importlib`, never edited. |
| `LICENSE` | Apache License 2.0, verbatim. |
| `NOTICE` | Upstream attribution notice, verbatim (required by Apache-2.0 §4d). |
| `person_models.json` | Person/model registry (points at download URLs). |
| `face_models.json` | Face model registry. |
| `plate_models.json` | License-plate model registry. |
| `requirements.txt` | Upstream dependency list, for reference. |
| `UPSTREAM.txt` | Upstream URL + the exact commit this was taken from. |

## What is NOT vendored

- `openscrub_web.py` / `zones_ui.py` — OpenScrub's own web UI. This project has
  its own, so they are deliberately excluded.
- `openscrub_gui.py` (legacy Tk UI), `openscrub_setup.py`, `openscrub_update.py`,
  `openscrub_vault.py`, Dockerfiles, packaging and test files.
- **Any model weights.** See `MODELS.md`.

## Changes

None. If any file in this directory is ever edited, the change **must** be
recorded here, as required by Apache-2.0 §4(b):

> "You must cause any modified files to carry prominent notices stating that
> You changed the files."

Current status: **no local modifications.**
