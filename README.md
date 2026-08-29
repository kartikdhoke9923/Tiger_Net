# Pench Tiger Monitoring — Augmentation System (Prototype)

Built as an **augmentation layer on top of M-STrIPES and ExtractCompare**,
not a replacement. See PRD discussion for why. This is a working prototype
to demonstrate the pipeline and prove out the security/offline-first design
— it is **not field-ready** until the stub sections below are replaced with
real models and a real central server.

## What's real and tested right now
- Full pipeline runs end-to-end: ingest → classify → shortlist → human
  review → sync attempt → dashboard (verified, see test run below)
- SQLite schema with proper foreign keys and separation of sensitive data
  (location is in its own table, never joined in unless role permits)
- Role-based access control, enforced and tested:
  - `researcher` role never receives latitude/longitude, even on request
  - `stpf` role denied general sightings access entirely
  - `field_ranger` scoped to their own beat only
  - Every access attempt (granted or denied) is written to `audit_log`
- Mandatory human confirmation: there is no code path that assigns a tiger
  ID without a `user_id` and a role check
- Offline-first sync: nothing is marked as synced unless a push actually
  succeeds; failed/untried syncs queue for retry, logged to
  `logs/sync_queue.jsonl`
- 3-tier confidence system for tiger ID shortlists (high ≥0.90 / medium
  0.50–0.90 / low <0.50) — replaces the flat 50% cutoff originally proposed,
  because a coin-flip threshold is too loose for population data on an
  endangered species

## What's real now (updated)
- **Tiger matching is now a real algorithm**, not a random stub: ORB keypoint
  feature matching via OpenCV (`app/idmatch/feature_matcher.py`). Tested
  against real images — identical image scores 1.0, same-subject/different-angle
  scores ~0.6, genuinely different subject scores ~0.02. It's a classical CV
  baseline (same family as HotSpotter), not a trained deep re-ID model — good
  enough to validate the pipeline honestly, not good enough to be your final
  accuracy claim to the forest department.
- **ATRW dataset loader** (`app/idmatch/atrw_loader.py`) — loads real tiger
  images + identity labels from the public ATRW (Amur Tiger Re-ID in the
  Wild) dataset for pipeline testing. **Read the caveat below before using it.**

## Critical caveat on ATRW data — do not skip this
ATRW tigers are **Amur (Siberian) tigers photographed in Chinese zoos** —
not Bengal tigers in a wild Indian forest. Different subspecies, different
stripe statistics, controlled zoo lighting vs. real IR camera-trap
conditions. Use ATRW to:
- Validate the pipeline mechanics work on real photographic content
- Later, as a starting point for transfer learning if you train a deep
  embedding model

Do **not** use ATRW-derived match results as evidence your system works on
actual Pench tigers. Every tiger loaded from ATRW is tagged
`source = 'atrw_benchmark'` in the DB specifically so it can never be
accidentally counted as real Pench population data — any dashboard or
report on real tiger counts must filter `WHERE source = 'pench_confirmed'`.

ATRW license is CC BY-NC-SA 4.0 (non-commercial/research use only) — confirm
this covers your intended use before going beyond a prototype.

**Before running the ATRW loader**: open your actual downloaded CSV/annotation
file and confirm it matches the format assumed in `parse_identity_list()`
inside `app/idmatch/atrw_loader.py`. I have not seen your specific file —
the function has a docstring flagging exactly what to check and adjust.

## What's still stubbed — replace before any real field use
| Module | File | What to do |
|---|---|---|
| Image classification | `app/prefilter/classifier.py` | Wire `MegaDetectorBackend` to real MegaDetector/PytorchWildlife inference |
| Central sync endpoint | `app/sync/sync_queue.py` | Replace `CENTRAL_ENDPOINT` placeholder with a real server + auth |
| EXIF timestamp reading | `app/ingestion/ingest.py` | Currently uses file mtime; use Pillow's EXIF reader on real camera files |
| Deep re-ID model | `app/idmatch/feature_matcher.py` | ORB is a working v1; swap in a trained embedding model (e.g. fine-tuned on real Bengal tiger images) for real accuracy |

Every stub has a docstring explaining exactly what it fakes and why.

## Project structure
```
app/
  db/schema.py          -- SQLite schema, single source of truth for structure
  ingestion/ingest.py    -- pulls images from data/incoming/<camera_code>/
  prefilter/classifier.py -- blank/animal/human/tiger classification
  idmatch/matcher.py      -- tiger ID shortlist generation, 3-tier confidence
  review/interface.py     -- human confirmation (CLI, swap for web later)
  sync/sync_queue.py      -- offline-first push to central system
  security/access_control.py -- RBAC + audit log, all sensitive reads go through here
  dashboard/summary.py    -- role-aware local dashboard
run_pipeline.py           -- runs all stages with checks, in order
tests/seed_demo_data.py   -- creates demo users/cameras/images to test with
```

## Running it
```bash
pip install opencv-python-headless numpy pillow --break-system-packages
python3 tests/seed_demo_data.py   # creates demo DB, users, cameras, real synthetic test images
python3 run_pipeline.py           # runs ingest -> classify -> shortlist -> sync -> dashboard
python3 -m app.review.interface   # view pending human-review queue
python3 -m app.dashboard.summary <user_id>  # role-specific dashboard

# Optional: load real ATRW images to test the matcher on real photos
# (read the ATRW caveat above first)
python3 -m app.idmatch.atrw_loader /path/to/atrw/images /path/to/reid_list.csv
```

## Before this touches real field data
1. Confirm with DFO/WCT/WII whether ExtractCompare processing is centralized
   or local at Pench — this determines if `idmatch` module is even needed,
   or if it should just format/forward data to the existing system instead
2. Confirm whether "security" concern is about data protection (this system
   is built for that) or camera-triggered poacher detection (not built —
   would need a live-alert module, different from what's here)
3. Get real reference tiger images and real camera trap samples before
   touching the stub classifiers
4. Confirm what compute is actually available at range offices — this
   assumes a local server/laptop-class device; adjust if it needs to run on
   more constrained hardware
