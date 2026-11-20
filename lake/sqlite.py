import sqlite3
from contextlib import contextmanager


def open_database(path, timeout=5.0):
    db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        return db
    except BaseException:
        db.close()
        raise


@contextmanager
def transaction(db, *, write):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
