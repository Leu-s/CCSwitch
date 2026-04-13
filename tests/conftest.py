"""
Shared pytest fixtures.

Each test module hard-codes `sqlite+aiosqlite:///./test_*.db` so the SQLite
files would otherwise land at the repo root and pollute the working tree.
We chdir into a session-scoped tmp dir so they end up inside it instead and
are cleaned up automatically when the session ends.
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_dbs(tmp_path_factory):
    original_cwd = os.getcwd()
    tmp = tmp_path_factory.mktemp("test_dbs")
    os.chdir(tmp)
    try:
        yield
    finally:
        os.chdir(original_cwd)
