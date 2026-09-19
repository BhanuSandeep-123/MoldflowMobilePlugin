"""Non-destructive post-Analyze inspection stage.

The existing ``cad_diagnostics.py`` remains untouched. This engine only uses
its existing mesh-diagnostics function; it does not start an analysis, change
process settings, remesh, move gates, repair CAD, or invoke Fusion.
"""
from __future__ import annotations

import logging
import time
from typing import Any


class InspectionEngine:
    def __init__(self, compute_jobs, logger: logging.Logger, timeout_seconds: float = 45.0):
        self.compute_jobs = compute_jobs
        self.log = logger
        self.timeout_seconds = timeout_seconds

    def _active_study(self):
        """Attach to the already-running Synergy session without launching it."""
        import synergy_connect  # imported only after shared plugin path is configured

        syn = synergy_connect.get_synergy(allow_launch=False)
        study_doc = syn.StudyDoc()
        if study_doc is None:
            raise RuntimeError("No active Moldflow StudyDoc is available")
        return syn, study_doc

    @staticmethod
    def _study_name(study_doc) -> str:
        for accessor in ("StudyName", "Name", "DisplayName", "Path"):
            try:
                value = getattr(study_doc, accessor)
                value = value() if callable(value) else value
                if value:
                    return str(value)
            except Exception:
                continue
        return ""

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "UNKNOWN",
            "study_name": job.get("name") or "",
            "scm_job_id": job.get("scm_job_id") or job.get("job_id") or "",
            "started_at": time.time(),
            "checks": [],
        }

        # Always begin with an SCM-level check. This proves the post-Analyze
        # event was observed without touching Synergy.
        result["checks"].append(
            {
                "name": "analysis_job",
                "status": "FOUND",
                "scm_job_id": result["scm_job_id"],
                "study_name": result["study_name"],
                "job_type": job.get("scm_type") or job.get("job_type") or "",
            }
        )

        try:
            import cad_diagnostics  # reuse only the specific inspection function

            syn, study_doc = self._active_study()
            actual_name = self._study_name(study_doc)
            if actual_name:
                result["study_name"] = actual_name

            try:
                analysis_status = study_doc.AnalysisStatus(0)
                result["analysis_status"] = str(analysis_status or "")
            except Exception:
                result["analysis_status"] = ""

            # Prefer the existing ready-aware routine when available. It keeps
            # polling the actual Moldflow mesh statistics rather than sleeping a
            # guessed amount of time.
            if hasattr(cad_diagnostics, "collect_mesh_diagnostics_when_ready"):
                mesh_report = cad_diagnostics.collect_mesh_diagnostics_when_ready(
                    syn,
                    study_doc,
                    self.log.info,
                    timeout=self.timeout_seconds,
                    poll=2.0,
                )
            else:
                mesh_report = cad_diagnostics.collect_mesh_diagnostics(
                    syn,
                    study_doc,
                    self.log.info,
                )

            result["mesh_diagnostics"] = mesh_report
            result["checks"].append(
                {
                    "name": "mesh_diagnostics",
                    "status": mesh_report.get("status", "UNKNOWN"),
                    "total_elements": mesh_report.get("total_elements", 0),
                    "counts": mesh_report.get("counts", {}),
                }
            )
            result["status"] = mesh_report.get("status", "UNKNOWN")

        except Exception as exc:
            result["checks"].append(
                {
                    "name": "synergy_mesh_inspection",
                    "status": "UNAVAILABLE",
                    "message": str(exc),
                }
            )
            result["inspection_warning"] = str(exc)
            result["status"] = "UNAVAILABLE"
            self.log.warning("Inspection could not access Synergy: %s", exc)

        result["finished_at"] = time.time()
        return result
