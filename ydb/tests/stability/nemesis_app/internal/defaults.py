import signal
import logging
import random

import subprocess
import time
from ydb.tests.tools.nemesis.library import base
from ydb.tests.library.nemesis.network.client import NetworkClient


class AbstractAgentNemesis(base.AbstractMonitoredNemesis):
    """Base class for agent-based nemesis implementations. Provides fault injection/extraction infrastructure."""

    def __init__(self):
        base.AbstractMonitoredNemesis.__init__(self, scope='node')
        self.__logger = logging.getLogger(self.__class__.__name__)

    def extract_fault(self):
        """Remove the fault from the system. Override in subclasses for specific cleanup logic."""
        self.__logger.info("Extracting fault")

    def inject_fault(self):
        self.__logger.info("=== INJECT_FAULT START: %s ===", str(self))
        self.on_success_inject_fault()
        self.__logger.info("=== INJECT_FAULT SUCCESS: %s ===", str(self))

    def prepare_fault(self, hosts, config):
        """
        Determine the action (inject/extract) and target hosts for the next execution.

        Args:
            hosts: List of available hosts in the cluster
            config: Configuration dictionary with nemesis-specific settings

        Returns:
            tuple: (action, target_hosts) where action is 'inject' or 'extract',
                   and target_hosts is a list of hostnames to target

        Default behavior: Randomly selects one host for injection.
        Override in subclasses for more complex targeting logic.
        """
        if hosts:
            return 'inject', [random.choice(hosts)]
        return None, []

    @property
    def nemesis_description(self):
        """Return the docstring of the nemesis class as its description."""
        return self.__class__.__doc__


class NetworkNemesis(AbstractAgentNemesis):
    """Simulates network partitions by isolating nodes. Gradually affects up to max_affected_nodes (default: 4), then restores all."""

    def __init__(self):
        super(NetworkNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.affected_hosts = set()

    def prepare_fault(self, hosts, config):
        """
        Implements stateful network isolation logic.

        Accumulates isolated hosts up to max_affected_nodes, then performs
        a full rollback to restore all nodes simultaneously.
        """
        max_affected = config.get('max_affected_nodes', 4)

        if len(self.affected_hosts) >= max_affected:
            # Rollback all
            targets = list(self.affected_hosts)
            self.affected_hosts.clear()
            return 'extract', targets
        else:
            # Inject fault on a new random host
            available_hosts = [h for h in hosts if h not in self.affected_hosts]
            if available_hosts:
                target_host = random.choice(available_hosts)
                self.affected_hosts.add(target_host)
                return 'inject', [target_host]
            return None, []

    def inject_fault(self):
        """Isolate the node from the network by dropping all packets."""
        self.__logger.info("=== INJECT_FAULT START: %s ===", str(self))
        client = NetworkClient('localhost', port=19001, ssh_username=None)
        self.__logger.info("Isolating node...")
        client.isolate_node()
        self.on_success_inject_fault()
        self.__logger.info("=== INJECT_FAULT SUCCESS: %s ===", str(self))

    def extract_fault(self):
        """Restore network connectivity by clearing all packet drops."""
        self.__logger.info("Extracting fault")
        client = NetworkClient('localhost', port=19001, ssh_username=None)
        self.__logger.info("Restoring node...")
        client.clear_all_drops()


class KillNodeNemesis(AbstractAgentNemesis):
    """Terminates YDB node processes with SIGKILL to simulate node failures."""

    def __init__(self):
        super(KillNodeNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        """Kill the YDB node process using SIGKILL."""
        cmd = "ps aux | grep '\\--ic-port' | grep -v grep | awk '{ print $2 }' | shuf -n 1 | xargs -r sudo kill -%d" % (
            int(signal.SIGKILL),
        )
        self.__logger.info(f"Executing: {cmd}")
        subprocess.check_call(cmd, shell=True)


class ShellNemesis(AbstractAgentNemesis):
    """Executes custom shell commands for flexible fault injection scenarios."""

    def __init__(self, cmd):
        super(ShellNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.cmd = cmd

    def inject_fault(self):
        """Execute the configured shell command."""
        self.__logger.info(f"Executing: {self.cmd}")
        subprocess.check_call(self.cmd, shell=True)


class TestLongNemesis(AbstractAgentNemesis):
    """Test nemesis that runs for 150 seconds. Used for testing long-running operations and UI responsiveness."""

    def __init__(self):
        super(TestLongNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        """Run a 150-second loop with progress logging."""
        for i in range(150):
            self.__logger.info(f"Iteration: {i}")
            time.sleep(1)


class ThrowingNemesis(AbstractAgentNemesis):
    """Test nemesis that always throws an exception. Used for testing error handling."""

    def __init__(self):
        super(ThrowingNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        """Raise an exception to simulate a failed operation."""
        raise Exception('some custom exception')


PROCESS_TYPES = {
    "TestShellNemesis": {
        "runner": ShellNemesis("echo 'Type 1 process started'; sleep 1; echo 'Type 1 output' >&2; sleep 1; echo 'Type 1 finished'"),
        "schedule": 10
    },
    "TestLongNemesis": {
        "runner": TestLongNemesis(),
        "schedule": 2000
    },
    "ThrowingNemesis": {
        "runner": ThrowingNemesis(),
        "schedule": 10
    },
    "NetworkNemesis": {
        "runner": NetworkNemesis(),
        "schedule": 200
    },
    "NodeKiller": {
        "runner": KillNodeNemesis(),
        "schedule": 300
    },
}
