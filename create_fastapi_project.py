#!/usr/bin/env python3
"""
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
"""

from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

# ─────────────────────────────────────────────────────────────────────────────
# Terminal helpers
# ─────────────────────────────────────────────────────────────────────────────

if os.name == "nt":
    os.system("")  # enables ANSI colours in Windows terminals

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(code: str):
    return lambda s: f"\033[{code}m{s}\033[0m" if USE_COLOR else str(s)


bold, dim, red, green, yellow, cyan = (_style(c) for c in ("1", "2", "31", "32", "33", "36"))


def section(title: str) -> None:
    print(f"\n{bold(cyan('── ' + title + ' '))}{cyan('─' * max(0, 56 - len(title)))}")


def ok(msg: str) -> None:
    print(f"{green('✔')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('!')} {msg}")


def note(msg: str) -> None:
    print(f"  {dim(msg)}")


class Abort(Exception):
    pass


class StepFailed(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Prompting (interactive, or pre-filled from an answers file)
# ─────────────────────────────────────────────────────────────────────────────


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("y", "yes", "true", "1"):
        return True
    if s in ("n", "no", "false", "0"):
        return False
    raise ValueError("Please answer y or n.")


class Prompter:
    def __init__(self, preset: dict | None = None, assume_defaults: bool = False) -> None:
        self.preset = preset or {}
        self.assume_defaults = assume_defaults

    def has(self, key: str | None) -> bool:
        return bool(key) and key in self.preset

    def ask(self, key, label, default=None, parse=lambda s: s, allow_empty=False,
            show_default=True, before_prompt=None):
        if self.has(key):
            try:
                value = parse(self.preset[key])
                note(f"{label}: {value if value != '' else '(none)'}   ← answers file")
                return value
            except ValueError as exc:
                warn(f"Answers file: '{key}' is invalid ({exc}).")
                if self.assume_defaults:
                    raise Abort(f"Fix '{key}' in the answers file.") from exc
        if self.assume_defaults and (default is not None or allow_empty):
            value = parse(default if default is not None else "")
            note(f"{label}: {value if value != '' else '(none)'}   ← default")
            return value
        if before_prompt:
            before_prompt()
        suffix = f" [{default}]" if show_default and default not in (None, "") else ""
        while True:
            try:
                raw = input(f"{bold('?')} {label}{dim(suffix)}: ").strip()
            except EOFError:
                print()
                if default is None and not allow_empty:
                    raise Abort(f"No answer given for: {label}") from None
                raw = ""
            if raw == "" and default is not None:
                raw = str(default)
            if raw == "" and not allow_empty:
                print(f"  {red('A value is required.')}")
                continue
            try:
                return parse(raw)
            except ValueError as exc:
                print(f"  {red(str(exc))}")

    def confirm(self, key, label, default=True) -> bool:
        hint = "Y/n" if default else "y/N"
        return self.ask(key, f"{label} ({hint})", default="y" if default else "n",
                        parse=parse_bool, show_default=False)

    def choose(self, key, label, options: list[tuple[str, str]], default: str) -> str:
        keys = [k for k, _ in options]

        def parse(value) -> str:
            s = str(value).strip().lower()
            if s.isdigit() and 1 <= int(s) <= len(options):
                return keys[int(s) - 1]
            if s in keys:
                return s
            raise ValueError(f"Pick 1-{len(options)} or one of: {', '.join(keys)}")

        def show() -> None:
            print(f"{bold('?')} {label}")
            for i, (k, desc) in enumerate(options, 1):
                marker = green("›") if k == default else " "
                print(f"  {marker} {i}) {desc}")

        return self.ask(key, "  Choose" if not self.has(key) else label,
                        default=str(keys.index(default) + 1), parse=parse, before_prompt=show)


# ─────────────────────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────────────────────


def split_words(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def to_snake(text: str) -> str:
    return "_".join(split_words(text))


def to_pascal(text: str) -> str:
    return "".join(w.capitalize() for w in split_words(text))


def to_kebab(text: str) -> str:
    return "-".join(split_words(text))


def pluralize(snake: str) -> str:
    if re.search(r"[^aeiou]y$", snake):
        return snake[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", snake):
        return snake + "es"
    return snake + "s"


def py_str(value: str) -> str:
    """Python string literal with double quotes."""
    return json.dumps(value, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# Specs
# ─────────────────────────────────────────────────────────────────────────────

TYPES = ["str", "text", "int", "float", "decimal", "bool", "date", "datetime", "uuid"]
TYPE_ALIASES = {
    "string": "str", "varchar": "str", "integer": "int", "number": "float", "double": "float",
    "numeric": "decimal", "money": "decimal", "boolean": "bool", "timestamp": "datetime",
    "guid": "uuid",
}
PY_TYPE = {
    "str": "str", "text": "str", "int": "int", "float": "float", "decimal": "Decimal",
    "bool": "bool", "date": "date", "datetime": "datetime", "uuid": "uuid.UUID",
}
UNIQUE_OK = {"str", "int", "date", "datetime", "uuid"}
DEFAULT_OK = {"str", "text", "int", "float", "decimal", "bool"}
NUMERIC = {"int", "float", "decimal"}
RESERVED_FIELDS = {
    "id", "created_at", "updated_at", "metadata", "registry",
    "str", "int", "float", "bool", "date", "datetime", "decimal", "uuid", "list", "dict",
    "copy", "json", "schema", "construct", "validate", "fields",
}
RESERVED_ENTITIES = {"base", "data", "self", "skip", "limit", "db", "stmt", "value", "service",
                     "payload", "repository"}


@dataclass
class FieldSpec:
    name: str
    type: str = "str"
    required: bool = True
    unique: bool = False
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    min_value: str | None = None
    default: str | None = None

    def describe(self) -> str:
        t = self.type
        if t == "str":
            t = f"str({self.max_length})"
        elif t == "decimal":
            t = f"decimal({self.precision},{self.scale})"
        bits = [t, "required" if self.required else "optional"]
        if self.unique:
            bits.append("unique")
        if self.min_value is not None:
            bits.append(f">= {self.min_value}")
        if self.default is not None:
            bits.append(f"default {self.default}")
        return ", ".join(bits)


@dataclass
class EntitySpec:
    name: str  # PascalCase class name
    plural: str  # snake_case plural, used for the table
    id_type: str = "int"
    timestamps: bool = True
    fields: list[FieldSpec] = field(default_factory=list)

    @property
    def snake(self) -> str:
        return to_snake(self.name)

    @property
    def kebab_plural(self) -> str:
        return to_kebab(self.plural)

    @property
    def id_py(self) -> str:
        return "uuid.UUID" if self.id_type == "uuid" else "int"


@dataclass
class DbSpec:
    mode: str = "docker"  # docker | compose | existing | none
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = "postgres"
    name: str = "app_db"
    test_name: str = "app_test_db"
    container: str | None = None
    image: str = "postgres:17"
    reuse_container: bool = False
    create_databases: bool = True

    def url(self, db: str, password: str | None = None, masked: bool = False) -> str:
        pw = "***" if masked else quote(self.password if password is None else password, safe="")
        return f"postgresql+psycopg://{quote(self.user, safe='')}:{pw}@{self.host}:{self.port}/{db}"

    @property
    def data_mount(self) -> str:
        last = self.image.split("/")[-1]
        tag = last.split(":", 1)[1] if ":" in last else "latest"
        m = re.match(r"(\d+)", tag)
        major = int(m.group(1)) if m else None
        # Postgres 18+ images keep data under /var/lib/postgresql/<major>/...
        return "/var/lib/postgresql/data" if major is not None and major < 18 else "/var/lib/postgresql"

    @property
    def volume(self) -> str:
        return f"{self.container}-data"


@dataclass
class ProjectSpec:
    project_name: str
    parent_dir: str
    app_title: str
    python_version: str
    api_prefix: str
    db: DbSpec
    entities: list[EntitySpec]
    ruff: bool = True
    makefile: bool = True
    run_migrations: bool = True
    run_tests: bool = True
    git_commit: bool = True

    @property
    def path(self) -> Path:
        return Path(self.parent_dir).expanduser().resolve() / self.project_name

    @property
    def slug(self) -> str:
        return to_snake(self.project_name)


# ─────────────────────────────────────────────────────────────────────────────
# Validation / parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_project_name(value) -> str:
    s = str(value).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", s):
        raise ValueError("Use letters, digits, - or _, starting with a letter (e.g. my-api).")
    return s


def parse_python_version(value) -> str:
    s = str(value).strip()
    m = re.fullmatch(r"3\.(\d+)", s)
    if not m or int(m.group(1)) < 11:
        raise ValueError("Use a version like 3.13 (3.11 or newer).")
    if not find_python(s):
        raise ValueError(f"Python {s} isn't installed (no python{s} on PATH).")
    return s


def parse_api_prefix(value) -> str:
    s = str(value).strip().rstrip("/")
    if s in ("", "none", "/"):
        return ""
    if not s.startswith("/"):
        s = "/" + s
    if not re.fullmatch(r"(/[A-Za-z0-9._-]+)+", s):
        raise ValueError("Use a path like /api/v1 (or 'none').")
    return s


def parse_db_name(value) -> str:
    s = str(value).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", s):
        raise ValueError("Use letters, digits and _ (max 63 chars).")
    return s


def parse_port(value) -> int:
    try:
        port = int(str(value).strip())
    except ValueError:
        raise ValueError("Port must be a number.") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def parse_container_name(value) -> str:
    s = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", s):
        raise ValueError("Use letters, digits, ., - or _.")
    return s


def parse_nonempty(value) -> str:
    s = str(value)
    if not s.strip():
        raise ValueError("A value is required.")
    return s.strip()


def parse_entity_name(value, taken: set[str]) -> str:
    words = split_words(str(value))
    if not words or not words[0][0].isalpha():
        raise ValueError("Use a name starting with a letter, e.g. Product or BlogPost.")
    snake = "_".join(words)
    if keyword.iskeyword(snake) or snake in RESERVED_ENTITIES:
        raise ValueError(f"'{snake}' is reserved; pick another name.")
    if snake in taken:
        raise ValueError(f"You already have an entity called {to_pascal(snake)}.")
    return "".join(w.capitalize() for w in words)


def parse_plural(value, taken_tables: set[str]) -> str:
    snake = to_snake(str(value))
    if not re.fullmatch(r"[a-z][a-z0-9_]*", snake) or keyword.iskeyword(snake):
        raise ValueError("Use letters, digits and _, starting with a letter.")
    if snake in taken_tables:
        raise ValueError(f"Table '{snake}' is already used by another entity.")
    return snake


def parse_field_name(value, taken: set[str]) -> str:
    raw = str(value).strip()
    if not raw:
        return ""
    name = to_snake(raw)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("Use letters, digits and _, starting with a letter.")
    if keyword.iskeyword(name) or name in RESERVED_FIELDS or name.startswith("model_"):
        raise ValueError(f"'{name}' is reserved; pick another name.")
    if name in taken:
        raise ValueError(f"'{name}' is already a field.")
    return name


def parse_type(value) -> str:
    s = str(value).strip().lower()
    s = TYPE_ALIASES.get(s, s)
    if s not in TYPES:
        raise ValueError(f"Choose one of: {', '.join(TYPES)}")
    return s


def parse_precision_scale(value) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[, ]\s*(\d+)\s*", str(value))
    if not m:
        raise ValueError("Use the form precision,scale e.g. 10,2")
    p, s = int(m.group(1)), int(m.group(2))
    if not (1 <= p <= 1000 and 0 <= s < p):
        raise ValueError("Precision 1-1000, scale smaller than precision.")
    return p, s


def parse_min_value(f: FieldSpec, value) -> str | None:
    s = str(value).strip() if value is not None else ""
    if s == "":
        return None
    return _finite_number(f, s)[1]


def _finite_number(f: FieldSpec, s: str) -> tuple[Decimal, str]:
    """Parses a number for field f; returns (Decimal value, normalised text)."""
    try:
        if f.type == "int":
            text = str(int(s))
        elif f.type == "float":
            text = repr(float(s))
        else:
            text = str(Decimal(s))
        number = Decimal(text)
    except (ValueError, InvalidOperation):
        raise ValueError(f"'{s}' isn't a valid {f.type}.") from None
    if not number.is_finite():
        raise ValueError("Use a finite number.")
    return number, text


def parse_default_for(f: FieldSpec, value) -> str | None:
    """Validates a default against the field's constraints; returns a normalised string."""
    if value is None:
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    s = str(value)
    if s.strip() == "":
        return None
    if f.type in ("str", "text"):
        if f.type == "str" and len(s) > (f.max_length or 0):
            raise ValueError(f"Default is longer than max length {f.max_length}.")
        return s
    s = s.strip()
    if f.type == "bool":
        return "true" if parse_bool(s) else "false"
    number, text = _finite_number(f, s)
    if f.type == "decimal":
        if number.quantize(Decimal(1).scaleb(-(f.scale or 0))) != number:
            raise ValueError(f"Default has more than {f.scale} decimal places.")
        if abs(number) >= Decimal(10) ** ((f.precision or 0) - (f.scale or 0)):
            raise ValueError(f"Default doesn't fit decimal({f.precision},{f.scale}).")
    if f.min_value is not None and number < Decimal(f.min_value):
        raise ValueError(f"Default is below the minimum ({f.min_value}).")
    return text


def normalise_field(f: FieldSpec, taken: set[str]) -> FieldSpec:
    """Validates a whole field (used for answers-file input) and fills in defaults."""
    f.name = parse_field_name(f.name, taken)
    if not f.name:
        raise ValueError("Field name is missing.")
    f.type = parse_type(f.type)
    f.required = parse_bool(f.required)
    f.unique = parse_bool(f.unique)
    if f.type == "str":
        f.max_length = int(f.max_length or 255)
        if not 1 <= f.max_length <= 10_485_760:
            raise ValueError(f"{f.name}: max_length must be 1-10485760.")
    else:
        f.max_length = None
    if f.type == "decimal":
        f.precision, f.scale = parse_precision_scale(f"{f.precision or 10},{f.scale if f.scale is not None else 2}")
    else:
        f.precision = f.scale = None
    if f.unique and f.type not in UNIQUE_OK:
        raise ValueError(f"{f.name}: unique is only supported for {', '.join(sorted(UNIQUE_OK))}.")
    if f.unique and f.type == "str" and f.max_length < 4:
        raise ValueError(f"{f.name}: unique text fields need a max length of at least 4.")
    f.min_value = parse_min_value(f, f.min_value) if f.type in NUMERIC else None
    if f.default is not None and (not f.required or f.unique or f.type not in DEFAULT_OK):
        raise ValueError(f"{f.name}: defaults are only for required, non-unique "
                         f"{'/'.join(sorted(DEFAULT_OK))} fields.")
    f.default = parse_default_for(f, f.default)
    return f


def sample_fields() -> list[FieldSpec]:
    return [
        FieldSpec("name", "str", required=True, unique=True, max_length=100),
        FieldSpec("description", "text", required=False),
        FieldSpec("quantity", "int", required=True, min_value="0", default="0"),
        FieldSpec("is_active", "bool", required=True, default="true"),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Environment checks
# ─────────────────────────────────────────────────────────────────────────────

GIT = shutil.which("git")


def quiet(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def find_python(version: str) -> list[str] | None:
    """Command that runs Python `version`: this interpreter, pythonX.Y on PATH, or the Windows py launcher."""
    if f"{sys.version_info.major}.{sys.version_info.minor}" == version:
        return [sys.executable]
    exe = shutil.which(f"python{version}")
    if exe:
        return [exe]
    if shutil.which("py") and quiet(["py", f"-{version}", "--version"]).returncode == 0:
        return ["py", f"-{version}"]
    return None


def venv_python(root: Path) -> str:
    return str(root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python")


def docker_status() -> str:
    if not shutil.which("docker"):
        return "missing"
    return "ok" if quiet(["docker", "info"], timeout=20).returncode == 0 else "not-running"


def compose_command() -> list[str] | None:
    if shutil.which("docker") and quiet(["docker", "compose", "version"]).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def container_state(name: str) -> str | None:
    out = quiet(["docker", "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.State}}"])
    return out.stdout.strip() or None


def container_details(name: str) -> dict:
    env_out = quiet(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", name])
    env = dict(line.split("=", 1) for line in env_out.stdout.splitlines() if "=" in line)
    image = quiet(["docker", "inspect", "-f", "{{.Config.Image}}", name]).stdout.strip()
    port_line = quiet(["docker", "port", name, "5432/tcp"]).stdout.strip().splitlines()
    port = port_line[0].rsplit(":", 1)[1] if port_line else None
    return {"env": env, "image": image, "port": port}


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def first_free_port(start: int = 5432) -> int:
    for port in range(start, start + 50):
        if not port_in_use(port):
            return port
    return start


# ─────────────────────────────────────────────────────────────────────────────
# Interview
# ─────────────────────────────────────────────────────────────────────────────


def ask_project(p: Prompter) -> tuple[str, str, str, str, str]:
    section("Project")
    name = p.ask("project_name", "Project name", default="my-project", parse=parse_project_name)

    def parse_parent(value) -> str:
        parent = Path(str(value).strip()).expanduser().resolve()
        target = parent / name
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError(f"{target} already exists and isn't empty.")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"{parent} is not a directory.")
        return str(parent)

    parent = p.ask("parent_dir", "Create it inside which directory", default=str(Path.cwd()),
                   parse=parse_parent)
    note(f"→ {Path(parent) / name}")
    title = p.ask("app_title", "App title (shown in the API docs)",
                  default=" ".join(w.capitalize() for w in split_words(name)), parse=parse_nonempty)
    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    python = p.ask("python_version", "Python version", default=here if sys.version_info >= (3, 11) else "3.13",
                   parse=parse_python_version)
    prefix = p.ask("api_prefix", "API path prefix ('none' for no prefix)", default="/api/v1",
                   parse=parse_api_prefix)
    return name, parent, title, python, prefix


def ask_port(p: Prompter, key: str, default: int, check_busy: bool) -> int:
    while True:
        port = p.ask(key, "Host port", default=default, parse=parse_port)
        if not check_busy or not port_in_use(port):
            return port
        if p.has(key) or p.assume_defaults:
            warn(f"Port {port} already has something listening on it; continuing anyway.")
            return port
        if p.confirm(None, f"Port {port} is already in use on this machine. Use it anyway?", False):
            return port


def ask_database(p: Prompter, slug: str, kebab: str, docker: str) -> DbSpec:
    section("Database")
    preset_mode = str(p.preset.get("db_mode", "")).lower()
    if not p.has("db_mode") or preset_mode in ("docker", "compose", "1", "2"):
        if docker == "missing":
            warn("Docker isn't installed, so it can't start Postgres for you (options 1-2 still "
                 "generate the files and commands).")
        elif docker == "not-running":
            warn("Docker is installed but not running. Start Docker Desktop/daemon first if you want "
                 "the container created automatically.")
    options = [
        ("docker", "New Docker container (docker run)"),
        ("compose", "docker-compose.yml inside the project (docker compose up)"),
        ("existing", "An existing Postgres server (local install, cloud, other container)"),
        ("none", "Skip for now - I'll set DATABASE_URL later"),
    ]
    mode = p.choose("db_mode", "How should Postgres be set up?", options,
                    default="docker" if docker == "ok" else "none")
    db = DbSpec(mode=mode, name=f"{slug}_db", test_name=f"{slug}_test_db")

    if mode == "none":
        note("Placeholder connection settings will be written to .env.")
        return db

    if mode in ("docker", "compose"):
        defaults = {"user": "postgres", "password": "postgres", "image": "postgres:17", "port": None}
        while True:
            db.container = p.ask("db_container", "Container name", default=f"{kebab}-postgres",
                                 parse=parse_container_name)
            state = container_state(db.container) if docker == "ok" else None
            if not state:
                break
            if mode == "docker" and p.confirm(
                "db_reuse_container",
                f"A container named '{db.container}' already exists ({state}). Reuse it?", True):
                db.reuse_container = True
                details = container_details(db.container)
                defaults["user"] = details["env"].get("POSTGRES_USER", "postgres")
                defaults["password"] = details["env"].get("POSTGRES_PASSWORD", "postgres")
                defaults["image"] = details["image"] or defaults["image"]
                defaults["port"] = int(details["port"]) if details["port"] else None
                note("Pre-filled the answers below from the existing container.")
                break
            if p.has("db_container") or p.assume_defaults:
                raise Abort(f"Container '{db.container}' already exists; choose another name.")
            warn(f"'{db.container}' is taken; pick another name.")
        if db.reuse_container:
            db.image = defaults["image"]
        else:
            db.image = p.ask("db_image", "Postgres image", default="postgres:17", parse=parse_nonempty)
        db.port = ask_port(p, "db_port", defaults["port"] or first_free_port(),
                           check_busy=not db.reuse_container)
        db.user = p.ask("db_user", "Database user", default=defaults["user"], parse=parse_nonempty)
        db.password = p.ask("db_password", "Database password", default=defaults["password"],
                            parse=parse_nonempty)
    else:  # existing
        db.host = p.ask("db_host", "Host", default="localhost", parse=parse_nonempty)
        db.port = p.ask("db_port", "Port", default=5432, parse=parse_port)
        db.user = p.ask("db_user", "User", default="postgres", parse=parse_nonempty)
        db.password = p.ask("db_password", "Password", default="postgres", parse=parse_nonempty)

    db.name = p.ask("db_name", "Database name (dev)", default=db.name, parse=parse_db_name)
    db.test_name = p.ask("db_test_name", "Database name (tests)", default=f"{db.name.removesuffix('_db')}_test_db",
                         parse=parse_db_name)
    if db.test_name == db.name:
        raise Abort("The test database must be different from the dev database.")
    if mode == "existing":
        db.create_databases = p.confirm("db_create", "Create these databases if they don't exist?", True)
    return db


def ask_fields(p: Prompter, entity: str, timestamps: bool) -> list[FieldSpec]:
    auto = "id" + (", created_at, updated_at" if timestamps else "")
    print(f"\n  Fields for {bold(entity)} ({auto} added automatically).")
    print(f"  {dim('Types: ' + ', '.join(TYPES) + '. Leave the name empty when you are done.')}")
    fields: list[FieldSpec] = []
    while True:
        taken = {f.name for f in fields}
        name = p.ask(None, f"Field {len(fields) + 1} name", default="", allow_empty=True,
                     parse=lambda v: parse_field_name(v, taken), show_default=False)
        if not name:
            if fields:
                return fields
            if p.confirm(None, "No fields yet. Use sample fields (name, description, quantity, "
                               "is_active)?", True):
                return sample_fields()
            continue
        f = FieldSpec(name=name)
        f.type = p.ask(None, "    Type", default="str", parse=parse_type)
        if f.type == "str":
            f.max_length = p.ask(None, "    Max length", default=255, parse=lambda v: _int_range(v, 1, 10_485_760))
        if f.type == "decimal":
            f.precision, f.scale = p.ask(None, "    Precision,scale", default="10,2", parse=parse_precision_scale)
        f.required = p.confirm(None, "    Required?", True)
        if f.type in UNIQUE_OK:
            while True:
                f.unique = p.confirm(None, "    Unique?", False)
                if not (f.unique and f.type == "str" and f.max_length < 4):
                    break
                print(f"  {red('Unique text fields need a max length of at least 4.')}")
        if f.type in NUMERIC:
            f.min_value = p.ask(None, "    Minimum value (blank = none)", default="", allow_empty=True,
                                parse=lambda v: parse_min_value(f, v), show_default=False)
        if f.required and not f.unique and f.type in DEFAULT_OK:
            hint = " (true/false)" if f.type == "bool" else ""
            f.default = p.ask(None, f"    Default value{hint} (blank = none)", default="",
                              allow_empty=True, parse=lambda v: parse_default_for(f, v), show_default=False)
        print(f"  {green('+')} {f.name}: {f.describe()}")
        fields.append(f)


def _int_range(value, low: int, high: int) -> int:
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError("Enter a whole number.") from None
    if not low <= n <= high:
        raise ValueError(f"Enter a number between {low} and {high}.")
    return n


def entity_from_dict(d: dict, taken: set[str], taken_tables: set[str]) -> EntitySpec:
    name = parse_entity_name(d.get("name", ""), taken)
    plural = parse_plural(d.get("plural") or pluralize(to_snake(name)), taken_tables)
    id_type = str(d.get("id_type", "int")).lower()
    if id_type not in ("int", "uuid"):
        raise ValueError(f"{name}: id_type must be int or uuid.")
    e = EntitySpec(name=name, plural=plural, id_type=id_type,
                   timestamps=parse_bool(d.get("timestamps", True)))
    raw_fields = d.get("fields") or []
    if not raw_fields:
        e.fields = sample_fields()
    for fd in raw_fields:
        allowed = {k: v for k, v in fd.items() if k in FieldSpec.__dataclass_fields__}
        e.fields.append(normalise_field(FieldSpec(**allowed), {f.name for f in e.fields}))
    return e


def ask_entities(p: Prompter) -> list[EntitySpec]:
    section("Entities")
    if p.has("entities"):
        entities: list[EntitySpec] = []
        for d in p.preset["entities"]:
            try:
                e = entity_from_dict(d, {x.snake for x in entities}, {x.plural for x in entities})
            except (ValueError, TypeError) as exc:
                raise Abort(f"Answers file, entity {d.get('name', '?')}: {exc}") from exc
            entities.append(e)
            note(f"{e.name} ({e.plural}): " + "; ".join(f"{f.name} {f.describe()}" for f in e.fields))
        return entities

    entities = []
    while True:
        taken = {e.snake for e in entities}
        tables = {e.plural for e in entities}
        name = p.ask(None, "Entity name (singular, e.g. Product or BlogPost)",
                     default="Item" if not entities else None,
                     parse=lambda v: parse_entity_name(v, taken))
        plural = p.ask(None, "Plural / table name", default=pluralize(to_snake(name)),
                       parse=lambda v: parse_plural(v, tables))
        id_type = p.ask(None, "Primary key type (int/uuid)", default="int",
                        parse=lambda v: _pick(v, ("int", "uuid")))
        timestamps = p.confirm(None, "Add created_at / updated_at timestamps?", True)
        fields = ask_fields(p, name, timestamps)
        entities.append(EntitySpec(name, plural, id_type, timestamps, fields))
        ok(f"{name}: " + ", ".join(f.name for f in fields))
        if p.assume_defaults or not p.confirm(None, "Add another entity?", False):
            return entities


def _pick(value, choices: tuple[str, ...]) -> str:
    s = str(value).strip().lower()
    if s not in choices:
        raise ValueError(f"Choose one of: {', '.join(choices)}")
    return s


def interview(p: Prompter) -> ProjectSpec:
    name, parent, title, python, prefix = ask_project(p)
    docker = docker_status()
    db = ask_database(p, to_snake(name), to_kebab(name), docker)
    entities = ask_entities(p)

    section("Extras")
    ruff = p.confirm("ruff", "Add Ruff for linting and formatting?", True)
    makefile = p.confirm("makefile", "Generate a Makefile with shortcuts (make dev, make test, ...)?", True)
    run_migrations = db.mode != "none" and p.confirm(
        "run_migrations", "Create and apply the first migration?", True)
    run_tests = p.confirm("run_tests", "Run the test suite at the end?", True)
    git_commit = bool(GIT) and p.confirm("git_commit", "Initialise git and make an initial commit?", True)
    return ProjectSpec(name, parent, title, python, prefix, db, entities, ruff, makefile,
                       run_migrations, run_tests, git_commit)


def print_summary(spec: ProjectSpec) -> None:
    section("Summary")
    db = spec.db
    rows = [
        ("Location", str(spec.path)),
        ("Python", spec.python_version),
        ("API prefix", spec.api_prefix or "(none)"),
    ]
    if db.mode == "none":
        rows.append(("Database", "skipped (placeholders in .env)"))
    else:
        how = {"docker": f"docker run → {db.container}" + (" (reuse)" if db.reuse_container else ""),
               "compose": f"docker compose → {db.container}",
               "existing": f"existing server {db.host}:{db.port}"}[db.mode]
        rows += [("Database", how), ("Dev DB", db.url(db.name, masked=True)), ("Test DB", db.url(db.test_name, masked=True))]
    for e in spec.entities:
        rows.append((f"Entity {e.name}", f"/{e.kebab_plural}, id {e.id_type}"
                     + (", timestamps" if e.timestamps else "")))
        for f in e.fields:
            rows.append(("", f"  {f.name}: {f.describe()}"))
    extras = [x for x, on in (("ruff", spec.ruff), ("Makefile", spec.makefile),
                              ("migrate", spec.run_migrations), ("run tests", spec.run_tests),
                              ("git commit", spec.git_commit)) if on]
    rows.append(("Extras", ", ".join(extras) or "none"))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {bold(k.ljust(width))}  {v}")


# ─────────────────────────────────────────────────────────────────────────────
# Code generation: per-field helpers
# ─────────────────────────────────────────────────────────────────────────────


def sa_type(f: FieldSpec) -> str | None:
    """Explicit SQLAlchemy type, or None where Mapped[...] already implies it (int, float, bool, date, uuid)."""
    return {
        "str": f"String({f.max_length})", "text": "Text",
        "decimal": f"Numeric({f.precision}, {f.scale})", "datetime": "DateTime(timezone=True)",
    }.get(f.type)


def default_code(f: FieldSpec) -> str:
    if f.type == "decimal":
        return f'Decimal("{f.default}")'
    if f.type == "bool":
        return "True" if f.default == "true" else "False"
    if f.type in ("str", "text"):
        return py_str(f.default)
    return f.default


def min_code(f: FieldSpec) -> str:
    return f'Decimal("{f.min_value}")' if f.type == "decimal" else str(f.min_value)


def num_base(f: FieldSpec) -> Decimal:
    """Smallest 'nice' non-negative number that satisfies the minimum."""
    base = Decimal(f.min_value) if f.min_value is not None else Decimal(0)
    base = max(base, Decimal(0))
    return base.to_integral_value(rounding=ROUND_CEILING)


def str_sample(f: FieldSpec, n: int) -> str:
    s = f"{f.name}-{n}"
    return s if len(s) <= (f.max_length or 0) else str(n)[-(f.max_length or 1):]


def dec_str(f: FieldSpec, value: Decimal) -> str:
    return str(value.quantize(Decimal(1).scaleb(-(f.scale or 0))))


def dec_sample(f: FieldSpec, n: int) -> str:
    half = Decimal("0.5") if (f.scale or 0) > 0 else Decimal(0)
    return dec_str(f, num_base(f) + n + half)


def py_sample(f: FieldSpec, n: int) -> str:
    """Python expression for a sample value (unit tests)."""
    t = f.type
    if t == "str":
        return py_str(str_sample(f, n))
    if t == "text":
        return py_str(f"{f.name} text {n}")
    if t == "int":
        return str(int(num_base(f)) + n)
    if t == "float":
        return repr(float(num_base(f)) + n + 0.5)
    if t == "decimal":
        return f'Decimal("{dec_sample(f, n)}")'
    if t == "bool":
        return "True" if n % 2 else "False"
    if t == "date":
        return f"date(2026, 1, {n})"
    if t == "datetime":
        return f"datetime(2026, 1, {n}, 12, 0, tzinfo=UTC)"
    return f"uuid.UUID(int={n})"


def json_expr(f: FieldSpec) -> str:
    """Python expression for a JSON value in integration tests; unique fields use the counter n."""
    t = f.type
    if f.unique:
        if t == "str":
            return f'f"{f.name}-{{n}}"' if (f.max_length or 0) >= len(f.name) + 6 else "str(n)"
        if t == "int":
            return f"{int(num_base(f))} + n"
        if t == "date":
            return "(date(2026, 1, 1) + timedelta(days=n)).isoformat()"
        if t == "datetime":
            return "(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=n)).isoformat()"
        return "str(uuid.uuid4())"
    if t == "str":
        return py_str(str_sample(f, 1))
    if t == "text":
        return py_str(f"Sample {f.name.replace('_', ' ')}")
    if t == "int":
        return str(int(num_base(f)) + 1)
    if t == "float":
        return repr(float(num_base(f)) + 1.5)
    if t == "decimal":
        return py_str(dec_sample(f, 1))
    if t == "bool":
        return "True"
    if t == "date":
        return '"2026-01-15"'
    if t == "datetime":
        return '"2026-01-15T12:00:00+00:00"'
    return "str(uuid.UUID(int=42))"


def json_patch_value(f: FieldSpec) -> str:
    t = f.type
    if t == "str":
        return py_str("updated"[: f.max_length])
    if t == "text":
        return py_str("Updated text")
    if t == "int":
        return str(int(num_base(f)) + 1000)
    if t == "float":
        return repr(float(num_base(f)) + 1000.25)
    if t == "decimal":
        top = Decimal(10) ** ((f.precision or 10) - (f.scale or 0)) - 1
        return py_str(dec_str(f, top))
    if t == "bool":
        return "False"
    if t == "date":
        return '"2030-06-15"'
    if t == "datetime":
        return '"2030-06-15T08:30:00+00:00"'
    return "str(uuid.UUID(int=999))"


def assert_equal(f: FieldSpec, actual: str, expected: str) -> str:
    if f.type == "bool" and expected in ("True", "False"):
        return f"assert {actual} is {expected}"
    if f.type == "decimal":
        want = f"Decimal({expected})" if expected.startswith('"') else f"Decimal(str({expected}))"
        return f"assert Decimal(str({actual})) == {want}"
    if f.type == "float":
        return f"assert {actual} == pytest.approx({expected})"
    if f.type == "datetime":
        return f"assert datetime.fromisoformat({actual}) == datetime.fromisoformat({expected})"
    return f"assert {actual} == {expected}"


def def_line(name: str, params: list[str], returns: str = "", indent: str = "") -> list[str]:
    """A `def` line, wrapped like ruff/black when it exceeds 100 columns."""
    ret = f" -> {returns}" if returns else ""
    one = f"{indent}def {name}({', '.join(params)}){ret}:"
    if len(one) <= 100:
        return [one]
    inner = f"{indent}    {', '.join(params)}"
    if len(inner) <= 100:
        return [f"{indent}def {name}(", inner, f"{indent}){ret}:"]
    return [f"{indent}def {name}(", *[f"{indent}    {x}," for x in params], f"{indent}){ret}:"]


def patch_field(e: EntitySpec) -> FieldSpec:
    for f in e.fields:
        if not f.unique:
            return f
    return e.fields[0]


def std_imports(*, uuid_mod=False, dates: set[str] | None = None, decimal=False,
                extra: list[str] | None = None) -> list[str]:
    """Standard-library import block in isort order: `import x` lines, then `from x import y`."""
    plain = {x for x in extra or [] if x.startswith("import ")}
    froms = [x for x in extra or [] if x.startswith("from ")]
    if uuid_mod:
        plain.add("import uuid")
    if dates:
        order = ["UTC", "date", "datetime", "timedelta"]
        froms.append("from datetime import " + ", ".join(n for n in order if n in dates))
    if decimal:
        froms.append("from decimal import Decimal")
    return sorted(plain) + sorted(froms, key=lambda line: line.split()[1])


def join_blocks(*blocks: list[str]) -> str:
    """Joins import blocks / code blocks with blank lines, skipping empty ones."""
    parts = ["\n".join(b) for b in blocks if b]
    return "\n\n".join(parts).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Code generation: entity files
# ─────────────────────────────────────────────────────────────────────────────


def gen_model(e: EntitySpec) -> str:
    types = {f.type for f in e.fields}
    sa_names = sorted({t.split("(")[0] for f in e.fields if (t := sa_type(f))})
    std = std_imports(uuid_mod=e.id_type == "uuid" or "uuid" in types,
                      dates={t for t in types if t in ("date", "datetime")},
                      decimal="decimal" in types)
    third = ([f"from sqlalchemy import {', '.join(sa_names)}"] if sa_names else []) + \
        ["from sqlalchemy.orm import Mapped, mapped_column"]
    first = [f"from app.models.base import Base{', TimestampMixin' if e.timestamps else ''}"]
    body = [f"class {e.name}({'TimestampMixin, ' if e.timestamps else ''}Base):",
            f'    __tablename__ = "{e.plural}"', ""]
    if e.id_type == "uuid":
        body.append("    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)")
    else:
        body.append("    id: Mapped[int] = mapped_column(primary_key=True)")
    for f in e.fields:
        ann = PY_TYPE[f.type] if f.required else f"{PY_TYPE[f.type]} | None"
        args = [t] if (t := sa_type(f)) else []
        if f.unique:
            args.append("unique=True")  # Postgres backs the constraint with an index
        if f.default is not None:
            args.append(f"default={default_code(f)}")
        body.append(f"    {f.name}: Mapped[{ann}]" + (f" = mapped_column({', '.join(args)})" if args else ""))
    return join_blocks(std, third, first) + "\n\n" + "\n".join(body) + "\n"


def schema_line(f: FieldSpec, mode: str) -> tuple[str, bool]:
    t = PY_TYPE[f.type]
    constraints = []
    if f.type in ("str", "text") and f.required:
        constraints.append("min_length=1")
    if f.type == "str":
        constraints.append(f"max_length={f.max_length}")
    if f.type == "decimal":
        constraints += [f"max_digits={f.precision}", f"decimal_places={f.scale}"]
    if f.min_value is not None:
        constraints.append(f"ge={min_code(f)}")
    if mode == "update" or not f.required:
        ann, default = f"{t} | None", "None"
    else:
        ann, default = t, (default_code(f) if f.default is not None else None)
    if constraints:
        args = ([f"default={default}"] if default is not None else []) + constraints
        return f"    {f.name}: {ann} = Field({', '.join(args)})", True
    if default is not None:
        return f"    {f.name}: {ann} = {default}", False
    return f"    {f.name}: {ann}", False


def gen_schema(e: EntitySpec) -> str:
    C = e.name
    base = [schema_line(f, "base") for f in e.fields]
    upd = [schema_line(f, "update") for f in e.fields]
    uses_field = any(u for _, u in base + upd)
    required = [f.name for f in e.fields if f.required]
    body = [f"class {C}Base(BaseModel):", *[line for line, _ in base], "", "",
            f"class {C}Create({C}Base):", "    pass", "", "",
            f"class {C}Update(BaseModel):",
            '    """PATCH body: every field is optional and only the fields sent are updated."""', "",
            *[line for line, _ in upd]]
    if required:
        body += ["", f"    @field_validator({', '.join(py_str(n) for n in required)})",
                 "    @classmethod",
                 "    def reject_explicit_null(cls, value):",
                 "        # Leaving a field out is fine; sending null for a NOT NULL column is not.",
                 "        if value is None:",
                 '            raise ValueError("cannot be null")',
                 "        return value"]
    body += ["", "", f"class {C}Read({C}Base):", "    model_config = ConfigDict(from_attributes=True)", "",
             f"    id: {e.id_py}"]
    if e.timestamps:
        body += ["    created_at: datetime", "    updated_at: datetime"]
    types = {f.type for f in e.fields}
    dates = {t for t in types if t in ("date", "datetime")} | ({"datetime"} if e.timestamps else set())
    std = std_imports(uuid_mod=e.id_type == "uuid" or "uuid" in types, dates=dates, decimal="decimal" in types)
    pyd = ["BaseModel", "ConfigDict"] + (["Field"] if uses_field else []) + (["field_validator"] if required else [])
    return join_blocks(std, [f"from pydantic import {', '.join(pyd)}"]) + "\n\n" + "\n".join(body) + "\n"


def gen_repository(e: EntitySpec) -> str:
    C, s = e.name, e.snake
    unique = any(f.unique for f in e.fields)  # the DB's unique constraint enforces it; we only translate the error
    std = std_imports(uuid_mod=e.id_type == "uuid")
    third = ((["from psycopg.errors import UniqueViolation"] if unique else []) + ["from sqlalchemy import select"]
             + (["from sqlalchemy.exc import IntegrityError"] if unique else []) + ["from sqlalchemy.orm import Session"])
    first = ((["from app.core.exceptions import ConflictError"] if unique else [])
             + [f"from app.models.{s} import {C}", f"from app.schemas.{s} import {C}Create, {C}Update"])
    order = f"{C}.id" if e.id_type == "int" or not e.timestamps else f"{C}.created_at, {C}.id"
    commit = "self._commit()" if unique else "self.db.commit()"
    body = [f"class {C}Repository:",
            f'    """All database access for {C} lives here, nothing else."""', "",
            "    def __init__(self, db: Session) -> None:",
            "        self.db = db", "",
            f"    def get(self, {s}_id: {e.id_py}) -> {C} | None:",
            f"        return self.db.get({C}, {s}_id)", "",
            f"    def get_all(self, skip: int, limit: int) -> list[{C}]:",
            f"        stmt = select({C}).order_by({order}).offset(skip).limit(limit)",
            "        return list(self.db.scalars(stmt))", "",
            f"    def create(self, data: {C}Create) -> {C}:",
            f"        {s} = {C}(**data.model_dump())",
            f"        self.db.add({s})",
            f"        {commit}  # expires {s}; the next attribute read reloads server defaults",
            f"        return {s}", "",
            f"    def update(self, {s}: {C}, data: {C}Update) -> {C}:",
            "        for field, value in data.model_dump(exclude_unset=True).items():",
            f"            setattr({s}, field, value)",
            f"        {commit}",
            f"        return {s}", "",
            f"    def delete(self, {s}: {C}) -> None:",
            f"        self.db.delete({s})",
            "        self.db.commit()"]
    if unique:
        body += ["",
                 "    def _commit(self) -> None:",
                 '        """Commit, turning a unique-constraint violation into a ConflictError (409)."""',
                 "        try:",
                 "            self.db.commit()",
                 "        except IntegrityError as exc:",
                 "            self.db.rollback()",
                 "            if isinstance(exc.orig, UniqueViolation):",
                 '                raise ConflictError(exc.orig.diag.message_detail or "Already exists") from exc',
                 "            raise"]
    return join_blocks(std, third, first) + "\n\n" + "\n".join(body) + "\n"


def gen_service(e: EntitySpec) -> str:
    C, s, P = e.name, e.snake, e.plural
    std = std_imports(uuid_mod=e.id_type == "uuid")
    first = ["from app.core.exceptions import NotFoundError",
             f"from app.models.{s} import {C}",
             f"from app.repositories.{s} import {C}Repository",
             f"from app.schemas.{s} import {C}Create, {C}Update"]
    body = [f"class {C}Service:",
            f'    """Business rules for {C}. Knows nothing about HTTP or SQL."""', "",
            f"    def __init__(self, repository: {C}Repository) -> None:",
            "        self.repository = repository", "",
            f"    def list_{P}(self, skip: int, limit: int) -> list[{C}]:",
            "        return self.repository.get_all(skip=skip, limit=limit)", "",
            f"    def get_{s}(self, {s}_id: {e.id_py}) -> {C}:",
            f"        {s} = self.repository.get({s}_id)",
            f"        if {s} is None:",
            f'            raise NotFoundError(f"{C} {{{s}_id}} not found")',
            f"        return {s}", "",
            f"    def create_{s}(self, data: {C}Create) -> {C}:",
            "        return self.repository.create(data)", "",
            *def_line(f"update_{s}", ["self", f"{s}_id: {e.id_py}", f"data: {C}Update"], C, "    "),
            f"        {s} = self.get_{s}({s}_id)",
            f"        return self.repository.update({s}, data)", "",
             f"    def delete_{s}(self, {s}_id: {e.id_py}) -> None:",
             f"        {s} = self.get_{s}({s}_id)",
             f"        self.repository.delete({s})"]
    return join_blocks(std, first) + "\n\n" + "\n".join(body) + "\n"


def gen_routes(e: EntitySpec) -> str:
    C, s, P = e.name, e.snake, e.plural
    std = std_imports(uuid_mod=e.id_type == "uuid", extra=["from typing import Annotated"])
    third = ["from fastapi import APIRouter, Query, status"]
    first = [f"from app.api.deps import {C}ServiceDep", f"from app.schemas.{s} import {C}Create, {C}Read, {C}Update"]
    idt = e.id_py
    body = [f'router = APIRouter(prefix="/{e.kebab_plural}", tags=["{e.kebab_plural}"])', "", "",
            f'@router.get("", response_model=list[{C}Read])',
            f"def list_{P}(",
            f"    service: {C}ServiceDep,",
            "    skip: Annotated[int, Query(ge=0)] = 0,",
            "    limit: Annotated[int, Query(ge=1, le=100)] = 100,",
            "):",
            f"    return service.list_{P}(skip=skip, limit=limit)", "", "",
            f'@router.post("", response_model={C}Read, status_code=status.HTTP_201_CREATED)',
            f"def create_{s}(payload: {C}Create, service: {C}ServiceDep):",
            f"    return service.create_{s}(payload)", "", "",
            f'@router.get("/{{{s}_id}}", response_model={C}Read)',
            *def_line(f"get_{s}", [f"{s}_id: {idt}", f"service: {C}ServiceDep"]),
            f"    return service.get_{s}({s}_id)", "", "",
            f'@router.patch("/{{{s}_id}}", response_model={C}Read)',
            *def_line(f"update_{s}", [f"{s}_id: {idt}", f"payload: {C}Update", f"service: {C}ServiceDep"]),
            f"    return service.update_{s}({s}_id, payload)", "", "",
            f'@router.delete("/{{{s}_id}}", status_code=status.HTTP_204_NO_CONTENT)',
            *def_line(f"delete_{s}", [f"{s}_id: {idt}", f"service: {C}ServiceDep"], "None"),
            f"    service.delete_{s}({s}_id)"]
    return join_blocks(std, third, first) + "\n" + "\n".join(body) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Code generation: tests
# ─────────────────────────────────────────────────────────────────────────────


def _sample_imports(e: EntitySpec, *, uuid_extra=False, extra: list[str] | None = None) -> list[str]:
    types = {f.type for f in e.fields}
    dates = set()
    if "date" in types:
        dates.add("date")
    if "datetime" in types:
        dates |= {"datetime", "UTC"}
    return std_imports(uuid_mod=uuid_extra or "uuid" in types, dates=dates, decimal="decimal" in types,
                       extra=extra)


def gen_unit_service_test(e: EntitySpec) -> str:
    C, s = e.name, e.snake
    pf = patch_field(e)
    ids = ("1", "2") if e.id_type == "int" else ("uuid.UUID(int=1)", "uuid.UUID(int=2)")
    std = _sample_imports(e, uuid_extra=e.id_type == "uuid", extra=["from unittest.mock import create_autospec"])
    first = ["from app.core.exceptions import NotFoundError",
             f"from app.models.{s} import {C}",
             f"from app.repositories.{s} import {C}Repository",
             f"from app.schemas.{s} import {C}Create, {C}Update",
             f"from app.services.{s} import {C}Service"]
    L = [f"ID_A = {ids[0]}", f"ID_B = {ids[1]}", "", "",
         "@pytest.fixture", "def repo():",
         "    # Autospec: the mock has exactly the repository's methods and signatures",
         f"    return create_autospec({C}Repository, instance=True)", "", "",
         "@pytest.fixture", "def service(repo):", f"    return {C}Service(repo)", "", "",
         f"def make_{s}(**overrides) -> {C}:", "    data = {", '        "id": ID_A,',
         *[f'        "{f.name}": {py_sample(f, 1)},' for f in e.fields], "    }",
         f"    return {C}(**(data | overrides))", "", "",
         "def create_payload(**overrides) -> dict:", "    return {",
         *[f'        "{f.name}": {py_sample(f, 1)},' for f in e.fields], "    } | overrides", "", ""]
    L += [f"def test_get_{s}_returns_it(service, repo):",
          f"    repo.get.return_value = make_{s}()", "",
          f"    result = service.get_{s}(ID_A)", "",
          "    assert result.id == ID_A",
          "    repo.get.assert_called_once_with(ID_A)", "", "",
          f"def test_get_{s}_raises_not_found(service, repo):",
          "    repo.get.return_value = None", "",
          "    with pytest.raises(NotFoundError):",
          f"        service.get_{s}(ID_B)", "", "",
          f"def test_create_{s}_saves_it(service, repo):",
          f"    repo.create.return_value = make_{s}()", "",
          f"    result = service.create_{s}({C}Create(**create_payload()))", "",
          "    assert result.id == ID_A",
          "    repo.create.assert_called_once()", "", "",
          f"def test_update_{s}_saves_changes(service, repo):",
          f"    existing = make_{s}()",
          "    repo.get.return_value = existing",
          "    repo.update.return_value = existing", "",
          f"    service.update_{s}(ID_A, {C}Update({pf.name}={py_sample(pf, 2)}))", "",
          "    repo.update.assert_called_once()", "", "",
          f"def test_update_missing_{s}_raises_not_found(service, repo):",
          "    repo.get.return_value = None", "",
          "    with pytest.raises(NotFoundError):",
          f"        service.update_{s}(ID_B, {C}Update({pf.name}={py_sample(pf, 2)}))", "", "",
          f"def test_delete_{s}_deletes_it(service, repo):",
          f"    existing = make_{s}()",
          "    repo.get.return_value = existing", "",
          f"    service.delete_{s}(ID_A)", "",
          "    repo.delete.assert_called_once_with(existing)", "", "",
          f"def test_delete_missing_{s}_raises_not_found(service, repo):",
          "    repo.get.return_value = None", "",
          "    with pytest.raises(NotFoundError):",
          f"        service.delete_{s}(ID_B)", "",
          "    repo.delete.assert_not_called()"]
    return join_blocks(std, ["import pytest"], first) + "\n" + "\n".join(L) + "\n"


def gen_unit_schema_test(e: EntitySpec) -> str | None:
    """Only the explicit-null rule is ours; lengths, minimums and defaults are pydantic's job."""
    required = [f for f in e.fields if f.required]
    if not required:
        return None
    L = [f"def test_update_rejects_explicit_null_{required[0].name}():",
         "    with pytest.raises(ValidationError):",
         f"        {e.name}Update({required[0].name}=None)"]
    return (join_blocks(["import pytest", "from pydantic import ValidationError"],
                        [f"from app.schemas.{e.snake} import {e.name}Update"]) + "\n\n" + "\n".join(L) + "\n")


def gen_integration_test(e: EntitySpec, api_prefix: str) -> str:
    s, P = e.snake, e.plural
    uniques = [f for f in e.fields if f.unique]
    pf = patch_field(e)
    other = next((f for f in e.fields if f is not pf), None)
    required_no_default = [f for f in e.fields if f.required and f.default is None]
    exprs = {f.name: json_expr(f) for f in e.fields}
    uses_n = any(f.unique and f.type != "uuid" for f in e.fields)
    all_code = " ".join(exprs.values()) + " " + json_patch_value(pf)
    dates = {name for name in ("date", "datetime", "timedelta", "UTC") if re.search(rf"\b{name}\b", all_code)}
    if any(f.type == "datetime" for f in e.fields):
        dates.add("datetime")
    needs_uuid = e.id_type == "uuid" or "uuid." in all_code
    needs_pytest = any(f.type == "float" for f in e.fields)
    std = std_imports(uuid_mod=needs_uuid, dates=dates, decimal=any(f.type == "decimal" for f in e.fields),
                      extra=["import itertools"] if uses_n else [])
    third = (["import pytest"] if needs_pytest else []) + ["from fastapi.testclient import TestClient"]
    missing = "{uuid.uuid4()}" if e.id_type == "uuid" else "999999"
    L = [f'BASE = "{api_prefix}/{e.kebab_plural}"']
    if uses_n:
        L += ["_seq = itertools.count(1)  # keeps unique fields unique across calls"]
    L += ["", "", "def make_payload(**overrides) -> dict:"]
    if uses_n:
        L += ["    n = next(_seq)"]
    L += ["    return {", *[f'        "{k}": {v},' for k, v in exprs.items()], "    } | overrides", "", "",
          f"def create_{s}(client: TestClient, **overrides) -> dict:",
          "    response = client.post(BASE, json=make_payload(**overrides))",
          "    assert response.status_code == 201, response.text",
          "    return response.json()", "", "",
          f"def test_create_{s}(client):",
          "    payload = make_payload()", "",
          "    response = client.post(BASE, json=payload)", "",
          "    assert response.status_code == 201, response.text",
          "    data = response.json()",
          '    assert data["id"]',
          *[f"    {assert_equal(f, f'data[\"{f.name}\"]', f'payload[\"{f.name}\"]')}" for f in e.fields]]
    if e.timestamps:
        L += ['    assert data["created_at"] and data["updated_at"]']
    L += ["", ""]
    if uniques:
        uf = uniques[0]
        L += [f"def test_create_{s}_duplicate_{uf.name}_returns_409(client):",
              "    first = make_payload()",
              f"    create_{s}(client, **first)", "",
              f'    response = client.post(BASE, json=make_payload({uf.name}=first["{uf.name}"]))', "",
              "    assert response.status_code == 409", "", ""]
    bad = "{}" if required_no_default else f'{{"{e.fields[0].name}": ["not", "valid"]}}'
    L += [f"def test_create_{s}_invalid_payload_returns_422(client):",
          f"    response = client.post(BASE, json={bad})", "",
          "    assert response.status_code == 422", "", "",
          f"def test_list_{P}(client):",
          f"    first = create_{s}(client)",
          f"    second = create_{s}(client)", "",
          "    response = client.get(BASE)", "",
          "    assert response.status_code == 200",
          '    assert {row["id"] for row in response.json()} == {first["id"], second["id"]}', "", "",
          f"def test_list_{P}_pagination(client):",
          "    for _ in range(3):",
          f"        create_{s}(client)", "",
          '    response = client.get(BASE, params={"skip": 1, "limit": 1})', "",
          "    assert response.status_code == 200",
          "    assert len(response.json()) == 1", "", "",
          f"def test_get_{s}(client):",
          f"    created = create_{s}(client)", "",
          "    response = client.get(f\"{BASE}/{created['id']}\")", "",
          "    assert response.status_code == 200",
          "    assert response.json() == created", "", "",
          f"def test_get_missing_{s}_returns_404(client):",
          f'    response = client.get(f"{{BASE}}/{missing}")', "",
          "    assert response.status_code == 404", "", "",
          f"def test_update_{s}_partial(client):",
          f"    created = create_{s}(client)", "",
          f"    response = client.patch(f\"{{BASE}}/{{created['id']}}\", json={{\"{pf.name}\": {json_patch_value(pf)}}})", "",
          "    assert response.status_code == 200, response.text",
          "    body = response.json()",
          f"    {assert_equal(pf, f'body[\"{pf.name}\"]', json_patch_value(pf))}"]
    if other:
        L += [f'    assert body["{other.name}"] == created["{other.name}"]  # untouched field kept']
    L += ["", ""]
    if uniques:
        uf = uniques[0]
        L += [f"def test_update_{s}_to_taken_{uf.name}_returns_409(client):",
              f"    first = create_{s}(client)",
              f"    second = create_{s}(client)", "",
              f"    response = client.patch(f\"{{BASE}}/{{second['id']}}\", json={{\"{uf.name}\": first[\"{uf.name}\"]}})", "",
              "    assert response.status_code == 409", "", ""]
    L += [f"def test_delete_{s}(client):",
          f"    created = create_{s}(client)", "",
          "    delete_response = client.delete(f\"{BASE}/{created['id']}\")",
          "    get_response = client.get(f\"{BASE}/{created['id']}\")", "",
          "    assert delete_response.status_code == 204",
          "    assert get_response.status_code == 404"]
    return join_blocks(std, third) + "\n" + "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Code generation: shared project files
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PY = '''\
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings. Real environment variables win over values in the .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    debug: bool = False

    database_url: str
    test_database_url: str | None = None


settings = Settings()
'''

DATABASE_PY = '''\
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=settings.debug,  # logs SQL when DEBUG=true
)

SessionLocal = sessionmaker(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one DB session per request, always closed."""
    with SessionLocal() as db:
        yield db
'''

EXCEPTIONS_PY = '''\
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for errors the API should turn into clean HTTP responses."""

    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
'''

BASE_PY = '''\
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Predictable constraint names -> clean, reviewable Alembic migrations
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
'''

ALEMBIC_ENV_PY = '''\
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings
from app.models import Base  # importing the package registers every model

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Online mode only: offline `--sql` script output isn't wired up; add it back from `alembic init` if needed.
engine = create_engine(settings.database_url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
'''

# `alembic init` writes ~150 lines, mostly comments; this is what's left once they're gone.
# The DB URL comes from .env via env.py, so there's no sqlalchemy.url placeholder.
ALEMBIC_INI = '''\
[alembic]
script_location = %(here)s/alembic
prepend_sys_path = .
path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
'''

INTEGRATION_CONFTEST_PY = '''\
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models import Base


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if not settings.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    engine = create_engine(settings.test_database_url)
    try:
        with engine.connect():
            pass
    except OperationalError:
        pytest.skip("Test database is not reachable - is Postgres running?")

    Base.metadata.drop_all(engine)  # start clean even if a previous run crashed
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """
    Each test runs inside a transaction that is rolled back afterwards,
    so tests never see each other's data. `create_savepoint` lets app code
    call session.commit() without actually committing the outer transaction.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
'''

# Lives in tests/unit: /health doesn't touch the database, so it shouldn't skip when Postgres is down.
HEALTH_TEST_PY = '''\
from fastapi.testclient import TestClient

from app.main import app


def test_health():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
'''


def render(template: str, **values: str) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    leftover = re.search(r"\{\{[A-Z_]+\}\}", out)
    if leftover:
        raise RuntimeError(f"Unfilled placeholder {leftover.group(0)}")
    return out


def gen_models_init(entities: list[EntitySpec]) -> str:
    imports = [("base", "Base")] + [(e.snake, e.name) for e in entities]
    lines = [
        "# Import every model here so Base.metadata knows about it",
        "# (Alembic autogenerate and test table creation rely on this).",
        *[f"from app.models.{mod} import {cls}" for mod, cls in sorted(imports)],
        "",
        f"__all__ = [{', '.join(py_str(c) for c in ['Base'] + [e.name for e in entities])}]",
    ]
    return "\n".join(lines) + "\n"


def gen_deps(entities: list[EntitySpec]) -> str:
    first = ["from app.core.database import get_db"]
    first += sorted(f"from app.repositories.{e.snake} import {e.name}Repository" for e in entities)
    first += sorted(f"from app.services.{e.snake} import {e.name}Service" for e in entities)
    body = ["DbSession = Annotated[Session, Depends(get_db)]"]
    for e in entities:
        body += ["", "",
                 f"def get_{e.snake}_service(db: DbSession) -> {e.name}Service:",
                 f"    return {e.name}Service({e.name}Repository(db))", "", "",
                 f"{e.name}ServiceDep = Annotated[{e.name}Service, Depends(get_{e.snake}_service)]"]
    return join_blocks(["from typing import Annotated"],
                       ["from fastapi import Depends", "from sqlalchemy.orm import Session"],
                       first) + "\n" + "\n".join(body) + "\n"


def gen_main(spec: ProjectSpec) -> str:
    modules = sorted(e.plural for e in spec.entities)
    prefix = f', prefix="{spec.api_prefix}"' if spec.api_prefix else ""
    return "\n".join([
        "from fastapi import FastAPI", "",
        # aliased so a resource called e.g. "settings" can't shadow app.core.config.settings
        *[f"from app.api.routes.{m} import router as {m}_router" for m in modules],
        "from app.core.config import settings",
        "from app.core.exceptions import register_exception_handlers", "",
        f"app = FastAPI(title={py_str(spec.app_title)}, debug=settings.debug)",
        "register_exception_handlers(app)",
        *[f"app.include_router({m}_router{prefix})" for m in modules], "", "",
        '@app.get("/health", tags=["health"])',
        "def health() -> dict[str, str]:",
        '    return {"status": "ok"}',
    ]) + "\n"


def gen_env(spec: ProjectSpec, example: bool = False) -> str:
    db = spec.db
    password = "change-me" if example and db.mode == "existing" else db.password
    lines = ["DEBUG=true", ""]
    if db.mode == "none":
        lines += ["# Placeholder: point this at your Postgres database.",
                  f"DATABASE_URL={db.url(db.name, password)}",
                  "# Uncomment once the test database exists (integration tests skip until then).",
                  f"# TEST_DATABASE_URL={db.url(db.test_name, password)}"]
    else:
        lines += [f"DATABASE_URL={db.url(db.name, password)}",
                  f"TEST_DATABASE_URL={db.url(db.test_name, password)}"]
    return "\n".join(lines) + "\n"


def yaml_str(value: str) -> str:
    # single-quoted YAML; "$" doubled so docker compose doesn't treat it as a variable
    return "'" + value.replace("'", "''").replace("$", "$$") + "'"


def gen_compose(db: DbSpec) -> str:
    return textwrap.dedent(f"""\
        services:
          db:
            image: {db.image}
            container_name: {db.container}
            environment:
              POSTGRES_USER: {yaml_str(db.user)}
              POSTGRES_PASSWORD: {yaml_str(db.password)}
              POSTGRES_DB: {yaml_str(db.name)}
            ports:
              - "{db.port}:5432"
            volumes:
              - pgdata:{db.data_mount}
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
              interval: 5s
              timeout: 5s
              retries: 10

        volumes:
          pgdata:
        """)


def docker_run_cmd(db: DbSpec, mask: bool = False) -> list[str]:
    pw = "***" if mask else db.password
    return ["docker", "run", "-d", "--name", db.container,
            "-e", f"POSTGRES_USER={db.user}", "-e", f"POSTGRES_PASSWORD={pw}", "-e", f"POSTGRES_DB={db.name}",
            "-p", f"{db.port}:5432", "-v", f"{db.volume}:{db.data_mount}", db.image]


def shell_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def db_commands(spec: ProjectSpec) -> dict[str, str]:
    db = spec.db
    if db.mode == "docker":
        return {"up": f"docker start {db.container}", "down": f"docker stop {db.container}",
                "shell": f"docker exec -it {db.container} psql -U {shlex.quote(db.user)} -d {db.name}",
                "logs": f"docker logs -f {db.container}"}
    if db.mode == "compose":
        return {"up": "docker compose up -d db", "down": "docker compose stop db",
                "shell": f"docker compose exec db psql -U {shlex.quote(db.user)} -d {db.name}",
                "logs": "docker compose logs -f db"}
    return {}


def gen_makefile(spec: ProjectSpec) -> str:
    t = "\t"
    targets: list[tuple[str, str, list[str]]] = [
        ("install", "Create .venv and install dependencies",
         [f"python{spec.python_version} -m venv .venv", "$(BIN)/pip install -r requirements-dev.txt"]),
        ("dev", "Run the API with auto-reload", ["$(BIN)/fastapi dev app/main.py"]),
        ("test", "Run all tests", ["$(BIN)/pytest"]),
        ("test-unit", "Run unit tests only", ["$(BIN)/pytest tests/unit"]),
        ("test-integration", "Run integration tests only", ["$(BIN)/pytest tests/integration"]),
        ("cov", "Tests with coverage (terminal + htmlcov/)",
         ["$(BIN)/pytest --cov --cov-report=term-missing --cov-report=html"]),
        ("migration", 'New migration: make migration m="add orders"',
         ['$(BIN)/alembic revision --autogenerate -m "$(m)"']),
        ("migrate", "Apply all migrations", ["$(BIN)/alembic upgrade head"]),
        ("downgrade", "Undo the last migration", ["$(BIN)/alembic downgrade -1"]),
    ]
    if spec.ruff:
        targets += [("lint", "Lint with ruff", ["$(BIN)/ruff check ."]),
                    ("format", "Format and auto-fix with ruff", ["$(BIN)/ruff format .", "$(BIN)/ruff check --fix ."])]
    cmds = db_commands(spec)
    if cmds:
        targets += [("db-up", "Start Postgres", [cmds["up"]]), ("db-down", "Stop Postgres", [cmds["down"]]),
                    ("db-shell", "Open psql", [cmds["shell"]]), ("db-logs", "Follow Postgres logs", [cmds["logs"]])]
    names = ["help"] + [n for n, _, _ in targets]
    out = [".DEFAULT_GOAL := help", "BIN := .venv/bin", f".PHONY: {' '.join(names)}", "",
           "help: ## Show available commands",
           f"{t}@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | "
           "awk 'BEGIN {FS = \":.*?## \"}; {printf \"  \\033[36m%-18s\\033[0m %s\\n\", $$1, $$2}'", ""]
    for name, desc, lines in targets:
        out.append(f"{name}: ## {desc}")
        out += [f"{t}{line}" for line in lines]
        out.append("")
    return "\n".join(out)


# Tool config only: dependencies live in requirements*.txt and the app is never pip-installed
# (pythonpath = "." lets tests import it).
PYPROJECT_TOOLS = '''\
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra"

[tool.coverage.run]
source = ["app"]
'''

PYPROJECT_RUFF = '''
[tool.ruff]
line-length = 100
target-version = "{{TARGET}}"  # no [project].requires-python for ruff to read it from

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]
exclude = ["alembic/versions/*"]  # generated migrations: formatted, not linted

[tool.ruff.lint.isort]
known-third-party = ["alembic"]
'''

# .pytest_cache/, .ruff_cache/ and htmlcov/ ship their own .gitignore (so does .venv on 3.13+).
GITIGNORE = '''\
__pycache__/
.venv/
.coverage

# Local env vars (commit .env.example instead)
.env
'''


def gen_readme(spec: ProjectSpec) -> str:
    db = spec.db
    cmds = db_commands(spec)
    start = {"docker": f"docker start {db.container}",
             "compose": "docker compose up -d db",
             "existing": f"# make sure Postgres at {db.host}:{db.port} is running",
             "none": "# set DATABASE_URL in .env first"}[db.mode]
    parts = [f"# {spec.app_title}", "",
             "FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL.", "",
             "## Quick start", "", "Every command in this README assumes the virtualenv is active.", "",
             "```bash", "source .venv/bin/activate", start, "alembic upgrade head",
             "fastapi dev app/main.py    # http://127.0.0.1:8000/docs", "```", "",
             "**First time on a new machine:**", "", "```bash",
             f"python{spec.python_version} -m venv .venv",
             ".venv/bin/pip install -r requirements-dev.txt",
             "cp .env.example .env"]
    if db.mode == "docker":
        c = docker_run_cmd(db)
        run_lines = [shell_join(c[:5])] + [shell_join(c[i:i + 2]) for i in range(5, 15, 2)] + [shlex.quote(c[15])]
        parts += [" \\\n  ".join(run_lines),
                  f'docker exec {db.container} psql -U {shlex.quote(db.user)} -c "CREATE DATABASE {db.test_name};"']
    elif db.mode == "compose":
        parts += ["docker compose up -d db",
                  f'docker compose exec db psql -U {shlex.quote(db.user)} -c "CREATE DATABASE {db.test_name};"']
    parts += ["```", "", "## Database", "",
              "Connection URLs live in `.env` (`DATABASE_URL`, `TEST_DATABASE_URL`)."]
    if cmds:
        parts += ["", "```bash", f"{cmds['up']}", f"{cmds['down']}", f"{cmds['shell']}", f"{cmds['logs']}", "```"]
    parts += ["", "## Tests", "",
              "Unit tests (`tests/unit/`) mock the repository and need no database. Integration tests "
              "(`tests/integration/`) hit the API against the test database; each test runs in a "
              "transaction that is rolled back. They skip themselves if the database is unreachable.", "",
              "```bash", "pytest                                 # everything",
              "pytest tests/unit                      # unit only",
              "pytest tests/integration               # integration only",
              "pytest -k duplicate -x                 # by name, stop at first failure",
              "pytest --cov --cov-report=term-missing # coverage", "```", "",
              "## Migrations", "", "```bash",
              'alembic revision --autogenerate -m "describe change"   # after editing models',
              "alembic upgrade head", "alembic downgrade -1", "alembic history", "```", "",
              "Always read autogenerated migrations before applying them.", ""]
    if spec.makefile:
        parts += ["## Make targets", "", "Run `make` to list them: `make dev`, `make test`, `make migrate`, "
                  '`make migration m="..."`' + (", `make db-up`" if cmds else "") + (", `make format`" if spec.ruff else "")
                  + ".", ""]
    tree = ["app/", "├── main.py               # the app: routers + error handlers",
            "├── core/                 # config (.env), database session, exceptions",
            "├── models/               # SQLAlchemy tables", "├── schemas/              # Pydantic request/response models",
            "├── repositories/         # DB queries (CRUD)", "├── services/             # business rules",
            "└── api/", "    ├── deps.py           # session → repository → service wiring",
            "    └── routes/           # endpoints",
            "tests/", "├── unit/                 # no DB, mocks", "└── integration/          # real test DB through HTTP"]
    parts += ["## Structure", "", "```", *tree, "```", "",
              "Request flow: route → service → repository → model/DB, with schemas at the edges.", "",
              "## Adding an entity", "",
              "1. `app/models/<name>.py`, then import it in `app/models/__init__.py`",
              "2. `app/schemas/<name>.py` (Create / Update / Read)",
              "3. `app/repositories/<name>.py` and `app/services/<name>.py`",
              "4. Dependency in `app/api/deps.py`, routes in `app/api/routes/`, include the router in `app/main.py`",
              '5. `alembic revision --autogenerate -m "create <table> table"` → review → `alembic upgrade head`',
              "6. Tests in `tests/unit/` and `tests/integration/`", ""]
    return "\n".join(parts)


def project_files(spec: ProjectSpec) -> dict[str, str]:
    prefix = spec.api_prefix
    target = "py" + spec.python_version.replace(".", "")
    files: dict[str, str] = {
        "app/__init__.py": "", "app/core/__init__.py": "", "app/schemas/__init__.py": "",
        "app/repositories/__init__.py": "", "app/services/__init__.py": "", "app/api/__init__.py": "",
        "app/api/routes/__init__.py": "", "tests/__init__.py": "", "tests/unit/__init__.py": "",
        "tests/integration/__init__.py": "",
        "app/core/config.py": CONFIG_PY,
        "app/core/database.py": DATABASE_PY,
        "app/core/exceptions.py": EXCEPTIONS_PY,
        "app/models/base.py": BASE_PY,
        "app/models/__init__.py": gen_models_init(spec.entities),
        "app/api/deps.py": gen_deps(spec.entities),
        "app/main.py": gen_main(spec),
        "tests/integration/conftest.py": INTEGRATION_CONFTEST_PY,
        "tests/unit/test_health.py": HEALTH_TEST_PY,
        ".env": gen_env(spec),
        ".env.example": gen_env(spec, example=True),
        ".gitignore": GITIGNORE,
        "pyproject.toml": PYPROJECT_TOOLS + (render(PYPROJECT_RUFF, TARGET=target) if spec.ruff else ""),
        "README.md": gen_readme(spec),
    }
    for e in spec.entities:
        files[f"app/models/{e.snake}.py"] = gen_model(e)
        files[f"app/schemas/{e.snake}.py"] = gen_schema(e)
        files[f"app/repositories/{e.snake}.py"] = gen_repository(e)
        files[f"app/services/{e.snake}.py"] = gen_service(e)
        files[f"app/api/routes/{e.plural}.py"] = gen_routes(e)
        files[f"tests/unit/test_{e.snake}_service.py"] = gen_unit_service_test(e)
        schema_test = gen_unit_schema_test(e)
        if schema_test:
            files[f"tests/unit/test_{e.snake}_schemas.py"] = schema_test
        files[f"tests/integration/test_{e.plural}_api.py"] = gen_integration_test(e, prefix)
    if spec.db.mode == "compose":
        files["docker-compose.yml"] = gen_compose(spec.db)
    if spec.makefile:
        files["Makefile"] = gen_makefile(spec)
    return files


# ─────────────────────────────────────────────────────────────────────────────
# Running things
# ─────────────────────────────────────────────────────────────────────────────


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, display: list[str] | None = None,
        timeout: int | None = 900, env: dict | None = None) -> subprocess.CompletedProcess:
    print(f"  {dim('$ ' + shell_join(display or cmd))}")
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        proc = subprocess.CompletedProcess(cmd, 1, "", str(exc))
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-15:])
        if tail:
            print(textwrap.indent(tail, "    "))
        if check:
            raise StepFailed(f"Command failed: {shell_join(display or cmd)}")
    return proc


DB_HELPER = r'''
import json, os, sys, time
import psycopg
from psycopg import sql

cfg = json.loads(os.environ["SCAFFOLD_DB"])
params = dict(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], connect_timeout=3)
result = {"connected": False, "error": None, "databases": {}}
fatal_markers = ("authentication failed", "no password supplied", "role \"")
deadline = time.monotonic() + cfg["timeout"]
admin = None
while admin is None:
    fatal = False
    for maintenance_db in ("postgres", "template1"):
        try:
            admin = psycopg.connect(dbname=maintenance_db, autocommit=True, **params)
            break
        except psycopg.OperationalError as exc:
            text = str(exc).strip()
            result["error"] = text.splitlines()[0].removeprefix("connection failed: ") if text else repr(exc)
            lowered = text.lower()
            if 'database "postgres" does not exist' in lowered:
                continue
            fatal = any(marker in lowered for marker in fatal_markers)
            break
    if admin is None:
        if fatal or time.monotonic() > deadline:
            print(json.dumps(result))
            sys.exit(0)
        time.sleep(1)

result["connected"] = True
result["error"] = None
with admin:
    for name in cfg["names"]:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None
        created = False
        if not exists and cfg["create"]:
            try:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                exists = created = True
            except psycopg.Error as exc:
                result["error"] = f"Could not create {name}: {str(exc).strip()}"
        tables = None
        if exists:
            with psycopg.connect(dbname=name, **params) as conn:
                tables = conn.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
                ).fetchone()[0]
        result["databases"][name] = {"exists": exists, "created": created, "tables": tables}
print(json.dumps(result))
'''


def prepare_database(spec: ProjectSpec, root: Path, todo: list[str]) -> dict | None:
    db = spec.db
    if db.mode == "none":
        todo.append("Point DATABASE_URL (and TEST_DATABASE_URL) in .env at a Postgres database, then run "
                    "`alembic revision --autogenerate -m \"initial schema\"` and `alembic upgrade head`.")
        return None

    if db.mode in ("docker", "compose"):
        status = docker_status()
        if status != "ok":
            warn("Docker isn't available, so the container wasn't started.")
            start_cmd = (db_commands(spec)["up"] if db.reuse_container or db.mode == "compose"
                         else shell_join(docker_run_cmd(db)))
            todo.append(f"Start Docker, run `{start_cmd}`, create the test database (see README), then "
                        "`alembic upgrade head`.")
            return None
        try:
            if db.mode == "docker" and db.reuse_container:
                if container_state(db.container) != "running":
                    run(["docker", "start", db.container])
            elif db.mode == "docker":
                note("(the first run may take a minute while Docker pulls the image)")
                run(docker_run_cmd(db), display=docker_run_cmd(db, mask=True))
            else:
                compose = compose_command()
                if not compose:
                    raise StepFailed("Neither `docker compose` nor `docker-compose` is available.")
                note("(the first run may take a minute while Docker pulls the image)")
                run([*compose, "up", "-d", "db"], cwd=root)
        except StepFailed as exc:
            warn(str(exc))
            todo.append("Start the Postgres container (see README), then `alembic upgrade head`.")
            return None

    create = db.mode != "existing" or db.create_databases
    payload = {"host": "127.0.0.1" if db.host == "localhost" else db.host, "port": db.port,
               "user": db.user, "password": db.password, "names": [db.name, db.test_name],
               "create": create, "timeout": 90 if db.mode in ("docker", "compose") else 10}
    print(f"  {dim('… waiting for Postgres and preparing databases')}")
    proc = run([venv_python(root), "-c", DB_HELPER], cwd=root, check=False,
               display=["python", "-c", "<db check>"],
               env={**os.environ, "SCAFFOLD_DB": json.dumps(payload)})
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        warn("Couldn't check the database.")
        todo.append("Check the database connection in .env, then `alembic upgrade head`.")
        return None
    if not result["connected"]:
        warn(f"Couldn't connect to Postgres: {result['error']}")
        todo.append("Fix the database connection in .env (or start Postgres).")
        return None
    for name, info in result["databases"].items():
        if info["created"]:
            ok(f"Created database {name}")
        elif info["exists"]:
            ok(f"Database {name} exists")
        else:
            warn(f"Database {name} doesn't exist" + (f" ({result['error']})" if result["error"] else ""))
            todo.append(f"Create database `{name}`.")
    return result


def create_project(spec: ProjectSpec) -> None:
    root = spec.path
    todo: list[str] = []
    results: dict[str, str] = {}

    section("Creating project")
    root.mkdir(parents=True, exist_ok=True)
    run([*find_python(spec.python_version), "-m", "venv", ".venv"], cwd=root)
    py = venv_python(root)
    pip_install = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    deps = ["alembic", "fastapi[standard]", "psycopg[binary]", "pydantic-settings", "sqlalchemy"]
    dev = ["httpx2", "pytest", "pytest-cov"] + (["ruff"] if spec.ruff else [])
    if run([*pip_install, *deps, *dev], cwd=root, check=False).returncode != 0:
        dev.remove("httpx2")  # TestClient falls back to httpx, which fastapi[standard] installs
        run([*pip_install, *deps, *dev], cwd=root)
    ok("Virtualenv (.venv) created and dependencies installed")

    # Floor each requirement at the version just installed, e.g. fastapi[standard]>=0.142.2
    names = [r.split("[")[0] for r in deps + dev]
    code = "import sys, importlib.metadata as m; print(*(m.version(n) for n in sys.argv[1:]))"
    installed = run([py, "-c", code, *names], display=["python", "-c", "<installed versions>"]).stdout.split()
    version = dict(zip(names, installed, strict=True))

    def requirements(reqs: list[str]) -> str:
        return "".join(f"{r}>={version[r.split('[')[0]]}\n" for r in reqs)

    files = project_files(spec) | {"requirements.txt": requirements(deps),
                                   "requirements-dev.txt": "-r requirements.txt\n" + requirements(dev)}
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    ok("Code, tests, .env and config written")

    run([py, "-m", "alembic", "init", "alembic"], cwd=root)
    (root / "alembic" / "env.py").write_text(ALEMBIC_ENV_PY, encoding="utf-8")
    (root / "alembic.ini").write_text(ALEMBIC_INI, encoding="utf-8")
    (root / "alembic" / "README").unlink(missing_ok=True)
    ok("Alembic configured")

    section("Database")
    db_result = prepare_database(spec, root, todo)
    dbs = (db_result or {}).get("databases", {})
    dev_info = dbs.get(spec.db.name, {})
    db_ready = bool(dev_info.get("exists"))

    if spec.run_migrations and db_ready:
        # Autogenerate diffs the models against a database, so it needs an empty one. A reused container,
        # or a Docker volume that outlived its container, can leave the dev DB holding an earlier run's
        # tables; then the test DB stands in (the test suite creates and drops its own tables).
        empty = next((n for n in (spec.db.name, spec.db.test_name)
                      if dbs.get(n, {}).get("exists") and not dbs[n].get("tables")), None)
        message = f"create {spec.entities[0].plural} table" if len(spec.entities) == 1 else "initial schema"
        env = {**os.environ, "DATABASE_URL": spec.db.url(empty)} if empty else None
        if empty is None:
            warn(f"{spec.db.name} and {spec.db.test_name} both already have tables; skipped autogenerate so "
                 "nothing unrelated gets dropped.")
            todo.append("Create the first migration yourself and review it carefully: "
                        f"`alembic revision --autogenerate -m \"{message}\"`.")
        elif run([py, "-m", "alembic", "revision", "--autogenerate", "-m", message], cwd=root, check=False,
                 env=env).returncode != 0:
            todo.append(f"Fix the error above, then `alembic revision --autogenerate -m \"{message}\"`.")
        elif dev_info.get("tables"):
            # autogenerate left an empty alembic_version in the test DB; drop it so the DB stays empty
            run([py, "-c", "import os, sqlalchemy as sa; c = sa.create_engine(os.environ['DATABASE_URL']).connect(); "
                 "c.execute(sa.text('DROP TABLE IF EXISTS alembic_version')); c.commit()"],
                cwd=root, check=False, env=env, display=["python", "-c", "<drop alembic_version>"])
            warn(f"First migration created (diffed against {empty}) but not applied: {spec.db.name} already "
                 f"has {dev_info['tables']} table(s), probably left over from an earlier run.")
            todo.append(f"Drop and recreate {spec.db.name}, then `alembic upgrade head`.")
        elif run([py, "-m", "alembic", "upgrade", "head"], cwd=root, check=False).returncode == 0:
            ok("First migration created and applied")
            results["Migrations"] = "applied"
        else:
            todo.append("Fix the migration error above, then `alembic upgrade head`.")
    elif spec.run_migrations:
        todo.append('Create and apply the first migration: `alembic revision --autogenerate -m '
                    '"initial schema"` then `alembic upgrade head`.')

    if spec.ruff:
        section("Formatting")
        run([py, "-m", "ruff", "check", "--fix", "--quiet", "."], cwd=root, check=False)
        run([py, "-m", "ruff", "format", "--quiet", "."], cwd=root, check=False)
        lint = run([py, "-m", "ruff", "check", "--quiet", "."], cwd=root, check=False)
        if lint.returncode == 0:
            ok("Ruff: clean")
        else:
            warn("Ruff reported issues (see above).")

    if spec.run_tests:
        section("Tests")
        test_db_ok = bool((db_result or {}).get("databases", {}).get(spec.db.test_name, {}).get("exists"))
        cmd = [py, "-m", "pytest", "-q"] + ([] if test_db_ok else ["tests/unit"])
        proc = run(cmd, cwd=root, check=False)
        summary_line = next((line for line in reversed(proc.stdout.strip().splitlines()) if line.strip()), "")
        summary_line = summary_line.strip("= ").strip()
        if proc.returncode == 0:
            ok(f"Tests: {summary_line}" + ("" if test_db_ok else " (unit only - no test database)"))
            results["Tests"] = summary_line
        else:
            warn(f"Tests failed: {summary_line}")
            todo.append("Look at the failing tests: `pytest -x -vv`.")
            results["Tests"] = f"FAILED ({summary_line})"

    if spec.git_commit:
        section("Git")
        run(["git", "init", "-q"], cwd=root, check=False)
        run(["git", "add", "-A"], cwd=root, check=False)
        commit = run(["git", "commit", "-q", "-m", "Initial project scaffold"], cwd=root, check=False)
        if commit.returncode == 0:
            ok("Initial commit created")
        else:
            warn("Couldn't commit (is git user.name / user.email set?). Files are staged.")

    final_report(spec, results, todo, db_ready)


def final_report(spec: ProjectSpec, results: dict[str, str], todo: list[str], db_ready: bool) -> None:
    section("Done")
    root = spec.path
    ok(bold(f"Project created at {root}"))
    print()
    print(f"  cd {shlex.quote(str(root))}")
    print("  .venv\\Scripts\\activate" if os.name == "nt" else "  source .venv/bin/activate")
    print("  fastapi dev app/main.py" + ("   # or: make dev" if spec.makefile else ""))
    print(f"  {dim('→ http://127.0.0.1:8000/docs')}")
    print()
    db = spec.db
    if db.mode != "none":
        print(f"  {bold('Database')}  {db.url(db.name, masked=True)}")
        if db.mode in ("docker", "compose"):
            print(f"  {bold('Container')} {db.container}  (user {db.user} / password {db.password})")
    for key, value in results.items():
        print(f"  {bold(key.ljust(9))} {value}")
    print(f"  {bold('Endpoints')} " + ", ".join(f"{spec.api_prefix}/{e.kebab_plural}" for e in spec.entities))
    if todo:
        print()
        warn(bold("Still to do:"))
        for item in todo:
            print(f"   • {item}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def load_answers(path: str) -> dict:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    mapping = {"mode": "db_mode", "container": "db_container", "image": "db_image", "port": "db_port",
               "user": "db_user", "password": "db_password", "name": "db_name", "test_name": "db_test_name",
               "host": "db_host", "create_databases": "db_create", "reuse_container": "db_reuse_container"}
    flat = {k: v for k, v in data.items() if k != "db"}
    for k, v in (data.get("db") or {}).items():  # also accept a nested "db" object
        if k in mapping:
            flat.setdefault(mapping[k], v)
    return flat


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a FastAPI + SQLAlchemy + Postgres project.")
    parser.add_argument("--answers", help="JSON file with pre-filled answers")
    parser.add_argument("--yes", "-y", action="store_true", help="accept defaults for anything not in --answers")
    args = parser.parse_args()

    print(bold("\nFastAPI project generator") + dim("  ·  FastAPI + SQLAlchemy + Alembic + Postgres + pytest"))
    print(dim("Press Enter to accept the [default]. Ctrl+C to quit.\n"))

    try:
        preset = load_answers(args.answers) if args.answers else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(red(f"Couldn't read answers file: {exc}"))
        return 1

    prompter = Prompter(preset, assume_defaults=args.yes)
    try:
        spec = interview(prompter)
        print_summary(spec)
        print()
        if not prompter.confirm(None, "Create the project now?", True):
            print("Nothing was created.")
            return 0
        create_project(spec)
    except KeyboardInterrupt:
        print("\n" + yellow("Aborted."))
        return 130
    except Abort as exc:
        print(red(f"\n{exc}"))
        return 1
    except StepFailed as exc:
        print(red(f"\n{exc}"))
        print(dim("The partially created project was left in place so you can inspect it."))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
