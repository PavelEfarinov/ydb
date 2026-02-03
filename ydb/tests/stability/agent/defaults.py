import signal
import logging
import abc
import six

import subprocess
import time
from ydb.tests.tools.nemesis.library import base
from ydb.tests.library.nemesis.network.client import NetworkClient


@six.add_metaclass(abc.ABCMeta)
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

    @abc.abstractmethod
    def run(self):
        pass


class NetworkNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(NetworkNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def run(self):
        with NetworkClient('localhost', port=19001, ssh_username=None) as client:
            self.__logger.info("Isolating node...")
            client.isolate_node()
            time.sleep(60)
            self.__logger.info("Restoring node...")
            # Context manager handles cleanup (clear_all_drops)


class KillNodeNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(KillNodeNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def run(self):
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

    def run(self):
        self.__logger.info(f"Executing: {self.cmd}")
        subprocess.check_call(self.cmd, shell=True)


class TestLongNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(TestLongNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def run(self):
        for i in range(150):
            self.__logger.info(f"Iteration: {i}")
            time.sleep(1)


class ThrowingNemesis(AbstractAgentNemesis):
    def __init__(self):
        super(ThrowingNemesis, self).__init__()
        self.__logger = logging.getLogger(self.__class__.__name__)

    def run(self):
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
