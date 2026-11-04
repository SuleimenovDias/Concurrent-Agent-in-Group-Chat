"""Compatibility entry point: migrations now own schema adoption and backfill."""
from init_db import init_db

if __name__ == '__main__':
    init_db()
