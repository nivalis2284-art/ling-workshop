"""The single reader/writer for the `settings` key/value table.

Every feature used to repeat "query, json.loads, validate, fall back to a
default" in its own copy. These cases pin the one behaviour those copies were
supposed to share, including the failure modes a hand-edited or stale row can
produce. A stored value is untrusted input: a reader must answer with a usable
value and never raise.
"""

from __future__ import annotations

from lingnexus.configuration.settings import clear, read_choice, read_int, read_json, write_json


def raw_write(app, key: str, text: str) -> None:
    """Store a literal string, bypassing write_json — simulates hand edits."""
    with app.database.transaction() as db:
        db.execute(
            "INSERT INTO settings VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, text),
        )


# --- read_json ------------------------------------------------------------


def test_missing_key_returns_the_default(app):
    assert read_json(app.database, "nope", "fallback") == "fallback"


def test_a_stored_value_is_returned_as_parsed_json(app):
    write_json(app.database, "k", {"a": [1, 2]})
    assert read_json(app.database, "k", None) == {"a": [1, 2]}


def test_unparseable_text_falls_back_instead_of_raising(app):
    raw_write(app, "k", "{not json")
    assert read_json(app.database, "k", "fallback") == "fallback"


def test_a_stored_null_is_a_value_not_a_fallback(app):
    # The row exists and holds valid JSON `null`; that is the stored value.
    raw_write(app, "k", "null")
    assert read_json(app.database, "k", "fallback") is None


# --- read_int -------------------------------------------------------------


def test_read_int_returns_a_stored_integer(app):
    write_json(app.database, "k", 7)
    assert read_int(app.database, "k", 40) == 7


def test_read_int_rejects_bools(app):
    # `True` is an `int` in Python; accepting it would turn a stray flag into
    # a step budget of 1. It must fall back.
    write_json(app.database, "k", True)
    assert read_int(app.database, "k", 40) == 40


def test_read_int_rejects_non_integers(app):
    write_json(app.database, "k", 1.5)
    assert read_int(app.database, "k", 40) == 40
    write_json(app.database, "k", "7")
    assert read_int(app.database, "k", 40) == 40


def test_read_int_enforces_bounds(app):
    write_json(app.database, "k", 0)
    assert read_int(app.database, "k", 40, minimum=1) == 40
    write_json(app.database, "k", 1000)
    assert read_int(app.database, "k", 40, maximum=100) == 40
    # A value exactly on the bound is accepted.
    write_json(app.database, "k", 1)
    assert read_int(app.database, "k", 40, minimum=1, maximum=100) == 1


# --- read_choice ----------------------------------------------------------


def test_read_choice_accepts_a_known_value(app):
    write_json(app.database, "k", "container")
    assert read_choice(app.database, "k", "local", {"local", "container"}) == "container"


def test_read_choice_falls_back_on_an_unknown_value(app):
    write_json(app.database, "k", "nonsense")
    assert read_choice(app.database, "k", "local", {"local", "container"}) == "local"


# --- write_json / clear ---------------------------------------------------


def test_write_json_overwrites_and_survives_a_round_trip(app):
    write_json(app.database, "k", "first")
    write_json(app.database, "k", "second")
    assert read_json(app.database, "k", None) == "second"


def test_write_json_keeps_text_readable(app):
    # ensure_ascii=False: a Chinese rule must not be stored as \uXXXX escapes.
    write_json(app.database, "k", ["历史是证据"])
    with app.database.transaction() as db:
        stored = db.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0]
    assert "历史是证据" in stored


def test_clear_restores_the_default(app):
    write_json(app.database, "k", "set")
    clear(app.database, "k")
    assert read_json(app.database, "k", "default") == "default"


def test_clear_on_a_missing_key_is_harmless(app):
    clear(app.database, "absent")
    assert read_json(app.database, "absent", "default") == "default"
