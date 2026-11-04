"""Pytest-wide test configuration.

The application binds its SQLAlchemy engine once, at import time, from the
``DATABASE_URL`` environment variable (falling back to a file-backed SQLite
database under ``instance/`` when it is unset — see ``app.py``). Most test
modules pin an in-memory database at their own import head, but whichever test
module imports ``app`` first wins for the whole process. When a module that
does not pin it (e.g. ``test_launch_diagnostics``) is imported first, the
engine binds to the file database and every later module that expects
``:memory:`` fails.

pytest imports this conftest before collecting any test module, so pinning the
in-memory database here guarantees the engine binds to it no matter the
collection order. ``setdefault`` is used so an explicit ``DATABASE_URL`` (for
example the file database the migration subprocess tests supply) still wins.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-only-secret")
