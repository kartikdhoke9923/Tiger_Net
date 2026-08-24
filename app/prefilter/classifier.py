"""
Image pre-filter: classifies raw camera trap images into
blank / animal_other / human / tiger_candidate.

IMPORTANT — read before wiring this to production:
This module is written against a pluggable `Detector` interface. It does NOT
ship a trained model, because:
  1. MegaDetector (Microsoft's open-source camera-trap detector) is the
     correct real backend for this -- do not train one from scratch.
     https://github.com/microsoft/CameraTraps
  2. Model weights need real hardware + real Pench image data to be useful.
     Shipping a fake "trained" model here would be worse than being honest
     that this step is a stub.

To go live: implement `MegaDetectorBackend.classify()` to call the actual
MegaDetector inference (via `PytorchWildlife` package or the ONNX export),
map its output classes (animal/person/vehicle/blank) to ours, and swap
`get_backend()` to return it instead of `StubBackend`.

The pipeline logic around it (batching, DB writes, confidence tiers,
routing tiger-candidates onward, routing humans to alerting) is real and
functional today.
"""

from dataclasses import dataclass
from pathlib import Path
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
    end-to-end before a real model is wired in. Uses filename hints only --
    NOT a real classifier. Replace before any field use.
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


class MegaDetectorBackend(Detector):
    """
    STUB — implement this to call real MegaDetector inference.
    Left unimplemented deliberately; see module docstring.
    """
    def classify(self, image_path: str) -> ClassificationResult:
        raise NotImplementedError(
            "Wire this up to MegaDetector / PytorchWildlife before using in production. "
            "See module docstring for guidance."
        )


def get_backend() -> Detector:
    return StubBackend()


def classify_and_store(image_id: int, image_path: str, conn=None):
    """
    Runs classification on one image, writes result to DB, and returns it.
    If classification == 'human', also creates a human_detections row so
    the alerting module (app/review or a future alerts module) can act on it.
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
