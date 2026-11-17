"""The SCRAM-SHA-256 verifier helper. Also proven against a real PostgreSQL by hand."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import re
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "scale" / "scram_verifier.py"
_spec = importlib.util.spec_from_file_location("scram_verifier", _PATH)
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)

FORMAT = re.compile(r"SCRAM-SHA-256\$(\d+):([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)")


def test_the_format_is_what_postgresql_accepts():
    m = FORMAT.fullmatch(sv.scram_verifier("pw"))
    assert m and m.group(1) == "4096"
    assert len(base64.b64decode(m.group(2))) == 16  # salt
    assert len(base64.b64decode(m.group(3))) == 32  # stored key
    assert len(base64.b64decode(m.group(4))) == 32  # server key


def test_the_keys_follow_the_scram_definition():
    salt = bytes(range(16))
    m = FORMAT.fullmatch(sv.scram_verifier("s3cret", salt=salt, iterations=100))
    salted = hashlib.pbkdf2_hmac("sha256", b"s3cret", salt, 100)
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert base64.b64decode(m.group(3)) == hashlib.sha256(client).digest()
    assert base64.b64decode(m.group(4)) == hmac.new(salted, b"Server Key", hashlib.sha256).digest()


def test_a_fixed_salt_is_repeatable_and_a_random_one_is_not():
    salt = b"\x01" * 16
    assert sv.scram_verifier("pw", salt=salt) == sv.scram_verifier("pw", salt=salt)
    assert sv.scram_verifier("pw") != sv.scram_verifier("pw")


def test_the_password_is_not_in_the_verifier():
    assert "hunter2-hunter2" not in sv.scram_verifier("hunter2-hunter2")


@pytest.mark.parametrize("password,iterations", [("", 4096), ("pw", 0), ("pw", -1)])
def test_bad_input_is_refused(password, iterations):
    with pytest.raises(ValueError):
        sv.scram_verifier(password, iterations=iterations)


def test_main_reads_standard_input(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    assert sv.main() == 0
    assert FORMAT.fullmatch(capsys.readouterr().out.strip())
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert sv.main() == 2
