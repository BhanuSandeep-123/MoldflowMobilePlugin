"""lib.jobs.models
-----------------
Domain models for jobs and reporting in the Moldflow mobile system.

NOTE: This is a documentation/domain model in Phase 3. Existing callers
continue to use plain dictionaries. This module formalizes the data contracts
for future phases without altering current behavior.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class JobPhase:
    """Represents a discrete stage or step within an SCM job."""
    name: str
    status: str = "UNKNOWN"
    percent: float = 0.0
    details: Optional[str] = None


@dataclass
class JobStatus:
    """Canonical domain model representing the state of an SCM / Moldflow job.

    Captures fields used across SCM polling (compute_jobs/client.py),
    mobile reporting (mobile_reporter/reporter.py), and the monitor
    (standalone_job_monitor.py).
    """
    job_id: str
    scm_job_id: Optional[str] = None
    name: str = ""
    type: str = ""
    status: str = "PENDING"  # PENDING | INPROGRESS | COMPLETED | FAILED | CANCELED
    percent: int = 0         # 0-100
    started: Optional[float] = None  # epoch seconds
    finished: bool = False
    worker: Optional[str] = None
    cloud: bool = False
    compute_source: Optional[str] = None  # LOCAL | CLOUD
    scm_type: Optional[str] = None
    scm_user: Optional[str] = None
    parent_job_id: Optional[str] = None
    error_message: Optional[str] = None
    cancel_requested: bool = False
    phases: List[JobPhase] = field(default_factory=list)
    raw_data: Optional[Dict[str, Any]] = None
