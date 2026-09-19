"""
Domain models and dataclasses for Network License Monitor (Stage 1).
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any
import hashlib


class ServerStatus(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    VENDOR_DOWN = "VENDOR_DOWN"
    UNKNOWN = "UNKNOWN"


class FeatureType(str, Enum):
    PACKAGE = "PACKAGE"
    COMPONENT = "COMPONENT"
    STANDALONE = "STANDALONE"
    UNKNOWN = "UNKNOWN"


class CatalogStatus(str, Enum):
    VERIFIED = "VERIFIED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ExecutionResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass
class ServerInfo:
    hostname: str
    port: int
    status: str
    lmgrd_version: Optional[str] = None
    adskflex_status: Optional[str] = None
    adskflex_version: Optional[str] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PackageInfo:
    feature_code: str
    total_issued: int
    in_use: int
    available: int
    utilization_pct: float
    product_family: Optional[str] = None
    product_name: Optional[str] = None
    catalog_status: str = CatalogStatus.UNKNOWN.value
    components: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FeatureInfo:
    feature_code: str
    total_issued: int
    in_use: int
    available: int
    feature_type: str
    product_family: Optional[str] = None
    product_name: Optional[str] = None
    year_version: Optional[str] = None
    catalog_status: str = CatalogStatus.UNKNOWN.value
    parent_package: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RawConsumer:
    username: str
    machine_name: str
    display: str
    feature_code: str
    selected_component: str
    version: str
    server_handle: str
    checkout_time_raw: str
    pid: str
    is_borrowed: bool = False


@dataclass
class PhysicalCheckout:
    checkout_id: str
    server_hostname: str
    username: str
    machine_name: str
    display: str
    package_feature: str
    selected_component: str
    version: str
    server_handle: str
    checkout_time: str
    checkout_time_precision: str = "MINUTE"
    pid: str = ""
    is_borrowed: bool = False
    is_incomplete: bool = False
    anomaly_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def generate_id(
        server_hostname: str,
        username: str,
        machine_name: str,
        selected_component_code: str,
        checkout_time_minute_str: str,
        pid: str,
    ) -> str:
        """
        Computes deterministic SHA256 checkout ID according to Rule 7:
        SHA256(server_hostname + username + machine_name + selected_component_code + checkout_time_minute + pid)
        All fields normalized (lowercased/trimmed, component code uppercase/trimmed).
        """
        norm_server = server_hostname.strip().lower()
        norm_user = username.strip().lower()
        norm_machine = machine_name.strip().lower()
        norm_comp = selected_component_code.strip().upper()
        norm_time = checkout_time_minute_str.strip()
        norm_pid = pid.strip()

        preimage = f"{norm_server}:{norm_user}:{norm_machine}:{norm_comp}:{norm_time}:{norm_pid}"
        return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    schema_version: str
    monitor_version: str
    captured_at: str
    server: ServerInfo
    packages: List[PackageInfo] = field(default_factory=list)
    features: List[FeatureInfo] = field(default_factory=list)
    checkouts: List[PhysicalCheckout] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "monitor_version": self.monitor_version,
            "captured_at": self.captured_at,
            "server": self.server.to_dict(),
            "packages": [p.to_dict() for p in self.packages],
            "features": [f.to_dict() for f in self.features],
            "checkouts": [c.to_dict() for c in self.checkouts],
            "anomalies": self.anomalies,
        }
