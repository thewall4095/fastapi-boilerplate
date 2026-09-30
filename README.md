Interactive generator for a FastAPI + SQLAlchemy 2 + Alembic + Postgres project.

It asks for the project name and location, how Postgres should run, your entities and their
fields, and a few extras. Then it creates the project with a .venv (python -m venv + pip),
starts/prepares the database, writes a layered CRUD codebase with unit + integration tests,
creates and applies the first migration, runs the tests and makes an initial git commit.

Usage (any Python 3.10+; the project's Python version must be on PATH as pythonX.Y)
    python3 create_fastapi_project.py                               # interactive
    python3 create_fastapi_project.py --answers answers.json        # pre-fill answers, ask the rest
    python3 create_fastapi_project.py --answers answers.json --yes  # fully non-interactive

An answers file is JSON keyed like the prompts (project_name, db_mode, entities, ruff, ...; see
load_answers). Leave out project_name to be asked for it; container and database names follow it.

Only the Python standard library is used; there is nothing to install.