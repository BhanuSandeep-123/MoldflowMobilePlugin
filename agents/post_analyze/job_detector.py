"""Detect newly submitted parent solve jobs directly from SCM."""
from __future__ import annotations

from typing import Any


class JobDetector:
    """Stateful detector for new parent solve jobs.

    The detector receives the existing production ``compute_jobs`` module from
    the agent. It never imports that module before the shared plugin path has
    been configured.
    """

    def __init__(self, compute_jobs, logger, only_local: bool = True) -> None:
        self.compute_jobs = compute_jobs
        self.log = logger
        self.only_local = only_local
        self.known_ids: set[str] = set()
        self.processed_ids: set[str] = set()
        self.initialized = False

        # Reuse the production solve types, but add the parent type observed
        # on Mesh + Analysis submissions.  Child phase types remain excluded
        # because matching is exact (for example, study:warp3d:01).
        solve_types = getattr(compute_jobs, "SOLVE_TYPES", ("study", "mesh+study"))
        self.solve_types = {str(x).strip().lower() for x in solve_types}
        self.solve_types.update({"mesh+analysis", "analysis"})

    def initialize(self) -> None:
        self.known_ids = self._snapshot_ids()
        self.initialized = True
        self.log.info("SCM baseline captured: %d existing job(s).", len(self.known_ids))

    def _snapshot_ids(self) -> set[str]:
        try:
            rows = self.compute_jobs.list_jobs()
        except Exception as exc:
            self.log.warning("Could not read SCM queue during baseline: %s", exc)
            return set()

        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            jid = str(row.get("jobID") or "").strip()
            if jid:
                ids.add(jid)
        return ids

    @staticmethod
    def _is_parent_type(job_type: str, allowed: set[str]) -> bool:
        # Exact comparison is intentional: child phase types such as
        # study:warp3d:01 must never be mistaken for the parent "study" row.
        return job_type in allowed

    def _is_candidate(self, row: dict[str, Any]) -> bool:
        jid = str(row.get("jobID") or "").strip()
        if not jid or jid in self.known_ids or jid in self.processed_ids:
            return False

        payload = row.get("payload") or {}
        job_type = str(payload.get("type") or "").strip().lower()
        if not self._is_parent_type(job_type, self.solve_types):
            return False

        if self.only_local:
            # Different SCM builds expose this flag differently. Reject an
            # explicitly cloud job, but do not reject a record that omits it.
            if payload.get("cloud") is True:
                return False

        return True

    def poll(self) -> list[dict[str, Any]]:
        try:
            rows = self.compute_jobs.list_jobs()
        except Exception as exc:
            self.log.warning("SCM poll failed: %s", exc)
            return []

        if not isinstance(rows, list):
            return []

        candidates = [
            row for row in rows
            if isinstance(row, dict) and self._is_candidate(row)
        ]

        def sort_key(row: dict[str, Any]) -> int:
            try:
                return int(str(row.get("epoch") or "0"))
            except Exception:
                return 0

        candidates.sort(key=sort_key)

        # Advance the known set for every visible job, not only solve jobs.
        for row in rows:
            if not isinstance(row, dict):
                continue
            jid = str(row.get("jobID") or "").strip()
            if jid:
                self.known_ids.add(jid)

        for row in candidates:
            jid = str(row.get("jobID") or "").strip()
            if not jid:
                continue
            self.processed_ids.add(jid)
            payload = row.get("payload") or {}
            self.log.info(
                "NEW ANALYSIS DETECTED: job=%s study=%s type=%s",
                jid,
                payload.get("name"),
                payload.get("type"),
            )

        return candidates
