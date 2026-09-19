"""Standalone post-Analyze Moldflow agent.

New flow only; the existing Moldflow plugin is not modified.

    User manually prepares a Moldflow study
        -> user clicks Analyze
        -> Simulation Compute Manager receives the solve
        -> this agent detects the newly-created parent solve job
        -> inspection starts
        -> mobile backend receives INSPECTION/RUNNING state
        -> SCM is monitored until terminal state

The agent reuses the existing production plugin modules from the directory
configured by ``existing_plugin_path``. Nothing is copied into this project.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "existing_plugin_path": r"C:\Users\UnoTEAM-0144\Documents\MoldflowSynergyPlugin\MoldflowSynergyPlugin",
    "poll_interval_seconds": 3.0,
    "startup_baseline": True,
    "inspection_enabled": True,
    "analysis_started_status": "STARTED",
    "inspection_status": "INSPECTION",
    "inspection_timeout_seconds": 45,
    "only_local_jobs": True,
    "log_file": "standalone_agent.log",
}


def load_config() -> dict[str, Any]:
    """Load standalone configuration without hiding structural errors."""
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_PATH.exists():
        return cfg
    try:
        user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Could not parse {CONFIG_PATH}: {exc}") from exc
    if not isinstance(user_cfg, dict):
        raise RuntimeError(f"Expected a JSON object in {CONFIG_PATH}")
    cfg.update(user_cfg)
    return cfg


def configure_shared_plugin_path(config: dict[str, Any]) -> Path:
    """Add the existing production plugin directory to sys.path.

    This MUST happen before importing compute_jobs/mobile_reporter because
    those modules intentionally live in the old plugin project and are reused
    rather than duplicated.
    """
    configured = str(config.get("existing_plugin_path") or "").strip()
    if not configured:
        raise RuntimeError(
            "existing_plugin_path is not configured. Set it to the existing "
            "MoldflowSynergyPlugin directory containing compute_jobs.py."
        )

    plugin_root = Path(configured).expanduser()
    if not plugin_root.is_absolute():
        plugin_root = (HERE / plugin_root).resolve()
    else:
        plugin_root = plugin_root.resolve()

    if not plugin_root.is_dir():
        raise FileNotFoundError(
            f"Existing Moldflow plugin directory does not exist:\n{plugin_root}"
        )

    required = ("compute_jobs.py", "mobile_reporter.py")
    missing = [name for name in required if not (plugin_root / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Existing Moldflow plugin directory is missing required module(s): "
            + ", ".join(missing)
            + f"\nDirectory: {plugin_root}"
        )

    path_text = str(plugin_root)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
    return plugin_root


def import_shared_modules(plugin_root: Path):
    """Import the existing production modules only after sys.path is ready."""
    try:
        import compute_jobs  # type: ignore
        import mobile_reporter  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Could not import the existing Moldflow plugin module(s).\n"
            f"Plugin root: {plugin_root}\n"
            f"Python executable: {sys.executable}\n"
            f"Original error: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "The existing Moldflow plugin module failed during import.\n"
            f"Plugin root: {plugin_root}\n"
            f"Original error: {exc}"
        ) from exc
    return compute_jobs, mobile_reporter


class StandaloneAgent:
    def __init__(self) -> None:
        self.config = load_config()
        self.plugin_root = configure_shared_plugin_path(self.config)
        self.compute_jobs, self.mobile_reporter = import_shared_modules(self.plugin_root)

        self.log = self._configure_logging()

        from job_detector import JobDetector
        from inspection_engine import InspectionEngine

        self.detector = JobDetector(
            compute_jobs=self.compute_jobs,
            logger=self.log,
            only_local=bool(self.config.get("only_local_jobs", True)),
        )
        self.inspector = InspectionEngine(
            compute_jobs=self.compute_jobs,
            logger=self.log,
            timeout_seconds=float(self.config.get("inspection_timeout_seconds", 45)),
        )

        self.stop_requested = False
        self.active: dict[str, dict[str, Any]] = {}

    def _configure_logging(self) -> logging.Logger:
        log_path = Path(str(self.config.get("log_file") or "standalone_agent.log"))
        if not log_path.is_absolute():
            log_path = HERE / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("MoldflowStandaloneAgent")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Avoid duplicate handlers if an embedding process constructs the
        # agent more than once.
        if not logger.handlers:
            formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.addHandler(stream_handler)

        return logger

    def stop(self, *_args) -> None:
        self.stop_requested = True

    def _job_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload") or {}
        jid = str(row.get("jobID") or "")
        summary = self.compute_jobs.summarize(row) or {}
        scm_user = str(
            payload.get("user")
            or summary.get("scm_user")
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or ""
        ).strip()
        return {
            "job_id": jid,
            "scm_job_id": jid,
            "name": summary.get("name") or payload.get("name") or "",
            "type": payload.get("type") or summary.get("type") or "",
            "job_type": payload.get("type") or "",
            "status": summary.get("status") or "QUEUED",
            "percent": summary.get("percent", 0),
            "started": time.time(),
            "finished": bool(summary.get("finished", False)),
            "scm_type": summary.get("type") or payload.get("type") or "",
            "compute_source": "CLOUD" if summary.get("cloud") else "LOCAL",
            "scm_user": scm_user or None,
            "worker": summary.get("worker") or "",
        }

    def _report(
        self,
        job: dict[str, Any],
        status: str | None = None,
        finished: bool | None = None,
        error_message: str | None = None,
    ) -> bool:
        data = dict(job)
        if status is not None:
            data["status"] = status
        if finished is not None:
            data["finished"] = finished
        if error_message:
            data["error_message"] = error_message
        try:
            return bool(self.mobile_reporter.report_status(data, log=self.log.info))
        except Exception:
            self.log.exception("Mobile status reporting failed for %s", job.get("scm_job_id"))
            return False

    def _save_inspection(self, job_id: str, inspection: dict[str, Any]) -> None:
        out = HERE / "inspection_results"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job_id}.json"
        path.write_text(json.dumps(inspection, indent=2, default=str), encoding="utf-8")
        self.log.info("Inspection result saved: %s", path)

    def _start_job(self, row: dict[str, Any]) -> None:
        job = self._job_payload(row)
        jid = job["scm_job_id"]
        if not jid:
            self.log.warning("Ignoring SCM row without jobID.")
            return
        if jid in self.active:
            return

        self.active[jid] = job
        self.log.info(
            "Starting post-Analyze workflow: job=%s study=%s type=%s",
            jid,
            job.get("name"),
            job.get("job_type"),
        )

        # Explicitly publish the beginning of the user's Moldflow analysis
        # before publishing the post-Analyze inspection stage.  The existing
        # production mobile_reporter owns HTTP, throttling, auth and retries;
        # this standalone app only supplies the lifecycle states.
        started_status = str(
            self.config.get("analysis_started_status") or "STARTED"
        ).strip() or "STARTED"
        self._report(
            job,
            status=started_status,
            finished=False,
        )

        inspection_status = str(
            self.config.get("inspection_status") or "INSPECTION"
        ).strip() or "INSPECTION"
        self._report(
            job,
            status=inspection_status,
            finished=False,
        )

        if bool(self.config.get("inspection_enabled", True)):
            try:
                inspection = self.inspector.run(job)
                job["inspection"] = inspection
                self._save_inspection(jid, inspection)
                self.log.info(
                    "Inspection finished for %s: %s",
                    jid,
                    inspection.get("status", "UNKNOWN"),
                )
            except Exception as exc:
                self.log.exception("Inspection failed for %s", jid)
                job["inspection"] = {
                    "status": "ERROR",
                    "message": str(exc),
                }
                self._report(job, error_message=str(exc))
        else:
            self.log.info("Inspection is disabled by configuration for %s.", jid)

        # Refresh the actual SCM state immediately after inspection so the
        # mobile client transitions from INSPECTION to the real queue state.
        self._refresh_job_from_scm(job)

        # A job that finished while inspection was running should not remain in
        # the active set.
        if job.get("finished"):
            self.log.info("Job %s already reached terminal state: %s", jid, job.get("status"))
            self._report(job)
            self.active.pop(jid, None)

    def _refresh_job_from_scm(self, job: dict[str, Any]) -> None:
        jid = str(job.get("scm_job_id") or "")
        if not jid:
            return
        row = self.compute_jobs.get_job(jid)
        if row is None:
            return
        summary = self.compute_jobs.summarize(row)
        if not summary:
            return
        job.update(
            {
                "name": summary.get("name") or job.get("name"),
                "status": summary.get("status") or job.get("status"),
                "percent": summary.get("percent", job.get("percent", 0)),
                "finished": bool(summary.get("finished")),
                "worker": summary.get("worker") or job.get("worker"),
                "compute_source": "CLOUD" if summary.get("cloud") else "LOCAL",
                "scm_type": summary.get("type") or job.get("scm_type"),
                "scm_user": (
                    summary.get("scm_user")
                    or job.get("scm_user")
                    or os.environ.get("USERNAME")
                    or os.environ.get("USER")
                ),
            }
        )

    def _update_active(self) -> None:
        for jid, job in list(self.active.items()):
            try:
                if self.mobile_reporter.check_cancel(jid, log=self.log.info):
                    self.log.info("Mobile cancellation requested for %s", jid)
                    scm_jid = str(job.get("scm_job_id") or jid)
                    cancelled = False
                    if hasattr(self.compute_jobs, "cancel_job"):
                        try:
                            cancelled = self.compute_jobs.cancel_job(scm_jid)
                        except Exception as exc:
                            self.log.exception("SCM cancel_job failed for %s: %s", scm_jid, exc)
                    else:
                        cancelled = True

                    if not cancelled:
                        self.log.warning("SCM cancellation unverified for %s; not reporting CANCELED", scm_jid)
                        continue

                    job.update(
                        {
                            "status": "CANCELED",
                            "finished": True,
                            "error_message": "Cancellation requested from mobile app",
                        }
                    )
                    self._report(job, status="CANCELED", finished=True)
                    self.active.pop(jid, None)
                    continue

                row = self.compute_jobs.get_job(jid)
                if row is None or row.get("_scm_deleted"):
                    scm_healthy = False
                    try:
                        scm_healthy = bool(self.compute_jobs.available())
                    except Exception:
                        scm_healthy = False

                    if scm_healthy:
                        self.log.warning(
                            "SCM job %s no longer exists on healthy SCM daemon; treating as CANCELED",
                            jid,
                        )
                        job.update(
                            {
                                "status": "CANCELED",
                                "finished": True,
                                "error_message": "Job terminated or deleted in SCM Job Manager",
                            }
                        )
                        self._report(job, status="CANCELED", finished=True)
                        self.active.pop(jid, None)
                        continue
                    else:
                        continue

                summary = self.compute_jobs.summarize(row)
                if not summary:
                    continue

                job.update(
                    {
                        "name": summary.get("name") or job.get("name"),
                        "status": summary.get("status") or job.get("status"),
                        "percent": summary.get("percent", job.get("percent", 0)),
                        "finished": bool(summary.get("finished")),
                        "worker": summary.get("worker") or job.get("worker"),
                        "compute_source": "CLOUD" if summary.get("cloud") else "LOCAL",
                        "scm_type": summary.get("type") or job.get("scm_type"),
                        "scm_user": (
                            summary.get("scm_user")
                            or job.get("scm_user")
                            or os.environ.get("USERNAME")
                            or os.environ.get("USER")
                        ),
                    }
                )

                backend_cancel = self._report(job)
                if backend_cancel:
                    self.log.info("Backend requested cancellation for %s", jid)

                if job.get("finished"):
                    self.log.info(
                        "Terminal SCM state for %s: %s",
                        jid,
                        job.get("status"),
                    )
                    self.active.pop(jid, None)
            except Exception:
                self.log.exception("Failed to update SCM job %s", jid)

    def run(self) -> None:
        self.log.info("Standalone Moldflow post-Analyze agent starting.")
        self.log.info("Python: %s", sys.executable)
        self.log.info("Shared plugin: %s", self.plugin_root)
        self.log.info("Existing Moldflow plugin flow is untouched.")

        # SCM is the source of truth for the post-Analyze trigger.
        # Existing jobs are ignored at startup unless recovery is explicitly
        # requested by disabling startup_baseline.
        if bool(self.config.get("startup_baseline", True)):
            self.detector.initialize()
        else:
            self.log.warning("startup_baseline=false; visible existing jobs may be treated as new.")

        interval = max(1.0, float(self.config.get("poll_interval_seconds", 3.0)))

        while not self.stop_requested:
            try:
                if self.compute_jobs.available():
                    for row in self.detector.poll():
                        self._start_job(row)
                    self._update_active()
                else:
                    self.log.warning("SCM service unavailable; retrying.")
            except Exception:
                self.log.exception("Agent cycle failed; continuing.")
            time.sleep(interval)

        self.log.info("Standalone Moldflow agent stopped.")


def main() -> int:
    try:
        agent = StandaloneAgent()
    except Exception as exc:
        print("Standalone Moldflow agent could not start.", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, agent.stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, agent.stop)

    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop()
        agent.log.info("Stopped by keyboard interrupt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
