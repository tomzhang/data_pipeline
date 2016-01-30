# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

import re
import time
from datetime import datetime
from optparse import OptionGroup

from yelp_batch import Batch
from yelp_batch import batch_command_line_options
from yelp_batch import batch_configure
from yelp_batch._db import BatchDBMixin
from yelp_conn.connection_set import ConnectionDef
from yelp_conn.connection_set import ConnectionSet
from yelp_conn.sqlatxn import TransactionManager
from yelp_conn.topology import ConnectionSetConfig
from yelp_conn.topology import TopologyFile
from yelp_lib.classutil import cached_property
from yelp_servlib.config_util import load_default_config


class FullRefreshRunner(Batch, BatchDBMixin):
    notify_emails = ['bam+batch@yelp.com']
    is_readonly_batch = False
    DEFAULT_AVG_ROWS_PER_SECOND_CAP = 50

    @batch_command_line_options
    def define_options(self, option_parser):
        opt_group = OptionGroup(option_parser, 'Full Refresh Runner Options')
        opt_group.add_option(
            '--cluster',
            dest='cluster',
            default='refresh_primary',
            help='Required: Specifies table cluster (default: %default).'
        )
        opt_group.add_option(
            '--database',
            dest='database',
            help='Specify the database to switch to after connecting to the '
                 'cluster.'
        )
        opt_group.add_option(
            '--table-name',
            dest='table_name',
            help='Required: Name of table to be refreshed.'
        )
        opt_group.add_option(
            '--batch-size',
            dest='batch_size',
            type='int',
            default=100,
            help='Number of rows to process between commits '
                 '(default: %default).'
        )
        opt_group.add_option(
            '--primary',
            dest='primary',
            help='Required: Comma separated string of primary key column names'
        )
        opt_group.add_option(
            '--dry-run',
            action="store_true",
            dest='dry_run',
            default=False
        )
        opt_group.add_option(
            '--config-path',
            dest='config_path',
            help='Required: Config file path for FullRefreshRunner '
                 '(default: %default)',
            default='/nail/srv/configs/data_pipeline_tools.yaml'
        )
        opt_group.add_option(
            '--topology-path',
            dest='topology_path',
            help='Path to the topology.yaml file.'
                 '(default: %default)',
            default='/nail/srv/configs/topology.yaml'
        )
        opt_group.add_option(
            '--where',
            dest='where_clause',
            default=None,
            help='Custom WHERE clause to specify which rows to refresh '
                 'Note: This option takes everything that would come '
                 'after the WHERE in a sql statement. '
                 'e.g: --where="country=\'CA\' AND city=\'Waterloo\'"'
        )
        opt_group.add_option(
            '--no-start-up-replication-wait',
            dest='wait_for_replication_on_startup',
            action='store_false',
            default=True,
            help='On startup, do not wait for replication to catch up to the '
                 'time batch was started (default: %default).'
        )
        opt_group.add_option(
            '--avg-rows-per-second-cap',
            help='Caps the throughput per second. Important since without any control for this '
                 'the batch can cause signifigant pipeline delays'
                 '(default: %default)',
            type='int',
            default=self.DEFAULT_AVG_ROWS_PER_SECOND_CAP
        )
        return opt_group

    @batch_configure
    def _init_global_state(self):
        load_default_config(self.options.config_path)
        if self.options.batch_size <= 0:
            raise ValueError("Batch size should be greater than 0")
        self.db_name = self.options.cluster
        self.database = self.options.database
        if not self.database:
            raise ValueError("--database must be specified")
        self.avg_rows_per_second_cap = self.options.avg_rows_per_second_cap
        if self.avg_rows_per_second_cap <= 0:
            raise ValueError("--avg-rows-per-second-cap should be greater than 0")
        self.table_name = self.options.table_name
        self.temp_table = '{table}_data_pipeline_refresh'.format(
            table=self.table_name
        )
        self.process_row_start_time = time.time()
        self.primary_key = self.options.primary
        self.processed_row_count = 0
        self.where_clause = self.options.where_clause
        self._connection_set = None

    def setup_connections(self):
        """Creates connections to the mySQL database.

        Builds a connection set to the cluster of the table that
         you are refreshing.

        This is overriding BatchDBMixin because we want to get connections
        based on the cluster instead of by replica names.
        TransactionManager also takes a custom connection_set_getter
         function which gets a connection set by cluster and topology file.
        """
        self._txn_mgr = TransactionManager(
            cluster_name=self.db_name,
            ro_replica_name=self.db_name,
            rw_replica_name=self.db_name,
            connection_set_getter=self.get_connection_set_from_cluster
        )

    @cached_property
    def total_row_count(self):
        with self.read_session() as session:
            self._use_db(session)
            query = self.build_select('COUNT(*)')
            value = self._execute_query(session, query).scalar()
            session.rollback()
            return value if value is not None else 0

    def build_select(
            self,
            select_item,
            order_col=None,
            offset=None,
            size=None
    ):
        base_query = 'SELECT {col} FROM {origin}'.format(
            col=select_item,
            origin=self.table_name
        )
        if self.where_clause is not None:
            base_query += ' WHERE {clause}'.format(clause=self.where_clause)
        if order_col is not None:
            base_query += ' ORDER BY {order}'.format(order=order_col)
        if offset is not None and size is not None:
            base_query += ' LIMIT {offset}, {size}'.format(
                offset=offset,
                size=size
            )
        return base_query

    def _wait_for_replication(self):
        """Lets first wait for ro_conn replication to catch up with the
        batch start time.
        """
        if self.options.wait_for_replication_on_startup:
            self.log.info(
                "Waiting for ro_conn replication to catch up with start time "
                "{start_time}".format(
                    start_time=datetime.fromtimestamp(self.starttime)
                )
            )
            with self.ro_conn() as ro_conn:
                self.wait_for_replication_until(self.starttime, ro_conn)

    def create_table_from_src_table(self, session):
        show_create_statement = 'SHOW CREATE TABLE {table_name}'.format(
            table_name=self.table_name
        )
        original_query = self._execute_query(
            session,
            show_create_statement
        ).fetchone()[1]
        max_replacements = 1
        refresh_table_create_query = original_query.replace(
            self.table_name,
            self.temp_table,
            max_replacements
        )
        # Substitute original engine with Blackhole engine
        refresh_table_create_query = re.sub(
            'ENGINE=[^\s]*',
            'ENGINE=BLACKHOLE',
            refresh_table_create_query
        )
        self.log.info("New blackhole table query: {query}".format(
            query=refresh_table_create_query
        ))
        self._execute_query(session, refresh_table_create_query)

    def _after_processing_rows(self, session, count):
        """Commits changes and makes sure replication and throughput cap
        catches up before moving on.
        """
        self._commit(session)

        # This code may seem counter-intuitive, but it's actually correct.
        # Tables shouldn't be unlocked until after the data changes have either
        # been committed or rolled back.  Committing or rolling back, does not,
        # however, unlock the locked tables.  The tables must be unlocked before
        # throttling to replication because failing to unlock them won't allow
        # replication to proceed, preventing replication from ever catching up.
        # session.commit() is called instead of _commit, since unlock tables
        # should be issued regardless of dry_run mode - it's necessary here
        # because sqlalchemy won't send the statement to MySQL without it.
        self._execute_query(session, 'UNLOCK TABLES')
        session.commit()

        with self.rw_conn() as rw_conn:
            self.throttle_to_replication(rw_conn)

        self._wait_for_throughput(count)

    def _wait_for_throughput(self, count):
        """Used to cap throughput when given the --avg-rows-per-second-cap flag.
        Sleeps for a certain amount of time based on elapsed time to process row, the number of rows processed (count)
        and the given cap so that the flag is enforced"""
        process_row_end_time = time.time()
        elapsed_time = process_row_end_time - self.process_row_start_time
        desired_elapsed_time = 1.0 / self.avg_rows_per_second_cap * count
        time_to_wait = max(desired_elapsed_time - elapsed_time, 0.0)
        self.log.info("Waiting for {} seconds to enforce avg throughput cap".format(time_to_wait))
        time.sleep(time_to_wait)

    def initial_action(self):
        self._wait_for_replication()
        with self.write_session() as session:
            self._use_db(session)
            self.create_table_from_src_table(session)
            self._commit(session)

    def _use_db(self, session):
        self._execute_query(
            session,
            "USE {database}".format(database=self.database)
        )

    def _commit(self, session):
        """Commits unless in dry_run mode, otherwise rolls back"""
        if self.options.dry_run:
            self.log.info("Executing rollback in dry-run mode")
            session.rollback()
        else:
            session.commit()

    def final_action(self):
        with self.write_session() as session:
            self._use_db(session)
            query = 'DROP TABLE IF EXISTS {temp_table}'.format(
                temp_table=self.temp_table
            )
            self._execute_query(session, query)
            self.log.info("Dropped table: {table}".format(table=self.temp_table))
            self._commit(session)

    def setup_transaction(self, session):
        self._use_db(session)
        self._execute_query(
            session,
            'LOCK TABLES {table} WRITE, {temp} WRITE'.format(
                table=self.table_name,
                temp=self.temp_table
            )
        )

    def count_inserted(self, session, offset):
        select_query = self.build_select(
            '*',
            self.primary_key,
            offset,
            self.options.batch_size
        )
        query = 'SELECT COUNT(*) FROM ({query}) AS T'.format(
            query=select_query
        )
        inserted_rows = self._execute_query(session, query)
        return inserted_rows.scalar()

    def insert_batch(self, session, offset):
        insert_query = 'INSERT INTO {temp} '.format(temp=self.temp_table)
        select_query = self.build_select(
            '*',
            self.primary_key,
            offset,
            self.options.batch_size
        )
        insert_query += select_query
        self._execute_query(session, insert_query)

    def process_table(self):
        self.log.info(
            "Total rows to be processed: {row_count}".format(
                row_count=self.total_row_count
            )
        )
        offset = 0
        count = self.options.batch_size
        while count >= self.options.batch_size:
            self.process_row_start_time = time.time()
            with self.write_session() as session:
                self.setup_transaction(session)
                count = self.count_inserted(session, offset)
                self.insert_batch(session, offset)
                self._after_processing_rows(session, count)
            offset += count
            self.processed_row_count += count

    def log_info(self):
        elapsed_time = time.time() - self.starttime
        self.log.info(
            "Processed {row_count} row(s) in {elapsed_time}".format(
                row_count=self.processed_row_count,
                elapsed_time=elapsed_time
            )
        )

    def run(self):
        try:
            self.initial_action()
            self.process_table()
            self.log_info()
        finally:
            self.final_action()

    def get_connection_set_from_cluster(self, cluster):
        """Given a cluster name, returns a connection to that cluster.
        """
        if self._connection_set:
            return self._connection_set
        topology = TopologyFile.new_from_file(self.options.topology_path)
        conn_defs = self._get_conn_defs(topology, cluster)
        conn_config = ConnectionSetConfig(cluster, conn_defs, read_only=False)
        self._connection_set = ConnectionSet.from_config(conn_config)
        return self._connection_set

    def _get_conn_defs(self, topology, cluster):
        replica_level = 'master'
        connection_cluster = topology.topologies[cluster, replica_level]
        conn_def = ConnectionDef(
            cluster,
            replica_level,
            auto_commit=False,
            database=connection_cluster.database
        )
        return {cluster: (conn_def, connection_cluster)}

    def _execute_query(self, session, query):
        self.log.debug("Executing query: {query}".format(query=query))
        return session.execute(query)


if __name__ == '__main__':
    FullRefreshRunner().start()