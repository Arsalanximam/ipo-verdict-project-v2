import os
import psycopg2

url = os.getenv("RENDER_DATABASE_URL")

if not url:
    print("DATABASE URL NOT FOUND")
    raise SystemExit

conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute("""
    SELECT company, sub_overall, fetched_at
    FROM ipo_snapshots
    WHERE fetched_at >= date_trunc('week', CURRENT_DATE)
      AND sub_overall IS NOT NULL
    ORDER BY company, fetched_at
""")

rows = cur.fetchall()

companies = {}

for company, subscription, fetched_at in rows:
    key = company.strip().lower()
    companies.setdefault(key, []).append(
        (company, float(subscription), fetched_at)
    )

falls = []

for entries in companies.values():
    for previous, current in zip(entries, entries[1:]):
        change = round(current[1] - previous[1], 2)

        if change < 0:
            falls.append(
                (
                    change,
                    current[0],
                    previous[1],
                    current[1],
                    previous[2],
                    current[2],
                )
            )

falls.sort()

print("\nCONSECUTIVE SUBSCRIPTION FALL CHECK")
print("=" * 70)

if not falls:
    print("NO CONSECUTIVE SUBSCRIPTION FALLS FOUND")
else:
    for change, company, previous, current, old_time, new_time in falls[:20]:
        print(
            f"{company}: "
            f"{previous}x -> {current}x "
            f"({change}x)"
        )

print("=" * 70)
print("Total consecutive falls:", len(falls))

cur.close()
conn.close()