import psycopg

conn = psycopg.connect('postgresql://postgres.ikvqhxbmgjzjkefrgzdc:Unoteam%40987123@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres')
cur = conn.cursor()
cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'devices';")
cols = cur.fetchall()
print('Devices columns:')
for c in cols:
    print(c)

cur.execute("SELECT * FROM devices ORDER BY updated_at DESC;")
rows = cur.fetchall()
print(f'Total devices: {len(rows)}')
for r in rows:
    print(r)

print('--- USERS ---')
cur.execute("SELECT user_id, email, full_name, is_active, is_admin FROM users;")
users = cur.fetchall()
for u in users:
    print(u)

cur.close()
conn.close()
