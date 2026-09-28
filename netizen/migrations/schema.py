"""Structural validation shared by frozen migration contracts, using SQLite metadata."""

from __future__ import annotations

import re
import sqlite3


_TOKEN = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|\w+|[^\s]", re.DOTALL)


def _tokens(sql: str) -> tuple[str, ...]:
    # Ignore presentation whitespace and identifier quoting, but preserve string
    # literals: CHECK(value = 'A') and CHECK(value = 'a') are different contracts.
    return tuple(
        token if token.startswith("'") else token.strip('"`[]').lower()
        for token in _TOKEN.findall(sql)
        if not token.startswith(("--", "/*"))
    )


def _checks(sql: str) -> tuple[tuple[str, ...], ...]:
    tokens = _tokens(sql)
    checks = []
    for start, token in enumerate(tokens):
        if token != "check" or tokens[start + 1:start + 2] != ("(",):
            continue
        depth = 0
        for end in range(start + 1, len(tokens)):
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            if depth == 0:
                checks.append(tokens[start + 1:end + 1])
                break
    return tuple(sorted(checks))


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_shape(connection: sqlite3.Connection, name: str, sql: str) -> tuple:
    tokens = _tokens(sql)
    if any(tokens[index:index + 2] == ("on", "conflict") for index in range(len(tokens))):
        # PRAGMA metadata omits constraint conflict policies. Frozen schemas use
        # SQLite's implicit ABORT policy; reject explicit policies rather than
        # incorrectly treating REPLACE/IGNORE as equivalent. Supporting a future
        # schema with explicit policies requires a versioned validator extension.
        raise RuntimeError(f"Channel database has unsupported conflict policy: {name}")
    quoted = _quote(name)
    columns = sorted(tuple(row)[1:] for row in connection.execute(f"PRAGMA table_xinfo({quoted})"))
    foreign_keys = sorted(tuple(row)[1:] for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"))
    unique_indexes = []
    for index in connection.execute(f"PRAGMA index_list({quoted})"):
        if not index[2]:
            continue  # Compatible query indexes are not schema-version changes.
        index_name = index[1]
        columns_and_order = tuple(
            # Column position can differ after a supported table rebuild.
            (row[0], row[2], row[3], row[4], row[5])
            for row in connection.execute(f"PRAGMA index_xinfo({_quote(index_name)})")
        )
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (index_name,),
        ).fetchone()[0]
        unique_indexes.append((
            index[3], index[4], columns_and_order,
            _tokens(index_sql or "") if index[3] == "c" else (),
        ))
    return tuple(columns), tuple(foreign_keys), tuple(sorted(unique_indexes, key=repr)), _checks(sql)


def require_schema(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """Match a frozen schema without depending on CREATE text or column order.

    Unique constraints, checks and triggers are part of the version's contract;
    non-unique performance indexes may evolve without a schema migration.
    Explicit ON CONFLICT policies are outside this validator's supported shape.
    """
    reference = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            reference.execute(statement)
        objects = {}
        for source in (reference, connection):
            objects[source] = {
                (row[0], row[1]): row[2]
                for row in source.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'view', 'trigger') AND name NOT LIKE 'sqlite_%'"
                )
            }
        expected, actual = objects[reference], objects[connection]
        if expected.keys() != actual.keys():
            raise RuntimeError("Channel database schema objects do not match the declared version")
        for (kind, name), sql in expected.items():
            if kind == "table":
                matches = _table_shape(connection, name, actual[kind, name]) == _table_shape(reference, name, sql)
            else:
                matches = _tokens(actual[kind, name]) == _tokens(sql)
            if not matches:
                raise RuntimeError(f"Channel database has invalid {kind} structure: {name}")
    finally:
        reference.close()
