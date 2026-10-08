# Models: downloaded by you, never bundled

**No model weights are shipped with this project, and none are committed to the
repository.**

This is deliberate, and it mirrors OpenScrub's own policy
("registry-download only, never bundled"). Two independent reasons:

1. **Size.** The segmentation and detection models are tens of megabytes.
2. **Licensing.** The models are **not** covered by OpenScrub's Apache-2.0.
   They carry their own terms, and some are restrictive.

## Model licences (read before you commercialize)

| Model | Licence | Notes |
|---|---|---|
| **YOLO11-seg** (person / full-body) | **AGPL-3.0** | Ultralytics. Strong copyleft. Distribution triggers source-disclosure obligations for the combined work. |
| **SCRFD det_10g** (face) | **Non-commercial research only** | InsightFace. Do **not** use commercially. |
| CenterFace | MIT | Permissive. |
| YuNet (built-in face default) | Apache-2.0 | Auto-downloaded, ~230 KB. No setup. |
| SFace (identity grouping) | Apache-2.0 | Auto-downloaded, ~38 MB. |
| VitTrack (generic tracker) | Apache-2.0 | Auto-downloaded, ~0.7 MB. |
| PP-OCRv5 / PP-HumanSeg | Apache-2.0 | Only needed for text/plate categories. |

## How the downloads work

The blur stage resolves models in this order:

1. A path you typed into the form (`person_model` / `face_model`).
2. The engine's own registry (`person_models.json`, `face_models.json`) —
   downloads on demand into your per-user data directory, e.g.
   `%LOCALAPPDATA%\OpenScrub\` on Windows.
3. A clear error telling you which URL to fetch and where to put the file.

The face model is **optional** — the engine falls back to the built-in YuNet
detector. The person (full-body silhouette) model is **required** for the blur
stage, and the stage reports itself unavailable until one is present.

## Bottom line

**If you publish or redistribute this project, do not commit model weights.**
`.gitignore` already blocks `*.onnx`, `*.pt` and `*.pth` for exactly this
reason. Users fetch their own, under their own licence terms.
