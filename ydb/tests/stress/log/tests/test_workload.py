# -*- coding: utf-8 -*-
import pytest

from ydb.tests.library.stress.fixtures import StressFixture
from ydb.tests.stress.log.workload.workload_log import YdbLogWorkload


class TestYdbLogWorkload(StressFixture):
    @pytest.fixture(autouse=True, scope="function")
    def setup(self):
        yield from self.setup_cluster(
            column_shard_config={
                'disabled_on_scheme_shard': False,
            })

    @pytest.mark.parametrize('store_type', ['row', 'column'])
    def test(self, store_type):
        workload = YdbLogWorkload(self.endpoint, self.database, store_type, f'log_{store_type}')

        workload.start()
        workload.join()
