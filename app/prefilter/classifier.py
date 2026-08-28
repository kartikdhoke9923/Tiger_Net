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

MODEL_VERSION = "MDV6-yolov10-e"  # change to "MDV6-yolov10-e" to test the bigger, slower, more accurate model
CONFIDENT_THRESHOLD = 0.65   # above this: trust the label      # below this: treat as blank
# Between these two = genuinely unsure -> goes to human review instead of a guess.
# These numbers are a first estimate from your 30-image test, not a validated cutoff.
# Expect to re-tune once you've reviewed more real results.

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
    def __init__(self):
        from PytorchWildlife.models import detection as pw_detection
        self.model = pw_detection.MegaDetectorV6(version=MODEL_VERSION)

    def classify(self, image_path: str) -> ClassificationResult:
        result = self.model.single_image_detection(image_path)
        labels = result["labels"]

        if not labels:
            return ClassificationResult("blank", 1.0)

        confidences = result["detections"].confidence
        categories = [label.split()[0] for label in labels]

        # Check for ANY person detection first, even if an animal in the same
        # frame scored higher. This is a poacher-detection system -- silently
        # dropping a real person because a cow scored higher confidence in the
        # same photo is not an acceptable trade-off.
        person_indices = [i for i, c in enumerate(categories) if c == "person"]
        if person_indices:
            best_person_idx = max(person_indices, key=lambda i: confidences[i])
            person_confidence = float(confidences[best_person_idx])
            if person_confidence >= CONFIDENT_THRESHOLD:
                return ClassificationResult("human", person_confidence)
            return ClassificationResult("uncertain", person_confidence)

        # No person in the frame -- proceed as before with the best detection
        best_idx = confidences.argmax()
        confidence = float(confidences[best_idx])
        if confidence < CONFIDENT_THRESHOLD:
            return ClassificationResult("uncertain", confidence)
        return ClassificationResult("animal_other", confidence)

_backend_singleton = None

def get_backend() -> Detector:
    global _backend_singleton
    if _backend_singleton is None:
        _backend_singleton = MegaDetectorBackend()
    return _backend_singleton


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
    counts = {"blank": 0, "animal_other": 0, "human": 0, "tiger_candidate": 0, "uncertain": 0}
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
