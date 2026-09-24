"""A deterministic, stdlib-only TOML *writer*.

``tomllib`` reads TOML but nothing in the stdlib writes it, and a runtime dependency is
disallowed (NFR-5/ADR-2). Harbor computes a content hash over the emitted ``task.toml``, and
NFR-1 requires two emits from one candidate to be byte-identical — a third-party writer does not
guarantee that across versions, so this module provides a small deterministic writer API
(:class:`TomlFloat`, :data:`TomlValue`, :func:`emit_document`) built from scratch with only the
standard library.

Determinism is the whole point: output order follows the caller's dict *insertion* order (no
``sorted``, no ``hash()``-ordered iteration), floats render with a pinned decimal point, and line
endings are always ``\\n`` regardless of platform. Building the actual ``task.toml`` value tree is
``harbor.py``'s job (D-2); this module only serializes one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

#: The value kinds a TOML document is built from. Recursive: arrays and tables nest. A caller
#: builds documents out of plain ``dict``/``list``; :class:`TomlFloat` marks a float whose
#: rendering must be pinned. Datetimes are passed as ISO strings (§8), not ``datetime`` objects.
type TomlValue = str | int | float | bool | TomlFloat | list[TomlValue] | dict[str, TomlValue]


@dataclass(frozen=True)
class TomlFloat:
    """Wraps a float so it always renders with a decimal point (``1800.0``, never ``1800``).

    Harbor's ``timeout_sec`` fields are floats; an integer-valued float emitted without its ``.0``
    fails Harbor's schema (§8, D-2's validators). A plain Python ``float`` in a document renders
    the same way — this wrapper exists so the *intent* ("this is a float") is explicit at the call
    site even when the value happens to be integral.
    """

    value: float


# A bare TOML key needs no quoting; anything else is written as a quoted basic string.
_BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Characters below the lowest printable code point, plus U+007F (DELETE), are control characters
# that a TOML basic string must escape; those without a named escape become ``\\uXXXX``.
_LOWEST_PRINTABLE_CODE_POINT = 0x20
_DELETE_CODE_POINT = 0x7F

# The named escapes TOML defines for basic strings; every other control character (and U+007F)
# is written as a ``\\uXXXX`` sequence.
_BASIC_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\f": "\\f",
    "\b": "\\b",
}


class TomlWriter:
    """Serializes a value tree into deterministic TOML bytes. Holds no state."""

    @classmethod
    def emit_document(cls, document: Mapping[str, TomlValue]) -> bytes:
        """Serialize a document to deterministic, LF-terminated, UTF-8 TOML bytes."""
        rendered_lines = cls.render_table(document, path=())
        spaced_lines = cls.insert_blank_lines_before_headers(rendered_lines)
        text = "\n".join(spaced_lines)
        if text:
            text += "\n"
        return text.encode("utf-8")

    @staticmethod
    def insert_blank_lines_before_headers(lines: list[str]) -> list[str]:
        """Put one blank line before each ``[table]``/``[[array]]`` header, for readability.

        A header is the only line that starts with ``[`` (bare keys start with a letter/digit,
        quoted keys with ``"``), so this stays unambiguous and deterministic.
        """
        spaced: list[str] = []
        for line in lines:
            if line.startswith("[") and spaced and spaced[-1] != "":
                spaced.append("")
            spaced.append(line)
        return spaced

    @classmethod
    def render_table(cls, table: Mapping[str, TomlValue], path: tuple[str, ...], *, is_array_element: bool = False) -> list[str]:
        """Render one table: its header (when needed), its scalar keys, then its sub-tables.

        TOML requires a table's scalar keys to precede any of its sub-table headers, so the two
        are emitted in separate passes; each pass preserves the caller's insertion order.
        """
        has_scalar = any(not cls.is_container(value) for value in table.values())
        has_container = any(cls.is_container(value) for value in table.values())
        lines: list[str] = []
        if cls.table_needs_header(path, has_scalar=has_scalar, has_container=has_container, is_array_element=is_array_element):
            lines.append(cls.render_header(path, is_array_element=is_array_element))
        lines.extend(cls.render_scalar_entries(table))
        lines.extend(cls.render_container_entries(table, path))
        return lines

    @classmethod
    def render_scalar_entries(cls, table: Mapping[str, TomlValue]) -> list[str]:
        """The ``key = value`` lines for every scalar (non-table, non-array-of-tables) entry."""
        return [
            f"{cls.render_key(key)} = {cls.render_value(value)}" for key, value in table.items() if not cls.is_container(value)
        ]

    @classmethod
    def render_container_entries(cls, table: Mapping[str, TomlValue], path: tuple[str, ...]) -> list[str]:
        """The header-bearing lines for every sub-table and array-of-tables entry, in order."""
        lines: list[str] = []
        for key, value in table.items():
            if isinstance(value, Mapping):
                lines.extend(cls.render_table(value, path=(*path, key)))
            elif cls.is_array_of_tables(value):
                for element in value:
                    lines.extend(cls.render_table(element, path=(*path, key), is_array_element=True))
        return lines

    @staticmethod
    def table_needs_header(path: tuple[str, ...], *, has_scalar: bool, has_container: bool, is_array_element: bool) -> bool:
        """Whether this table prints its own header line.

        An array-of-tables element always does. The root never does. A super-table that holds only
        sub-tables is collapsed (``[metadata.origin]`` rather than a bare ``[metadata]`` then
        ``[metadata.origin]``). An otherwise-empty table prints its header so it round-trips.
        """
        if is_array_element:
            return True
        if not path:
            return False
        if has_scalar:
            return True
        return not has_container

    @classmethod
    def render_header(cls, path: tuple[str, ...], *, is_array_element: bool) -> str:
        """The ``[a.b]`` (table) or ``[[a.b]]`` (array-of-tables) header for a dotted path."""
        dotted_path = ".".join(cls.render_key(segment) for segment in path)
        return f"[[{dotted_path}]]" if is_array_element else f"[{dotted_path}]"

    @classmethod
    def render_key(cls, key: str) -> str:
        """A bare key verbatim; anything with unsafe characters as a quoted basic string."""
        if _BARE_KEY_PATTERN.match(key):
            return key
        return cls.render_basic_string(key)

    @classmethod
    def render_value(cls, value: TomlValue) -> str:
        """Render a scalar or an inline array. Tables and arrays-of-tables never reach here."""
        # bool is a subclass of int, so it must be tested first or ``True`` renders as ``1``.
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, TomlFloat):
            return cls.render_float(value.value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return cls.render_float(value)
        if isinstance(value, str):
            return cls.render_basic_string(value)
        if isinstance(value, Sequence):
            return cls.render_inline_array(value)
        # A bare Mapping here means a table was placed where an inline value was expected; inline
        # tables are unsupported because nothing in task.toml (§8) needs them.
        raise TypeError(f"cannot render {type(value).__name__} as an inline TOML value")

    @classmethod
    def render_inline_array(cls, items: Sequence[TomlValue]) -> str:
        """An inline array of scalars, e.g. ``[1234]`` or ``["high", "medium"]``; ``[]`` when empty."""
        return "[" + ", ".join(cls.render_value(item) for item in items) + "]"

    @staticmethod
    def render_float(value: float) -> str:
        """Render a float so it always carries a decimal point and round-trips exactly.

        ``repr`` gives the shortest decimal that round-trips and is deterministic for a given
        value, but an integral float (``1800.0``) can come back without a point on some paths, so
        a ``.0`` is appended when neither a point nor an exponent is present.
        """
        text = repr(value)
        if "." not in text and "e" not in text and "E" not in text:
            text += ".0"
        return text

    @staticmethod
    def render_basic_string(text: str) -> str:
        """A TOML basic string: quoted, with backslashes/quotes/control characters escaped."""
        escaped = "".join(TomlWriter.escape_string_character(character) for character in text)
        return f'"{escaped}"'

    @staticmethod
    def escape_string_character(character: str) -> str:
        """Escape one character for a basic string; controls beyond the named set go to ``\\uXXXX``."""
        named_escape = _BASIC_STRING_ESCAPES.get(character)
        if named_escape is not None:
            return named_escape
        code_point = ord(character)
        if code_point < _LOWEST_PRINTABLE_CODE_POINT or code_point == _DELETE_CODE_POINT:
            return f"\\u{code_point:04X}"
        return character

    @staticmethod
    def is_container(value: TomlValue) -> bool:
        """True for a value that becomes a table or array-of-tables (not a scalar/inline array)."""
        return isinstance(value, Mapping) or TomlWriter.is_array_of_tables(value)

    @staticmethod
    def is_array_of_tables(value: TomlValue) -> TypeGuard[Sequence[Mapping[str, TomlValue]]]:
        """True for a non-empty sequence whose every element is a table (an array-of-tables)."""
        if isinstance(value, str) or not isinstance(value, Sequence):
            return False
        return len(value) > 0 and all(isinstance(element, Mapping) for element in value)


def emit_document(document: Mapping[str, TomlValue]) -> bytes:
    """Serialize a document to deterministic, LF-terminated, UTF-8 TOML bytes.

    Stdlib-only. Output order follows ``document``'s insertion order; the caller controls it.
    """
    return TomlWriter.emit_document(document)
