from app.db.schema import get_connection
from tests.seed_demo_data import seed_users, seed_cameras

conn = get_connection()
seed_users(conn)
seed_cameras(conn)
conn.close()
print("users + cameras added, your real images in data/incoming untouched")