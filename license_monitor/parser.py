"""
Parser and normalizer for FlexNet lmstat output.
Implements Stage 1 parsing, feature classification, seat authority rules,
and checkout deduplication.
"""

import re
import datetime
from typing import Dict, List, Tuple, Optional, Any

from .models import (
    ServerStatus,
    FeatureType,
    CatalogStatus,
    ServerInfo,
    PackageInfo,
    FeatureInfo,
    RawConsumer,
    PhysicalCheckout,
    Snapshot,
    ExecutionResult,
)


class FlexNetParser:
    """
    Parser for Autodesk FlexNet `lmutil lmstat -a` output.
    Follows all frozen architecture rules:
    - Rule 2: PACKAGE is seat authority
    - Rule 3: Generic parser, no unverified assumptions
    - Rule 4: Unknown features preserved
    - Rule 5: Error diagnostics preserved (-15, -96)
    - Rule 6: UP, DOWN, VENDOR_DOWN, UNKNOWN states
    - Rule 7: Deterministic checkout ID
    - Rule 8: Server handle is diagnostic only
    - Rule 10: MINUTE precision for checkout times
    """

    # Regex patterns
    RE_FLEXNET_ERROR = re.compile(
        r"Error getting status:\s*(.*?)\s*\((-?\d+),.*?\)",
        re.IGNORECASE
    )
    RE_SERVER_STATUS = re.compile(
        r"^([A-Za-z0-9_.-]+):\s+license server\s+(UP|DOWN)(?:\s+\([^)]+\))?\s*(v[0-9.]+)?",
        re.MULTILINE | re.IGNORECASE
    )
    RE_VENDOR_STATUS = re.compile(
        r"^\s*(adskflex):\s+(UP|DOWN)\s*(v[0-9.]+)?",
        re.MULTILINE | re.IGNORECASE
    )
    RE_FEATURE_HEADER = re.compile(
        r"^Users of ([A-Za-z0-9_]+):\s+\(Total of (\d+)\s+licenses?\s+issued;\s+Total of (\d+)\s+licenses?\s+in use\)",
        re.MULTILINE
    )
    # Consumer line:
    # UnoTEAM-0144 LAPTOP-CA2QN87F LAPTOP-CA2QN87F 88232MFS_2027_0F (v1.000) (LAPTOP-CA2QN87F/27000 101), start Fri 9/18 11:23, PID: 24336
    RE_CONSUMER_LINE = re.compile(
        r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\(([^)]+)\)\s+\(([^)]+)\),\s+start\s+(.*?),\s+PID:\s*(\d+)",
        re.MULTILINE
    )

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.known_packages = self.config.get("known_packages", {})
        self.server_cfg = self.config.get("server", {})
        self.default_hostname = self.server_cfg.get("hostname", "UNKNOWN")
        self.default_port = self.server_cfg.get("port", 27000)

    def parse_execution_result(
        self,
        exec_result: ExecutionResult,
        captured_at: Optional[str] = None
    ) -> Snapshot:
        """
        Parses an ExecutionResult (exit code, stdout, stderr) into a normalized Snapshot.
        """
        if captured_at is None:
            captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        stdout = exec_result.stdout or ""
        stderr = exec_result.stderr or ""
        combined_text = f"{stdout}\n{stderr}".strip()

        # Check for errors and determine server status
        server_info = self._parse_server_status(combined_text, exec_result)

        snapshot = Snapshot(
            schema_version=self.config.get("schema_version", "1.0"),
            monitor_version=self.config.get("monitor_version", "1.0.0"),
            captured_at=captured_at,
            server=server_info,
            packages=[],
            features=[],
            checkouts=[],
            anomalies=[],
        )

        # If server is DOWN or UNKNOWN, return snapshot with error state
        if server_info.status in (ServerStatus.DOWN.value, ServerStatus.UNKNOWN.value):
            return snapshot

        # Parse feature blocks and consumers
        feature_blocks, raw_consumers = self._parse_features_and_consumers(combined_text)

        # Classify features and build PackageInfo and FeatureInfo lists
        packages, features, package_map = self._classify_features(feature_blocks)
        snapshot.packages = packages
        snapshot.features = features

        # Deduplicate consumers and generate PhysicalCheckouts
        checkouts, anomalies = self._normalize_checkouts(
            raw_consumers=raw_consumers,
            package_map=package_map,
            server_hostname=server_info.hostname,
        )
        snapshot.checkouts = checkouts
        snapshot.anomalies = anomalies

        return snapshot

    def _parse_server_status(
        self,
        text: str,
        exec_result: ExecutionResult
    ) -> ServerInfo:
        hostname = self.default_hostname
        port = self.default_port

        # Extract target from command if available as authoritative target fallback
        if exec_result.command and "-c" in exec_result.command:
            try:
                c_idx = exec_result.command.index("-c")
                if c_idx + 1 < len(exec_result.command):
                    target_arg = exec_result.command[c_idx + 1]
                    if "@" in target_arg:
                        p_str, h_str = target_arg.split("@", 1)
                        if p_str.isdigit():
                            port = int(p_str)
                        if h_str:
                            hostname = h_str
                    else:
                        hostname = target_arg
            except Exception:
                pass

        status = ServerStatus.UNKNOWN.value
        lmgrd_version = None
        adskflex_status = None
        adskflex_version = None
        error_code = None
        error_message = None

        if exec_result.timed_out:
            return ServerInfo(
                hostname=hostname,
                port=port,
                status=ServerStatus.DOWN.value,
                error_code=-96,
                error_message="Query timed out waiting for license server response",
            )

        # Check for FlexNet specific error code in output
        err_match = self.RE_FLEXNET_ERROR.search(text)
        if err_match:
            error_message = err_match.group(1).strip()
            error_code = int(err_match.group(2).strip())
            status = ServerStatus.DOWN.value

            # Extract hostname if present in license server status line
            port_host_match = re.search(r"License server status:\s*(\d+)?@?([A-Za-z0-9_.-]+)", text)
            if port_host_match:
                if port_host_match.group(1):
                    try:
                        port = int(port_host_match.group(1))
                    except ValueError:
                        pass
                if port_host_match.group(2):
                    hostname = port_host_match.group(2)

            return ServerInfo(
                hostname=hostname,
                port=port,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )

        # Check for server host line
        srv_match = self.RE_SERVER_STATUS.search(text)
        if srv_match:
            hostname = srv_match.group(1)
            srv_up_down = srv_match.group(2).upper()
            lmgrd_version = srv_match.group(3)
        else:
            srv_up_down = None

        # Check for vendor daemon line
        vnd_match = self.RE_VENDOR_STATUS.search(text)
        if vnd_match:
            adskflex_status = vnd_match.group(2).upper()
            adskflex_version = vnd_match.group(3)

        # Determine consolidated server state according to Rule 6
        if srv_up_down == "UP" and adskflex_status == "UP":
            status = ServerStatus.UP.value
        elif srv_up_down == "UP" and adskflex_status != "UP":
            status = ServerStatus.VENDOR_DOWN.value
            error_message = "lmgrd is running, but adskflex vendor daemon is down"
        elif srv_up_down == "DOWN":
            status = ServerStatus.DOWN.value
            error_message = "License server is down"
        elif exec_result.exit_code != 0:
            status = ServerStatus.DOWN.value
            error_code = exec_result.exit_code
            error_message = f"Process returned non-zero exit code: {exec_result.exit_code}"
        elif srv_up_down is None and not text.strip():
            status = ServerStatus.UNKNOWN.value
            error_message = "Empty output from license query"
        elif srv_up_down is None:
            status = ServerStatus.UNKNOWN.value
            error_message = "Could not identify license server status line"

        return ServerInfo(
            hostname=hostname,
            port=port,
            status=status,
            lmgrd_version=lmgrd_version,
            adskflex_status=adskflex_status,
            adskflex_version=adskflex_version,
            error_code=error_code,
            error_message=error_message,
        )

    def _parse_features_and_consumers(
        self,
        text: str
    ) -> Tuple[List[Dict[str, Any]], List[RawConsumer]]:
        feature_blocks = []
        raw_consumers = []

        # Find all feature header positions
        matches = list(self.RE_FEATURE_HEADER.finditer(text))
        for i, match in enumerate(matches):
            feature_code = match.group(1).strip()
            total_issued = int(match.group(2).strip())
            in_use = int(match.group(3).strip())

            # Text slice for this feature block up to next feature or end of text
            start_idx = match.end()
            end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block_text = text[start_idx:end_idx]

            feature_blocks.append({
                "feature_code": feature_code,
                "total_issued": total_issued,
                "in_use": in_use,
            })

            # If in_use > 0, parse consumer lines inside block_text
            if in_use > 0:
                for c_match in self.RE_CONSUMER_LINE.finditer(block_text):
                    username = c_match.group(1).strip()
                    machine_name = c_match.group(2).strip()
                    display = c_match.group(3).strip()
                    selected_component = c_match.group(4).strip()
                    version = c_match.group(5).strip()
                    server_handle = c_match.group(6).strip()
                    checkout_time_raw = c_match.group(7).strip()
                    pid = c_match.group(8).strip()

                    consumer_line = c_match.group(0).lower()
                    is_borrowed = "linger" in consumer_line or "borrow" in consumer_line

                    raw_consumers.append(RawConsumer(
                        username=username,
                        machine_name=machine_name,
                        display=display,
                        feature_code=feature_code,
                        selected_component=selected_component,
                        version=version,
                        server_handle=server_handle,
                        checkout_time_raw=checkout_time_raw,
                        pid=pid,
                        is_borrowed=is_borrowed,
                    ))

        return feature_blocks, raw_consumers

    def _classify_features(
        self,
        feature_blocks: List[Dict[str, Any]]
    ) -> Tuple[List[PackageInfo], List[FeatureInfo], Dict[str, PackageInfo]]:
        packages: List[PackageInfo] = []
        features: List[FeatureInfo] = []
        package_map: Dict[str, PackageInfo] = {}

        for block in feature_blocks:
            code = block["feature_code"]
            issued = block["total_issued"]
            used = block["in_use"]
            avail = issued - used

            # Classification logic
            if code in self.known_packages:
                pkg_cfg = self.known_packages[code]
                f_type = FeatureType.PACKAGE.value
                family = pkg_cfg.get("product_family")
                prod_name = pkg_cfg.get("product_name")
                cat_status = pkg_cfg.get("catalog_status", CatalogStatus.VERIFIED.value)
                components = pkg_cfg.get("components", [])
                year_ver = None
                parent_pkg = None
            elif code.endswith("_T_F"):
                # Uncatalogued package (e.g. unknown package code)
                f_type = FeatureType.PACKAGE.value
                family = self._extract_family_from_code(code)
                prod_name = f"Unknown Product ({family})" if family else "Unknown Package"
                cat_status = CatalogStatus.UNKNOWN.value
                components = []
                year_ver = None
                parent_pkg = None
            else:
                # Check if it is a component of one of our known packages
                matched_parent = None
                for p_code, p_meta in self.known_packages.items():
                    if code in p_meta.get("components", []):
                        matched_parent = p_code
                        break

                if matched_parent:
                    f_type = FeatureType.COMPONENT.value
                    parent_meta = self.known_packages[matched_parent]
                    family = parent_meta.get("product_family")
                    prod_name = parent_meta.get("product_name")
                    cat_status = parent_meta.get("catalog_status", CatalogStatus.VERIFIED.value)
                    year_ver = self._extract_year_version(code)
                    parent_pkg = matched_parent
                elif re.search(r"_\d{4}_0F$", code):
                    f_type = FeatureType.COMPONENT.value
                    family = self._extract_family_from_code(code)
                    prod_name = f"Unknown Component ({family})" if family else "Unknown Component"
                    cat_status = CatalogStatus.UNKNOWN.value
                    year_ver = self._extract_year_version(code)
                    parent_pkg = None
                else:
                    f_type = FeatureType.UNKNOWN.value
                    family = None
                    prod_name = None
                    cat_status = CatalogStatus.UNKNOWN.value
                    year_ver = None
                    parent_pkg = None

            # Build FeatureInfo
            f_info = FeatureInfo(
                feature_code=code,
                total_issued=issued,
                in_use=used,
                available=avail,
                feature_type=f_type,
                product_family=family,
                product_name=prod_name,
                year_version=year_ver,
                catalog_status=cat_status,
                parent_package=parent_pkg,
            )
            features.append(f_info)

            # If it is a PACKAGE, build PackageInfo
            if f_type == FeatureType.PACKAGE.value:
                util_pct = round((used / issued * 100.0), 2) if issued > 0 else 0.0
                pkg_info = PackageInfo(
                    feature_code=code,
                    total_issued=issued,
                    in_use=used,
                    available=avail,
                    utilization_pct=util_pct,
                    product_family=family,
                    product_name=prod_name,
                    catalog_status=cat_status,
                    components=components,
                )
                packages.append(pkg_info)
                package_map[code] = pkg_info

        return packages, features, package_map

    def _normalize_checkouts(
        self,
        raw_consumers: List[RawConsumer],
        package_map: Dict[str, PackageInfo],
        server_hostname: str,
    ) -> Tuple[List[PhysicalCheckout], List[str]]:
        """
        Deduplicates raw consumers according to Rule 2 and Rule 7.
        Pairs PACKAGE consumers with COMPONENT consumers into a single PhysicalCheckout.
        """
        checkouts: List[PhysicalCheckout] = []
        anomalies: List[str] = []

        # Split consumers into package consumers and component/other consumers
        package_consumers: List[RawConsumer] = []
        component_consumers: List[RawConsumer] = []
        other_consumers: List[RawConsumer] = []

        for c in raw_consumers:
            if c.feature_code in package_map or c.feature_code.endswith("_T_F"):
                package_consumers.append(c)
            elif re.search(r"_\d{4}_0F$", c.feature_code):
                component_consumers.append(c)
            else:
                other_consumers.append(c)

        used_component_indices = set()

        # Match each package consumer with its corresponding component consumer
        for pkg_c in package_consumers:
            matched_comp: Optional[RawConsumer] = None
            matched_idx = -1

            for idx, comp_c in enumerate(component_consumers):
                if idx in used_component_indices:
                    continue

                # Match criteria: username, machine, pid, and checkout time
                if (
                    comp_c.username.lower() == pkg_c.username.lower()
                    and comp_c.machine_name.lower() == pkg_c.machine_name.lower()
                    and comp_c.pid == pkg_c.pid
                    and comp_c.checkout_time_raw == pkg_c.checkout_time_raw
                ):
                    # Also verify component code matches selected_component in package line
                    if comp_c.feature_code.upper() == pkg_c.selected_component.upper():
                        matched_comp = comp_c
                        matched_idx = idx
                        break

            if matched_comp:
                used_component_indices.add(matched_idx)
                # Compute deterministic checkout ID
                cid = PhysicalCheckout.generate_id(
                    server_hostname=server_hostname,
                    username=pkg_c.username,
                    machine_name=pkg_c.machine_name,
                    selected_component_code=pkg_c.selected_component,
                    checkout_time_minute_str=pkg_c.checkout_time_raw,
                    pid=pkg_c.pid,
                )

                checkouts.append(PhysicalCheckout(
                    checkout_id=cid,
                    server_hostname=server_hostname,
                    username=pkg_c.username,
                    machine_name=pkg_c.machine_name,
                    display=pkg_c.display,
                    package_feature=pkg_c.feature_code,
                    selected_component=pkg_c.selected_component,
                    version=matched_comp.version,
                    server_handle=pkg_c.server_handle,  # diagnostic only
                    checkout_time=pkg_c.checkout_time_raw,
                    checkout_time_precision="MINUTE",
                    pid=pkg_c.pid,
                    is_borrowed=pkg_c.is_borrowed or matched_comp.is_borrowed,
                    is_incomplete=False,
                ))
            else:
                # Incomplete package consumer: package checkout without component
                cid = PhysicalCheckout.generate_id(
                    server_hostname=server_hostname,
                    username=pkg_c.username,
                    machine_name=pkg_c.machine_name,
                    selected_component_code=pkg_c.selected_component,
                    checkout_time_minute_str=pkg_c.checkout_time_raw,
                    pid=pkg_c.pid,
                )
                anomaly_msg = (
                    f"Incomplete checkout: Package '{pkg_c.feature_code}' user '{pkg_c.username}' "
                    f"has no matching component entry for '{pkg_c.selected_component}'."
                )
                anomalies.append(anomaly_msg)
                checkouts.append(PhysicalCheckout(
                    checkout_id=cid,
                    server_hostname=server_hostname,
                    username=pkg_c.username,
                    machine_name=pkg_c.machine_name,
                    display=pkg_c.display,
                    package_feature=pkg_c.feature_code,
                    selected_component=pkg_c.selected_component,
                    version=pkg_c.version,
                    server_handle=pkg_c.server_handle,
                    checkout_time=pkg_c.checkout_time_raw,
                    checkout_time_precision="MINUTE",
                    pid=pkg_c.pid,
                    is_borrowed=pkg_c.is_borrowed,
                    is_incomplete=True,
                    anomaly_note=anomaly_msg,
                ))

        # Check for orphan component consumers (unpaired)
        for idx, comp_c in enumerate(component_consumers):
            if idx not in used_component_indices:
                cid = PhysicalCheckout.generate_id(
                    server_hostname=server_hostname,
                    username=comp_c.username,
                    machine_name=comp_c.machine_name,
                    selected_component_code=comp_c.feature_code,
                    checkout_time_minute_str=comp_c.checkout_time_raw,
                    pid=comp_c.pid,
                )
                anomaly_msg = (
                    f"Orphan component checkout: Component '{comp_c.feature_code}' user '{comp_c.username}' "
                    f"has no matching package entry."
                )
                anomalies.append(anomaly_msg)
                checkouts.append(PhysicalCheckout(
                    checkout_id=cid,
                    server_hostname=server_hostname,
                    username=comp_c.username,
                    machine_name=comp_c.machine_name,
                    display=comp_c.display,
                    package_feature="UNPAIRED_COMPONENT",
                    selected_component=comp_c.feature_code,
                    version=comp_c.version,
                    server_handle=comp_c.server_handle,
                    checkout_time=comp_c.checkout_time_raw,
                    checkout_time_precision="MINUTE",
                    pid=comp_c.pid,
                    is_borrowed=comp_c.is_borrowed,
                    is_incomplete=True,
                    anomaly_note=anomaly_msg,
                ))

        # Check for other unclassified consumers
        for oth in other_consumers:
            cid = PhysicalCheckout.generate_id(
                server_hostname=server_hostname,
                username=oth.username,
                machine_name=oth.machine_name,
                selected_component_code=oth.feature_code,
                checkout_time_minute_str=oth.checkout_time_raw,
                pid=oth.pid,
            )
            anomaly_msg = f"Standalone/Unclassified feature checkout: '{oth.feature_code}' by '{oth.username}'."
            checkouts.append(PhysicalCheckout(
                checkout_id=cid,
                server_hostname=server_hostname,
                username=oth.username,
                machine_name=oth.machine_name,
                display=oth.display,
                package_feature=oth.feature_code,
                selected_component=oth.selected_component,
                version=oth.version,
                server_handle=oth.server_handle,
                checkout_time=oth.checkout_time_raw,
                checkout_time_precision="MINUTE",
                pid=oth.pid,
                is_borrowed=oth.is_borrowed,
                is_incomplete=False,
                anomaly_note=anomaly_msg,
            ))

        return checkouts, anomalies

    @staticmethod
    def _extract_year_version(code: str) -> Optional[str]:
        m = re.search(r"_(\d{4})_0F$", code)
        return m.group(1) if m else None

    @staticmethod
    def _extract_family_from_code(code: str) -> Optional[str]:
        # Examples: 77800MFS_T_F -> MFS, 77400MFIA_T_F -> MFIA, 76800MFAA_T_F -> MFAA
        m = re.search(r"^\d+([A-Za-z]+)_", code)
        return m.group(1) if m else None
