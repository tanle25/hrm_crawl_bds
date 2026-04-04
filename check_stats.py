from db import connect_db, get_database_url
from dotenv import load_dotenv
load_dotenv('.env')
conn = connect_db(get_database_url(None))
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM llm_enrichment_queue WHERE status = 'pending'")
print('pending:', cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM llm_enrichment_queue WHERE status = 'completed'")
print('completed:', cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM llm_post_analyses WHERE status = 'completed'")
print('analyses:', cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM search_chunks")
print('chunks:', cur.fetchone()[0])

# Sample some enriched data
cur.execute("""
    SELECT result_json->>'title', result_json->>'is_ban', result_json->>'is_mua',
           result_json->>'property_type', result_json->>'price_text'
    FROM llm_post_analyses
    WHERE status = 'completed'
    LIMIT 5
""")
print('\nSample enriched posts:')
for r in cur.fetchall():
    print(f'  title={r[0]}, ban={r[1]}, mua={r[2]}, type={r[3]}, price={r[4]}')

conn.close()
