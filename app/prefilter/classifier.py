"""
Image pre-filter: classifies raw camera trap images into
blank / animal_other / human / tiger_candidate.

Backend selection (get_backend):
- PENCH_PREFILTER_BACKEND=megadetector  -> real MegaDetector inference via
  PytorchWildlife (model weights MDV5A are downloaded on first use; needs
  torch installed). This is now IMPLEMENTED -- see MegaDetectorBackend.
- anything else / unset                 -> StubBackend (filename hints only).

Honest limits of the real backend (do not oversell this):
  1. MegaDetector detects animal / person / vehicle + blank. It does NOT do
     species ID. An 'animal' detection is therefore routed as animal_other,
     NOT tiger_candidate. Promoting a crop to tiger_candidate requires the
     optional `species_filter` callable (a species classifier you supply --
     e.g. a fine-tuned model on real Bengal tiger data, which per PRD section
     13 does not exist yet). Default behavior is conservative on purpose:
     fewer false tigers is better than inflated candidate volume.
  2. First run downloads weights (~large file) -- do that at the office with
     connectivity if possible; afterwards inference is fully offline.

The pipeline logic around the backend (batching, DB writes, routing
tiger-candidates onward to idmatch, routing humans to alerting) is real and
functional today.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.db.schema import get_connection


@dataclass
class ClassificationResult:
    label: str          # 'blank' | 'animal_other' | 'human' | 'tiger_candidate'
    confidence: float    # 0.0 - 1.0


class Detector:
    """Interface every backend must implement."""
    def classify(self, image_path: str) -> ClassificationResult:
        raise NotImplementedError


class StubBackend(Detector):
    """
    Deterministic placeholder so the pipeline is runnable and testable
    end-to-end before/without the real model. Uses filename hints only --
    NOT a real classifier.
    """
    def classify(self, image_path: str) -> ClassificationResult:
        name = Path(image_path).stem.lower()
        if "blank" in name or "empty" in name:
            return ClassificationResult("blank", 0.97)
        if "human" in name or "person" in name or "poacher" in name:
            return ClassificationResult("human", 0.93)
        if "tiger" in name:
            return ClassificationResult("tiger_candidate", 0.88)
        return ClassificationResult("animal_other", 0.75)


# MegaDetector category ids -> what they mean (stable across MDV4/MDV5)
_MD_CATEGORY_ANIMAL = "0"
_MD_CATEGORY_PERSON = "1"
_MD_CATEGORY_VEHICLE = "2"

DEFAULT_DETECTION_THRESHOLD = 0.20   # below this, treat frame as effectively blank
DEFAULT_TIGER_PROMOTION_THRESHOLD = 0.80  # min confidence for species_filter promotion


class MegaDetectorBackend(Detector):
    """
    REAL MegaDetector inference via PytorchWildlife.

    Requires:
        pip install torch torchvision pytorch-wildlife
      (weights download automatically on first use -- afterwards offline)

    Mapping to our labels:
        person            -> human
        animal + species_filter(crop) says tiger -> tiger_candidate
        animal otherwise  -> animal_other
        no detection >= threshold -> blank
        vehicle           -> blank (irrelevant to wildlife review queue)

    species_filter: optional callable(image_path) -> float in [0,1], the
    confidence that the crop contains a tiger. Without it, animals are NEVER
    labeled tiger_candidate -- MegaDetector alone cannot make that call.
    """

    def __init__(self,
                 detection_threshold: float = DEFAULT_DETECTION_THRESHOLD,
                 tiger_promotion_threshold: float = DEFAULT_TIGER_PROMOTION_THRESHOLD,
                 species_filter=None,
                 device: str = "cpu"):
        try:
            from PytorchWildlife.models import detection_model_v2
        except ImportError as e:
            raise RuntimeError(
                "MegaDetector backend selected but PytorchWildlife/torch are not "
                "installed. Run: pip install torch torchvision pytorch-wildlife "
                "(first use downloads MDV5A weights; afterwards it runs offline). "
                f"Original import error: {e}"
            ) from e
        self.model = detection_model_v2.MegaDetectorV5(version="MDV5A", device=device)
        self.detection_threshold = detection_threshold
        self.tiger_promotion_threshold = tiger_promotion_threshold
        self.species_filter = species_filter

    def classify(self, image_path: str) -> ClassificationResult:
        import cv2  # deferred so environments without cv2 can still use StubBackend

        img = cv2.imread(str(image_path))
        if img is None:
            raise ValueError(f"Could not decode image: {image_path}")
        # PytorchWildlife expects RGB
        result = self.model.single_image_detection(img[:, :, ::-1], dims=img.shape[:2])

        dets = result.get("detections", result)
        labels = np.asarray(dets.get("labels", []), dtype=str)
        confs = np.asarray(dets.get("confidence", dets.get("confidences", [])), dtype=float)

        best_label, best_conf = None, 0.0
        for label, conf in zip(labels, confs):
            if conf >= self.detection_threshold and conf > best_conf:
                best_label, best_conf = label, float(conf)

        if best_label == _MD_CATEGORY_PERSON:
            return ClassificationResult("human", best_conf)
        if best_label == _MD_CATEGORY_ANIMAL:
            if self.species_filter is not None:
                tiger_conf = float(self.species_filter(image_path))
                if tiger_conf >= self.tiger_promotion_threshold:
                    return ClassificationResult("tiger_candidate", tiger_conf)
            return ClassificationResult("animal_other", best_conf)
        # blank frame or vehicle-only trigger
        return ClassificationResult("blank", max(best_conf, 0.99))


def get_backend() -> Detector:
    """
    Picks backend from PENCH_PREFILTER_BACKEND env var. Default remains the
    stub so the demo pipeline always runs without heavyweight deps; set
    PENCH_PREFILTER_BACKEND=megadetector once torch/pytorch-wildlife are
    installed on the range-office machine.
    """
    choice = os.environ.get("PENCH_PREFILTER_BACKEND", "stub").strip().lower()
    if choice in ("megadetector", "md", "pytorchwildlife"):
        return MegaDetectorBackend()
    return StubBackend()


def classify_and_store(image_id: int, image_path: str, conn=None):
    """
    Runs classification on one image, writes result to DB, and returns it.
    If classification == 'human', also creates a human_detections row so
    the alerting module can act on it.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    backend = get_backend()
    result = backend.classify(image_path)

    conn.execute(
        "UPDATE images SET classification = ?, classification_confidence = ? WHERE id = ?",
        (result.label, result.confidence, image_id),
    )

    if result.label == "human":
        row = conn.execute("SELECT camera_id FROM images WHERE id = ?", (image_id,)).fetchone()
        camera = conn.execute(
            "SELECT zone_type FROM cameras WHERE id = ?", (row["camera_id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO human_detections (image_id, camera_id, zone_type) VALUES (?, ?, ?)",
            (image_id, row["camera_id"], camera["zone_type"] if camera else "general"),
        )

    conn.commit()
    if own_conn:
        conn.close()

    return result


def run_batch(image_rows):
    """
    Process a batch of unclassified images. Expects rows with 'id' and
    'file_path'. Returns counts per label for a quick sanity check /
    operator-facing summary after a batch run.
    """
    conn = get_connection()
    counts = {"blank": 0, "animal_other": 0, "human": 0, "tiger_candidate": 0}
    for row in image_rows:
        result = classify_and_store(row["id"], row["file_path"], conn=conn)
        counts[result.label] += 1
    conn.close()
    return counts


if __name__ == "__main__":
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, file_path FROM images WHERE classification = 'unclassified' OR classification IS NULL"
    ).fetchall()
    conn.close()
    if not rows:
        print("[prefilter] no unclassified images found. Run ingestion first.")
    else:
        summary = run_batch(rows)
        print(f"[prefilter] processed {len(rows)} images: {summary}")
