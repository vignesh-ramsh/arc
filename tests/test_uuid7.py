"""arc.uuid7 — client-side generation/decoding of the same UUIDv7 scheme
pgdb's arc_uuid_generate_v7() stamps onto every table's `id` by default
(plugins/pgdb/pgdb/ddl.py). Byte-for-byte cross-compatibility against
a REAL Postgres-generated id is exercised in
tests/integration/test_uuid7_pg_compat.py, not here — this file is pure
unit-level, no DB, matching arc/tests's own "kernel tests never touch
Postgres" convention (conftest.py)."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

from arc.uuid7 import extract_timestamp, generate


class TestGenerate:
    def test_returns_a_real_version_7_uuid(self):
        u = generate()
        assert isinstance(u, uuid.UUID)
        assert u.version == 7

    def test_variant_bits_are_rfc_4122(self):
        u = generate()
        assert u.variant == uuid.RFC_4122

    def test_two_calls_never_collide(self):
        assert generate() != generate()

    def test_successive_values_sort_by_creation_time(self):
        """The whole point of UUIDv7 over UUIDv4 — plain string/int
        comparison already gives chronological order across millisecond
        boundaries, no separate timestamp column needed to sort by "when
        created." Only across boundaries, deliberately: this mirrors
        Postgres's own arc_uuid_generate_v7() (pgdb/ddl.py), which has
        no monotonic sub-millisecond counter — two ids generated within
        the SAME millisecond are ordered by their random bytes, not
        generation order, on both sides of this implementation alike.
        Spaced 2ms apart here so each call lands in a different
        millisecond and the cross-boundary guarantee is what's actually
        under test."""
        ids = []
        for _ in range(5):
            ids.append(generate())
            time.sleep(0.002)
        assert ids == sorted(ids)


class TestExtractTimestamp:
    def test_round_trips_a_freshly_generated_id(self):
        before = time.time()
        u = generate()
        after = time.time()
        ts = extract_timestamp(u)
        assert ts.tzinfo is timezone.utc
        # Millisecond-resolution embedded timestamp vs. wall-clock time
        # read just before/after generate() — real slack, not exact
        # equality, since the two clocks are read microseconds apart.
        assert before - 0.001 <= ts.timestamp() <= after + 0.001

    def test_accepts_a_string_form_too(self):
        u = generate()
        assert extract_timestamp(str(u)) == extract_timestamp(u)

    def test_rejects_a_version_4_uuid(self):
        with pytest.raises(ValueError, match="not version 7"):
            extract_timestamp(uuid.uuid4())

    def test_a_known_value_decodes_to_the_expected_instant(self):
        # 1700000000000 ms since epoch = 2023-11-14T22:13:20Z. Hand-built
        # rather than round-tripped through generate() — proves the byte
        # LAYOUT itself (which 6 bytes, which end is big-endian), not just
        # "whatever generate() happens to produce."
        ts_bytes = (1700000000000).to_bytes(6, "big")
        rest = bytes([0x70, 0x00, 0x80, 0, 0, 0, 0, 0, 0, 0])  # version/variant set, rest zeroed
        u = uuid.UUID(bytes=ts_bytes + rest)
        assert u.version == 7
        assert extract_timestamp(u) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
