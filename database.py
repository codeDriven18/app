"""
PostgreSQL Database Module for Bozorlik AI
Handles all database operations with async support
"""
import os
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
import asyncpg
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class Database:
    """PostgreSQL database manager with connection pooling"""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool = None
    
    async def connect(self):
        """Create connection pool"""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                command_timeout=60
            )
            logger.info("✅ PostgreSQL connection pool created successfully")
            await self.init_schema()
        except Exception as e:
            logger.error(f"❌ Failed to connect to PostgreSQL: {e}")
            raise
    
    async def disconnect(self):
        """Close connection pool"""
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed")
    
    async def init_schema(self):
        """Initialize database schema"""
        async with self.pool.acquire() as conn:
            # Create tables if they don't exist
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    phone VARCHAR(32),
                    username VARCHAR(100),
                    language VARCHAR(10) NOT NULL DEFAULT 'ru',
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_languages (
                    user_id BIGINT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL DEFAULT 'ru',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    list_data JSONB NOT NULL,
                    items_list TEXT[],
                    final_amount DECIMAL(10, 2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_history_user_id 
                ON user_history(user_id)
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_history_created_at 
                ON user_history(created_at DESC)
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shopping_lists (
                    list_id VARCHAR(120) PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    language VARCHAR(10) NOT NULL DEFAULT 'ru',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    shared_slug VARCHAR(120),
                    list_data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Ensure shopping_lists has all columns for older deployments
            try:
                shopping_cols = await conn.fetch("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'shopping_lists'
                """)
                col_names = {row['column_name'] for row in shopping_cols}

                if 'status' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'")
                if 'shared_slug' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN shared_slug VARCHAR(120)")
                if 'language' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN language VARCHAR(10) NOT NULL DEFAULT 'ru'")
                if 'list_data' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN list_data JSONB NOT NULL DEFAULT '{}'::jsonb")
                if 'updated_at' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                if 'created_at' not in col_names:
                    await conn.execute("ALTER TABLE shopping_lists ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            except Exception as e:
                logger.error(f"Error aligning shopping_lists schema: {e}")

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_shopping_lists_user_status
                ON shopping_lists(user_id, status)
            """)

            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_shopping_lists_shared_slug
                ON shopping_lists(shared_slug)
                WHERE shared_slug IS NOT NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_shopping_lists_updated
                ON shopping_lists(updated_at DESC)
            """)
            
            # Check if shared_lists table exists and its structure
            try:
                # Check if table exists
                exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'shared_lists'
                    )
                """)
                
                if not exists:
                    # Create new table
                    await conn.execute("""
                        CREATE TABLE shared_lists (
                            list_id VARCHAR(50) PRIMARY KEY,
                            owner_id BIGINT NOT NULL,
                            list_data JSONB NOT NULL,
                            participants BIGINT[],
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    logger.info("✅ Created shared_lists table")
                else:
                    # Table exists, check and add missing columns
                    columns = await conn.fetch("""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_name = 'shared_lists'
                    """)
                    column_names = [row['column_name'] for row in columns]
                    
                    if 'owner_id' not in column_names:
                        await conn.execute("ALTER TABLE shared_lists ADD COLUMN owner_id BIGINT")
                        logger.info("✅ Added owner_id column to shared_lists")
                    
                    if 'participants' not in column_names:
                        await conn.execute("ALTER TABLE shared_lists ADD COLUMN participants BIGINT[]")
                        logger.info("✅ Added participants column to shared_lists")
                    
                    if 'updated_at' not in column_names:
                        await conn.execute("ALTER TABLE shared_lists ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        logger.info("✅ Added updated_at column to shared_lists")
                    
                    if 'list_data' not in column_names:
                        await conn.execute("ALTER TABLE shared_lists ADD COLUMN list_data JSONB")
                        logger.info("✅ Added list_data column to shared_lists")
                        
            except Exception as e:
                logger.error(f"Error setting up shared_lists table: {e}")
            
            try:
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_shared_lists_owner 
                    ON shared_lists(owner_id)
                """)
            except Exception:
                pass
            
            # Create expenses table if needed
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shopping_expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount DECIMAL(10, 2) NOT NULL,
                    currency VARCHAR(10) DEFAULT 'UZS',
                    list_id VARCHAR(50),
                    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expenses_user_id 
                ON shopping_expenses(user_id)
            """)
            
            logger.info("✅ Database schema initialized")
    
    # ===== USER LANGUAGES =====
    
    async def get_user_language(self, user_id: int) -> str:
        """Get user's preferred language"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT language FROM user_languages WHERE user_id = $1",
                user_id
            )
            return row['language'] if row else 'ru'

    async def upsert_user(self, user_id: int, language: Optional[str] = None, phone: Optional[str] = None,
                          username: Optional[str] = None):
        """Create or update user profile and language preference"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, phone, username, language, last_seen)
                VALUES ($1, $2, $3, COALESCE($4, 'ru'), CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE
                SET phone = COALESCE($2, users.phone),
                    username = COALESCE($3, users.username),
                    language = COALESCE($4, users.language),
                    last_seen = CURRENT_TIMESTAMP
                """,
                user_id, phone, username, language
            )

            # Keep user_languages table in sync for backward compatibility
            if language:
                await conn.execute(
                    """
                    INSERT INTO user_languages (user_id, language, updated_at)
                    VALUES ($1, $2, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id)
                    DO UPDATE SET language = $2, updated_at = CURRENT_TIMESTAMP
                    """,
                    user_id, language
                )
    
    async def set_user_language(self, user_id: int, language: str):
        """Set user's preferred language"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_languages (user_id, language, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) 
                DO UPDATE SET language = $2, updated_at = CURRENT_TIMESTAMP
            """, user_id, language)

            await conn.execute(
                """
                INSERT INTO users (user_id, language, last_seen)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE
                SET language = $2, last_seen = CURRENT_TIMESTAMP
                """,
                user_id, language
            )
    
    async def get_all_user_languages(self) -> Dict[int, str]:
        """Get all user languages"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, language FROM user_languages")
            return {row['user_id']: row['language'] for row in rows}
    
    # ===== USER HISTORY =====
    
    async def add_user_history(
        self, 
        user_id: int, 
        list_data: Dict, 
        items_list: List[str], 
        final_amount: Optional[float] = None
    ):
        """Add shopping list to user history"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_history (user_id, list_data, items_list, final_amount)
                VALUES ($1, $2, $3, $4)
            """, user_id, json.dumps(list_data), items_list, final_amount)
    
    async def get_user_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Get user's shopping history"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT list_data, items_list, final_amount, created_at
                FROM user_history
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, user_id, limit)
            
            return [{
                'list_data': json.loads(row['list_data']),
                'items_list': list(row['items_list']),
                'final_amount': float(row['final_amount']) if row['final_amount'] else None,
                'created_at': row['created_at'].isoformat()
            } for row in rows]
    
    async def get_all_user_history(self) -> Dict[int, List[Dict]]:
        """Get all user histories (for compatibility)"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, list_data, items_list, final_amount, created_at
                FROM user_history
                ORDER BY user_id, created_at DESC
            """)
            
            result = {}
            for row in rows:
                user_id = row['user_id']
                if user_id not in result:
                    result[user_id] = []
                
                result[user_id].append({
                    'list_data': json.loads(row['list_data']),
                    'items_list': list(row['items_list']),
                    'final_amount': float(row['final_amount']) if row['final_amount'] else None,
                    'created_at': row['created_at'].isoformat()
                })
            
            return result

    # ===== ACTIVE LISTS =====

    async def save_active_list(
        self,
        list_id: str,
        user_id: int,
        language: str,
        list_data: Dict,
        status: str = "active",
        shared_slug: Optional[str] = None
    ):
        """Upsert active shopping list for a user"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shopping_lists (list_id, user_id, language, status, shared_slug, list_data, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP)
                ON CONFLICT (list_id) DO UPDATE
                SET language = EXCLUDED.language,
                    status = EXCLUDED.status,
                    shared_slug = EXCLUDED.shared_slug,
                    list_data = EXCLUDED.list_data,
                    updated_at = CURRENT_TIMESTAMP
                """,
                list_id,
                user_id,
                language,
                status,
                shared_slug,
                json.dumps(list_data)
            )

    async def get_active_list(self, user_id: int) -> Optional[Dict]:
        """Return latest active list for user"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT list_id, language, status, shared_slug, list_data, updated_at
                FROM shopping_lists
                WHERE user_id = $1 AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                user_id
            )

            if not row:
                return None

            return {
                "list_id": row["list_id"],
                "language": row["language"],
                "status": row["status"],
                "shared_slug": row["shared_slug"],
                "list_data": json.loads(row["list_data"]),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }

    async def mark_list_completed(self, list_id: str):
        """Mark list as completed"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE shopping_lists
                SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                WHERE list_id = $1
                """,
                list_id
            )
    
    # ===== SHARED LISTS =====
    
    async def create_shared_list(
        self, 
        list_id: str, 
        owner_id: int, 
        list_data: Dict,
        participants: List[int] = None
    ):
        """Create a new shared list"""
        async with self.pool.acquire() as conn:
            # Check column types
            try:
                col_type = await conn.fetchval("""
                    SELECT data_type FROM information_schema.columns 
                    WHERE table_name = 'shared_lists' AND column_name = 'list_id'
                """)
                
                if col_type and 'int' in col_type.lower():
                    # Old schema with integer list_id - use hash of string as int
                    numeric_id = abs(hash(list_id)) % (10 ** 9)
                    await conn.execute("""
                        INSERT INTO shared_lists (list_id, owner_id, list_data, participants, updated_at)
                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                        ON CONFLICT (list_id)
                        DO UPDATE SET 
                            owner_id = $2,
                            list_data = $3, 
                            participants = $4,
                            updated_at = CURRENT_TIMESTAMP
                    """, numeric_id, owner_id, json.dumps(list_data), participants or [owner_id])
                else:
                    # New schema with varchar list_id
                    await conn.execute("""
                        INSERT INTO shared_lists (list_id, owner_id, list_data, participants, updated_at)
                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                        ON CONFLICT (list_id)
                        DO UPDATE SET 
                            list_data = $3, 
                            participants = $4,
                            updated_at = CURRENT_TIMESTAMP
                    """, list_id, owner_id, json.dumps(list_data), participants or [owner_id])
            except Exception as e:
                logger.error(f"Error creating shared list: {e}")
                # Fallback - try with string
                try:
                    await conn.execute("""
                        INSERT INTO shared_lists (list_id, owner_id, list_data, participants, updated_at)
                        VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    """, list_id, owner_id, json.dumps(list_data), participants or [owner_id])
                except:
                    pass

        # Keep shopping_lists table in sync for quick retrieval
        try:
            await self.save_active_list(
                list_id=list_id,
                user_id=owner_id,
                language=list_data.get("language", "ru"),
                list_data=list_data,
                status="shared",
                shared_slug=list_id
            )
        except Exception as e:
            logger.error(f"Error syncing shared list into shopping_lists: {e}")
    
    async def get_shared_list(self, list_id: str) -> Optional[Dict]:
        """Get shared list by ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT list_id, owner_id, list_data, participants, created_at, updated_at
                FROM shared_lists
                WHERE list_id = $1
            """, list_id)
            
            if not row:
                # Fallback to shopping_lists table
                row = await conn.fetchrow(
                    """
                    SELECT list_id, user_id AS owner_id, list_data, created_at, updated_at, shared_slug
                    FROM shopping_lists
                    WHERE list_id = $1 OR shared_slug = $1
                    LIMIT 1
                    """,
                    list_id
                )

                if not row:
                    return None

                return {
                    'list_id': row['list_id'],
                    'owner_id': row['owner_id'],
                    'list_data': json.loads(row['list_data']),
                    'participants': [],
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
                }

            return {
                'list_id': row['list_id'],
                'owner_id': row['owner_id'],
                'list_data': json.loads(row['list_data']),
                'participants': list(row['participants']),
                'created_at': row['created_at'].isoformat(),
                'updated_at': row['updated_at'].isoformat()
            }
    
    async def update_shared_list(self, list_id: str, list_data: Dict):
        """Update shared list data"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE shared_lists
                SET list_data = $2, updated_at = CURRENT_TIMESTAMP
                WHERE list_id = $1
            """, list_id, json.dumps(list_data))
    
    async def add_participant_to_list(self, list_id: str, user_id: int):
        """Add participant to shared list"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE shared_lists
                SET participants = array_append(participants, $2),
                    updated_at = CURRENT_TIMESTAMP
                WHERE list_id = $1 
                AND NOT ($2 = ANY(participants))
            """, list_id, user_id)
    
    async def get_all_shared_lists(self) -> Dict[str, Dict]:
        """Get all shared lists"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT list_id, owner_id, list_data, participants, created_at, updated_at
                FROM shared_lists
            """)
            
            return {
                row['list_id']: {
                    'owner_id': row['owner_id'],
                    'list_data': json.loads(row['list_data']),
                    'participants': list(row['participants']),
                    'created_at': row['created_at'].isoformat(),
                    'updated_at': row['updated_at'].isoformat()
                }
                for row in rows
            }
    
    # ===== EXPENSES =====
    
    async def add_expense(
        self,
        user_id: int,
        amount: float,
        currency: str = "UZS",
        list_id: Optional[str] = None,
        date: Optional[datetime] = None
    ):
        """Add shopping expense"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO shopping_expenses (user_id, amount, currency, list_id, date)
                VALUES ($1, $2, $3, $4, $5)
            """, user_id, amount, currency, list_id, date or datetime.now())
    
    async def get_user_expenses(
        self, 
        user_id: int, 
        limit: int = 50
    ) -> List[Dict]:
        """Get user's expenses"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT amount, currency, list_id, date, created_at
                FROM shopping_expenses
                WHERE user_id = $1
                ORDER BY date DESC
                LIMIT $2
            """, user_id, limit)
            
            return [{
                'amount': float(row['amount']),
                'currency': row['currency'],
                'list_id': row['list_id'],
                'date': row['date'].isoformat(),
                'created_at': row['created_at'].isoformat()
            } for row in rows]


# Global database instance
db: Optional[Database] = None


async def get_db() -> Database:
    """Get database instance"""
    global db
    if db is None:
        raise RuntimeError("Database not initialized")
    return db


async def init_db(database_url: str):
    """Initialize database connection"""
    global db
    db = Database(database_url)
    await db.connect()
    return db


async def close_db():
    """Close database connection"""
    global db
    if db:
        await db.disconnect()
        db = None
