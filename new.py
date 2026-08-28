import json

with open("wcs_camera_traps.json") as f:
    data = json.load(f)

print("Top-level keys:", list(data.keys()))
print("Sample category:", data["categories"][0])
print("Sample image:", data["images"][0])
print("Sample annotation:", data["annotations"][0])