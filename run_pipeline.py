"""
End-to-end pipeline runner.

Order matches PRD build order:
  1. init DB (idempotent)
  2. ingest new images from data/incoming/
  3. pre-filter classify (blank / animal / human / tiger_candidate)
  4. generate tiger-ID shortlists for tiger_candidate images
  5. (human review happens separately -- app/review, requires a person)
  6. attempt sync (will show as "queued for retry" until a real endpoint exists)
  7. print dashboard for a demo user

Each stage prints a check summary before moving to the next, so a failure
or an empty result at any stage is visible immediately rather than silently
propagating.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db.schema import init_db, get_connection
from app.ingestion.ingest import ingest_folder
from app.prefilter.classifier import run_batch
from app.idmatch.matcher import generate_shortlist
from app.sync.sync_queue import run_sync
from app.dashboard.summary import render_dashboard


def check(label, condition, detail=""):
    status = "OK" if condition else "WARN"
    print(f"[{status}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def main():
    print("=== Pench Tiger Monitoring Pipeline — dry run ===\n")

    print("-- Stage 1: DB init --")
    init_db()
    check("database reachable", Path("data/pench.db").exists())

    print("\n-- Stage 2: Ingestion --")
    ingested = ingest_folder()
    total_new = sum(ingested.values()) if ingested else 0
    check("ingestion ran", True, f"{total_new} new image(s) across {len(ingested)} camera(s)")

    print("\n-- Stage 3: Pre-filter classification --")
    conn = get_connection()
    unclassified = conn.execute(
        "SELECT id, file_path FROM images WHERE classification = 'unclassified' OR classification IS NULL"
    ).fetchall()
    conn.close()
    if unclassified:
        summary = run_batch(unclassified)
        check("classification ran", True, str(summary))
    else:
        check("classification ran", False, "no unclassified images — did ingestion find anything?")

    print("\n-- Stage 4: Tiger ID shortlist generation --")
    conn = get_connection()
    candidates = conn.execute(
        "SELECT id, file_path FROM images WHERE classification = 'tiger_candidate' AND reviewed = 0"
    ).fetchall()
    shortlist_count = 0
    for row in candidates:
        result = generate_shortlist(row["id"], row["file_path"], conn=conn)
        shortlist_count += 1
        if result["no_reference_tigers"]:
            print(f"    image {row['id']}: no reference tigers in DB yet — will need manual 'new individual' flag")
    conn.close()
    check("shortlists generated", shortlist_count > 0 or len(candidates) == 0,
          f"{shortlist_count} shortlist(s) generated for {len(candidates)} tiger-candidate image(s)")

    print("\n-- Stage 5: Human review --")
<<<<<<< HEAD
    print("    (skipped in automated run — see: python -m app.review.interface)")
=======
    print("    (skipped in automated run -- options:")
    print("       CLI: python3 -m app.review.interface")
    print("       Web: python3 -m app.dashboard.web  -> http://127.0.0.1:8070)")
>>>>>>> b2727e2c528f5d462e2e467856663ce86d7f5a25

    print("\n-- Stage 6: Sync attempt --")
    sync_result = run_sync()
    check("sync attempted", True, str(sync_result))
    if sync_result["queued_for_retry"] > 0:
        print("    NOTE: CENTRAL_ENDPOINT is a placeholder in app/sync/sync_queue.py — "
              "this is expected until wired to a real server.")

    print("\n-- Stage 7: Dashboard --")
    conn = get_connection()
    any_user = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    conn.close()
    if any_user:
        render_dashboard(any_user["id"])
    else:
        print("    no users in DB yet — run tests/seed_demo_data.py first")

    print("\n=== Pipeline run complete ===")


if __name__ == "__main__":
    main()
