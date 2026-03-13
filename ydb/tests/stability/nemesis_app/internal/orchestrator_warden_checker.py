"""
OrchestratorWardenChecker - Orchestrator-side asynchronous liveness and safety checks collector.

This module provides the OrchestratorWardenChecker class that runs on the orchestrator
and performs LIVENESS checks (tablets alive, boot queue, etc. - uses HTTP monitoring, no SSH needed)
and orchestrator-level SAFETY checks (PDisk state, aggregated VERIFY failed errors).

Uses warden definitions from liveness_warden_factory() and orchestrator_safety_warden_factory().
"""

import asyncio
import json
import logging
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Any

from ydb.tests.stability.nemesis_app.internal.agent_warden_checker import (
    WardenCheckResult,
    WardenCheckReport,
    agent_safety_warden_factory,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Warden Factory Functions (Orchestrator-specific)
# =============================================================================

def liveness_warden_factory() -> List[Dict[str, Any]]:
    """
    Returns list of liveness warden definitions.
    These run centrally on orchestrator via HTTP monitoring.
    """
    return [
        {
            'name': 'AllTabletsAlive',
            'description': 'Check that all tablets are alive',
        },
        {
            'name': 'BootQueueSize',
            'description': 'Check boot queue size is acceptable',
        },
        {
            'name': 'SchemeShardNoInFlightTx',
            'description': 'Check SchemeShard has no stuck in-flight transactions',
        },
        {
            'name': 'TxCompleteLag',
            'description': 'Check transaction completion lag',
        },
    ]


def orchestrator_safety_warden_factory() -> List[Dict[str, Any]]:
    """
    Returns list of orchestrator safety warden definitions.
    These run centrally on orchestrator via HTTP monitoring.
    """
    return [
        {
            'name': 'AllPDisksAreInValidState',
            'description': 'Check all PDisks are in valid state',
        },
        {
            'name': 'UnifiedAgentVerifyFailedAggregated',
            'description': 'Aggregate and deduplicate VERIFY failed errors from all agents',
        },
    ]


def get_all_warden_definitions() -> List[Dict[str, Any]]:
    """Get all warden definitions as a list of dicts for API."""
    all_wardens = []

    for w in liveness_warden_factory():
        all_wardens.append({
            'name': w['name'],
            'category': 'liveness',
            'description': w['description'],
            'location': 'master'
        })

    for w in agent_safety_warden_factory():
        all_wardens.append({
            'name': w['name'],
            'category': 'safety',
            'description': w['description'],
            'location': 'agent'
        })

    for w in orchestrator_safety_warden_factory():
        all_wardens.append({
            'name': w['name'],
            'category': 'safety',
            'description': w['description'],
            'location': 'master'
        })

    return all_wardens


# =============================================================================
# Post-processors for violations
# =============================================================================

def deduplicate_verify_failed(violations: List[str]) -> List[str]:
    """
    Post-process VERIFY failed violations:
    - Deduplicate by lines 2 and 3 of each stack trace (the actual error location)
    - Count occurrences of each unique error
    - Return formatted output with count and one sample stack trace per unique error

    Args:
        violations: List of full stack traces (each is a multi-line string)

    Returns:
        List of formatted violations with counts
    """
    if not violations:
        return []

    # Group by uniqueness key (lines 2-3 of stack trace)
    unique_errors: Dict[str, List[str]] = defaultdict(list)

    for violation in violations:
        lines = violation.split('\n')
        # Get lines 2 and 3 (0-indexed: 1 and 2) as uniqueness key
        if len(lines) >= 3:
            key = f"{lines[1].strip()}|{lines[2].strip()}"
        elif len(lines) >= 2:
            key = lines[1].strip()
        else:
            key = lines[0].strip() if lines else "unknown"

        unique_errors[key].append(violation)

    # Format output
    result = []
    total_count = len(violations)
    unique_count = len(unique_errors)

    result.append(f"Found {total_count} VERIFY failed error(s), {unique_count} unique type(s)")

    for key, error_list in sorted(unique_errors.items(), key=lambda x: -len(x[1])):
        count = len(error_list)
        sample = error_list[0]
        result.append(f"[{count}x] {sample}")

    return result


# =============================================================================
# Minimal Cluster Objects for Wardens
# =============================================================================

class MinimalNode:
    """Minimal node object for wardens that need node.host and node.mon_port."""

    def __init__(self, host: str, mon_port: int, node_id: int):
        self.host = host
        self.mon_port = mon_port
        self.node_id = node_id
        self._monitor = None

    @property
    def monitor(self):
        if self._monitor is None:
            from ydb.tests.library.clients.kikimr_monitoring import KikimrMonitor
            self._monitor = KikimrMonitor(self.host, self.mon_port)
        return self._monitor


class MinimalCluster:
    """
    Minimal cluster-like object for wardens.

    Provides the interface expected by wardens:
    - cluster.nodes: dict of node_id -> node
    - cluster.slots: dict of slot_id -> slot (empty for minimal)
    - node.host, node.mon_port, node.monitor
    """

    def __init__(self, hosts: List[str], mon_port: int = 8765):
        self.nodes = {}
        self.slots = {}
        for i, host in enumerate(hosts):
            node_id = i + 1
            self.nodes[node_id] = MinimalNode(host, mon_port, node_id)


# =============================================================================
# Orchestrator Warden Checker
# =============================================================================

class OrchestratorWardenChecker:
    """
    Orchestrator-side warden checker that runs LIVENESS checks centrally.

    Liveness checks use HTTP monitoring endpoints and can be run from
    a central location (orchestrator). No SSH required.

    Uses warden definitions from liveness_warden_factory() and
    orchestrator_safety_warden_factory().

    Args:
        hosts: List of host addresses for monitoring
        mon_port: Monitoring port (default 8765)
    """

    def __init__(self, hosts: List[str] = None, mon_port: int = 8765):
        self._last_report: WardenCheckReport = WardenCheckReport(status='idle')
        self._is_running: bool = False
        self._lock = threading.Lock()
        self._hosts = hosts or []
        self._mon_port = mon_port
        self._cluster = None

    def set_hosts(self, hosts: List[str], mon_port: int = None):
        """Set or update the list of hosts to monitor."""
        self._hosts = hosts
        if mon_port is not None:
            self._mon_port = mon_port
        # Invalidate cluster cache
        self._cluster = None

    def is_running(self) -> bool:
        """Check if checks are currently running."""
        with self._lock:
            return self._is_running

    def get_last_result(self) -> Dict[str, Any]:
        """Return the last check result as a dictionary."""
        with self._lock:
            return self._last_report.to_dict()

    def get_available_checks(self) -> List[Dict[str, Any]]:
        """Return list of available checks for master."""
        checks = []
        for w in liveness_warden_factory():
            checks.append({
                'name': w['name'],
                'category': 'liveness',
                'description': w['description'],
                'location': 'master'
            })
        for w in orchestrator_safety_warden_factory():
            checks.append({
                'name': w['name'],
                'category': 'safety',
                'description': w['description'],
                'location': 'master'
            })
        return checks

    async def start_checks(self) -> bool:
        """
        Start running liveness checks asynchronously in a separate thread.

        Returns:
            True if checks were started, False if already running
        """
        with self._lock:
            if self._is_running:
                logger.debug("Orchestrator checks already running, skipping")
                return False
            self._is_running = True
            self._last_report = WardenCheckReport(
                status='running',
                started_at=datetime.utcnow().isoformat() + 'Z'
            )

        logger.info("Starting orchestrator warden checks in background thread")

        # Run checks in background thread
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._run_checks_sync)
        return True

    def _run_checks_sync(self):
        """Synchronous wrapper for running checks in a thread."""
        start_time = datetime.utcnow()
        logger.info("Orchestrator warden checks execution started")

        try:
            # Run checks synchronously without creating a new event loop
            # This avoids deadlock when run_in_executor is called inside
            cluster = self._get_cluster()
            liveness_results = []
            safety_results = []

            if cluster is not None:
                logger.debug("Running liveness checks...")
                liveness_results = self._run_liveness_checks_sync(cluster)
                logger.debug(f"Liveness checks completed: {len(liveness_results)} checks")

                # PDisk check also uses HTTP, run it here
                logger.debug("Running PDisk safety check...")
                safety_results = self._run_pdisk_check_sync(cluster)
                logger.debug(f"PDisk check completed: {len(safety_results)} checks")

                # Run aggregated VERIFY failed check
                logger.debug("Running aggregated VERIFY failed check...")
                aggregated_result = self._run_aggregated_verify_failed_check_sync()
                safety_results.append(aggregated_result)
                logger.debug(f"Aggregated VERIFY failed check completed: status={aggregated_result.status}")

            # Count results by status
            liveness_ok = sum(1 for r in liveness_results if r.status == 'ok')
            liveness_violation = sum(1 for r in liveness_results if r.status == 'violation')
            safety_ok = sum(1 for r in safety_results if r.status == 'ok')
            safety_violation = sum(1 for r in safety_results if r.status == 'violation')

            with self._lock:
                self._last_report = WardenCheckReport(
                    status='completed',
                    started_at=self._last_report.started_at,
                    completed_at=datetime.utcnow().isoformat() + 'Z',
                    liveness_checks=liveness_results,
                    safety_checks=safety_results
                )
                self._is_running = False

            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"Orchestrator warden checks completed in {elapsed:.1f}s: "
                f"liveness({liveness_ok} ok, {liveness_violation} violations), "
                f"safety({safety_ok} ok, {safety_violation} violations)"
            )

        except Exception as e:
            logger.error(f"Error running orchestrator checks: {e}")
            with self._lock:
                self._last_report = WardenCheckReport(
                    status='error',
                    started_at=self._last_report.started_at,
                    completed_at=datetime.utcnow().isoformat() + 'Z',
                    liveness_checks=[],
                    safety_checks=[],
                    error_message=str(e)
                )
                self._is_running = False

    def _get_cluster(self):
        """Create a minimal cluster-like object for wardens."""
        if self._cluster is None and self._hosts:
            self._cluster = MinimalCluster(self._hosts, self._mon_port)
        return self._cluster

    # Default path to the compiled nemesis binary
    NEMESIS_BINARY_PATH = '/Berkanavt/nemesis/bin/agent'

    def _run_liveness_checks_sync(
        self,
        cluster,
        timeout_seconds: int = 60
    ) -> List[WardenCheckResult]:
        """
        Run liveness checks using subprocess with hard timeout.

        This method spawns a separate process to run liveness checks.
        The process can be killed if it exceeds the timeout, providing
        a reliable way to prevent hanging on slow/loaded clusters.

        Args:
            cluster: The cluster object (used for config path)
            timeout_seconds: Total timeout for all liveness checks (default 60s)
        """
        from ydb.tests.stability.nemesis_app.internal.config import Settings

        # Get config path from settings
        try:
            settings = Settings()
            yaml_config = settings.yaml_config_location
        except Exception as e:
            logger.error(f"Failed to get settings: {e}")
            return [WardenCheckResult(
                name='LivenessChecks',
                category='liveness',
                violations=[],
                status='error',
                error_message=f"Failed to get settings: {e}"
            )]

        logger.info(f"Running liveness checks via subprocess with {timeout_seconds}s timeout")

        # Build command to run liveness checks
        # Use the compiled nemesis binary directly
        cmd = [
            self.NEMESIS_BINARY_PATH,
            'liveness',
            '--yaml-config-location', yaml_config,
            '--mon-port', str(self._mon_port)
        ]

        try:
            start_time = time.time()
            logger.info(f"Executing: {' '.join(cmd)}")

            # Run subprocess with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds
            )

            elapsed = time.time() - start_time
            logger.info(f"Subprocess completed in {elapsed:.1f}s with return code {result.returncode}")

            if result.stderr:
                logger.debug(f"Subprocess stderr: {result.stderr[:500]}")

            # Parse JSON output
            if result.stdout:
                try:
                    output = json.loads(result.stdout)
                    checks = output.get('checks', [])

                    # Convert to WardenCheckResult objects
                    results = []
                    for check in checks:
                        results.append(WardenCheckResult(
                            name=check.get('name', 'Unknown'),
                            category=check.get('category', 'liveness'),
                            violations=check.get('violations', []),
                            status=check.get('status', 'error'),
                            error_message=check.get('error_message')
                        ))

                    if output.get('status') == 'error' and output.get('error_message'):
                        logger.error(f"Liveness subprocess error: {output['error_message']}")

                    return results

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse liveness output: {e}")
                    logger.error(f"Raw output: {result.stdout[:500]}")
                    return [WardenCheckResult(
                        name='LivenessChecks',
                        category='liveness',
                        violations=[],
                        status='error',
                        error_message=f"Failed to parse output: {e}"
                    )]
            else:
                logger.error("No output from liveness subprocess")
                return [WardenCheckResult(
                    name='LivenessChecks',
                    category='liveness',
                    violations=[],
                    status='error',
                    error_message="No output from subprocess"
                )]

        except subprocess.TimeoutExpired:
            logger.warning(f"Liveness checks timed out after {timeout_seconds}s - killing subprocess")
            return [WardenCheckResult(
                name='LivenessChecks',
                category='liveness',
                violations=[],
                status='error',
                error_message=f"Timeout after {timeout_seconds}s - subprocess killed"
            )]

        except Exception as e:
            logger.error(f"Failed to run liveness subprocess: {e}")
            return [WardenCheckResult(
                name='LivenessChecks',
                category='liveness',
                violations=[],
                status='error',
                error_message=str(e)
            )]

    def _run_pdisk_check_sync(self, cluster) -> List[WardenCheckResult]:
        """Run PDisk state check (uses HTTP, not SSH)."""
        results = []

        try:
            from ydb.tests.library.wardens.disk import AllPDisksAreInValidStateSafetyWarden

            pdisk_warden = AllPDisksAreInValidStateSafetyWarden(
                cluster,
                timeout_seconds=30
            )

            violations = pdisk_warden.list_of_safety_violations()
            status = 'violation' if violations else 'ok'
            results.append(WardenCheckResult(
                name='AllPDisksAreInValidState',
                category='safety',
                violations=violations if violations else [],
                status=status
            ))
            if violations:
                logger.info(f"AllPDisksAreInValidState: {len(violations)} violation(s) found")
        except Exception as e:
            logger.error(f"AllPDisksAreInValidState: error - {e}")
            results.append(WardenCheckResult(
                name='AllPDisksAreInValidState',
                category='safety',
                violations=[],
                status='error',
                error_message=str(e)
            ))

        return results

    def _run_aggregated_verify_failed_check_sync(
        self,
        max_wait_seconds: int = 120,
        poll_interval_seconds: float = 2.0
    ) -> WardenCheckResult:
        """
        Run aggregated VERIFY failed check synchronously.
        This check waits for all agents to complete their safety checks,
        then aggregates and deduplicates VERIFY failed errors from all hosts.

        Args:
            max_wait_seconds: Maximum time to wait for all agents to complete (default 120s)
            poll_interval_seconds: Interval between polling attempts (default 2s)
        """
        import requests
        from ydb.tests.stability.nemesis_app.routers.orchestrator_router import hosts, get_app_port, is_local_host

        port = get_app_port()

        def get_agent_status(host: str) -> Dict[str, Any]:
            """Get the warden check status from an agent."""
            try:
                if is_local_host(host):
                    from ydb.tests.stability.nemesis_app.routers.agent_router import warden_checker
                    return warden_checker.get_last_result()
                else:
                    resp = requests.get(f"http://{host}:{port}/api/warden/result", timeout=10)
                    return resp.json()
            except Exception as e:
                logger.error(f"Failed to get status from {host}: {e}")
                return {"status": "error", "error_message": str(e)}

        def is_agent_completed(result: Dict[str, Any]) -> bool:
            """Check if an agent has completed its checks."""
            status = result.get("status", "idle")
            # Consider completed if status is 'completed' or 'error'
            # 'idle' means checks haven't started, 'running' means still in progress
            return status in ("completed", "error")

        # Wait for all agents to complete their checks
        start_time = time.time()
        all_completed = False
        pending_hosts = set(hosts)

        logger.info(f"Waiting for {len(hosts)} agents to complete safety checks...")

        while not all_completed and (time.time() - start_time) < max_wait_seconds:
            still_pending = set()

            for host in pending_hosts:
                result = get_agent_status(host)
                if not is_agent_completed(result):
                    still_pending.add(host)
                else:
                    logger.debug(f"Agent {host} completed with status: {result.get('status')}")

            pending_hosts = still_pending

            if pending_hosts:
                logger.debug(f"Still waiting for {len(pending_hosts)} agents: {pending_hosts}")
                time.sleep(poll_interval_seconds)
            else:
                all_completed = True

        if pending_hosts:
            logger.warning(
                f"Timeout waiting for agents to complete. "
                f"Still pending after {max_wait_seconds}s: {pending_hosts}"
            )

        elapsed_wait = time.time() - start_time
        logger.info(f"All agents completed (or timed out) after {elapsed_wait:.1f}s")

        # Now collect VERIFY failed violations from all agents
        verify_failed_violations = []
        verify_failed_hosts = []

        def get_verify_failed_from_host(host: str):
            """Extract VERIFY failed violations from an agent's result."""
            try:
                result = get_agent_status(host)

                # Extract VERIFY failed violations
                if result.get("safety_checks"):
                    for check in result["safety_checks"]:
                        if "UnifiedAgentVerifyFailed" in check.get("name", ""):
                            if check.get("violations"):
                                return host, check["violations"]
                return host, []
            except Exception as e:
                logger.error(f"Failed to get VERIFY failed from {host}: {e}")
                return host, []

        # Get results from all hosts sequentially
        logger.debug("Collecting VERIFY failed violations from all agents...")
        for host in hosts:
            host_result, violations = get_verify_failed_from_host(host)
            if violations:
                logger.debug(f"Agent {host}: {len(violations)} VERIFY failed violation(s)")
                verify_failed_violations.extend(violations)
                verify_failed_hosts.append(host_result)

        # Apply deduplication post-processor
        aggregated_violations = []
        if verify_failed_violations:
            logger.info(f"Aggregating {len(verify_failed_violations)} VERIFY failed violations from {len(verify_failed_hosts)} hosts")
            aggregated_violations = deduplicate_verify_failed(verify_failed_violations)
            logger.info(f"After deduplication: {len(aggregated_violations)} unique violation types")
        else:
            logger.debug("No VERIFY failed violations found across all agents")

        # Add warning if some agents didn't complete in time
        error_message = None
        if pending_hosts:
            error_message = f"Timeout: agents {list(pending_hosts)} did not complete in {max_wait_seconds}s"

        return WardenCheckResult(
            name='UnifiedAgentVerifyFailedAggregated',
            category='safety',
            violations=aggregated_violations,
            status='violation' if aggregated_violations else 'ok',
            error_message=error_message,
            affected_hosts=verify_failed_hosts
        )


# Global instance for the orchestrator (liveness + safety checks)
orchestrator_warden_checker = OrchestratorWardenChecker()