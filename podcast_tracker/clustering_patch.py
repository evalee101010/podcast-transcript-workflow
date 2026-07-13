"""Best-effort speedup for FunASR's CAM++ speaker clustering on long audio.

FunASR's CAM++ speaker diarization clusters speech segments with spectral
clustering, whose eigendecomposition uses a dense ``scipy.linalg.eigh`` call.
That is O(N^3) in the number of segments. For multi-hour, many-speaker
recordings N grows large enough that clustering alone can take 10+ hours.

This patch monkeypatches the dense eig call inside FunASR's clustering backend
with a sparse top-k solver (``scipy.sparse.linalg.eigsh``), which is roughly
O(N^2 * k). Spectral clustering only needs the smallest few eigenvectors, so
results are equivalent while being dramatically faster on long audio.

It is intentionally defensive:
  * If FunASR's internal module layout differs from what we expect, it logs a
    warning and leaves the pipeline running unpatched (correct, just slower).
  * For a typical 1-hour, 2-speaker podcast N is small and even the unpatched
    path is fine, so a failed patch is not fatal.

Disable with the env var PODTRACK_DISABLE_CLUSTER_PATCH=1.

Credit: the O(N^3) -> O(N^2*k) idea follows zxkane/audio-transcriber's
patch_clustering.py; this is an independent, defensive reimplementation.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("podcast_tracker.clustering_patch")

_APPLIED = False


def apply_clustering_speedup() -> bool:
    """Patch FunASR spectral clustering for faster long-audio diarization.

    Returns True if the patch was applied (or already applied), False if it was
    skipped or could not be applied. Never raises.
    """
    global _APPLIED
    if _APPLIED:
        return True
    if os.getenv("PODTRACK_DISABLE_CLUSTER_PATCH") == "1":
        logger.info("Clustering speedup disabled via PODTRACK_DISABLE_CLUSTER_PATCH=1.")
        return False

    try:
        import numpy as np
        import scipy.linalg
        from scipy.sparse.linalg import eigsh
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Clustering speedup skipped (numpy/scipy unavailable): %s", exc)
        return False

    # Locate FunASR's spectral clustering backend. Module path has shifted
    # across versions, so try the known candidates.
    target_module = None
    for name in (
        "funasr.models.campplus.cluster_backend",
        "funasr.models.campplus.utils",
        "funasr.models.eend_vc.cluster_backend",
    ):
        try:
            module = __import__(name, fromlist=["*"])
        except Exception:
            continue
        if hasattr(module, "scipy") or "scipy" in getattr(module, "__dict__", {}):
            target_module = module
            break

    if target_module is None:
        logger.warning(
            "Could not locate FunASR clustering backend; running unpatched "
            "(fine for short audio, slower for multi-hour recordings)."
        )
        return False

    original_eigh = scipy.linalg.eigh

    def fast_eigh(a, *args, **kwargs):  # noqa: ANN001
        # Spectral clustering only needs the smallest handful of eigenpairs.
        # Use the sparse solver when the matrix is large enough to benefit and
        # the call is a plain symmetric eig (no generalized / subset args we
        # don't understand). Otherwise fall back to the dense routine.
        try:
            n = a.shape[0]
        except Exception:
            return original_eigh(a, *args, **kwargs)

        simple = (
            not args
            and "b" not in kwargs
            and "eigvals_only" not in kwargs
            and "subset_by_index" not in kwargs
            and "subset_by_value" not in kwargs
        )
        if not simple or n <= 256:
            return original_eigh(a, *args, **kwargs)

        try:
            k = min(max(8, int(np.sqrt(n))), n - 1)
            vals, vecs = eigsh(np.asarray(a, dtype=float), k=k, which="SM")
            order = np.argsort(vals)
            return vals[order], vecs[:, order]
        except Exception as exc:  # pragma: no cover - numerical edge cases
            logger.debug("Sparse eig fell back to dense: %s", exc)
            return original_eigh(a, *args, **kwargs)

    try:
        target_module.scipy.linalg.eigh = fast_eigh  # type: ignore[attr-defined]
    except Exception:
        # Some versions import eigh by name rather than via the scipy package.
        try:
            target_module.eigh = fast_eigh  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("Could not install clustering speedup: %s", exc)
            return False

    _APPLIED = True
    logger.info("Applied CAM++ spectral-clustering speedup (sparse eigsh).")
    return True
