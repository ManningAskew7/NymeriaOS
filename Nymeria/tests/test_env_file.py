"""Unit tests for the shared dotenv merge-writer (`nymeria/config/env_file.py`).

This is the one primitive that both the offline `nymeria init` finalize and the
online `PATCH /settings` handler write through, so its formatting, merge, and
atomic-0600 guarantees are covered here once rather than in each caller.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from dotenv import dotenv_values

from nymeria.config.env_file import (
    env_line_key,
    format_env_value,
    is_env_key_name,
    merge_env_lines,
    parse_env_value,
    write_env_file,
    EnvValueError,
)


def test_format_env_value_bools_and_none():
    assert format_env_value(True) == "true"
    assert format_env_value(False) == "false"
    assert format_env_value(None) == ""
    assert format_env_value("") == ""


def test_format_env_value_plain_and_base64_unquoted():
    assert format_env_value("gpt-5.5") == "gpt-5.5"
    assert format_env_value("claude-opus-4-8") == "claude-opus-4-8"
    # Fernet keys are base64url and end with `=`; `-`, `_`, `=` are all in the
    # safe set, so the key must write unquoted (compose env_file quoting is
    # version-fragile and we never want to exercise it for a secret).
    fernet = "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u="
    assert format_env_value(fernet) == fernet


def test_format_env_value_quotes_special_chars():
    assert format_env_value("model with space") == '"model with space"'
    assert format_env_value('he said "hi"') == '"he said \\"hi\\""'
    assert format_env_value("a\\b") == '"a\\\\b"'


def test_parse_env_value_inverts_format():
    # parse(format(x)) == x for the cases format produces: plain, base64, and the
    # quoted/escaped special-char path. This is the property _sync_updated_env_vars
    # relies on so a hot-reloaded value never carries literal quotes.
    for value in (
        "gpt-5.5",
        "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=",
        "model with space",
        'he said "hi"',
        "a\\b",
        "trailing space ",
        '{"k": "v", "n": 1}',
    ):
        assert parse_env_value(format_env_value(value)) == value


def test_parse_env_value_leaves_unquoted_and_empty():
    assert parse_env_value("gpt-5.5") == "gpt-5.5"
    assert parse_env_value("") == ""


def test_merge_env_lines_overlays_and_preserves_order():
    existing = ["# a comment", "", "KEEP=untouched", "CHANGE=old"]
    out = merge_env_lines(existing, [("CHANGE", "new"), ("ADD", "fresh")])
    assert out == [
        "# a comment",
        "",
        "KEEP=untouched",
        "CHANGE=new",
        "ADD=fresh",
    ]


def test_merge_env_lines_tolerates_spaced_lines_and_empty_existing():
    # A leading-whitespace / `KEY = value` line still matches on the stripped key
    # and is normalized to `KEY=value`.
    assert merge_env_lines(["  CHANGE = old  "], [("CHANGE", "new")]) == ["CHANGE=new"]
    # Empty existing means every produced key is appended in order.
    assert merge_env_lines([], [("A", "1"), ("B", "2")]) == ["A=1", "B=2"]


def test_merge_env_lines_collapses_duplicate_keys_to_one_line(caplog):
    # #301. Every reader of these files takes the LAST occurrence of a key
    # (python-dotenv, the pydantic dotenv source, run.py's boot load, the
    # restart re-merge), so preserving a later duplicate leaves the writer and
    # every reader disagreeing about which line is live: the write reports
    # success, the process serves the new value, and the next restart silently
    # reverts. Exactly one line survives, at the first occurrence, and unrelated
    # lines between the duplicates are untouched.
    existing = ["DUP=secret-one", "# a comment between them", "KEEP=me", "DUP=secret-two"]
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        out = merge_env_lines(existing, [("DUP", "x")])
    assert out == ["DUP=x", "# a comment between them", "KEEP=me"]
    # Removing a line from a file the user owns is announced rather than silent,
    # and named by KEY only: the line removed may hold a secret.
    assert "DUP" in caplog.text
    assert "secret-one" not in caplog.text
    assert "secret-two" not in caplog.text


def test_merge_env_lines_collapses_however_many_duplicates_there_are(caplog):
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        assert merge_env_lines(["K=1", "K=2", "K=3"], [("K", "final")]) == ["K=final"]
    # One warning per key, carrying the count, not one per removed line.
    assert len(caplog.records) == 1
    assert "2" in caplog.text


def test_merge_env_lines_without_duplicates_says_nothing(caplog):
    # The collapse is the exceptional path: an ordinary write must not log.
    with caplog.at_level(logging.WARNING, logger="nymeria.config.env_file"):
        out = merge_env_lines(["K=old", "OTHER=1"], [("K", "new")])
    assert out == ["K=new", "OTHER=1"]
    assert caplog.records == []


def test_merge_env_lines_drop_removes_retired_keys():
    # A drop key's existing line is removed instead of preserved; comments,
    # blanks, and other keys are untouched.
    existing = ["# keep", "A=1", "RETIRED=x:y", "B=2"]
    out = merge_env_lines(existing, [("A", "9")], drop=["RETIRED"])
    assert out == ["# keep", "A=9", "B=2"]
    # Dropping a key that is not in the file is a no-op.
    assert merge_env_lines(["X=1"], [], drop=["RETIRED"]) == ["X=1"]


def test_merge_env_lines_produced_wins_over_drop():
    # A key in both produced and drop is written, so callers can pass a static
    # drop list and let produced membership decide retire-vs-update.
    out = merge_env_lines(["K=old"], [("K", "new")], drop=["K"])
    assert out == ["K=new"]
    # Drop also removes duplicate occurrences of a retired key.
    out = merge_env_lines(["GONE=1", "GONE=2"], [], drop=["GONE"])
    assert out == []


def test_write_env_file_fresh_writes_header_and_0600(tmp_path: Path):
    path = tmp_path / "config.env"
    lines = write_env_file(
        path, [("A", "1"), ("B", "2")], merge=False, header="# header"
    )
    assert lines == ["# header", "A=1", "B=2"]
    assert path.read_text(encoding="utf-8") == "# header\nA=1\nB=2\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_preserves_untouched(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("# keep\nKEEP=yes\nCHANGE=old\n", encoding="utf-8")
    lines = write_env_file(path, [("CHANGE", "new")], merge=True)
    text = path.read_text(encoding="utf-8")
    assert "# keep" in text
    assert "KEEP=yes" in text
    assert "CHANGE=new" in text
    assert "CHANGE=old" not in text
    # untouched line + the single changed line, no duplicate appended
    assert lines == ["# keep", "KEEP=yes", "CHANGE=new"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_missing_file_appends_all(tmp_path: Path):
    path = tmp_path / "config.env"
    lines = write_env_file(path, [("A", "1")], merge=True)
    assert lines == ["A=1"]
    assert path.read_text(encoding="utf-8") == "A=1\n"


def test_write_env_file_merge_refuses_a_file_it_cannot_read(tmp_path: Path, monkeypatch):
    """Only a MISSING file merges as empty. Any other read failure (permissions,
    a transient I/O error) raises before the write: treating it as empty would
    rewrite the file with only the produced keys, silently dropping the vault
    key and every saved credential it held."""
    path = tmp_path / "settings.env"
    original = "NYMERIA_SECRETS_KEY=kept\nOPENAI_API_KEY=kept\n"
    path.write_text(original, encoding="utf-8")
    real_read_text = Path.read_text

    def unreadable(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(PermissionError):
        write_env_file(path, [("LLM_MODEL", "new")], merge=True)
    with pytest.raises(PermissionError):
        write_env_file(path, [], merge=True, drop=["OPENAI_API_KEY"])

    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["settings.env"]  # no temp left


@pytest.mark.parametrize(
    "line, key",
    [
        ("OPENAI_API_KEY=x", "OPENAI_API_KEY"),
        ("  LLM_MODEL = y", "LLM_MODEL"),
        ("EMPTY=", "EMPTY"),
        ("export OPENAI_API_KEY=x", "export OPENAI_API_KEY"),  # not a key name
        ("# OPENAI_API_KEY=x", None),
        ("   ", None),
        ("NO_EQUALS", None),
    ],
)
def test_env_line_key_reads_lines_as_the_merge_does(line: str, key: str | None):
    # #435: the removal's "can the writer drop this line" check and the
    # writer share this parse; an export line names no droppable key.
    assert env_line_key(line) == key
    if key is not None and is_env_key_name(key):
        assert merge_env_lines([line], [], drop=[key]) == []
    elif key is not None:
        assert merge_env_lines([line], [], drop=[key.split()[-1]]) == [line]


@pytest.mark.parametrize(
    "name, ok",
    [
        ("OPENAI_API_KEY", True),
        ("A", True),
        ("S3_ACCESS_KEY_ID", True),
        ("openai_api_key", False),
        ("1BAD", False),
        ("_LEADING", False),
        ("HAS SPACE", False),
        ("SK-PASTED-VALUE", False),
        ("KEY\n", False),
        ("", False),
    ],
)
def test_is_env_key_name_is_the_whole_name_shape(name: str, ok: bool):
    assert is_env_key_name(name) is ok


def test_write_env_file_merge_reads_back_as_written_over_a_duplicated_key(
    tmp_path: Path,
):
    # The property the whole of #301 exists for, asserted through the real
    # reader rather than the line list: after a merge write, a dotenv parse of
    # the file returns what the writer intended, whatever shape the file was in.
    # Before the collapse this file read back as "old", so `PATCH /settings`
    # reported success, served "new" until the next restart, and then reverted.
    path = tmp_path / ".env"
    path.write_text("LLM_MODEL=old\n# note\nLLM_MODEL=old\n", encoding="utf-8")

    write_env_file(path, [("LLM_MODEL", "new")], merge=True)

    assert dotenv_values(path) == {"LLM_MODEL": "new"}
    assert path.read_text(encoding="utf-8") == "LLM_MODEL=new\n# note\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_file_merge_collapses_a_duplicated_vault_key(tmp_path: Path):
    # The highest-consequence instance of #301, and the one the item missed:
    # `snapshot restore --write-env` writes the credential-vault Fernet key
    # through this same writer (`core/snapshot.py`). Landing it on a dead
    # duplicate line meant the restore reported success and the restored vault
    # could not be decrypted, because every reader still saw the OLD key.
    fernet = "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u="
    path = tmp_path / "config.env"
    path.write_text(
        "NYMERIA_SECRETS_KEY=stale-key-one\n"
        "# left over from a hand edit\n"
        "NYMERIA_SECRETS_KEY=stale-key-two\n",
        encoding="utf-8",
    )

    write_env_file(
        path, [("NYMERIA_SECRETS_KEY", format_env_value(fernet))], merge=True
    )

    assert dotenv_values(path) == {"NYMERIA_SECRETS_KEY": fernet}
    # The base64url key must also survive unquoted, the other property this
    # file already pins for the Fernet case.
    assert f"NYMERIA_SECRETS_KEY={fernet}" in path.read_text(encoding="utf-8")


# -- #156: one physical line per key, whatever the value holds --------------
#
# The writer's invariant: every value it emits is ONE physical line that
# python-dotenv (the settings loader) and docker compose decode back to the
# exact original. Line breaks are escaped inside double quotes; the few
# characters the writer's merge and the repo's one-line readers split on
# (both parsers read them literally) are refused with EnvValueError before
# anything is written. The real reader is the oracle throughout, never
# `parse_env_value` alone. Not asserted here, deliberately:
# `$word` under compose and `${NAME}` anywhere (both interpolate; pre-existing
# and out of this slice, backlog follow-up), so the corpus holds no `${`.

# Every character `str.splitlines` (the merge's line split) breaks on.
_LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

_ROUND_TRIP_CORPUS = [
    "You are a bot.\nAPI_PORT=6666\nEVIL_KEY=pwned",  # the reproduced injection
    "a\r\nb",  # CRLF: the CR is kept, not normalized
    "a\rb",  # lone CR
    "\n",  # a value that is only a newline
    "a\vb\fc",  # vertical tab and form feed
    'he said "hi"',
    "'single'",
    '"already quoted"',
    "C:\\new\\path",  # backslashes, with a backslash-n lookalike inside
    "literal \\n",  # the two characters backslash and n, not a newline
    "\\t \\r \\v \\f \\a \\b \\' \\\\ end",  # every escape spelling as literal text
    'a\\"b',  # a backslash directly before a quote
    "pa#ss word",
    "a #not-a-comment",
    "trailing ",
    " leading",
    "a\tb",
    '{"k": "v", "n": [1, 2]}',
    "caf\u00e9 \u2713 \U0001f642",
    "a=b=c",
    "pa$word",
    "bell\a back\b esc\x1b del\x7f",
    "C:\\models\\",  # trailing backslash that needs no quotes
    "\\",
    "\\\\server\\share\\",
    "gpt-5.5",
    "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u=",
]


def _dotenv_read(rhs: str) -> str | None:
    return dotenv_values(stream=io.StringIO(f"K={rhs}\n"))["K"]


@pytest.mark.parametrize("value", _ROUND_TRIP_CORPUS)
def test_every_value_is_one_line_the_reader_decodes_exactly(value: str):
    formatted = format_env_value(value)

    assert not any(ch in _LINE_BREAKS for ch in formatted)
    assert _dotenv_read(formatted) == value
    # The live export (`_sync_updated_env_vars`) must equal what every later
    # reload reads, so the inverse agrees with the real reader too.
    assert parse_env_value(formatted) == value


def test_line_breaks_get_the_escapes_both_parsers_decode():
    assert format_env_value("\n") == '"\\n"'
    assert format_env_value("a\r\nb") == '"a\\r\\nb"'
    assert format_env_value("a\vb\fc") == '"a\\vb\\fc"'
    # Escape order: the backslash is escaped first, so a path's `\n` is two
    # literal characters, never a newline, and a literal backslash-n stays one.
    assert format_env_value("C:\\new\\path") == '"C:\\\\new\\\\path"'
    assert format_env_value("literal \\n") == '"literal \\\\n"'
    assert format_env_value('he said "hi"') == '"he said \\"hi\\""'
    assert format_env_value('"already quoted"') == '"\\"already quoted\\""'


def _legacy_format(text: str) -> str:
    """The formatter before #156, for values holding no line break."""
    if all(c.isalnum() or c in "/._:-=" for c in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@pytest.mark.parametrize(
    "value",
    [
        v
        for v in _ROUND_TRIP_CORPUS
        if not any(ch in "\n\r\v\f" for ch in v) and not v.endswith("\\")
    ]
    + ["a:b:c", "web_search_tavily:web_fetch", "/data", "https://x.example/v1"],
)
def test_values_without_line_breaks_format_exactly_as_before(value: str):
    # Every file written before this change reads identically, and a rewrite
    # of an existing value is byte-identical: only line breaks gained escapes.
    assert format_env_value(value) == _legacy_format(value)


@pytest.mark.parametrize(
    "raw",
    [r'"\n"', r'"\r\t\v\f\a\b"', r'"\\n"', r'"\'x\'"', r'"a\qb"', r'"\"in\""', r'"plain"'],
)
def test_parse_env_value_decodes_exactly_what_python_dotenv_decodes(raw: str):
    # python-dotenv's double-quote set (`\\ \' \" \a \b \f \n \r \t \v`),
    # left to right, an unknown escape kept literal backslash and all.
    assert parse_env_value(raw) == _dotenv_read(raw)


@pytest.mark.parametrize("value", ["a\rb", "a\r\nb", "a\vb", "a\fb", "x\ny"])
def test_a_value_survives_an_unrelated_merge_write(tmp_path: Path, value: str):
    # The merge reads with universal newlines and splits on the whole
    # `str.splitlines` set, so a raw CR used to come back as LF and a raw VT
    # or FF split the value across lines at the next write of ANY key.
    path = tmp_path / ".env"
    write_env_file(
        path, [("PROMPT", format_env_value(value)), ("API_PORT", "8095")], merge=True
    )

    write_env_file(path, [("API_PORT", "8096")], merge=True)

    assert dotenv_values(path) == {"PROMPT": value, "API_PORT": "8096"}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_rewriting_a_multi_line_value_never_lands_its_lines_as_bindings(
    tmp_path: Path,
):
    # The delayed injection, at the writer: the second write of the same key
    # used to replace only the first physical line, leaving `API_PORT=6666` and
    # `EVIL_KEY=pwned"` behind as real bindings.
    path = tmp_path / ".env"
    path.write_text("API_PORT=8095\n", encoding="utf-8")
    payload = "You are a bot.\nAPI_PORT=6666\nEVIL_KEY=pwned"

    write_env_file(path, [("TWITCH_SYSTEM_PROMPT", format_env_value(payload))], merge=True)
    write_env_file(path, [("TWITCH_SYSTEM_PROMPT", format_env_value("short"))], merge=True)

    assert dotenv_values(path) == {"API_PORT": "8095", "TWITCH_SYSTEM_PROMPT": "short"}


def test_a_pem_private_key_round_trips_and_still_loads(tmp_path: Path):
    # The consumer (`tools/transform_utility_integrations.py`) hands the value
    # straight to load_pem_private_key, so it needs the real newlines back.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert pem.count("\n") >= 3
    path = tmp_path / ".env"

    write_env_file(
        path,
        [("CRYPTO_SIGN_PRIVATE_KEY", format_env_value(pem)), ("API_PORT", "8095")],
        merge=True,
    )

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    read_back = dotenv_values(path)["CRYPTO_SIGN_PRIVATE_KEY"]
    assert read_back == pem
    loaded = serialization.load_pem_private_key(read_back.encode(), password=None)
    assert isinstance(loaded, ec.EllipticCurvePrivateKey)
    assert loaded.private_numbers() == key.private_numbers()


def test_reading_and_rewriting_a_written_file_is_byte_identical(tmp_path: Path):
    # Idempotence through the real reader: what the reader returns, formatted
    # again, is exactly what is already on disk.
    path = tmp_path / ".env"
    pairs = [(f"K{i}", format_env_value(v)) for i, v in enumerate(_ROUND_TRIP_CORPUS)]
    write_env_file(path, pairs, merge=False, header="# it38")
    first = path.read_bytes()

    values = dotenv_values(path)
    write_env_file(
        path, [(k, format_env_value(v or "")) for k, v in values.items()], merge=True
    )

    assert values == {f"K{i}": v for i, v in enumerate(_ROUND_TRIP_CORPUS)}
    assert path.read_bytes() == first


@pytest.mark.parametrize(
    "char", ["\x00", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_characters_no_env_line_can_carry_are_refused(char: str):
    # NUL fits no environment variable; the other six are line breaks to
    # `str.splitlines`, so the merge and the repo's one-line readers would
    # take what follows as a binding (python-dotenv and compose read them
    # literally; the constraint is ours). Refused by name (the code point),
    # never by echoing the value, which may be a secret.
    value = f"sk-it38-secret{char}tail"

    with pytest.raises(EnvValueError) as exc_info:
        format_env_value(value)

    message = str(exc_info.value)
    assert isinstance(exc_info.value, ValueError)
    assert f"U+{ord(char):04X}" in message
    assert "sk-it38-secret" not in message
    assert "tail" not in message


@pytest.mark.parametrize(
    "value",
    ["C:\\it38 Program Files\\", "it38 b\\", 'it38"\\', "it38\nrows\\", "\\\\it38 \\"],
)
def test_a_trailing_backslash_inside_quotes_is_refused(value: str):
    # python-dotenv before 1.2.3 (the dev venv and the slim uv tool run
    # 1.2.2) reads `"...\\"` as an escaped quote and swallows every following
    # line up to the next quote in the file, the vault key included. Refused
    # with the way out named, and the value never echoed.
    with pytest.raises(EnvValueError) as exc_info:
        format_env_value(value)

    message = str(exc_info.value)
    assert "trailing backslash" in message
    assert "drop the trailing backslash" in message
    assert "it38" not in message


@pytest.mark.parametrize(
    "value", ["C:\\models\\", "C:\\", "\\", "a\\\\", "C:/x\\y\\", "\\\\server\\share\\"]
)
def test_a_trailing_backslash_that_needs_no_quotes_is_written_bare(
    tmp_path: Path, value: str
):
    # Unquoted, every reader takes a backslash literally (python-dotenv 1.2.2
    # and 1.2.3, compose env_file and --env-file interpolation; measured), so
    # a value of safe characters plus backslashes is written bare instead of
    # refused. A Windows drive root such as `C:\` is the case that matters.
    fernet = "k7Jn-3xQp9_aB2cD4eF6gH8iJ0kL2mN4oP6qR8sT0u="
    path = tmp_path / ".env"

    write_env_file(
        path,
        [
            ("MODEL_DIR", format_env_value(value)),
            ("NYMERIA_SECRETS_KEY", format_env_value(fernet)),
            ("OTHER", format_env_value("quoted value")),
        ],
        merge=True,
    )

    assert format_env_value(value) == value
    assert parse_env_value(format_env_value(value)) == value
    assert dotenv_values(path) == {
        "MODEL_DIR": value,
        "NYMERIA_SECRETS_KEY": fernet,
        "OTHER": "quoted value",
    }
