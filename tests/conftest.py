import pytest

from glacier_monitor import create_app
from glacier_monitor.db import get_db, init_db


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(tmp_path / "test.db"),
            "WRITE_API_KEY": "",
        }
    )
    with application.app_context():
        init_db(seed=True)
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def database(app):
    with app.app_context():
        yield get_db()

