import sqlite3

import pytest

from retailpulse import db, generate


@pytest.fixture(scope="session")
def data():
    return generate.generate(generate.Config(seed=7, customers=400, orders=3000))


@pytest.fixture(scope="session")
def _session_conn(data):
    c = db.connect(":memory:")
    db.create_schema(c)
    db.load(c, data)
    return c


@pytest.fixture
def conn(_session_conn: sqlite3.Connection) -> sqlite3.Connection:
    """The shared read-only fixture database, guaranteed to be outside any transaction.

    Python's sqlite3 module opens an implicit ``BEGIN`` before an INSERT/UPDATE/DELETE. When such a
    statement fails (the constraint tests rely on ``IntegrityError``), that transaction stays open on
    the shared connection; a later ``Connection.backup()`` from a connection that holds an open write
    transaction on an in-memory database never obtains a consistent snapshot and blocks forever.
    Rolling back before and after every test keeps the fixture read-only in practice and keeps the
    whole suite hang-free.
    """
    if _session_conn.in_transaction:
        _session_conn.rollback()
    yield _session_conn
    if _session_conn.in_transaction:
        _session_conn.rollback()
