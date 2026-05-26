import asyncio
import sys
import json
from pathlib import Path
import psycopg

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.symbols import load_symbols
from shared.config import DATABASE_DSN

async def migrate():
    symbols = load_symbols()
    if not symbols:
        print("No symbols found in symbols.yaml to migrate.")
        return

    try:
        conn = await psycopg.AsyncConnection.connect(DATABASE_DSN)
        async with conn:
            async with conn.cursor() as cur:
                # Create table just in case schema hasn't been applied yet
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS tracked_symbols (
                        symbol            TEXT PRIMARY KEY,
                        keywords          JSONB NOT NULL DEFAULT '[]'::jsonb,
                        future            TEXT,
                        sector            TEXT,
                        require_uppercase BOOLEAN NOT NULL DEFAULT FALSE,
                        block_phrases     JSONB NOT NULL DEFAULT '[]'::jsonb,
                        is_active         BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                
                print(f"Migrating {len(symbols)} symbols...")
                inserted = 0
                for symbol, cfg in symbols.items():
                    try:
                        await cur.execute("""
                            INSERT INTO tracked_symbols 
                            (symbol, keywords, future, sector, require_uppercase, block_phrases, is_active)
                            VALUES (%s, %s, %s, %s, %s, %s, true)
                            ON CONFLICT (symbol) DO NOTHING
                        """, (
                            symbol,
                            json.dumps(cfg.get("keywords", [])),
                            cfg.get("future"),
                            cfg.get("sector"),
                            cfg.get("require_uppercase", False),
                            json.dumps(cfg.get("block_phrases", []))
                        ))
                        if cur.rowcount > 0:
                            inserted += 1
                    except Exception as e:
                        print(f"Failed to migrate {symbol}: {e}")
                
                print(f"Migration complete! Inserted {inserted} new symbols.")
    except Exception as e:
        print(f"Database connection error: {e}")

if __name__ == "__main__":
    asyncio.run(migrate())
