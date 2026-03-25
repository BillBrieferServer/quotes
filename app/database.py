import aiosqlite
import os

DB_PATH = os.environ.get("DB_PATH", "/app/data/quotes.db")

async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db

async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('quote', 'note')),
            content TEXT NOT NULL,
            source TEXT DEFAULT '',
            author TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS entry_topics (
            entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
            topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
            PRIMARY KEY (entry_id, topic_id)
        );
        CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type);
        CREATE INDEX IF NOT EXISTS idx_entries_content ON entries(content);
        CREATE INDEX IF NOT EXISTS idx_topics_name ON topics(name);

        -- Image support
        CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY);
    
    """)
    # Run migrations
    try:
        await db.execute("SELECT image FROM entries LIMIT 1")
    except Exception:
        await db.execute("ALTER TABLE entries ADD COLUMN image TEXT DEFAULT ''")

    await db.commit()
    await db.close()
