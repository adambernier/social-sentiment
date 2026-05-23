import os
import sys
import argparse
import time
from pathlib import Path
import psycopg

# Add service directory and project root to path
service_dir = Path(__file__).resolve().parent
sys.path.append(str(service_dir))
sys.path.append(str(service_dir.parent))

from shared.topics import TopicModel
from preprocess import clean_text

def main():
    parser = argparse.ArgumentParser(description="Backfill topic classifications for historical posts.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of processed posts.")
    parser.add_argument("--dry-run", action="store_true", help="Print classifications without committing database changes.")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for DB updates.")
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_DSN", "postgresql://postgres:sentiment@postgres:5432/sentiment")
    
    print("Connecting to database...")
    try:
        conn = psycopg.connect(dsn)
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

    print("Fetching unclassified posts...")
    query = "SELECT id, text FROM posts WHERE topic_label IS NULL ORDER BY timestamp DESC"
    if args.limit is not None:
        query += f" LIMIT {args.limit}"

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    total_rows = len(rows)
    print(f"Found {total_rows} posts to classify.")
    if total_rows == 0:
        print("Nothing to do.")
        conn.close()
        return

    print("Loading topic model...")
    topic_model = TopicModel()

    print("Classifying posts...")
    updates = []
    spam_count = 0
    classified_count = 0
    start_time = time.time()

    for idx, (post_id, text) in enumerate(rows, 1):
        cleaned = clean_text(text)
        topic_id, topic_label = topic_model.predict(cleaned)
        
        if topic_label == "General / Outlier":
            spam_count += 1
        else:
            classified_count += 1

        updates.append((topic_label, topic_id, post_id))

        if idx % 100 == 0 or idx == total_rows:
            elapsed = time.time() - start_time
            avg_speed = idx / (elapsed if elapsed > 0 else 0.001)
            print(f"Progress: {idx}/{total_rows} posts processed (Spam/Outlier: {spam_count}, Classified: {classified_count}). Speed: {avg_speed:.1f} posts/sec")

    if args.dry_run:
        print("\n[DRY RUN] Classified results (first 20):")
        for topic_label, topic_id, post_id in updates[:20]:
            orig_text = next(r[1] for r in rows if r[0] == post_id)
            print(f"  Post {post_id} -> {topic_label}: {orig_text[:100]}...")
        print(f"\n[DRY RUN] Done. Total processed: {total_rows}. Database was NOT modified.")
        conn.close()
        return

    print(f"\nWriting {total_rows} updates to database in batches of {args.batch_size}...")
    update_query = "UPDATE posts SET topic_label = %s, topic_id = %s, scored_at = NOW() WHERE id = %s"
    
    try:
        with conn.cursor() as cur:
            for i in range(0, len(updates), args.batch_size):
                batch = updates[i:i+args.batch_size]
                cur.executemany(update_query, batch)
        conn.commit()
        print("Database update complete!")
    except Exception as e:
        print(f"Error updating database: {e}")
        conn.rollback()
    finally:
        conn.close()

    total_time = time.time() - start_time
    print(f"\nSummary:")
    print(f"  Total processed: {total_rows}")
    print(f"  Spam/Outlier:    {spam_count} ({spam_count/total_rows*100:.1f}%)")
    print(f"  Classified:      {classified_count} ({classified_count/total_rows*100:.1f}%)")
    print(f"  Total time:      {total_time:.1f} seconds")
    print(f"  Average speed:   {total_rows/(total_time if total_time > 0 else 0.001):.1f} posts/sec")

if __name__ == "__main__":
    main()
