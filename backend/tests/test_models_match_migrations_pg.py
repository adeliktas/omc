"""A freshly migrated database has every column the models declare (a fresh install found one missing)."""

import os

import pytest
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import app.models  # noqa: F401 - registers every table


def test_every_model_column_exists_after_migrations():
    url = os.getenv("MC_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires a disposable, freshly migrated MC_TEST_POSTGRES_URL")
    insp = inspect(create_engine(url.replace("+asyncpg", "+psycopg")))
    missing = [f"{t.name}.{c.name}" for t in SQLModel.metadata.sorted_tables if insp.has_table(t.name)
               for c in t.columns if c.name not in {x["name"] for x in insp.get_columns(t.name)}]
    assert missing == []
