import psycopg

conn = psycopg.connect('postgresql://postgres.ikvqhxbmgjzjkefrgzdc:Unoteam%40987123@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres')
cur = conn.cursor()

cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'job_notifications';")
print("job_notifications columns:")
for c in cur.fetchall():
    print(c)

cur.execute("SELECT * FROM job_notifications LIMIT 10;")
print("\njob_notifications rows:")
for r in cur.fetchall():
    print(r)

cur.close()
conn.close()
