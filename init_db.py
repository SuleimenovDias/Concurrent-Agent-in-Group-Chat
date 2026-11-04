"""Initialize or safely upgrade the development database with Alembic."""
from alembic import command
from alembic.config import Config
from pathlib import Path


def init_db() -> None:
    command.upgrade(Config(str(Path(__file__).with_name('alembic.ini'))), 'head')
    print('Database migrations applied.')


if __name__ == '__main__':
    init_db()
