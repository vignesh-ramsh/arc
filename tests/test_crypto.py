"""arc.crypto — hash()/verify_hash() are pure stdlib, no project needed;
encrypt()/decrypt() need a project only for the no-key default path
(this project's own .arc/arc.mkey). No Postgres involved anywhere here,
matching arc/tests's own "kernel tests never touch Postgres" convention
(conftest.py)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import arc
from arc.crypto import CryptoError, decrypt, encrypt, hash, verify_hash  # noqa: A001


class TestHash:
    def test_matches_the_existing_hashlib_sha256_call_sites_byte_for_byte(self):
        """Load-bearing: real rows already exist (_sessions.token_hash,
        _access_keys.key_hash) computed via
        hashlib.sha256(raw.encode()).hexdigest() — arc.hash() must
        reproduce that exact digest for a plain string, or adopting it
        silently invalidates every stored session/API key."""
        raw = "some-raw-token-value"
        assert hash(raw) == hashlib.sha256(raw.encode()).hexdigest()

    def test_bytes_input_is_hashed_directly_too(self):
        raw = b"some-raw-bytes"
        assert hash(raw) == hashlib.sha256(raw).hexdigest()

    def test_is_deterministic(self):
        assert hash("payload") == hash("payload")

    def test_different_payloads_hash_differently(self):
        assert hash("payload-a") != hash("payload-b")

    def test_non_string_payloads_are_supported_via_codec(self):
        # Not compared against hashlib directly (goes through arc.codec
        # first) — only the invariants a caller can actually rely on:
        # deterministic, and distinct inputs don't collide.
        assert hash({"a": 1, "b": 2}) == hash({"a": 1, "b": 2})
        assert hash([1, 2, 3]) != hash([3, 2, 1])
        assert hash(12345) != hash(12346)

    def test_returns_a_hex_string(self):
        digest = hash("anything")
        assert isinstance(digest, str)
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not valid hex


class TestVerifyHash:
    def test_true_for_matching_strings(self):
        assert verify_hash("abc123", "abc123") is True

    def test_false_for_mismatched_strings(self):
        assert verify_hash("abc123", "abc124") is False

    def test_works_against_real_hash_output(self):
        digest = hash("a-token")
        assert verify_hash(digest, hash("a-token")) is True
        assert verify_hash(digest, hash("a-different-token")) is False

    def test_accepts_mixed_str_and_bytes(self):
        assert verify_hash("abc123", b"abc123") is True
        assert verify_hash(b"abc123", "abc123") is True


class TestEncryptDecryptWithExplicitKey:
    def test_round_trips_a_string(self):
        token = encrypt("hello world", key="a passphrase")
        assert decrypt(token, key="a passphrase") == "hello world"

    def test_round_trips_a_dict(self):
        payload = {"user": "vignesh", "roles": ["admin", "editor"], "n": 3}
        token = encrypt(payload, key="a passphrase")
        assert decrypt(token, key="a passphrase") == payload

    def test_round_trips_a_list_and_a_number(self):
        assert decrypt(encrypt([1, 2, 3], key="k"), key="k") == [1, 2, 3]
        assert decrypt(encrypt(42, key="k"), key="k") == 42

    def test_ciphertext_is_not_the_plaintext(self):
        token = encrypt("secret value", key="k")
        assert "secret value" not in token

    def test_wrong_key_raises_crypto_error(self):
        token = encrypt("hello", key="right-key")
        with pytest.raises(CryptoError):
            decrypt(token, key="wrong-key")

    def test_accepts_bytes_key_too(self):
        token = encrypt("hello", key=b"raw-bytes-key")
        assert decrypt(token, key=b"raw-bytes-key") == "hello"

    def test_tampered_token_raises_crypto_error(self):
        token = encrypt("hello", key="k")
        tampered = token[:-4] + ("A" * 4 if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(CryptoError):
            decrypt(tampered, key="k")

    def test_different_keys_never_collide(self):
        token = encrypt("hello", key="key-one")
        with pytest.raises(CryptoError):
            decrypt(token, key="key-two")


class TestEncryptDecryptWithNoKey:
    def test_raises_crypto_error_with_no_project_bound(self):
        with pytest.raises(CryptoError, match="arc.boot"):
            encrypt("hello")

    def test_resolves_the_current_projects_master_key_once_booted(self, project: Path):
        arc.boot(project_root=project)
        token = encrypt("hello world")
        assert decrypt(token) == "hello world"

    def test_round_trips_non_string_payloads_too(self, project: Path):
        arc.boot(project_root=project)
        payload = {"a": [1, 2, 3], "b": None, "c": 4.5}
        assert decrypt(encrypt(payload)) == payload

    def test_derives_a_different_key_than_arc_secrets_own(self, project: Path):
        """The whole point of the HKDF context separation: a value
        encrypted with the project's default key must NOT be decryptable
        using arc.secrets' own Fernet key derived from the same root
        master key — they must be cryptographically independent."""
        from arc.secrets import _fernet_from_mkey
        from cryptography.fernet import InvalidToken

        arc.boot(project_root=project)
        token = encrypt("hello world")
        secrets_fernet = _fernet_from_mkey(project / ".arc" / "arc.mkey")
        with pytest.raises(InvalidToken):
            secrets_fernet.decrypt(token.encode())

    def test_reachable_via_the_arc_module_directly(self, project: Path):
        arc.boot(project_root=project)
        assert arc.decrypt(arc.encrypt("via arc.*")) == "via arc.*"
        assert arc.verify_hash(arc.hash("x"), arc.hash("x")) is True
