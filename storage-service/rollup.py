"""
Rollup & Prune maintenance script for Social Sentiment data retention.

Atomically moves old posts into hourly_sentiment_agg and prunes raw data.

Usage:
    python rollup.py [--retention-days 7] [--quote-retention-days 90] [--dry-run]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from db import DB
from shared.config import DATABASE_DSN


def main():
    parser = argparse.ArgumentParser(description="Rollup old posts into hourly aggregates and prune raw data.")
    parser.add_argument("--retention-days", type=int, default=7,
                        help="Posts older than this many days will be rolled up and pruned (default: 7)")
    parser.add_argument("--quote-retention-days", type=int, default=90,
                        help="Stock quotes older than this many days will be pruned (default: 90)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without modifying anything")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    # Align to the hour so the retention boundary cannot split one sentiment
    # bucket between the aggregate and raw-post tiers.
    post_cutoff = (now - timedelta(days=args.retention_days)).replace(
        minute=0, second=0, microsecond=0
    )
    quote_cutoff = now - timedelta(days=args.quote_retention_days)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Rollup & Prune")
    print(f"  Post retention:  {args.retention_days} days (cutoff: {post_cutoff.isoformat()})")
    print(f"  Quote retention: {args.quote_retention_days} days (cutoff: {quote_cutoff.isoformat()})")
    print()

    db = DB(DATABASE_DSN)

    if args.dry_run:
        # Count what would be affected
        with db.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM posts WHERE timestamp < %s", [post_cutoff])
            posts_to_rollup = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(DISTINCT (symbol, "
                "date_trunc('hour', timestamp, 'UTC'))) "
                "FROM posts WHERE timestamp < %s",
                [post_cutoff]
            )
            agg_rows = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM stock_quotes WHERE timestamp < %s", [quote_cutoff])
            quotes_to_prune = cur.fetchone()[0]

        print(f"  Posts to roll up:          {posts_to_rollup:,}")
        print(f"  Aggregation rows created:  {agg_rows:,}")
        print(f"  Posts to prune:            {posts_to_rollup:,}")
        print(f"  Stock quotes to prune:     {quotes_to_prune:,}")
        print()
        print("No changes made (dry run).")
        return

    # Step 1: Atomically move posts into the cold tier
    print("Step 1: Atomically rolling up and pruning old posts...")
    rolled_up, pruned_posts = db.rollup_and_prune_posts(post_cutoff)
    print(f"  Upserted {rolled_up:,} aggregation rows.")
    print(f"  Deleted {pruned_posts:,} posts.")

    # Step 2: Prune quotes
    print("Step 2: Pruning old stock quotes...")
    pruned_quotes = db.prune_old_quotes(quote_cutoff)
    print(f"  Deleted {pruned_quotes:,} stock quotes.")

    # Summary
    print()
    print("Done!")
    print(f"  Aggregation rows upserted: {rolled_up:,}")
    print(f"  Posts pruned:              {pruned_posts:,}")
    print(f"  Quotes pruned:            {pruned_quotes:,}")


if __name__ == "__main__":
    main()
