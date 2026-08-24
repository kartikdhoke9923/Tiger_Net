"""
Real image-matching backend using ORB (Oriented FAST and Rotated BRIEF)
keypoint features + brute-force Hamming matching, via OpenCV.

Why ORB and not a deep-learning re-ID model, honestly:
- ORB needs no downloaded weights, no GPU, no training data of your own to
  start working today. It runs on the actual pixel content of two images
  and gives a real similarity score.
- It is a legitimate, published category of approach for this exact
  problem: classical local-feature matching (SIFT/ORB-family descriptors)
  is the same family of technique HotSpotter-style tools are built on for
  wildlife re-ID -- matching consistent local texture patterns (stripes)
  between crops of the same individual.
- It will NOT match state-of-the-art deep re-ID accuracy (see the ATRW
  papers -- deep pose-guided models like PPbM/PPGNet substantially beat
  classical baselines). Treat ORB as your working v1, not your final
  system. The `Matcher` interface in matcher.py is deliberately built so
  you can swap in a trained deep embedding model later without touching
  the rest of the pipeline.

What this module actually does, concretely:
1. Loads two images, converts to grayscale
2. Detects ORB keypoints + descriptors in each
3. Matches descriptors between the two images (Hamming distance, since ORB
   descriptors are binary)
4. Filters matches by distance threshold ("good matches")
5. Converts match count/quality into a 0-1 similarity score

This is real, deterministic, and testable against real images -- not
filename-based guessing like the old stub.
"""

import cv2
import numpy as np
from pathlib import Path


class ImageLoadError(Exception):
    pass


def _load_gray(image_path: str):
    path = Path(image_path)
    if not path.exists():
        raise ImageLoadError(f"Image not found: {image_path}")
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ImageLoadError(f"OpenCV could not decode image (corrupt or unsupported format): {image_path}")
    return img


class ORBMatcher:
    """
    Real ORB feature matcher for comparing two tiger crop images.
    Drop-in for the `Matcher` interface in app/idmatch/matcher.py --
    implements the same `.score(query_path, reference_path) -> float` method.
    """

    def __init__(self, n_features: int = 1000, good_match_ratio: float = 0.75):
        self.orb = cv2.ORB_create(nfeatures=n_features)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.good_match_ratio = good_match_ratio  # Lowe's ratio test threshold

    def score(self, query_image_path: str, reference_image_path: str) -> float:
        img1 = _load_gray(query_image_path)
        img2 = _load_gray(reference_image_path)

        kp1, des1 = self.orb.detectAndCompute(img1, None)
        kp2, des2 = self.orb.detectAndCompute(img2, None)

        if des1 is None or des2 is None or len(kp1) == 0 or len(kp2) == 0:
            # No usable features in one of the images (e.g. blank/very low
            # texture crop) -- honestly report zero confidence, not a guess.
            return 0.0

        # knnMatch with k=2 to apply Lowe's ratio test -- standard way to
        # filter out ambiguous/noisy matches rather than counting everything.
        matches = self.bf.knnMatch(des1, des2, k=2)

        good_matches = []
        for pair in matches:
            if len(pair) != 2:
                continue
            m, n = pair
            if m.distance < self.good_match_ratio * n.distance:
                good_matches.append(m)

        # Normalize by the smaller keypoint set size -- otherwise an image
        # with far more keypoints than the other artificially caps the score.
        denom = min(len(kp1), len(kp2))
        if denom == 0:
            return 0.0

        raw_score = len(good_matches) / denom
        # Clip to [0, 1] -- in practice raw_score rarely exceeds ~0.6-0.7
        # even for genuine same-individual matches with ORB, so this is a
        # ceiling, not a typical value. Don't expect scores near 1.0 often.
        return round(min(raw_score, 1.0), 3)


def get_backend():
    """Factory so matcher.py can swap this in without other files knowing details."""
    return ORBMatcher()


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python -m app.idmatch.feature_matcher <image1> <image2>")
        sys.exit(1)
    matcher = ORBMatcher()
    score = matcher.score(sys.argv[1], sys.argv[2])
    print(f"Similarity score: {score}")
