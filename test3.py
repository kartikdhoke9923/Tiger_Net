from app.review.interface import pending_species_review, reclassify_image
from app.db.schema import get_connection

conn = get_connection()

print("--- Images needing a 'is this a tiger?' check ---")
queue = pending_species_review(conn)
if not queue:
    print("Nothing pending. Run the classifier on some images first.")
else:
    for item in queue:
        print(item)

    # Example: tag the first one as a tiger, ranger_amit (user_id=1) doing the tagging
    first = queue[0]
    reclassify_image(first["id"], "tiger_candidate", user_id=1, conn=conn)
    print(f"\nTagged image {first['id']} as tiger_candidate")

    # Confirm it's now waiting for individual-ID matching, not closed out
    row = conn.execute("SELECT classification, reviewed FROM images WHERE id = ?", (first["id"],)).fetchone()
    print(f"New state: classification={row['classification']}, reviewed={row['reviewed']}")

conn.close()