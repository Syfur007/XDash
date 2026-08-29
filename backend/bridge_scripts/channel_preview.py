"""bridge_scripts/channel_preview.py '<json args>' — renders every channel
datasets.channels.build_channels_from_groups() produces for one real sample
image as small base64 PNG tiles, so a channel_mode (m1..m5) becomes an
actual picture instead of an abstract config string — the spec's S2 gate
artifact ("Channel montage figure per dataset").

Args (single JSON-encoded object):
  image_path: repo-relative or absolute path to a real image file.
  mode: one of "m1".."m5".
  modality: "colour" | "grayscale" (drives modality_effective_channels() —
    the same modality-driven drop of the ycbcr group that
    datasets/augment.py's AugmentationPolicy applies at training time, so
    the preview matches what a real dataloader would actually produce).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def _channel_to_png_b64(channel, signed: bool) -> str:
    import numpy as np
    from PIL import Image

    arr = np.asarray(channel, dtype=np.float32)
    if signed:
        # xy/rtheta channels are ~[-1, 1]; remap to [0, 1] before the
        # generic per-tile min-max stretch below so a genuinely-zero
        # channel (e.g. a corner of an xy map) doesn't get stretched into
        # noise — the sign is meaningful here, unlike rgb/ycbcr.
        arr = (arr + 1.0) / 2.0
    lo, hi = float(arr.min()), float(arr.max())
    normalized = (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)
    img = Image.fromarray((normalized * 255).clip(0, 255).astype("uint8"), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def main(argv):
    if not argv:
        raise ValueError("usage: channel_preview.py '<json args: {image_path, mode, modality}>'")
    args = json.loads(argv[0])
    image_path = args.get("image_path")
    mode = args.get("mode", "m1")
    modality = args.get("modality", "colour")
    if not image_path:
        raise ValueError("image_path is required")
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    import numpy as np
    from PIL import Image

    from datasets.channels import (
        CHANNEL_GROUP_SIZES,
        build_channels_from_groups,
        modality_effective_channels,
    )

    img = Image.open(image_path).convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0

    groups = modality_effective_channels(mode, modality)
    channels = build_channels_from_groups(arr, groups)

    tiles = []
    offset = 0
    for group in groups:
        size = CHANNEL_GROUP_SIZES[group]
        for i in range(size):
            tiles.append({
                "group": group,
                "index_in_group": i,
                "png": _channel_to_png_b64(channels[:, :, offset + i], signed=group in ("xy", "rtheta")),
            })
        offset += size

    return {
        "mode": mode,
        "modality": modality,
        "groups": groups,
        "effective_channels": int(channels.shape[-1]),
        "source_size": [img.height, img.width],
        "tiles": tiles,
    }


if __name__ == "__main__":
    run_main(main)
