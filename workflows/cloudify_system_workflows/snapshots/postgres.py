#########
# Copyright (c) 2017-2019 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import os
import json
from uuid import uuid4
from contextlib import closing

import psycopg2
from psycopg2.extras import execute_values

from cloudify.workflows import ctx
from cloudify.cryptography_utils import encrypt
from cloudify.exceptions import NonRecoverableError
from cloudify.cluster_status import (
    STATUS_REPORTER_USERS,
    MANAGER_STATUS_REPORTER,
    MANAGER_STATUS_REPORTER_ID,
    BROKER_STATUS_REPORTER_ID,
    DB_STATUS_REPORTER_ID
)

from .constants import ADMIN_DUMP_FILE, LICENSE_DUMP_FILE
from .utils import run as run_shell

POSTGRESQL_DEFAULT_PORT = 5432
_STATUS_REPORTERS_QUERY_TUPLE = ', '.join(
    "'{0}'".format(reporter) for reporter in STATUS_REPORTER_USERS)

_STATUS_REPORTERS_IDS_QUERY_TUPLE = "'{0}', '{1}', '{2}'".format(
    MANAGER_STATUS_REPORTER_ID,
    DB_STATUS_REPORTER_ID,
    BROKER_STATUS_REPORTER_ID)


class Postgres(object):
    """Use as a context manager

    with Postgres(config) as postgres:
        postgres.restore()
    """
    _TRUNCATE_QUERY = "TRUNCATE {0} CASCADE;"
    _POSTGRES_DUMP_FILENAME = 'pg_data'
    _STAGE_DB_NAME = 'stage'
    _COMPOSER_DB_NAME = 'composer'
    _TABLES_TO_KEEP = ['alembic_version', 'provider_context', 'roles',
                       'licenses']
    _CONFIG_TABLES = ['config', 'rabbitmq_brokers', 'certificates', 'managers',
                      'db_nodes']
    _TABLES_TO_EXCLUDE_ON_DUMP = _TABLES_TO_KEEP + ['snapshots'] + \
        _CONFIG_TABLES
    _TABLES_TO_RESTORE = ['users', 'tenants']
    _STAGE_TABLES_TO_EXCLUDE = ['"SequelizeMeta"']
    _COMPOSER_TABLES_TO_EXCLUDE = ['"SequelizeMeta"']

    def __init__(self, config):
        self._print_postgres_config(config)
        self._bin_dir = config.postgresql_bin_path
        self._db_name = config.postgresql_db_name
        self._host = config.postgresql_host
        self._port = str(POSTGRESQL_DEFAULT_PORT)
        self._username = config.postgresql_username
        self._password = config.postgresql_password
        self._connection = None
        self.current_execution_date = None
        self.hashed_execution_token = None
        if ':' in self._host:
            self._host, self._port = self._host.split(':')
            ctx.logger.debug('Updating Postgres config: host: {0}, port: {1}'
                             .format(self._host, self._port))

    def restore(self, tempdir, premium_enabled, license=None):
        ctx.logger.info('Restoring DB from postgres dump')
        dump_file = os.path.join(tempdir, self._POSTGRES_DUMP_FILENAME)

        # Add to the beginning of the dump queries that recreate the schema
        clear_tables_queries = self._get_clear_tables_queries()
        dump_file = self._prepend_dump(dump_file, clear_tables_queries)

        # Don't change admin user during the restore or the workflow will
        # fail to correctly execute (the admin user update query reverts it
        # to the one from before the restore)
        admin_query, admin_protected_query = \
            self._get_admin_user_update_query()
        self._append_dump(dump_file, admin_query, admin_protected_query)

        self._restore_dump(dump_file, self._db_name)

        self._make_api_token_keys()

        ctx.logger.debug('Postgres restored')

    def dump(self, tempdir, include_logs, include_events):
        ctx.logger.info('Dumping Postgres, include logs {0} include events {1}'
                        .format(include_logs, include_events))
        destination_path = os.path.join(tempdir, self._POSTGRES_DUMP_FILENAME)
        admin_dump_path = os.path.join(tempdir, ADMIN_DUMP_FILE)
        try:
            if not include_logs:
                self._TABLES_TO_EXCLUDE_ON_DUMP = \
                    self._TABLES_TO_EXCLUDE_ON_DUMP + ['logs']
            if not include_events:
                self._TABLES_TO_EXCLUDE_ON_DUMP = \
                    self._TABLES_TO_EXCLUDE_ON_DUMP + ['events']
            self._dump_to_file(
                destination_path,
                self._db_name,
                exclude_tables=self._TABLES_TO_EXCLUDE_ON_DUMP
            )
            self._dump_admin_user_to_file(
                admin_dump_path,
                self._db_name,
            )
        except Exception as ex:
            raise NonRecoverableError('Error during dumping Postgres data, '
                                      'exception: {0}'.format(ex))
        self._append_delete_current_execution(destination_path)

    def dump_stage(self, tempdir):
        self._dump_db(
            tempdir=tempdir,
            database_name=self._STAGE_DB_NAME,
            exclude_tables=self._STAGE_TABLES_TO_EXCLUDE,
        )

    def dump_composer(self, tempdir):
        self._dump_db(
            tempdir=tempdir,
            database_name=self._COMPOSER_DB_NAME,
            exclude_tables=self._COMPOSER_TABLES_TO_EXCLUDE,
        )

    def _dump_db(self, tempdir, database_name, exclude_tables=()):
        if not self._db_exists(database_name):
            return
        destination_path = os.path.join(tempdir, database_name + '_data')
        try:
            self._dump_to_file(
                destination_path=destination_path,
                db_name=database_name,
                exclude_tables=exclude_tables,
            )
        except Exception as ex:
            raise NonRecoverableError(
                'Error during dumping {db_name} data. Exception: '
                '{exception}'.format(
                    db_name=database_name,
                    exception=ex,
                )
            )

    def _restore_db(self, tempdir, database_name):
        if not self._db_exists(database_name):
            return
        ctx.logger.info('Restoring {db} DB'.format(db=database_name))
        dump_file = os.path.join(tempdir, database_name + '_data')
        self._restore_dump(dump_file, database_name)
        ctx.logger.debug('{db} DB restored'.format(db=database_name))

    def restore_stage(self, tempdir):
        self._restore_db(tempdir, self._STAGE_DB_NAME)

    def restore_composer(self, tempdir):
        self._restore_db(tempdir, self._COMPOSER_DB_NAME)

    def _db_exists(self, db_name):
        """Return True if the stage DB exists"""

        exists_query = "SELECT 1 FROM pg_database " \
                       "WHERE datname='{0}'".format(db_name)
        response = self.run_query(exists_query)
        # Will either be an empty list, or a list with 1 in it
        return bool(response['all'])

    def _append_delete_current_execution(self, dump_file):
        """Append to the dump file a query that deletes the current execution
        """
        delete_current_execution_query = "DELETE FROM executions " \
                                         "WHERE id = '{0}';" \
                                         .format(ctx.execution_id)
        self._append_dump(dump_file, delete_current_execution_query)

    def _get_admin_user_update_query(self):
        """Returns a tuple of (query, print_query):
        query - updates the admin user in the DB and
        protected_query - hides the credentials for the logs file
        """
        username, password = self._get_admin_credentials()
        base_query = "UPDATE users " \
                     "SET username='{0}', password='{1}' " \
                     "WHERE id=0;"
        return (base_query.format(username, password),
                base_query.format('*'*8, '*'*8))

    @staticmethod
    def _find_reporter_role(roles_mapping, reporter_id):
        if reporter_id not in roles_mapping:
            raise NonRecoverableError('Illegal state - '
                                      'missing status reporter user\'s {0}'
                                      'roles in mapping {1}'.format(
                                        reporter_id,
                                        roles_mapping))
        return roles_mapping[reporter_id]

    def upsert_status_reporters_users(self, reporters, reporters_roles):
        """
        Handles the insertion/update of status reporters users to the manager,
        which will maintain their current api token (and not from the
        snapshot).
        :param reporters Needed user info for the current status reporter
         users.
        :param reporters_roles Mapping between user id an role id.
        """
        create_user_query = """
        INSERT INTO users (username, password, api_token_key, active, id)
        VALUES
           (
              '{0}',
              '{1}',
              '{2}',
              TRUE,
              {3}
           )
        ON CONFLICT (username) DO
        UPDATE SET password='{1}', api_token_key='{2}', active=TRUE, id={3}
        WHERE users.username = '{0}';"""

        create_user_role_query = """
        INSERT INTO users_roles (user_id, role_id)
        VALUES ({0}, {1})
        ON CONFLICT (user_id, role_id)
        DO NOTHING;
        """

        create_user_tenant_query = """
        INSERT INTO users_tenants (user_id, tenant_id, role_id)
        VALUES ({0}, 0, {1})
        ON CONFLICT (user_id, tenant_id)
        DO NOTHING;
        """
        reporters_roles = {r['user_id']: r['role_id'] for r in reporters_roles}
        queries = []
        for reporter in reporters:
            username = reporter['username']
            password = reporter['password']
            api_token_key = reporter['api_token_key']
            reporter_id = reporter['id']
            queries.append(
                create_user_query.format(username,
                                         password,
                                         api_token_key,
                                         reporter_id))
            role_id = self._find_reporter_role(reporters_roles, reporter_id)
            queries.append(
                create_user_role_query.format(
                    reporter_id,
                    role_id
                ))

            queries.append(
                create_user_tenant_query.format(
                    reporter_id,
                    role_id
                ))

        full_query = '\n'.join(queries)
        self.run_query(full_query)

    def _get_execution_restore_query(self):
        """Return a query that creates an execution to the DB with the ID (and
        other data) from the snapshot restore execution
        """
        return "INSERT INTO executions (id, created_at, " \
               "is_system_workflow, " \
               "status, workflow_id, _tenant_id, _creator_id, token) " \
               "VALUES ('{0}', '{1}', 't', 'started', 'restore_snapshot', " \
               "0, 0, '{2}');".format(ctx.execution_id,
                                      self.current_execution_date,
                                      self.hashed_execution_token)

    def dump_config_tables(self, tempdir):
        pg_dump_bin = os.path.join(self._bin_dir, 'pg_dump')
        path = os.path.join(tempdir, 'config.dump')
        command = [pg_dump_bin,
                   '-a',
                   '--host', self._host,
                   '--port', self._port,
                   '-U', self._username,
                   self._db_name,
                   '-f', path]

        for table in self._CONFIG_TABLES:
            command += ['-t', table]

        run_shell(command)
        return path

    def restore_config_tables(self, config_path):
        new_dump_file = self._prepend_dump(config_path, [
            'delete from {0};'.format(table)
            for table in self._CONFIG_TABLES
        ])
        self._restore_dump(new_dump_file, self._db_name)

    def dump_status_reporter_users(self, tempdir):
        ctx.logger.debug('Dumping status reporter users...')
        path = os.path.join(tempdir, 'status_reporter_users.dump')
        command = self.get_psql_command(self._db_name)

        query = (
            'select array_to_json(array_agg(row)) from ('
            'select * from users where id in ({0})'
            ') row;'.format(_STATUS_REPORTERS_IDS_QUERY_TUPLE)
        )
        command.extend([
            '-c', query,
            '-t',  # Dump just the data, without extra headers, etc
            '-o', path,
        ])
        run_shell(command)
        ctx.logger.debug('Dumped status reporter users to {}'.format(path))
        return path

    def dump_status_reporter_roles(self, tempdir):
        ctx.logger.debug('Dumping status reporter users\' roles')
        path = os.path.join(tempdir, 'status_reporter_roles.dump')
        command = self.get_psql_command(self._db_name)

        query = (
            'select array_to_json(array_agg(row)) from ('
            'select * from users_roles where user_id in ({0})'
            ') row;'.format(_STATUS_REPORTERS_IDS_QUERY_TUPLE)
        )
        command.extend([
            '-c', query,
            '-t',  # Dump just the data, without extra headers, etc
            '-o', path,
        ])
        run_shell(command)
        ctx.logger.debug('Dumped status reporter roles to {}'.format(path))
        return path

    @staticmethod
    def _restore_json_dump_file(dump_path):
        with open(dump_path) as dump_file:
            return json.load(dump_file)

    def restore_status_reporters(self,
                                 status_reporter_roles_path,
                                 status_reporter_users_path):
        users = self._restore_json_dump_file(status_reporter_users_path)
        roles = self._restore_json_dump_file(status_reporter_roles_path)
        self.upsert_status_reporters_users(users, roles)

    def dump_status_reporters(self, tempdir):
        status_reporter_users_path = self.dump_status_reporter_users(
            tempdir)
        status_reporter_roles_path = self.dump_status_reporter_roles(
            tempdir)
        return status_reporter_roles_path, status_reporter_users_path

    def restore_current_execution(self):
        self.run_query(self._get_execution_restore_query())

    def init_current_execution_data(self):
        response = self.run_query("SELECT created_at, token "
                                  "FROM executions "
                                  "WHERE id='{0}'".format(ctx.execution_id))
        if not response:
            raise NonRecoverableError('Illegal state - missing execution date '
                                      'for current execution')
        self.current_execution_date = response['all'][0][0]
        self.hashed_execution_token = response['all'][0][1]

    def drop_db(self):
        ctx.logger.info('Dropping db')
        drop_db_bin = os.path.join(self._bin_dir, 'dropdb')
        command = [drop_db_bin,
                   '--host', self._host,
                   '--port', self._port,
                   '-U', self._username,
                   self._db_name]
        run_shell(command)

    def create_db(self):
        ctx.logger.debug('Creating db')
        create_db_bin = os.path.join(self._bin_dir, 'createdb')
        command = [create_db_bin,
                   '--host', self._host,
                   '--port', self._port,
                   '-U', self._username,
                   '-T', 'template0',
                   self._db_name]
        run_shell(command)

    def _dump_to_file(self, destination_path, db_name, exclude_tables=None,
                      table=None):
        ctx.logger.debug('Creating db dump file: {0}, excluding: {1}'.
                         format(destination_path, exclude_tables))
        flags = []
        if exclude_tables:
            flags = ["--exclude-table={0}".format(t)
                     for t in exclude_tables]
            flags.extend(["--exclude-table-data={0}".format(t)
                          for t in exclude_tables])
        pg_dump_bin = os.path.join(self._bin_dir, 'pg_dump')
        command = [pg_dump_bin,
                   '-a',
                   '--host', self._host,
                   '--port', self._port,
                   '-U', self._username,
                   db_name,
                   '-f', destination_path]
        if table:
            command += ['--table', table]
        command.extend(flags)
        run_shell(command)

    def _dump_admin_user_to_file(self, destination_path, db_name):
        ctx.logger.debug('Dumping admin account')
        command = self.get_psql_command(db_name)

        # Hardcoded uid as we only allow running restore on a clean manager
        # at the moment, so admin must be the first user (ID=0)
        query = (
            'select row_to_json(row) from ('
            'select * from users where id=0'
            ') row;'
        )
        command.extend([
            '-c', query,
            '-t',  # Dump just the data, without extra headers, etc
            '-o', destination_path,
        ])
        run_shell(command)

    def get_psql_command(self, db_name=None):
        psql_bin = os.path.join(self._bin_dir, 'psql')
        db_name = db_name or self._db_name
        return [
            psql_bin,
            '--host', self._host,
            '--port', self._port,
            '-U', self._username,
            db_name,
        ]

    def _restore_dump(self, dump_file, db_name, table=None):
        """Execute `psql` to restore an SQL dump into the DB
        """
        ctx.logger.debug('Restoring db dump file: {0}'.format(dump_file))
        command = self.get_psql_command(db_name)
        command.extend([
            '-v', 'ON_ERROR_STOP=1',
            '--single-transaction',
            '-f', dump_file
        ])
        if table:
            command += ['--table', table]
        run_shell(command)

    @staticmethod
    def _append_dump(dump_file, query, protected_query=None):
        """
        `protected_query` is the same string as `query` only that it hides
        sensitive information, e.g. username and password.
        """
        print_query = protected_query or query
        ctx.logger.debug('Adding to end of dump: {0}'.format(print_query))
        with open(dump_file, 'a') as f:
            f.write('\n{0}\n'.format(query))

    @staticmethod
    def _prepend_dump(dump_file, queries):
        queries_str = '\n'.join(queries)
        ctx.logger.debug('Adding to beginning of dump: {0}'
                         .format(queries_str))
        pre_dump_file = '{0}.pre'.format(dump_file)
        new_dump_file = '{0}.new'.format(dump_file)
        with open(pre_dump_file, 'a') as f:
            f.write('\n{0}\n'.format(queries_str))
        # using cat command and output redirection
        # to avoid reading file content into memory (for big dumps)
        cat_content = 'cat {0} {1}'.format(pre_dump_file, dump_file)
        run_shell(command=cat_content, redirect_output_path=new_dump_file)
        return new_dump_file

    def run_query(self, query, vars=None, bulk_query=False):
        str_query = query.decode(encoding='UTF-8', errors='replace')
        str_query = str_query.replace(u"\uFFFD", "?")
        ctx.logger.debug('Running query: {0}'.format(str_query))
        with closing(self._connection.cursor()) as cur:
            try:
                if bulk_query:
                    execute_values(cur, query, vars)
                else:
                    cur.execute(query, vars)
                status_message = cur.statusmessage
                fetchall = cur.fetchall()
                result = {'status': status_message, 'all': fetchall}
                ctx.logger.debug('Running query result status: {0}'
                                 .format(status_message))
            except Exception as e:
                fetchall = None
                status_message = str(e)
                result = {'status': status_message, 'all': fetchall}
                if status_message != 'no results to fetch':
                    ctx.logger.error('Running query result status: {0}'
                                     .format(status_message))
            return result

    def _make_api_token_keys(self):
        # If this is from a snapshot that precedes token keys we need to
        # generate them
        result = self.run_query(
            "SELECT id, api_token_key FROM users"
        )

        for row in result['all']:
            uid = row[0]
            api_token_key = row[1]
            if not api_token_key:
                api_token_key = uuid4().hex
                self.run_query(
                    "UPDATE users "
                    "SET api_token_key=%s "
                    "WHERE id=%s",
                    (api_token_key, uid),
                )

    def get_deployment_creator_ids_and_tokens(self):
        result = self.run_query(
            "SELECT tenants.name, deployments.id,"
            "users.id, users.api_token_key "
            "FROM deployments, users, tenants "
            "WHERE deployments._creator_id=users.id "
            "AND tenants.id=deployments._tenant_id"
        )

        details = {}
        # Make structure the same as the deployments:
        # { 'tenant1': {'deploymentid': {info}, ...}, ...}
        for row in result['all']:
            tenant = row[0]
            deployment = row[1]
            if tenant not in details:
                details[tenant] = {}
            details[tenant][deployment] = {
                'uid': row[2],
                'token': row[3],
            }
        return details

    def encrypt_values(self, encryption_key, table_name, column_name,
                       primary_key='_storage_id'):
        """Encrypt the values of one column in a table
        """
        values = self.run_query("SELECT {0}, {1} FROM {2}".format(
            primary_key, column_name, table_name))

        # There is no relevant data in the snapshot
        if len(values['all']) < 1:
            return

        encrypted_values = []
        for value in values['all']:
            encrypted_value = encrypt(bytes(value[1]), encryption_key)
            encrypted_values.append((value[0], encrypted_value))

        update_query = """UPDATE {0}
                          SET {1} = encrypted_values.value
                          FROM (VALUES %s) AS encrypted_values ({2}, value)
                          WHERE {0}.{2} = encrypted_values.{2}""" \
            .format(table_name, column_name, primary_key)
        self.run_query(update_query, vars=encrypted_values, bulk_query=True)

    def _connect(self):
        try:
            conn = psycopg2.connect(
                database=self._db_name,
                user=self._username,
                password=self._password,
                host=self._host,
                port=self._port
            )
            conn.autocommit = True
            return conn
        except psycopg2.DatabaseError as e:
            raise Exception('Error during connection to postgres: {0}'
                            .format(str(e)))

    def __enter__(self):
        self._connection = self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._connection:
            self._connection.close()

    def _get_clear_tables_queries(self, preserve_defaults=False):
        all_tables = self._get_all_tables()
        all_tables = [table for table in all_tables if
                      table not in self._TABLES_TO_KEEP]

        queries = [self._TRUNCATE_QUERY.format(table) for table in all_tables]
        if preserve_defaults:
            self._add_preserve_defaults_queries(queries)
        return queries

    def _add_preserve_defaults_queries(self, queries):
        """Replace regular truncate queries for users/tenants with ones that
        preserve the default user (id=0)/tenant (id=0)
        Used when restoring old snapshots that will not have those entities
        :param queries: List of truncate queries
        """
        queries.remove(self._TRUNCATE_QUERY.format('users'))
        queries.append('DELETE FROM users CASCADE '
                       'WHERE id != 0 AND username NOT IN ({0});'
                       ''.format(_STATUS_REPORTERS_QUERY_TUPLE))
        queries.remove(self._TRUNCATE_QUERY.format('tenants'))
        queries.append('DELETE FROM tenants CASCADE WHERE id != 0;')

    def _get_all_tables(self):
        result = self.run_query("SELECT tablename "
                                "FROM pg_tables "
                                "WHERE schemaname = 'public';")

        # result['all'] is a list of tuples, each with a single value
        return [res[0] for res in result['all']]

    def _get_admin_credentials(self):
        response = self.run_query("SELECT username, password "
                                  "FROM users WHERE id=0")
        if not response:
            raise NonRecoverableError('Illegal state - '
                                      'missing admin user in db')
        return response['all'][0]

    def _get_status_reporters_credentials(self):
        response = self.run_query(
            "SELECT username, password, api_token_key, id "
            "FROM users WHERE username IN ({0})"
            "".format(_STATUS_REPORTERS_QUERY_TUPLE))
        if not response['all']:
            raise NonRecoverableError('Illegal state - '
                                      'missing status reporter users in db')
        return response['all']

    def dump_license_to_file(self, tmp_dir):
        destination = os.path.join(tmp_dir, LICENSE_DUMP_FILE)
        self._dump_to_file(destination, self._db_name, table='licenses')

    def restore_license_from_dump(self, tmp_dir):
        dump_file = os.path.join(tmp_dir, LICENSE_DUMP_FILE)
        self._restore_dump(dump_file, self._db_name, table='licenses')

    @staticmethod
    def _print_postgres_config(config):
        postgres_password, postgres_username = config.postgresql_password, \
                                               config.postgresql_username
        config.postgresql_password = config.postgresql_username = '********'
        ctx.logger.debug('Init Postgres config: {0}'.format(config))
        config.postgresql_password, config.postgresql_username = \
            postgres_password, postgres_username

    def get_manager_reporter_info(self):
        query = """
        SELECT
            json_build_object(
                'id', id,
                'api_token_key', api_token_key
            )
        FROM users
        WHERE username = '{0}'
        """.format(MANAGER_STATUS_REPORTER)
        result = self.run_query(query)
        return result['all'][0][0]
