from app.prefilter.classifier import get_backend
import glob

backend = get_backend()
tests = [
    ("wcs_sample_images/empty", "blank"),
    ("wcs_sample_images/bos_taurus", "animal_other"),
    ("wcs_sample_images/tayassu_pecari", "animal_other"),
]

for folder, expected in tests:
    files = glob.glob(f"{folder}/*.jpg")[:10]
    correct = uncertain = wrong = 0
    for f in files:
        result = backend.classify(f)
        if result.label == "uncertain":
            uncertain += 1; tag = "UNCERTAIN"
        elif result.label == expected:
            correct += 1; tag = "OK"
        else:
            wrong += 1; tag = "WRONG"
        print(f"{tag}: {f} -> {result.label} ({result.confidence:.2f})")
    print(f"{folder}: {correct} correct / {uncertain} uncertain / {wrong} wrong (of {len(files)})\n")