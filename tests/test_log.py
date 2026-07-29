"""arc.log's console-only advisory filter (_ExcludeAdvisoryFilter) — added
alongside Kernel.advise() now double-logging (warnings.warn() AND
logging.getLogger("arc.advisory")): without this filter, every advisory
would reprint on the console a second time (once per line, once per
Granian worker process) now that it also flows through logging."""

from __future__ import annotations

import logging

from arc.log import _ExcludeAdvisoryFilter, category_for


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
