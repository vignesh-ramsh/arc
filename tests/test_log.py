"""arc.log's console-only advisory filter (_ExcludeAdvisoryFilter) — added
alongside Kernel.advise() now double-logging (warnings.warn() AND
logging.getLogger("arc.advisory")): without this filter, every advisory
would reprint on the console a second time (once per line, once per
Granian worker process) now that it also flows through logging."""

from __future__ import annotations

import logging

import pytest

from arc.log import _ExcludeAdvisoryFilter, _FileOnlyFilter, category_for


class TestExcludeAdvisoryFilter:
    def test_an_arc_advisory_record_is_filtered_out(self):
        record = logging.LogRecord("arc.advisory", logging.INFO, __file__, 1, "msg", (), None)
        assert _ExcludeAdvisoryFilter().filter(record) is False

    def test_any_other_logger_name_passes_through(self):
        record = logging.LogRecord("gateway", logging.INFO, __file__, 1, "msg", (), None)
        assert _ExcludeAdvisoryFilter().filter(record) is True


class TestCategoryForAdvisory:
    def test_arc_advisory_falls_into_system_category(self):
        # Not called out in _CATEGORY_PREFIXES — same catch-all bucket as
        # the kernel's own other loggers, so advisories still land in
        # logs/system.jsonl even though the console never shows them.
        assert category_for("arc.advisory") == "system"


class TestFileOnlyLoggers:
    """arc.log.file_only(): a chatty plugin logger (gateway's per-request access
    line) can be kept off the console — where every line costs a pipe write and
    a pass through `arc run`'s single relay thread — while still reaching its
    logs/<category>.jsonl. Nothing here may leak between tests, so each one
    starts and ends with an empty registry."""

    @staticmethod
    def _record(name: str) -> logging.LogRecord:
        return logging.LogRecord(name, logging.INFO, __file__, 1, "msg", (), None)

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from arc import log

        log._file_only_loggers.clear()
        yield
        log._file_only_loggers.clear()

    def test_with_nothing_registered_everything_reaches_the_console(self):
        assert _FileOnlyFilter().filter(self._record("gateway.access")) is True

    def test_a_registered_logger_is_kept_off_the_console(self):
        from arc import log

        log.file_only("gateway.access")
        assert _FileOnlyFilter().filter(self._record("gateway.access")) is False

    def test_its_children_are_kept_off_too(self):
        from arc import log

        log.file_only("gateway.access")
        assert _FileOnlyFilter().filter(self._record("gateway.access.sampled")) is False

    def test_siblings_parents_and_lookalike_names_are_unaffected(self):
        from arc import log

        log.file_only("gateway.access")
        for name in ("gateway", "gateway.middleware", "gateway.accessory", "relay.access"):
            assert _FileOnlyFilter().filter(self._record(name)) is True, name

    def test_console_too_undoes_it(self):
        from arc import log

        log.file_only("gateway.access")
        log.console_too("gateway.access")
        assert _FileOnlyFilter().filter(self._record("gateway.access")) is True

    def test_registering_twice_and_undoing_something_never_registered_are_harmless(self):
        from arc import log

        log.file_only("gateway.access")
        log.file_only("gateway.access")
        log.console_too("never.registered")
        assert _FileOnlyFilter().filter(self._record("gateway.access")) is False

    def test_a_file_only_record_still_lands_in_its_category_file(self, tmp_path):
        """The point of "file only": the file router has no such filter."""
        import json

        from arc import log

        log.file_only("gateway.access")
        record = self._record("gateway.access")
        router = log._CategoryRouter(tmp_path, max_bytes=1_000_000, backup_count=1)
        router.emit(record)
        router.close()

        assert _FileOnlyFilter().filter(record) is False  # not on the console...
        line = json.loads((tmp_path / "gateway.jsonl").read_text().splitlines()[0])  # ...but in the file
        assert line["logger"] == "gateway.access"

    def test_the_access_logger_is_filed_under_the_gateway_category(self):
        assert category_for("gateway.access") == "gateway"
