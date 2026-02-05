import signal
import logging
import abc
import random

import subprocess
import time
from ydb.tests.tools.nemesis.library import base
from ydb.tests.library.nemesis.network.client import NetworkClient


class AbstractAgentNemesis(base.AbstractMonitoredNemesis):
    def __init__(self):
        base.AbstractMonitoredNemesis.__init__(self, scope='node')
        self.__logger = logging.getLogger(self.__class__.__name__)

    def prepare_state(self):
        self.__logger.info("Prepare state")

    def extract_fault(self):
        self.__logger.info("Extracting fault")

    def inject_fault(self):
        self.__logger.info("=== INJECT_FAULT START: %s ===", str(self))
        self.run()
        self.on_success_inject_fault()
        self.__logger.info("=== INJECT_FAULT SUCCESS: %s ===", str(self))

    def prepare_fault(self, hosts, config):
        """
        Determines the action (inject/extract) and target hosts for the next execution.
        Returns a tuple: (action, target_hosts)
        """
        if hosts:
            return 'inject', [random.choice(hosts)]
        return None, []


class NetworkNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(NetworkNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.affected_hosts = set()

    def prepare_fault(self, hosts, config):
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
        self.__logger.info("=== INJECT_FAULT START: %s ===", str(self))
        client = NetworkClient('localhost', port=19001, ssh_username=None)
        self.__logger.info("Isolating node...")
        client.isolate_node()
        self.on_success_inject_fault()
        self.__logger.info("=== INJECT_FAULT SUCCESS: %s ===", str(self))

    def extract_fault(self):
        self.__logger.info("Extracting fault")
        client = NetworkClient('localhost', port=19001, ssh_username=None)
        self.__logger.info("Restoring node...")
        client.clear_all_drops()


class KillNodeNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(KillNodeNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        cmd = "ps aux | grep '\\--ic-port' | grep -v grep | awk '{ print $2 }' | tail -n 1 | xargs -r sudo kill -%d" % (
            int(signal.SIGKILL),
        )
        self.__logger.info(f"Executing: {cmd}")
        subprocess.check_call(cmd, shell=True)


class ShellNemesis(AbstractAgentNemesis):
    def __init__(self, cmd):
        super(ShellNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.cmd = cmd

    def inject_fault(self):
        self.__logger.info(f"Executing: {self.cmd}")
        subprocess.check_call(self.cmd, shell=True)


class TestLongNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(TestLongNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
        for i in range(150):
            self.__logger.info(f"Iteration: {i}")
            time.sleep(1)


class ThrowingNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(ThrowingNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def inject_fault(self):
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
