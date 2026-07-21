from __future__ import annotations

import re

from collector.qqnt.schema_probe import MessageTableCandidate, SchemaProbeReport
from collector.qqnt.versions.fallback import FallbackSQLiteAdapter


class QQNT99xAdapter(FallbackSQLiteAdapter):
    """Heuristic basic-field adapter; protobuf/media parsing remains Q8."""

    name = "qqnt-9.9.x-basic"
    _TABLE_PATTERN = re.compile(r"(?:^|_)(?:group|c2c|private)?_?msg(?:_|$)", re.I)

    def candidates(self, report: SchemaProbeReport) -> tuple[MessageTableCandidate, ...]:
        return tuple(candidate for candidate in report.message_candidates if self._TABLE_PATTERN.search(candidate.table))

    def supports(self, report: SchemaProbeReport) -> bool:
        return bool(self.candidates(report))
