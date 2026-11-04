import os
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def migrate(path):
    env = {**os.environ, 'DATABASE_URL': f'sqlite+aiosqlite:///{path}'}
    result = subprocess.run([str(PYTHON), 'init_db.py'], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_fresh_migration_idempotent(tmp_path):
    path = tmp_path / 'fresh.db'
    migrate(path)
    migrate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0001_versioned_backend'
        assert conn.execute('SELECT COUNT(*) FROM task_dependencies').fetchone()[0] == 0


def test_legacy_data_adoption_preserves_history(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE tasks (task_id VARCHAR(36) PRIMARY KEY, message_id VARCHAR(36) NOT NULL,
                chat_id VARCHAR(100) NOT NULL, user_id VARCHAR(100) NOT NULL, content TEXT NOT NULL,
                status VARCHAR(30) NOT NULL, result TEXT, version INTEGER NOT NULL);
            CREATE TABLE messages (message_id VARCHAR(36) PRIMARY KEY, chat_id VARCHAR(100) NOT NULL,
                user_id VARCHAR(100) NOT NULL, content TEXT NOT NULL, task_id VARCHAR(36),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE task_versions (task_id VARCHAR(36), version INTEGER, content TEXT NOT NULL,
                status VARCHAR(30) NOT NULL, result TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (task_id, version));
            INSERT INTO tasks VALUES ('a','m','room','alice','updated instruction','completed','current answer',2);
            INSERT INTO messages (message_id,chat_id,user_id,content,task_id) VALUES ('m','room','alice','original','a');
            INSERT INTO task_versions (task_id,version,content,status,result) VALUES ('a',1,'original','completed','old answer');
            INSERT INTO task_versions (task_id,version,content,status) VALUES ('a',2,'stale snapshot','queued');
        ''')
    migrate(path)
    migrate(path)
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT version,content,status,result FROM task_versions ORDER BY version').fetchall()
        assert rows == [(1,'original','completed','old answer'), (2,'updated instruction','completed','current answer')]
        assert conn.execute('SELECT sequence,routing_status FROM messages').fetchone() == (1,'stored_no_action')
        assert conn.execute('SELECT last_message_sequence FROM chat_states').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


async def test_legacy_adoption_on_selected_database_engine(db):
    """Also exercises legacy adoption on real PostgreSQL when TEST_DATABASE_URL is set."""
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from database import Base

    engine = db[0]
    spec = importlib.util.spec_from_file_location('baseline', ROOT / 'migrations/versions/0001_versioned_backend.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        # This fixture owns only its fresh temporary database/schema.
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text('''CREATE TABLE tasks (
            task_id VARCHAR(36) PRIMARY KEY, message_id VARCHAR(36) NOT NULL,
            chat_id VARCHAR(100) NOT NULL, user_id VARCHAR(100) NOT NULL,
            content TEXT NOT NULL, status VARCHAR(30) NOT NULL, result TEXT, version INTEGER NOT NULL)'''))
        await connection.execute(text("INSERT INTO tasks VALUES ('a','m','chat','alice','original','completed','saved',1)"))
        def upgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                migration.upgrade()
        await connection.run_sync(upgrade)
        row = (await connection.execute(text('SELECT content,status,result FROM task_versions'))).one()
        assert tuple(row) == ('original', 'completed', 'saved')
        assert (await connection.execute(text('SELECT COUNT(*) FROM tasks'))).scalar_one() == 1
