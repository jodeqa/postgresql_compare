import psycopg2

conn = psycopg2.connect(
    host="127.0.0.1",
    port=5432,
    dbname="dynamic_law",
    user="jodeqa",
    password="@2103191108030702ff#DD"
)

cur = conn.cursor()

# Quick Diagnostic Test Script
cur.execute("""
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog','information_schema')
""")
print(cur.fetchall())

# Show All Schemas and Tables (regardless of search_path)
cur.execute("SELECT table_schema, table_name FROM information_schema.tables ORDER BY table_schema, table_name;")
for row in cur.fetchall():
    print(row)

# Print user, schema, search_path
cur.execute("SELECT current_database(), current_user, session_user, current_schema(), version(), current_setting('search_path');")
print("Session info:", cur.fetchall())

# Print all schemas and tables
cur.execute("SELECT table_schema, table_name FROM information_schema.tables ORDER BY table_schema, table_name;")
rows = cur.fetchall()
print("All tables:")
for r in rows:
    print(r)

#  check ownership
cur.execute("SELECT current_database(), current_user;")
print(cur.fetchone())

# try to add schema to public
# cur.execute("SET search_path TO dynamic_law, public;")
# cur.execute("GRANT USAGE ON SCHEMA public TO jodeqa;")
# cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO jodeqa;")

conn.close()
