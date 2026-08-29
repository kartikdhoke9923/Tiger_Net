from app.db.schema import get_connection

conn = get_connection()

before = conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]

conn.execute("""
    DELETE FROM sightings
    WHERE id NOT IN (
        SELECT MIN(id) FROM sightings
        WHERE tiger_id IS NULL AND confirmed_at IS NULL
        GROUP BY image_id
    )
    AND tiger_id IS NULL AND confirmed_at IS NULL
""")
conn.commit()

after = conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]
print(f"Sightings: {before} -> {after}")
conn.close()