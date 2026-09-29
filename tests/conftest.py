import pytest
from retailpulse import db, generate


@pytest.fixture(scope="session")
def data():
    return generate.generate(generate.Config(seed=7, customers=400, orders=3000))


@pytest.fixture(scope="session")
def conn(data):
    c = db.connect(":memory:")
    db.create_schema(c)
    db.load(c, data)
    return c
