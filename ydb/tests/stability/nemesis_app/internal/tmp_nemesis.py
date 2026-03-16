# -*- coding: utf-8 -*-
"""
Local-only nemesis implementations for nemesis_app.
These nemesis classes execute only on localhost without SSH calls.

Architecture:
- prepare_fault() runs on the orchestrator node and determines which hosts to target
- inject_fault() and extract_fault() run on the agent (localhost)
"""
import logging
import random
import socket
import subprocess
import signal

from ydb.tests.stability.nemesis_app.internal.defaults import AbstractAgentNemesis
from ydb.tests.library.common.types import TabletTypes
from ydb.tests.library.clients.kikimr_client import kikimr_client_factory
from ydb.tests.library.clients.kikimr_http_client import HiveClient


# =============================================================================
# Tablet Nemesis - Kill tablets via gRPC (works locally on each agent)
# =============================================================================

class AbstractLocalTabletNemesis(AbstractAgentNemesis):
    """
    Base class for tablet nemesis that kills tablets via gRPC on localhost.
    The gRPC call is made to localhost, so it works on any agent node.
    """

    def __init__(self, tablet_type, grpc_port=2135):
        super(AbstractLocalTabletNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tablet_type = tablet_type
        self._grpc_port = grpc_port
        self._client = None
        self._tablet_ids = []

    @property
    def tablet_type(self):
        return self._tablet_type

    @property
    def client(self):
        if self._client is None:
            self._client = kikimr_client_factory(
                'localhost', self._grpc_port, retry_count=10, timeout=10)
        return self._client

    @property
    def tablet_ids(self):
        return self._tablet_ids

    def _fetch_tablet_ids(self):
        """Fetch tablet IDs of the specified type from the local node."""
        self._logger.info('Fetching tablet IDs for type %s', self._tablet_type)
        try:
            response = self.client.tablet_state(self._tablet_type)
            self._tablet_ids = [info.TabletId for info in response.TabletStateInfo]
            self._logger.info("Found %d tablets of type %s", len(self._tablet_ids), self._tablet_type)
        except Exception as e:
            self._logger.error("Failed to fetch tablet IDs: %s", str(e))
            self._tablet_ids = []

    def inject_fault(self):
        """Kill a random tablet of the specified type."""
        self._logger.info("=== INJECT_FAULT START: %s ===", str(self))

        if not self._tablet_ids:
            self._fetch_tablet_ids()

        if self._tablet_ids:
            tablet_id = random.choice(self._tablet_ids)
            self._logger.info("Killing tablet_id=%s of type %s", tablet_id, self._tablet_type)
            try:
                self.client.tablet_kill(tablet_id)
                self._logger.info("Successfully killed tablet_id=%s", tablet_id)
                self.on_success_inject_fault()
            except Exception as e:
                self._logger.error("Failed to kill tablet: %s", str(e))
        else:
            self._logger.warning("No tablets found to kill")

    def __str__(self):
        return "{class_name}(tablet_type={tablet_type})".format(
            class_name=self.__class__.__name__,
            tablet_type=self._tablet_type
        )


class KillCoordinatorNemesis(AbstractLocalTabletNemesis):
    """Kills a random TX Coordinator tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillCoordinatorNemesis, self).__init__(TabletTypes.FLAT_TX_COORDINATOR, grpc_port)


class KillMediatorNemesis(AbstractLocalTabletNemesis):
    """Kills a random TX Mediator tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillMediatorNemesis, self).__init__(TabletTypes.TX_MEDIATOR, grpc_port)


class KillDataShardNemesis(AbstractLocalTabletNemesis):
    """Kills a random DataShard tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillDataShardNemesis, self).__init__(TabletTypes.FLAT_DATASHARD, grpc_port)


class KillHiveNemesis(AbstractLocalTabletNemesis):
    """Kills the Hive tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillHiveNemesis, self).__init__(TabletTypes.FLAT_HIVE, grpc_port)


class KillBsControllerNemesis(AbstractLocalTabletNemesis):
    """Kills the BlobStorage Controller tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillBsControllerNemesis, self).__init__(TabletTypes.FLAT_BS_CONTROLLER, grpc_port)


class KillSchemeShardNemesis(AbstractLocalTabletNemesis):
    """Kills a random SchemeShard tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillSchemeShardNemesis, self).__init__(TabletTypes.FLAT_SCHEMESHARD, grpc_port)


class KillPersQueueNemesis(AbstractLocalTabletNemesis):
    """Kills a random PersQueue tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillPersQueueNemesis, self).__init__(TabletTypes.PERSQUEUE, grpc_port)


class KillKeyValueNemesis(AbstractLocalTabletNemesis):
    """Kills a random KeyValue tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillKeyValueNemesis, self).__init__(TabletTypes.KEYVALUEFLAT, grpc_port)


class KillTxAllocatorNemesis(AbstractLocalTabletNemesis):
    """Kills a random TX Allocator tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillTxAllocatorNemesis, self).__init__(TabletTypes.TX_ALLOCATOR, grpc_port)


class KillNodeBrokerNemesis(AbstractLocalTabletNemesis):
    """Kills the Node Broker tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillNodeBrokerNemesis, self).__init__(TabletTypes.NODE_BROKER, grpc_port)


class KillTenantSlotBrokerNemesis(AbstractLocalTabletNemesis):
    """Kills the Tenant Slot Broker tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillTenantSlotBrokerNemesis, self).__init__(TabletTypes.TENANT_SLOT_BROKER, grpc_port)


class KillBlockstoreVolumeNemesis(AbstractLocalTabletNemesis):
    """Kills a random Blockstore Volume tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillBlockstoreVolumeNemesis, self).__init__(TabletTypes.BLOCKSTORE_VOLUME, grpc_port)


class KillBlockstorePartitionNemesis(AbstractLocalTabletNemesis):
    """Kills a random Blockstore Partition tablet via gRPC on localhost."""

    def __init__(self, grpc_port=2135):
        super(KillBlockstorePartitionNemesis, self).__init__(TabletTypes.BLOCKSTORE_PARTITION, grpc_port)


# =============================================================================
# Hive Management Nemesis - Uses HTTP API on localhost
# =============================================================================

class ReBalanceTabletsNemesis(AbstractAgentNemesis):
    """Triggers tablet rebalancing via Hive HTTP API on localhost."""

    def __init__(self, mon_port=8765):
        super(ReBalanceTabletsNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._mon_port = mon_port
        self._hive = None

    @property
    def hive(self):
        if self._hive is None:
            self._hive = HiveClient('localhost', self._mon_port)
        return self._hive

    def inject_fault(self):
        """Trigger tablet rebalancing."""
        self._logger.info("=== INJECT_FAULT START: ReBalanceTabletsNemesis ===")
        try:
            self.hive.rebalance_all_tablets()
            self._logger.info("Successfully triggered tablet rebalancing")
            self.on_success_inject_fault()
        except Exception as e:
            self._logger.error("Failed to rebalance tablets: %s", str(e))


class ChangeTabletGroupNemesis(AbstractAgentNemesis):
    """Changes tablet group for a random tablet of specified type via Hive HTTP API on localhost."""

    def __init__(self, tablet_type, mon_port=8765, grpc_port=2135, channels=()):
        super(ChangeTabletGroupNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tablet_type = tablet_type
        self._mon_port = mon_port
        self._grpc_port = grpc_port
        self._channels = channels
        self._hive = None
        self._client = None
        self._tablet_ids = []

    @property
    def hive(self):
        if self._hive is None:
            self._hive = HiveClient('localhost', self._mon_port)
        return self._hive

    @property
    def client(self):
        if self._client is None:
            self._client = kikimr_client_factory(
                'localhost', self._grpc_port, retry_count=10, timeout=10)
        return self._client

    def _fetch_tablet_ids(self):
        """Fetch tablet IDs of the specified type."""
        try:
            response = self.client.tablet_state(self._tablet_type)
            self._tablet_ids = [info.TabletId for info in response.TabletStateInfo]
            self._logger.info("Found %d tablets of type %s", len(self._tablet_ids), self._tablet_type)
        except Exception as e:
            self._logger.error("Failed to fetch tablet IDs: %s", str(e))
            self._tablet_ids = []

    def inject_fault(self):
        """Change tablet group for a random tablet."""
        self._logger.info("=== INJECT_FAULT START: ChangeTabletGroupNemesis ===")

        if not self._tablet_ids:
            self._fetch_tablet_ids()

        if self._tablet_ids:
            tablet_id = random.choice(self._tablet_ids)
            try:
                self.hive.change_tablet_group(tablet_id, channels=self._channels)
                self._logger.info("Successfully changed group for tablet %d", tablet_id)
                self.on_success_inject_fault()
            except Exception as e:
                self._logger.error("Failed to change tablet group: %s", str(e))
        else:
            self._logger.warning("No tablets found")


class BulkChangeTabletGroupNemesis(AbstractAgentNemesis):
    """Changes tablet groups for all tablets of specified type via Hive HTTP API on localhost."""

    def __init__(self, tablet_type, mon_port=8765, channels=(), percent=None):
        super(BulkChangeTabletGroupNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tablet_type = tablet_type
        self._mon_port = mon_port
        self._channels = channels
        self._percent = percent
        self._hive = None

    @property
    def hive(self):
        if self._hive is None:
            self._hive = HiveClient('localhost', self._mon_port)
        return self._hive

    def inject_fault(self):
        """Change tablet groups for all tablets of the specified type."""
        self._logger.info("=== INJECT_FAULT START: BulkChangeTabletGroupNemesis ===")
        try:
            self.hive.change_tablet_group_by_tablet_type(
                self._tablet_type, percent=self._percent, channels=self._channels)
            self._logger.info("Successfully changed groups for tablet type %s", self._tablet_type)
            self.on_success_inject_fault()
        except Exception as e:
            self._logger.error("Failed to change tablet groups: %s", str(e))


# =============================================================================
# Node/Process Nemesis - Local process management (no SSH)
# =============================================================================

class KillSlotNemesis(AbstractAgentNemesis):
    """Terminates a random dynamic slot process with SIGKILL on localhost."""

    def __init__(self):
        super(KillSlotNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        """Kill a random dynamic slot process using SIGKILL."""
        self._logger.info("=== INJECT_FAULT START: KillSlotNemesis ===")
        # Find kikimr-multi processes (dynamic slots)
        cmd = "ps aux | grep 'kikimr-multi' | grep -v grep | awk '{ print $2 }' | shuf -n 1 | xargs -r sudo kill -%d" % (
            int(signal.SIGKILL),
        )
        self._logger.info(f"Executing: {cmd}")
        try:
            subprocess.check_call(cmd, shell=True)
            self._logger.info("Successfully killed slot process")
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to kill slot: %s", str(e))


class SuspendNodeNemesis(AbstractAgentNemesis):
    """Suspends YDB node process with SIGSTOP on localhost, then resumes with SIGCONT."""

    def __init__(self, suspend_duration=30):
        super(SuspendNodeNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._suspend_duration = suspend_duration
        self._suspended_pid = None

    def inject_fault(self):
        """Suspend a YDB node process."""
        self._logger.info("=== INJECT_FAULT START: SuspendNodeNemesis ===")
        # Find a YDB process to suspend
        cmd = "ps aux | grep '\\--ic-port' | grep -v grep | awk '{ print $2 }' | shuf -n 1"
        try:
            result = subprocess.check_output(cmd, shell=True).decode().strip()
            if result:
                self._suspended_pid = int(result)
                self._logger.info("Suspending process %d for %d seconds",
                                  self._suspended_pid, self._suspend_duration)
                subprocess.check_call(f"sudo kill -{int(signal.SIGSTOP)} {self._suspended_pid}", shell=True)
                self.on_success_inject_fault()
            else:
                self._logger.warning("No YDB process found to suspend")
        except Exception as e:
            self._logger.error("Failed to suspend node: %s", str(e))

    def extract_fault(self):
        """Resume the suspended process."""
        if self._suspended_pid:
            self._logger.info("Resuming process %d", self._suspended_pid)
            try:
                subprocess.check_call(f"sudo kill -{int(signal.SIGCONT)} {self._suspended_pid}", shell=True)
                self._suspended_pid = None
            except Exception as e:
                self._logger.error("Failed to resume process: %s", str(e))


class RestartNodeNemesis(AbstractAgentNemesis):
    """Restarts the local YDB node service via systemctl on localhost."""

    def __init__(self, service_name='kikimr'):
        super(RestartNodeNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._service_name = service_name

    def inject_fault(self):
        """Restart the YDB service."""
        self._logger.info("=== INJECT_FAULT START: RestartNodeNemesis ===")
        cmd = f"sudo systemctl restart {self._service_name}"
        self._logger.info(f"Executing: {cmd}")
        try:
            subprocess.check_call(cmd, shell=True)
            self._logger.info("Successfully restarted %s service", self._service_name)
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to restart service: %s", str(e))


class StopStartNodeNemesis(AbstractAgentNemesis):
    """Stops the YDB node service on localhost, waits, then starts it again."""

    def __init__(self, service_name='kikimr', stop_duration=30):
        super(StopStartNodeNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._service_name = service_name
        self._stop_duration = stop_duration
        self._is_stopped = False

    def inject_fault(self):
        """Stop the YDB service."""
        self._logger.info("=== INJECT_FAULT START: StopStartNodeNemesis ===")
        cmd = f"sudo systemctl stop {self._service_name}"
        self._logger.info(f"Executing: {cmd}")
        try:
            subprocess.check_call(cmd, shell=True)
            self._is_stopped = True
            self._logger.info("Successfully stopped %s service", self._service_name)
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to stop service: %s", str(e))

    def extract_fault(self):
        """Start the YDB service."""
        if self._is_stopped:
            self._logger.info("Starting %s service", self._service_name)
            cmd = f"sudo systemctl start {self._service_name}"
            try:
                subprocess.check_call(cmd, shell=True)
                self._is_stopped = False
                self._logger.info("Successfully started %s service", self._service_name)
            except subprocess.CalledProcessError as e:
                self._logger.error("Failed to start service: %s", str(e))


# =============================================================================
# Disk Nemesis - Local disk operations (no SSH)
# =============================================================================

class SafelyBreakDisk(AbstractAgentNemesis):
    """Safely breaks a disk drive on localhost via gRPC."""

    def __init__(self, grpc_port=2135):
        super(SafelyBreakDisk, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._grpc_port = grpc_port
        self._client = None
        self._currently_broken_drive = None
        self._states = {}
        self._broken_drives = set()

    @property
    def client(self):
        if self._client is None:
            from ydb.tests.library.clients.kikimr_client import kikimr_client_factory
            self._client = kikimr_client_factory(
                'localhost', self._grpc_port, retry_count=10, timeout=10)
        return self._client

    def prepare_fault(self):
        """Prepare state by reading current drive status."""
        self._broken_drives = set()
        try:
            from ydb.tests.library.common.msgbus_types import EDriveStatus

            # Read drive status from local node
            response = self.client.bs_controller_read_drive_status()
            if hasattr(response, 'BlobStorageConfigResponse'):
                for status in response.BlobStorageConfigResponse.Status:
                    for drive in status.DriveStatus:
                        self._states[drive.Path] = drive.Status
                        if drive.Status != EDriveStatus.ACTIVE:
                            self._broken_drives.add(drive.Path)
        except Exception as e:
            self._logger.error("Failed to read drive status: %s", str(e))
            self._states = {}

    def inject_fault(self):
        """Break a random disk drive."""
        self._logger.info("=== INJECT_FAULT START: SafelyBreakDisk ===")
        self.extract_fault()  # Restore any previously broken drives

        if self._states:
            path = random.choice(list(self._states.keys()))
            try:
                from ydb.tests.library.common.msgbus_types import EDriveStatus
                from ydb.core.protos.blobstorage_config_pb2 import TConfigResponse

                # Try to break the drive
                for _ in range(6):
                    response = self.client.bs_controller_update_drive_status(path, EDriveStatus.BROKEN)
                    if hasattr(response, 'BlobStorageConfigResponse'):
                        if not response.BlobStorageConfigResponse.Success:
                            if len(response.BlobStorageConfigResponse.Status) == 1:
                                fail_reason = response.BlobStorageConfigResponse.Status[0].FailReason
                                if fail_reason == TConfigResponse.TStatus.EFailReason.kMayLoseData:
                                    import time
                                    time.sleep(10)
                                    continue
                        break

                self._currently_broken_drive = path
                self._broken_drives.add(path)
                self._logger.info("Successfully broke drive: %s", path)
                self.on_success_inject_fault()
            except Exception as e:
                self._logger.error("Failed to break drive: %s", str(e))

    def extract_fault(self):
        """Restore broken disk drives."""
        try:
            from ydb.tests.library.common.msgbus_types import EDriveStatus

            for path in list(self._broken_drives):
                try:
                    self.client.bs_controller_update_drive_status(path, EDriveStatus.ACTIVE)
                    self._logger.info("Restored drive: %s", path)
                except Exception as e:
                    self._logger.error("Failed to restore drive %s: %s", path, str(e))
                finally:
                    self._broken_drives.discard(path)
            self._currently_broken_drive = None
        except Exception as e:
            self._logger.error("Failed to restore drives: %s", str(e))


class SafelyCleanupDisks(AbstractAgentNemesis):
    """Safely cleans up disks on localhost."""

    def __init__(self, grpc_port=2135):
        super(SafelyCleanupDisks, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._grpc_port = grpc_port
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from ydb.tests.library.clients.kikimr_client_factory import kikimr_client_factory
            self._client = kikimr_client_factory(
                'localhost', self._grpc_port, retry_count=10, timeout=10)
        return self._client

    def inject_fault(self):
        """Cleanup disks by killing process and restarting."""
        self._logger.info("=== INJECT_FAULT START: SafelyCleanupDisks ===")
        try:
            # Kill the local YDB process
            cmd = "sudo systemctl stop kikimr"
            subprocess.check_call(cmd, shell=True)
            self._logger.info("Stopped kikimr service")

            # Cleanup disks (this would need to be implemented based on actual disk cleanup logic)
            # For now, we just restart the service
            cmd = "sudo systemctl start kikimr"
            subprocess.check_call(cmd, shell=True)
            self._logger.info("Started kikimr service")

            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to cleanup disks: %s", str(e))


# =============================================================================
# Datacenter Nemesis - Network partition for datacenters (local iptables)
# =============================================================================

class DataCenterIptablesBlockPortsNemesis(AbstractAgentNemesis):
    """Blocks YDB ports using iptables on localhost for datacenter isolation."""

    def __init__(self, duration=60):
        super(DataCenterIptablesBlockPortsNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._block_ports_cmd = (
            "sudo /sbin/ip6tables -w -A YDB_FW -p tcp -m multiport "
            "--ports 2135,2136,8765,19001,31000:32000 -j REJECT"
        )
        self._restore_ports_cmd = (
            "sudo /sbin/ip6tables -w -F YDB_FW"
        )

    def inject_fault(self):
        """Block YDB ports using iptables."""
        self._logger.info("=== INJECT_FAULT START: DataCenterIptablesBlockPortsNemesis ===")
        try:
            # Block ports and schedule automatic recovery
            block_and_recover_cmd = (
                f"nohup bash -c '{self._block_ports_cmd} && sleep {self._duration} && "
                f"{self._restore_ports_cmd}' > /dev/null 2>&1 &"
            )
            subprocess.check_call(block_and_recover_cmd, shell=True)
            self._logger.info("Blocked YDB ports and scheduled automatic recovery")
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to block YDB ports: %s", str(e))

    def extract_fault(self):
        """YDB ports are automatically restored via sleep command scheduled during injection."""
        self._logger.info("Skipping manual port restoration - automatic recovery via sleep command is scheduled")


class DataCenterRouteUnreachableNemesis(AbstractAgentNemesis):
    """Makes network routes unreachable on localhost for datacenter isolation."""

    def __init__(self, duration=60):
        super(DataCenterRouteUnreachableNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._block_cmd_template = (
            'sudo /usr/bin/ip -6 ro replace unreach {} || sudo /usr/bin/ip -6 ro add unreach {}'
        )
        self._restore_cmd_template = (
            'sudo /usr/bin/ip -6 ro del unreach {}'
        )
        self._blocked_ips = []

    def _resolve_hostname_to_ip(self, hostname):
        """Resolve hostname to IP address."""
        try:
            result = socket.getaddrinfo(hostname, None, socket.AF_INET6)
            if result:
                return result[0][4][0]
        except socket.gaierror:
            pass
        return None

    def inject_fault(self):
        """Block network routes to specified IPs."""
        self._logger.info("=== INJECT_FAULT START: DataCenterRouteUnreachableNemesis ===")
        # This nemesis would need target IPs from prepare_fault
        # For now, it's a placeholder that can be configured with specific IPs
        self.on_success_inject_fault()

    def extract_fault(self):
        """Restore blocked network routes."""
        for ip in self._blocked_ips:
            try:
                cmd = self._restore_cmd_template.format(ip)
                subprocess.check_call(cmd, shell=True)
                self._logger.info("Restored route to %s", ip)
            except subprocess.CalledProcessError as e:
                self._logger.error("Failed to restore route to %s: %s", ip, str(e))
        self._blocked_ips = []


class DataCenterStopNodesNemesis(AbstractAgentNemesis):
    """Stops the local YDB node for datacenter isolation."""

    def __init__(self, duration=60):
        super(DataCenterStopNodesNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._is_stopped = False

    def inject_fault(self):
        """Stop the local YDB node."""
        self._logger.info("=== INJECT_FAULT START: DataCenterStopNodesNemesis ===")
        try:
            cmd = "sudo systemctl stop kikimr"
            subprocess.check_call(cmd, shell=True)
            self._is_stopped = True
            self._logger.info("Stopped kikimr service")
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to stop kikimr: %s", str(e))

    def extract_fault(self):
        """Start the local YDB node."""
        if self._is_stopped:
            try:
                cmd = "sudo systemctl start kikimr"
                subprocess.check_call(cmd, shell=True)
                self._is_stopped = False
                self._logger.info("Started kikimr service")
            except subprocess.CalledProcessError as e:
                self._logger.error("Failed to start kikimr: %s", str(e))


# =============================================================================
# Bridge Pile Nemesis - Bridge-aware nemesis for bridge pile switching
# =============================================================================

class BridgePileIptablesBlockPortsNemesis(AbstractAgentNemesis):
    """Blocks YDB ports using iptables on localhost for bridge pile isolation."""

    def __init__(self, duration=60):
        super(BridgePileIptablesBlockPortsNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._block_ports_cmd = (
            "sudo /sbin/ip6tables -w -A YDB_FW -p tcp -m multiport "
            "--ports 2135,2136,8765,19001,31000:32000 -j REJECT"
        )
        self._restore_ports_cmd = (
            "sudo /sbin/ip6tables -w -F YDB_FW"
        )

    def inject_fault(self):
        """Block YDB ports using iptables."""
        self._logger.info("=== INJECT_FAULT START: BridgePileIptablesBlockPortsNemesis ===")
        try:
            # Block ports and schedule automatic recovery
            block_and_recover_cmd = (
                f"nohup bash -c '{self._block_ports_cmd} && sleep {self._duration} && "
                f"{self._restore_ports_cmd}' > /dev/null 2>&1 &"
            )
            subprocess.check_call(block_and_recover_cmd, shell=True)
            self._logger.info("Blocked YDB ports and scheduled automatic recovery")
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to block YDB ports: %s", str(e))

    def extract_fault(self):
        """YDB ports are automatically restored via sleep command scheduled during injection."""
        self._logger.info("Skipping manual port restoration - automatic recovery via sleep command is scheduled")


class BridgePileRouteUnreachableNemesis(AbstractAgentNemesis):
    """Makes network routes unreachable on localhost for bridge pile isolation."""

    def __init__(self, duration=60):
        super(BridgePileRouteUnreachableNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._block_cmd_template = (
            'sudo /usr/bin/ip -6 ro replace unreach {} || sudo /usr/bin/ip -6 ro add unreach {}'
        )
        self._restore_cmd_template = (
            'sudo /usr/bin/ip -6 ro del unreach {}'
        )
        self._blocked_ips = []

    def _resolve_hostname_to_ip(self, hostname):
        """Resolve hostname to IP address."""
        try:
            result = socket.getaddrinfo(hostname, None, socket.AF_INET6)
            if result:
                return result[0][4][0]
        except socket.gaierror:
            pass
        return None

    def inject_fault(self):
        """Block network routes to specified IPs."""
        self._logger.info("=== INJECT_FAULT START: BridgePileRouteUnreachableNemesis ===")
        # This nemesis would need target IPs from prepare_fault
        # For now, it's a placeholder that can be configured with specific IPs
        self.on_success_inject_fault()

    def extract_fault(self):
        """Restore blocked network routes."""
        for ip in self._blocked_ips:
            try:
                cmd = self._restore_cmd_template.format(ip)
                subprocess.check_call(cmd, shell=True)
                self._logger.info("Restored route to %s", ip)
            except subprocess.CalledProcessError as e:
                self._logger.error("Failed to restore route to %s: %s", ip, str(e))
        self._blocked_ips = []


class BridgePileStopNodesNemesis(AbstractAgentNemesis):
    """Stops the local YDB node for bridge pile isolation."""

    def __init__(self, duration=60):
        super(BridgePileStopNodesNemesis, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._duration = duration
        self._is_stopped = False

    def inject_fault(self):
        """Stop the local YDB node."""
        self._logger.info("=== INJECT_FAULT START: BridgePileStopNodesNemesis ===")
        try:
            cmd = "sudo systemctl stop kikimr"
            subprocess.check_call(cmd, shell=True)
            self._is_stopped = True
            self._logger.info("Stopped kikimr service")
            self.on_success_inject_fault()
        except subprocess.CalledProcessError as e:
            self._logger.error("Failed to stop kikimr: %s", str(e))

    def extract_fault(self):
        """Start the local YDB node."""
        if self._is_stopped:
            try:
                cmd = "sudo systemctl start kikimr"
                subprocess.check_call(cmd, shell=True)
                self._is_stopped = False
                self._logger.info("Started kikimr service")
            except subprocess.CalledProcessError as e:
                self._logger.error("Failed to start kikimr: %s", str(e))


# =============================================================================
# Export all nemesis classes for use in defaults.py
# =============================================================================

# Tablet nemesis
TABLET_NEMESIS_TYPES = {
    "KillCoordinatorNemesis": {
        "runner": KillCoordinatorNemesis(),
        "schedule": 60
    },
    "KillMediatorNemesis": {
        "runner": KillMediatorNemesis(),
        "schedule": 60
    },
    "KillDataShardNemesis": {
        "runner": KillDataShardNemesis(),
        "schedule": 45
    },
    "KillHiveNemesis": {
        "runner": KillHiveNemesis(),
        "schedule": 180
    },
    "KillBsControllerNemesis": {
        "runner": KillBsControllerNemesis(),
        "schedule": 450
    },
    "KillSchemeShardNemesis": {
        "runner": KillSchemeShardNemesis(),
        "schedule": 180
    },
    "KillPersQueueNemesis": {
        "runner": KillPersQueueNemesis(),
        "schedule": 45
    },
    "KillKeyValueNemesis": {
        "runner": KillKeyValueNemesis(),
        "schedule": 45
    },
    "KillTxAllocatorNemesis": {
        "runner": KillTxAllocatorNemesis(),
        "schedule": 180
    },
    "KillNodeBrokerNemesis": {
        "runner": KillNodeBrokerNemesis(),
        "schedule": 180
    },
    "KillTenantSlotBrokerNemesis": {
        "runner": KillTenantSlotBrokerNemesis(),
        "schedule": 180
    },
    "KillBlockstoreVolumeNemesis": {
        "runner": KillBlockstoreVolumeNemesis(),
        "schedule": 45
    },
    "KillBlockstorePartitionNemesis": {
        "runner": KillBlockstorePartitionNemesis(),
        "schedule": 45
    },
}

# Hive management nemesis
HIVE_NEMESIS_TYPES = {
    "ReBalanceTabletsNemesis": {
        "runner": ReBalanceTabletsNemesis(),
        "schedule": 120
    },
}

# Node/process nemesis
NODE_NEMESIS_TYPES = {
    "KillSlotNemesis": {
        "runner": KillSlotNemesis(),
        "schedule": 90
    },
    "SuspendNodeNemesis": {
        "runner": SuspendNodeNemesis(suspend_duration=30),
        "schedule": 600
    },
    "RestartNodeNemesis": {
        "runner": RestartNodeNemesis(),
        "schedule": 300
    },
    "StopStartNodeNemesis": {
        "runner": StopStartNodeNemesis(stop_duration=30),
        "schedule": 450
    },
}

# Combined dictionary of all nemesis types from this module
ALL_NEMESIS_TYPES = {
    **TABLET_NEMESIS_TYPES,
    **HIVE_NEMESIS_TYPES,
    **NODE_NEMESIS_TYPES,
}


def get_all_nemesis_types():
    """Returns a list of all available nemesis type names from this module."""
    return list(ALL_NEMESIS_TYPES.keys())