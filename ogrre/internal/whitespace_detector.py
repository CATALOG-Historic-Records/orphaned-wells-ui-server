"""
whitespace_detector.py

Detects the percentage of "non-ink" (near-white) pixels in an image.
Supports configurable thresholds for both strictness and target percentage.
Accepts local file paths or HTTP/HTTPS URLs — auto-detected.

Memory strategy for URLs:
  - Streams the response in chunks into a BytesIO buffer (no temp file on disk).
  - BytesIO is passed directly to PIL, which decodes only what it needs.
  - The buffer and image are explicitly closed/deleted after the numpy array
    is built, so peak memory = compressed download + one RGB pixel array.
"""

import io
import urllib.request
from pathlib import Path

from PIL import Image
import numpy as np

from ogrre.internal.util import time_it


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_url(source: str) -> bool:
    """Return True if source looks like an HTTP/HTTPS URL."""
    return source.lower().startswith(("http://", "https://"))


def _open_image(source: str) -> Image.Image:
    """
    Open an image from a local path or a URL.

    For URLs the response is streamed into an in-memory BytesIO buffer in
    64 KB chunks — no temp file is ever written to disk.
    """
    if _is_url(source):
        buf = io.BytesIO()
        req = urllib.request.Request(
            source,
            headers={"User-Agent": "whitespace-detector/1.0"},
        )
        with urllib.request.urlopen(req) as response:
            while chunk := response.read(65536):  # 64 KB chunks
                buf.write(chunk)
        buf.seek(0)
        img = Image.open(buf)
        img.load()  # force full decode while buf is still in scope
        buf.close()
        return img
    else:
        return Image.open(source)


def detect_whitespace(
    image_path: str,
    threshold: int = 240,
    min_whitespace_pct: float = None,
    channel_mode: str = "all",
) -> dict:
    """
    Detect the percentage of near-white (non-ink) pixels in an image.

    Parameters
    ----------
    image_path : str
        Local file path OR an HTTP/HTTPS URL pointing to an image.
        Supports any format PIL can read: JPEG, PNG, TIFF, BMP, WebP, etc.
        URLs are streamed into memory — no temp file is written to disk.

    threshold : int (0–255), default 240
        Strictness of what counts as "white/non-ink".
        Higher = stricter (only very bright pixels qualify).
        Lower = more lenient (includes off-white, light grey, cream, etc.).

        Suggested presets:
          255         — Pure white only (extremely strict)
          245–254     — Very strict: near-pure white
          230–244     — Strict: bright white + very light grey  ← default ~240
          200–229     — Moderate: includes light grey, ivory
          150–199     — Lax: light colors, washed-out backgrounds
          <150        — Very lax: mid-tones also count as "non-ink"

    min_whitespace_pct : float or None, default None
        If provided, the function will also return a boolean `meets_threshold`
        indicating whether the detected whitespace percentage >= this value.
        Example: pass 10.0 to check if at least 10% of the image is whitespace.

    channel_mode : str, default "all"
        How to evaluate each pixel across R, G, B channels:
          "all"  — ALL channels must be >= threshold (strict: only grey/white tones)
          "any"  — ANY channel >= threshold (lenient: catches near-white in one channel)
          "mean" — The MEAN of channels must be >= threshold (balanced)
          "luma" — Uses perceptual luminance (0.299R + 0.587G + 0.114B) >= threshold

    Returns
    -------
    dict with keys:
        whitespace_pct    : float — Percentage of pixels classified as non-ink (0–100)
        ink_pct           : float — Percentage of pixels classified as ink (0–100)
        total_pixels      : int   — Total pixel count
        white_pixels      : int   — Count of non-ink pixels
        threshold         : int   — The threshold used
        channel_mode      : str   — The channel mode used
        source_type       : str   — "url" or "file"
        meets_threshold   : bool  — (only if min_whitespace_pct is provided)
        min_whitespace_pct: float — (only if min_whitespace_pct is provided)

    Examples
    --------
    # Local file — default settings
    result = detect_whitespace("scan.png")
    print(f"{result['whitespace_pct']:.1f}% non-ink")

    # URL — strict, check if >= 5%
    result = detect_whitespace("https://example.com/img.png", threshold=250, min_whitespace_pct=5.0)

    # Lax: light greys + cream count, check if >= 10%
    result = detect_whitespace("scan.png", threshold=200, min_whitespace_pct=10.0)

    # Perceptual luminance — most visually accurate
    result = detect_whitespace("scan.png", threshold=235, channel_mode="luma")
    """
    source_type = "url" if _is_url(image_path) else "file"

    img = _open_image(image_path).convert("RGB")

    # Build the pixel array, then immediately close + release the PIL image
    # so peak RAM = one float32 RGB array rather than array + PIL internals.
    pixels = np.array(img, dtype=np.float32)  # shape: (H, W, 3)
    img.close()
    del img
    r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]

    if channel_mode == "all":
        mask = (r >= threshold) & (g >= threshold) & (b >= threshold)
    elif channel_mode == "any":
        mask = (r >= threshold) | (g >= threshold) | (b >= threshold)
    elif channel_mode == "mean":
        mean = (r + g + b) / 3.0
        mask = mean >= threshold
    elif channel_mode == "luma":
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        mask = luma >= threshold
    else:
        raise ValueError(
            f"Invalid channel_mode '{channel_mode}'. "
            "Choose from: 'all', 'any', 'mean', 'luma'."
        )

    total_pixels = pixels.shape[0] * pixels.shape[1]
    white_pixels = int(np.sum(mask))
    whitespace_pct = (white_pixels / total_pixels) * 100.0

    result = {
        "whitespace_pct": round(whitespace_pct, 4),
        "ink_pct": round(100.0 - whitespace_pct, 4),
        "total_pixels": total_pixels,
        "white_pixels": white_pixels,
        "threshold": threshold,
        # "channel_mode": channel_mode,
        # "source_type": source_type,
    }

    if min_whitespace_pct is not None:
        result["min_whitespace_pct"] = min_whitespace_pct
        result["meets_threshold"] = whitespace_pct >= min_whitespace_pct

    return result


# ---------------------------------------------------------------------------
# Convenience wrappers for common use-cases
# ---------------------------------------------------------------------------


@time_it
def is_mostly_whitespace(
    image_path: str,
    min_whitespace_pct: float = 50.0,
    threshold: int = 240,
    channel_mode: str = "all",
) -> bool:
    """Return True if the image has >= min_whitespace_pct non-ink pixels."""
    result = detect_whitespace(
        image_path,
        threshold=threshold,
        min_whitespace_pct=min_whitespace_pct,
        channel_mode=channel_mode,
    )
    return result["meets_threshold"]


@time_it
def whitespace_pct(
    image_path: str,
    threshold: int = 240,
    channel_mode: str = "luma",
) -> float:
    """Return just the whitespace percentage as a float."""
    return detect_whitespace(
        image_path, threshold=threshold, channel_mode=channel_mode
    )["whitespace_pct"]


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------
@time_it
def batch_is_mostly_whitespace(
    sources: list[str],
    min_whitespace_pct: float = 50.0,
    threshold: int = 240,
    channel_mode: str = "all",
    max_workers: int = 8,
) -> list[dict]:
    """
    Check is_mostly_whitespace for a list of file paths and/or URLs in parallel.

    Uses a thread pool so that many images can be downloaded/read concurrently.
    Threads are ideal here because the bottleneck is I/O (network or disk),
    not CPU — threads release the GIL while waiting, so you get true parallelism
    without the overhead of spawning separate processes.

    Parameters
    ----------
    sources : list[str]
        Any mix of local file paths and HTTP/HTTPS URLs.

    min_whitespace_pct : float, default 50.0
        Passed to detect_whitespace. Each result includes `meets_threshold`.

    threshold : int, default 240
        Strictness of "non-ink". See detect_whitespace for the full scale.

    channel_mode : str, default "all"
        Pixel evaluation strategy. See detect_whitespace for options.

    max_workers : int, default 8
        Maximum number of concurrent threads.
        - For mostly URLs: 8-16 is a good range (network-bound).
        - For mostly local files: 4-8 is usually enough (disk-bound).
        - Raising this too high gives diminishing returns and uses more RAM
          (each thread holds its own pixel array while running).

    Returns
    -------
    list[dict], one entry per source, in the same order as `sources`.
    Each dict contains all keys from detect_whitespace, plus:
        source               : str       -- The original path or URL
        is_mostly_whitespace : bool|None -- True if meets_threshold; None on error
        error                : str|None  -- Exception message if the image failed

    Example
    -------
    sources = [
        "https://example.com/page1.png",
        "https://example.com/page2.jpg",
        "/local/scans/doc.tiff",
    ]
    results = batch_is_mostly_whitespace(sources, min_whitespace_pct=10.0, threshold=245)

    for r in results:
        if r["error"]:
            print(f"FAILED  {r[\'source\']}: {r[\'error\']}")
        else:
            flag = "mostly whitespace" if r["is_mostly_whitespace"] else "has ink"
            print(f"{flag}  ({r[\'whitespace_pct\']:.1f}%)  {r[\'source\']}")
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process(source: str) -> dict:
        try:
            result = detect_whitespace(
                source,
                threshold=threshold,
                min_whitespace_pct=min_whitespace_pct,
                channel_mode=channel_mode,
            )
            # result["source"] = source
            result["is_mostly_whitespace"] = result["meets_threshold"]
            result["error"] = None
            return result
        except Exception as exc:
            return {
                # "source": source,
                "is_mostly_whitespace": None,
                "error": str(exc),
            }

    # Submit all jobs upfront, collect results in original index order.
    # as_completed() lets fast images return immediately without waiting
    # for slower ones ahead of them in the list.
    results_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_process, src): i for i, src in enumerate(sources)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results_map[idx] = future.result()

    return [results_map[i] for i in range(len(sources))]
