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
    correct = 0
    for f in files:
        result = backend.classify(f)
        ok = result.label == expected
        correct += ok
        print(f"{'OK' if ok else 'WRONG'}: {f} -> {result.label} ({result.confidence:.2f})")
    print(f"{folder}: {correct}/{len(files)} correct\n")