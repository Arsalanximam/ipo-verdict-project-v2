import os
import psycopg2

url = os.getenv("RENDER_DATABASE_URL")

if not url:
    print("DATABASE URL NOT FOUND")
    raise SystemExit

conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute(
    "DELETE FROM ipo_snapshots WHERE UPPER(TRIM(company)) = 'NSE'"
)

print("Deleted NSE rows:", cur.rowcount)

conn.commit()

cur.close()
conn.close()