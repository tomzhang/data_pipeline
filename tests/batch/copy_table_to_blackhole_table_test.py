# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import mock
import pytest

from data_pipeline.batch.copy_table_to_blackhole_table import FullRefreshRunner
from data_pipeline.batch.copy_table_to_blackhole_table import get_connection_set_from_cluster
from data_pipeline.batch.copy_table_to_blackhole_table import TopologyFile


class TestFullRefreshRunner(object):

    @pytest.fixture
    def base_path(self):
        return "data_pipeline.batch.copy_table_to_blackhole_table"

    @pytest.fixture
    def topology_path(self):
        return "/nail/srv/configs/topology.yaml"

    @pytest.fixture
    def cluster(self):
        return "test_cluster"

    @pytest.fixture
    def database_name(self):
        return "yelp"

    @pytest.fixture
    def table_name(self):
        return "test_db"

    @pytest.fixture
    def temp_name(self, table_name):
        return "{name}_data_pipeline_refresh".format(name=table_name)

    @pytest.fixture
    def show_table_query(self, table_name):
        return 'SHOW CREATE TABLE {0}'.format(table_name)

    @pytest.fixture
    def fake_query(self):
        return 'SELECT * FROM faketable'

    @pytest.fixture
    def fake_original_table(self):
        return (
            'CREATE TABLE test_db('
            'PersonID int,'
            'LastName varchar(255),'
            'FirstName varchar(255),'
            'Address varchar(255),'
            'City varchar(255))'
            'ENGINE=InnoDB'
        )

    @pytest.fixture
    def fake_new_table(self):
        return (
            'CREATE TABLE test_db_data_pipeline_refresh('
            'PersonID int,'
            'LastName varchar(255),'
            'FirstName varchar(255),'
            'Address varchar(255),'
            'City varchar(255))'
            'ENGINE=BLACKHOLE'
        )

    @pytest.yield_fixture
    def mock_load_config(self, base_path):
        with mock.patch(base_path + '.load_default_config'):
            yield

    @pytest.yield_fixture
    def refresh_batch(self, cluster, table_name, mock_load_config):
        batch = FullRefreshRunner()
        batch.process_commandline_options([
            '--dry-run',
            '--table-name={0}'.format(table_name),
            '--primary=id',
            '--cluster={0}'.format(cluster)
        ])
        batch._init_global_state()
        yield batch

    @pytest.yield_fixture
    def refresh_batch_db_option(
        self,
        database_name,
        table_name,
        mock_load_config
    ):
        batch = FullRefreshRunner()
        batch.process_commandline_options([
            '--dry-run',
            '--table-name={0}'.format(table_name),
            '--database={0}'.format(database_name)
        ])
        batch.setup_connections = mock.Mock()
        batch._init_global_state()
        yield batch

    @pytest.yield_fixture
    def refresh_batch_custom_where(self, table_name, mock_load_config):
        batch = FullRefreshRunner()
        batch.process_commandline_options([
            '--dry-run',
            '--table-name={0}'.format(table_name),
            '--primary=id',
            '--where={0}'.format("country='CA'")
        ])
        batch._init_global_state()
        yield batch

    @pytest.yield_fixture
    def _read(self, refresh_batch):
        with mock.patch.object(
            refresh_batch,
            'read_session',
            autospec=True
        ) as mock_read_session:
            yield mock_read_session

    @pytest.yield_fixture
    def _write(self, refresh_batch):
        with mock.patch.object(
            refresh_batch,
            'write_session',
            autospec=True
        ) as mock_write_session:
            yield mock_write_session

    @pytest.yield_fixture
    def _read_session(self, refresh_batch):
        with refresh_batch.read_session() as refresh_batch._read_session:
            yield

    @pytest.yield_fixture
    def _write_session(self, refresh_batch):
        with refresh_batch.write_session() as refresh_batch._write_session:
            yield

    @pytest.yield_fixture
    def sessions(
        self,
        refresh_batch,
        _read,
        _write,
        _read_session,
        _write_session
    ):
        yield

    @pytest.yield_fixture
    def mock_process_rows(self):
        with mock.patch.object(
                FullRefreshRunner,
                '_after_processing_rows'
        ) as mock_process_rows:
            yield mock_process_rows

    @pytest.yield_fixture
    def mock_row_count(self):
        with mock.patch.object(
            FullRefreshRunner,
            'total_row_count',
            new_callable=mock.PropertyMock
        ) as mock_row_count:
            yield mock_row_count

    @pytest.yield_fixture
    def mock_execute(self):
        with mock.patch.object(
            FullRefreshRunner,
            'execute_sql'
        ) as mock_execute:
            yield mock_execute

    @pytest.yield_fixture
    def mock_create_table_src(self):
        with mock.patch.object(
            FullRefreshRunner,
            'create_table_from_src_table'
        ) as mock_create:
            yield mock_create

    def test_setup_connections(
        self,
        base_path,
        refresh_batch,
        cluster,
        topology_path
    ):
        with mock.patch(
            base_path + '.RefreshTransactionManager'
        ) as mock_manager, mock.patch(
            base_path + '.get_connection_set_from_cluster'
        ) as mock_get_conn:
            refresh_batch.setup_connections()
            mock_manager.assert_called_once_with(
                cluster_name=cluster,
                topology_path=topology_path,
                connection_set_getter=mock_get_conn
            )

    def test_initial_action_no_db(
        self,
        refresh_batch,
        mock_execute,
        mock_process_rows,
        mock_create_table_src
    ):
        refresh_batch.initial_action()
        assert mock_execute.call_count == 0
        self.assert_initial_action(mock_create_table_src, mock_process_rows)

    def test_initial_action_with_db(
        self,
        database_name,
        refresh_batch_db_option,
        mock_execute,
        mock_process_rows,
        mock_create_table_src
    ):
        refresh_batch_db_option.initial_action()
        mock_execute.assert_called_once_with(
            "USE {0}".format(database_name),
            is_write_session=True
        )
        self.assert_initial_action(mock_create_table_src, mock_process_rows)

    def assert_initial_action(self, mock_create, mock_process):
        mock_create.assert_called_once_with()
        mock_process.assert_called_once_with()

    def test_final_action(self, refresh_batch, temp_name, mock_execute):
        refresh_batch.final_action()
        mock_execute.assert_called_once_with(
            'DROP TABLE IF EXISTS {0}'.format(temp_name),
            is_write_session=True
        )

    def test_after_row_processing(self, refresh_batch, sessions):
        refresh_batch._commit_changes()
        refresh_batch._read_session.rollback.assert_called_once_with()
        assert refresh_batch._write_session.commit.call_count == 0

    def test_build_select(
        self,
        refresh_batch,
        refresh_batch_custom_where,
        table_name
    ):
        offset = 0
        batch_size = refresh_batch_custom_where.options.batch_size
        expected_where_query = (
            'SELECT * FROM {origin} WHERE {clause} ORDER BY id '
            'LIMIT {offset}, {batch_size}'
        ).format(
            origin=table_name,
            clause="country='CA'",
            offset=offset,
            batch_size=batch_size
        )
        where_query = refresh_batch_custom_where.build_select(
            '*',
            'id',
            offset,
            batch_size
        )
        expected_count_query = 'SELECT COUNT(*) FROM {origin}'.format(
            origin=table_name
        )
        count_query = refresh_batch.build_select('COUNT(*)')
        assert expected_where_query == where_query
        assert expected_count_query == count_query

    def test_create_table_from_src_table(
        self,
        refresh_batch,
        fake_original_table,
        fake_new_table,
        show_table_query
    ):
        with mock.patch.object(
                refresh_batch,
                'execute_sql',
                autospec=True
        ) as mock_execute:
            mock_execute.return_value.fetchone.return_value = [
                'test_db',
                fake_original_table
            ]
            refresh_batch.create_table_from_src_table()
            calls = [
                mock.call(show_table_query, is_write_session=False),
                mock.call(fake_new_table, is_write_session=True)
            ]
            mock_execute.assert_has_calls(calls, any_order=True)

    def test_execute_sql_read(self, refresh_batch, sessions, fake_query):
        refresh_batch.execute_sql(fake_query, is_write_session=False)
        refresh_batch._read_session.execute.assert_called_once_with(
            fake_query
        )
        assert refresh_batch._write_session.execute.call_count == 0

    def test_execute_sql_write(self, refresh_batch, sessions, fake_query):
        refresh_batch.execute_sql(fake_query, is_write_session=True)
        assert refresh_batch._read_session.execute.call_count == 0
        assert refresh_batch._write_session.execute.call_count == 0

    def insert_batch_test_helper(
        self,
        batch,
        temp_name,
        table_name,
        mock_execute,
        clause
    ):
        offset = 0
        batch.insert_batch(offset)
        if clause is not None:
            query = (
                'INSERT INTO {0} SELECT * FROM {1} WHERE {2} '
                'ORDER BY id LIMIT {3}, {4}'
            ).format(
                temp_name,
                table_name,
                clause,
                offset,
                batch.options.batch_size
            )
        else:
            query = (
                'INSERT INTO {0} SELECT * FROM {1} '
                'ORDER BY id LIMIT {2}, {3}'
            ).format(
                temp_name,
                table_name,
                offset,
                batch.options.batch_size
            )
        mock_execute.assert_called_once_with(query, is_write_session=True)

    def test_insert_batch_default_where(
        self,
        refresh_batch,
        mock_execute,
        table_name,
        temp_name
    ):
        clause = None
        self.insert_batch_test_helper(
            refresh_batch,
            temp_name,
            table_name,
            mock_execute,
            clause
        )

    def test_insert_batch_custom_where(
        self,
        refresh_batch_custom_where,
        temp_name,
        table_name,
        mock_execute,
    ):
        clause = "country='CA'"
        self.insert_batch_test_helper(
            refresh_batch_custom_where,
            temp_name,
            table_name,
            mock_execute,
            clause
        )

    def test_process_table(
        self,
        refresh_batch,
        mock_row_count,
        mock_process_rows
    ):
        with mock.patch.object(
            refresh_batch,
            'insert_batch'
        ) as mock_insert, mock.patch.object(
            refresh_batch,
            'count_inserted'
        ) as mock_rows, mock.patch.object(
            refresh_batch,
            'options',
            autospec=True
        ) as mock_options:
            mock_rows.side_effect = [10, 10, 5]
            mock_options.batch_size = 10
            mock_row_count.return_value = 25
            refresh_batch.process_table()
            calls = [mock.call(0), mock.call(10), mock.call(20)]
            mock_insert.assert_has_calls(calls)

    def test_get_connection_set_from_cluster(
        self,
        base_path,
        database_name,
        topology_path
    ):
        mock_topology = mock.Mock()
        mock_conn_defs = mock.Mock()
        mock_conn_config = mock.Mock()
        with mock.patch.object(
            TopologyFile,
            'new_from_file',
            return_value=mock_topology
        ) as mock_tf, mock.patch(
            base_path + '._get_conn_defs',
            return_value=mock_conn_defs
        ) as mock_get_defs, mock.patch(
            base_path + '.ConnectionSetConfig',
            return_value=mock_conn_config
        ) as mock_init_config, mock.patch(
            base_path + '.ConnectionSet'
        ) as mock_conn:
            get_connection_set_from_cluster(database_name, topology_path)
            mock_tf.assert_called_once_with(topology_path)
            mock_get_defs.assert_called_once_with(
                mock_topology,
                database_name
            )
            mock_init_config.assert_called_once_with(
                database_name,
                mock_conn_defs,
                read_only=False
            )
            mock_conn.from_config.assert_called_once_with(mock_conn_config)
