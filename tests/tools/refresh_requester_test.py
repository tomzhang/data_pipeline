# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import pytest

from data_pipeline.schematizer_clientlib.models.refresh import Priority
from data_pipeline.tools.refresh_requester import FullRefreshRequester


class TestFullRefreshRequester(object):

    @pytest.fixture
    def source(self, namespace, registered_schema):
        return registered_schema.topic.source

    @pytest.fixture
    def refresh_requester(self, containers):
        return FullRefreshRequester()

    def test_invalid_priority(self, refresh_requester):
        with pytest.raises(KeyError):
            refresh_requester.process_commandline_options(['--priority=INVALID', "--source-id=1"])
            refresh_requester.run()

    def test_run_invalid_batch(self, refresh_requester):
        with pytest.raises(ValueError) as e:
            refresh_requester.process_commandline_options(
                [
                    '--priority=MEDIUM',
                    '--batch-size=0',
                    "--source-id=1"
                ]
            )
        assert e.value.message == "--batch-size option must be greater than 0."

    def test_run_no_source_id(self, refresh_requester):
        with pytest.raises(ValueError) as e:
            refresh_requester.process_commandline_options(
                [
                    '--priority=MEDIUM',
                    '--batch-size=50',
                ]
            )
        assert e.value.message == "--source-id or both of--source-name and --namespace must be defined"

    def test_run_only_source_name(self, refresh_requester):
        with pytest.raises(ValueError) as e:
            refresh_requester.process_commandline_options(
                [
                    '--priority=MEDIUM',
                    '--batch-size=50',
                    '--source-name=test'
                ]
            )
        assert e.value.message == "--source-id or both of--source-name and --namespace must be defined"

    def test_run_only_namespace(self, refresh_requester):
        with pytest.raises(ValueError) as e:
            refresh_requester.process_commandline_options(
                [
                    '--priority=MEDIUM',
                    '--batch-size=50',
                    '--namespace=test'
                ]
            )
        assert e.value.message == "--source-id or both of--source-name and --namespace must be defined"

    def test_valid_run(self, refresh_requester, source):
        refresh_requester.process_commandline_options(
            [
                '--source-id=' + str(source.source_id),
                '--batch-size=250',
                '--priority=MAX',
                '--offset=0'
            ]
        )
        actual_refresh = refresh_requester.create_request()
        self._check_refresh(actual_refresh, source.name, None)

    def test_valid_run_namespace_source_name(self, refresh_requester, source):
        refresh_requester.process_commandline_options(
            [
                '--source-name=' + source.name,
                '--namespace=' + source.namespace.name,
                '--batch-size=250',
                '--priority=MAX',
                '--offset=0'
            ]
        )
        actual_refresh = refresh_requester.create_request()
        self._check_refresh(actual_refresh, source.name, None)

    def test_invalid_run_namespace_source_name_not_found(self, refresh_requester, source):
        with pytest.raises(ValueError) as e:
            refresh_requester.process_commandline_options(
                [
                    '--source-name=bad_source_that_doesnt_exist',
                    '--namespace=' + source.namespace.name,
                    '--batch-size=250',
                    '--priority=MAX',
                    '--offset=0'
                ]
            )
            refresh_requester.run()
        assert "Found no sources" in e.value.message

    def test_valid_with_avg_rows_per_second_cap(self, refresh_requester, source):
        refresh_requester.process_commandline_options(
            [
                '--source-id=' + str(source.source_id),
                '--batch-size=250',
                '--priority=MAX',
                '--offset=0',
                '--avg-rows-per-second-cap=100'
            ]
        )
        actual_refresh = refresh_requester.create_request()
        self._check_refresh(actual_refresh, source.name, 100)

    def _check_refresh(self, refresh, source_name, avg_rows_per_second_cap):
        assert refresh.source_name == source_name
        assert refresh.avg_rows_per_second_cap == avg_rows_per_second_cap
        assert refresh.priority == Priority.MAX.value
        assert refresh.status.value == "NOT_STARTED"
        assert refresh.offset == 0
        assert refresh.batch_size == 250
