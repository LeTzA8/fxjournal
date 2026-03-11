import importlib
import os
from pathlib import Path

import pytest

TEST_DB_PATH = Path(__file__).resolve().parent / "_pytest_fxjournal.sqlite"

os.environ["SECRET_KEY"] = "pytest-secret-key"
os.environ["TOKEN_SALT"] = "pytest-token-salt"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["WTF_CSRF_ENABLED"] = "0"
os.environ["APP_ENV"] = "test"

app_module = importlib.import_module("app")
models_module = importlib.import_module("models")

app = app_module.app
db = models_module.db


@pytest.fixture(scope="session")
def test_app():
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )

    with app.app_context():
        db.drop_all()
        db.create_all()

    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture
def app_ctx(test_app):
    with test_app.app_context():
        yield test_app


@pytest.fixture
def client(test_app):
    with test_app.test_client() as test_client:
        yield test_client
