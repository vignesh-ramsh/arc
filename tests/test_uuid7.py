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
        created." Spaced 2ms apart here so each call lands in a different
        millisecond and the cross-boundary guarantee is what's actually
        under test; TestOrderWithinAMillisecond covers ids made inside
        one millisecond."""
        ids = []
        for _ in range(5):
            ids.append(generate())
            time.sleep(0.002)
        assert ids == sorted(ids)


class TestOrderWithinAMillisecond:
    """The 12 bits after the version hold the elapsed part of the millisecond
    (RFC 9562 section 6.2, method 3), the same as pgdb's arc_uuid_generate_v7()
    writes on PostgreSQL 14 to 17, so ids made one after another sort in the
    order they were made."""

    @staticmethod
    def _made_at(monkeypatch, ms: int, into_ms_ns: int) -> uuid.UUID:
        monkeypatch.setattr(time, "time_ns", lambda: ms * 1_000_000 + into_ms_ns)
        return generate()

    @pytest.mark.parametrize(
        ("into_ms_ns", "expected_fraction"),
        [
            (0, 0),  # the first instant of the millisecond
            (244, 0),  # still inside the first step: 1_000_000 / 4096 = 244.14 ns per step
            (245, 1),  # the first nanosecond of the second step
            (500_000, 2048),  # halfway through
            (999_999, 4095),  # the last nanosecond
        ],
    )
    def test_the_fraction_of_the_millisecond_goes_in_the_12_bits_after_the_version(
        self, monkeypatch, into_ms_ns, expected_fraction
    ):
        u = self._made_at(monkeypatch, 1_700_000_000_000, into_ms_ns)
        assert (u.bytes[6] & 0x0F) << 8 | u.bytes[7] == expected_fraction
        # the fraction must not disturb the version, the variant or the timestamp
        assert u.version == 7
        assert u.variant == uuid.RFC_4122
        assert int.from_bytes(u.bytes[:6], "big") == 1_700_000_000_000

    def test_a_later_instant_sorts_after_an_earlier_one_in_the_same_millisecond(self, monkeypatch):
        earlier = self._made_at(monkeypatch, 1_700_000_000_000, 100_000)
        later = self._made_at(monkeypatch, 1_700_000_000_000, 900_000)
        assert earlier < later

    def test_the_millisecond_still_outranks_the_fraction(self, monkeypatch):
        late_in_one_ms = self._made_at(monkeypatch, 1_700_000_000_000, 999_999)
        early_in_the_next = self._made_at(monkeypatch, 1_700_000_000_001, 0)
        assert late_in_one_ms < early_in_the_next

    def test_the_remaining_bits_are_still_random(self, monkeypatch):
        # at one frozen instant everything but the random tail is identical
        assert self._made_at(monkeypatch, 1_700_000_000_000, 5) != self._made_at(monkeypatch, 1_700_000_000_000, 5)

    def test_ids_made_back_to_back_come_out_in_creation_order(self):
        ids = [generate() for _ in range(20_000)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_the_timestamp_still_reads_to_the_millisecond(self, monkeypatch):
        u = self._made_at(monkeypatch, 1_700_000_000_000, 987_654)
        assert extract_timestamp(u).timestamp() == 1_700_000_000.0  # the whole millisecond, not the fraction


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
