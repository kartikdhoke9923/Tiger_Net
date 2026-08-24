"""Pre-filter: backend routing, DB writes, and the human-detection alert hook."""

from pathlib import Path

import pytest

from app.prefilter import classifier
from app.prefilter.classifier import (
    ClassificationResult,
    StubBackend,
    classify_and_store,
    run_batch,
    get_backend,
)


class FakeDetector:
    """Injectable fake so tests drive pipeline logic without a model."""

    def __init__(self, label: str, confidence: float = 0.9):
        self.label, self.confidence = label, confidence
        self.seen = []

    def classify(self, image_path):
        self.seen.append(image_path)
        return ClassificationResult(self.label, self.confidence)


class PathKeyedFake(FakeDetector):
    """Returns the result mapped to each specific file path."""

    def __init__(self, table: dict):
        super().__init__("unused")
        self.table = table

    def classify(self, image_path):
        self.seen.append(image_path)
        return self.table[image_path]


@pytest.mark.parametrize("filename,label", [
    ("img_blank_0001.jpg", "blank"),
    ("img_empty.jpg", "blank"),
    ("img_human_0004.jpg", "human"),
    ("person_walking.jpg", "human"),
    ("poacher_night.jpg", "human"),
    ("img_tiger_0002.jpg", "tiger_candidate"),
    ("img_deer_0003.jpg", "animal_other"),
])
def test_stub_backend_filename_mapping(filename, label):
    result = StubBackend().classify(f"/x/{filename}")
    assert result.label == label
    assert 0.0 <= result.confidence <= 1.0


def test_get_backend_defaults_to_stub(monkeypatch):
    monkeypatch.delenv("PENCH_PREFILTER_BACKEND", raising=False)
    assert isinstance(get_backend(), StubBackend)


def test_get_backend_selects_megadetector_and_fails_loud_without_deps(monkeypatch):
    monkeypatch.setenv("PENCH_PREFILTER_BACKEND", "megadetector")
    with pytest.raises(RuntimeError, match="PytorchWildlife"):
        get_backend()  # not installed here -> must say so clearly, not silently fall back


def test_classify_and_store_writes_result(db, camera_ids, incoming_image):
    image_id, path = incoming_image(camera_ids["PTR-CAM-001"])
    fake = FakeDetector("animal_other", 0.81)

    import unittest.mock as mock
    with mock.patch.object(classifier, "get_backend", lambda: fake):
        result = classify_and_store(image_id, str(path), conn=db)

    assert result.label == "animal_other"
    row = db.execute(
        "SELECT classification, classification_confidence FROM images WHERE id=?",
        (image_id,),
    ).fetchone()
    assert row["classification"] == "animal_other"
    assert abs(row["classification_confidence"] - 0.81) < 1e-9
    assert fake.seen == [str(path)]  # the real file was actually passed to the backend


def test_human_detection_creates_alert_row_with_zone_type(db, camera_ids, incoming_image):
    """PRD section 6: humans in restricted zones must surface as STPF alerts."""
    image_id, path = incoming_image(camera_ids["PTR-CAM-002"], name="human_shot.jpg")

    import unittest.mock as mock
    with mock.patch.object(classifier, "get_backend",
                           lambda: FakeDetector("human", 0.95)):
        classify_and_store(image_id, str(path), conn=db)

    alert = db.execute(
        "SELECT zone_type FROM human_detections WHERE image_id=?", (image_id,)
    ).fetchone()
    assert alert is not None
    assert alert["zone_type"] == "restricted"  # PTR-CAM-002 is in the restricted zone


def test_non_human_classification_does_not_create_alerts(db, camera_ids, incoming_image):
    image_id, path = incoming_image(camera_ids["PTR-CAM-001"])

    import unittest.mock as mock
    with mock.patch.object(classifier, "get_backend",
                           lambda: FakeDetector("tiger_candidate", 0.88)):
        classify_and_store(image_id, str(path), conn=db)

    n = db.execute("SELECT COUNT(*) AS n FROM human_detections").fetchone()["n"]
    assert n == 0


def test_run_batch_counts_all_labels(db, incoming_image):
    spec = {
        "blank_1.jpg": ("blank", 0.97),
        "deer_2.jpg": ("animal_other", 0.75),
        "person_3.jpg": ("human", 0.93),
        "stripes_4.jpg": ("tiger_candidate", 0.88),
    }
    rows = []
    for i, (name, _) in enumerate(spec.items()):
        cam = db.execute("SELECT id FROM cameras LIMIT 1 OFFSET ?", (i % 3,)).fetchone()
        image_id, p = incoming_image(cam["id"], name=name)
        rows.append({"id": image_id, "file_path": str(p)})

    table = {}
    for row in rows:
        name = Path(row["file_path"]).name
        label, conf = spec[name]
        table[row["file_path"]] = ClassificationResult(label, conf)

    import unittest.mock as mock
    with mock.patch.object(classifier, "get_backend",
                           lambda: PathKeyedFake(table)):
        counts = run_batch(rows)

    assert counts == {"blank": 1, "animal_other": 1, "human": 1, "tiger_candidate": 1}


def test_megadetector_label_mapping():
    """
    The real backend's category->label mapping, tested without torch:
    feed canned PytorchWildlife-shaped results through the mapping logic by
    stubbing the model + cv2.imread.
    """
    import numpy as np
    from app.prefilter.classifier import MegaDetectorBackend

    backend = MegaDetectorBackend.__new__(MegaDetectorBackend)  # skip __init__/torch
    backend.detection_threshold = 0.20
    backend.tiger_promotion_threshold = 0.80
    backend.species_filter = None
    backend.model = None

    def md_result(labels, confs):
        return {"detections": {"labels": np.array(labels, dtype=object),
                               "confidence": np.array(confs)}}

    cases = [
        (md_result(["1"], [0.91]), "human"),                       # person -> human
        (md_result(["0"], [0.77]), "animal_other"),                # animal, no species filter -> other
        (md_result(["2"], [0.66]), "blank"),                       # vehicle -> not review-relevant
        (md_result([], []), "blank"),                              # nothing detected -> blank
        (md_result(["0", "1"], [0.55, 0.90]), "human"),            # both present -> person wins on conf
    ]
    img = np.ones((10, 10, 3), dtype=np.uint8)
    import unittest.mock as mock
    with mock.patch("cv2.imread", return_value=img):
        for result_obj, expected in cases:
            with mock.patch.object(backend, "model") as fake_model:
                fake_model.single_image_detection.return_value = result_obj
                assert backend.classify("fake.jpg").label == expected


def test_megadetector_species_filter_promotes_tiger():
    import numpy as np
    from app.prefilter.classifier import MegaDetectorBackend

    backend = MegaDetectorBackend.__new__(MegaDetectorBackend)
    backend.detection_threshold = 0.20
    backend.tiger_promotion_threshold = 0.80
    backend.species_filter = lambda p: 0.92   # external tiger classifier says yes
    backend.model = None

    img = np.ones((10, 10, 3), dtype=np.uint8)
    result_obj = {"detections": {"labels": np.array(["0"]),
                                 "confidence": np.array([0.6])}}
    import unittest.mock as mock
    with mock.patch("cv2.imread", return_value=img), \
         mock.patch.object(backend, "model") as fake_model:
        fake_model.single_image_detection.return_value = result_obj
        out = backend.classify("fake.jpg")
    assert out.label == "tiger_candidate"
    assert abs(out.confidence - 0.92) < 1e-9


def test_megadetector_low_species_score_stays_animal_other():
    """Conservative rule: without a confident species call, never claim 'tiger'."""
    import numpy as np
    from app.prefilter.classifier import MegaDetectorBackend

    backend = MegaDetectorBackend.__new__(MegaDetectorBackend)
    backend.detection_threshold = 0.20
    backend.tiger_promotion_threshold = 0.80
    backend.species_filter = lambda p: 0.5    # below promotion threshold
    backend.model = None

    img = np.ones((10, 10, 3), dtype=np.uint8)
    result_obj = {"detections": {"labels": np.array(["0"]),
                                 "confidence": np.array([0.6])}}
    import unittest.mock as mock
    with mock.patch("cv2.imread", return_value=img), \
         mock.patch.object(backend, "model") as fake_model:
        fake_model.single_image_detection.return_value = result_obj
        assert backend.classify("fake.jpg").label == "animal_other"
