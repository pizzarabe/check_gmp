#!/usr/bin/python3
# -*- coding: utf-8 -*-
# GMP Nagios Command Plugin
#
# Description: A nagios command plugin for the Greenbone Management Protocol
#
# Authors:
# Raphael Grewe <raphael.grewe@greenbone.net>
#
# Copyright:
# Copyright (C) 2017 Greenbone Networks GmbH
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import os
import sys
import sqlite3
import tempfile
import signal

from argparse import RawTextHelpFormatter
from datetime import datetime
from lxml import etree
from gmp.gvm_connection import (SSHConnection,
                                TLSConnection,
                                UnixSocketConnection)

__version__ = '1.0.5'

logger = logging.getLogger(__name__)

help_text = """
    Check-GMP Nagios Command Plugin {version} (C) 2017 Greenbone Networks GmbH

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
    """.format(version=__version__)

NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3

NAGIOS_MSG = ['OK', 'WARNING', 'CRITICAL', 'UNKNOWN']

MAX_RUNNING_INSTANCES = 10

global args
args = None

global conn
conn = None

global im
im = None

tmp_path = '%s/check_gmp/' % tempfile.gettempdir()
tmp_path_db = tmp_path + 'reports.db'


class InstanceManager:
    '''Class that manage instances of this plugin

    All new reports will be cached in a sqlite database.
    The first call with a unknown host takes longer,
    because the gvm has to generate the report.
    Second call will retrieve the data from the database if the scanend
    time not differs.

    Addtionally this class handles all instances of check_gmp. No more than
    MAX_RUNNING_INSTANCES can run simultaneously. Other instances are stopped
    and wait for continuation.
    '''

    def __init__(self, path):
        '''Initialise the sqlite database.

        Create it if it does not exist else connect to it.

        Arguments:
            path {string} -- Path to the database
        '''
        self.cursor = None
        self.con_db = None
        self.path = path
        self.pid = os.getpid()

        # Try to read file with informations about cached reports
        # First check whether the file exist or not
        exist = os.path.isfile(path)
        logger.debug('DB file exist?: %s ' % exist)

        if not exist:
            os.makedirs(os.path.dirname(path), exist_ok=True)

            # Create the db file
            open(path, 'a').close()

            # Connect to db
            self.connect_db()

            # Create the tables
            self.cursor.execute('''CREATE TABLE Report(
                host text,
                scan_end text,
                params_used text,
                report text
            )''')

            self.cursor.execute('''CREATE TABLE Instance(
                created_at text,
                pid integer,
                pending integer default 0
            )''')

            logger.debug('Tables created')
        else:
            self.connect_db()

    def connect_db(self):
        '''Connect to the databse

        Simply connect to the database at location <path>
        '''
        try:
            logger.debug('connect db: %s' % self.path)
            self.con_db = sqlite3.connect(self.path)
            self.cursor = self.con_db.cursor()
            logger.debug(sqlite3.sqlite_version)
        except Exception as e:
            logger.debug(e)

    def close_db(self):
        '''Close database
        '''
        self.con_db.close()

    def set_host(self, host):
        '''Sets the host variable

        Arguments:
            host {string} -- Given ip or hostname of target
        '''
        self.host = host

    def old_report(self, last_scan_end, params_used):
        '''Decide whether the current report is old or not

        At first the last scanend and the params that were used are fetched
        from the database. If no report is fetched, then True will be returned.
        The next step is to compare the old and the new scanend.
        If the scanends matches, then return False, because it is the same
        report. Else the old report will be deleted.

        Arguments:
            last_scan_end {string} -- Last scan end of report
            params_used {string} -- Params used for this check

        Returns:
            bool -- True: An old report or empty, False: It is the same report
        '''

        # Before we do anything here, check existing instance

        # Retrieve the scan_end value
        self.cursor.execute('SELECT scan_end, params_used FROM Report WHERE'
                            ' host=?', (self.host,))
        db_entry = self.cursor.fetchone()

        logger.debug('%s %s' % (db_entry, last_scan_end))

        if not db_entry:
            return True
        else:
            old = parse_date(db_entry[0])
            new = parse_date(last_scan_end)

            logger.debug(
                'Old time (from db): %s\n'
                'New time (from rp): %s' % (old, new))

            if new <= old and params_used == db_entry[1]:
                return False
            else:
                # Report is newer. Delete old entry.
                logger.debug('Delete old report for host %s' % self.host)
                self.delete_report()
                return True

    def load_local_report(self):
        '''Load report from local database

        Select the report from the database according due the hostname or ip.

        Returns:
            [obj] -- lxml object
        '''
        self.cursor.execute(
            'SELECT report FROM Report WHERE host=?', (self.host,))
        db_entry = self.cursor.fetchone()

        if db_entry:
            return etree.fromstring(db_entry[0])
        else:
            logger.debug('Report from host %s is not in the db' % self.host)

    def add_report(self, scan_end, params_used, report):
        '''Create new entry with the lxml report

        Create a string from the lxml object and add it to the database.
        Additional data is the scanend and the params used.

        Arguments:
            scan_end {string} -- Scan end of the report
            params_used {string} -- Params used for this check
            report {obj} -- lxml object
        '''

        data = etree.tostring(report)

        logger.debug(
            'add_report: %s, %s, %s' % (self.host, scan_end, params_used))

        # Insert values
        self.cursor.execute('INSERT INTO Report VALUES (?, ?, ?, ?)',
                            (self.host, scan_end, params_used, data))

        # Save the changes
        self.con_db.commit()

    def delete_report(self):
        '''Delete report from database
        '''
        self.cursor.execute('DELETE FROM Report WHERE host=?', (self.host,))

        # Save the changes
        self.con_db.commit()

    def delete_entry_with_ip(self, ip):
        '''Delete report from database with given ip

        Arguments:
            ip {string} -- IP-Adress
        '''
        logger.debug('Delete entry with ip: %s' % ip)
        self.cursor.execute('DELETE FROM Report WHERE host=?', (ip,))
        self.cursor.execute('VACUUM')

        # Save the changes
        self.con_db.commit()

    def delete_older_entries(self, days):
        '''Delete reports from database older than given days

        Arguments:
            days {int} -- Number of days in past
        '''
        logger.debug('Delete entries older than: %s days' % days)
        self.cursor.execute('DELETE FROM Report WHERE scan_end <= '
                            'date("now", "-%s day")' % days)
        self.cursor.execute('VACUUM')

        # Save the changes
        self.con_db.commit()

    def has_entries(self, pending):
        '''Return number of instance entries

        Return the number of pending or non pending instances entries.
        '''
        self.cursor.execute(
            'SELECT count(*) FROM Instance WHERE pending=?', (pending,))

        res = self.cursor.fetchone()

        return res[0]

    def check_instances(self):
        '''This method check the status of check_gmp instances

        Check whether instances are pending or not and start instances
        according to the number saved in the MAX_RUNNING_INSTANCES variable.
        '''

        # Need to check whether any instances are in the database that were
        # killed f.e. because a restart of nagios
        self.clean_orphaned_instances()

        # How many processes are currently running?
        number_instances = self.has_entries(pending=0)

        # How many pending entries are waiting?
        number_pending_instances = self.has_entries(pending=1)

        logger.debug('check_instances: %i %i' % (
            number_instances, number_pending_instances))

        if number_instances < MAX_RUNNING_INSTANCES and \
                number_pending_instances == 0:
            # Add entry for running process and go on
            logger.debug('Fall 1')
            self.add_instance(pending=0)

        elif number_instances < MAX_RUNNING_INSTANCES and \
                number_pending_instances > 0:
            # Change pending entries and wake them up until enough instances
            # are running
            logger.debug('Fall 2')

            while (number_instances < MAX_RUNNING_INSTANCES and
                    number_pending_instances > 0):
                pending_entries = self.get_oldest_pending_entries(
                    MAX_RUNNING_INSTANCES - number_instances)

                logger.debug('Oldest pending pids: %s' % (pending_entries))

                for entry in pending_entries:
                    created_at = entry[0]
                    pid = entry[1]

                    # Change status to not pending and continue the process
                    self.update_pending_status(created_at, False)
                    self.start_process(pid)

                # Refresh number of instances for next while loop
                number_instances = self.has_entries(pending=0)
                number_pending_instances = self.has_entries(pending=1)

            # TODO: Check if this is really necessary
            # self.add_instance(pending=0)
            # if number_instances >= MAX_RUNNING_INSTANCES:
                # self.stop_process(self.pid)

        elif number_instances >= MAX_RUNNING_INSTANCES and \
                number_pending_instances == 0:
            # There are running enough instances and no pending instances
            # Add new entry with pending status true and stop this instance
            logger.debug('Fall 3')
            self.add_instance(pending=1)
            self.stop_process(self.pid)

        elif number_instances >= MAX_RUNNING_INSTANCES and \
                number_pending_instances > 0:
            # There are running enough instances and there are min one
            # pending instance
            # Add new entry with pending true and stop this instance
            logger.debug('Fall 4')
            self.add_instance(pending=1)
            self.stop_process(self.pid)

        # If an entry is pending and the same params at another process is
        # starting, then exit with gmp pending since data
        # if self.has_pending_entries():
            # Check if an pending entry is the same as this process
            # If hostname
        #    date = datetime.now()
        #    end_session('GMP PENDING: since %s' % date, NAGIOS_OK)
        #    end_session('GMP RUNNING: since', NAGIOS_OK)

    def add_instance(self, pending):
        '''Add new instance entry to database

        Retrieve the current time in ISO 8601 format. Create a new entry with
        pending status and the dedicated pid

        Arguments:
            pending {int} -- State of instance
        '''
        current_time = datetime.now().isoformat()

        # Insert values
        self.cursor.execute('INSERT INTO Instance VALUES (?, ?, ?)',
                            (current_time, self.pid, pending))

        # Save the changes
        self.con_db.commit()

    def get_oldest_pending_entries(self, number):
        '''Return the oldest last entries of pending entries from database

        Return the oldest instances with status pending limited by the variable
        <number>
        '''
        self.cursor.execute('SELECT * FROM Instance WHERE pending=1 ORDER BY '
                            'created_at LIMIT ? ', (number,))
        return self.cursor.fetchall()

    def update_pending_status(self, date, status):
        '''Update pending status of instance

        The date variable works as a primary key for the instance table.
        The entry with date get his pending status updated.

        Arguments:
            date {string} -- Date of creation for entry
            status {int} -- Status of instance
        '''
        self.cursor.execute('UPDATE Instance SET pending=? WHERE created_at=?',
                            (status, date))

        # Save the changes
        self.con_db.commit()

    def delete_instance(self, pid=0):
        '''Delete instance from database

        If a pid different from zero is given, then delete the entry with
        given pid. Else delete the entry with the pid stored in this class
        instance.

        Keyword Arguments:
            pid {number} -- Process Indentificattion Number (default: {0})
        '''
        if not pid:
            pid = self.pid

        logger.debug('Delete entry with pid: %i' % (pid))
        self.cursor.execute('DELETE FROM Instance WHERE pid=?', (pid,))

        # Save the changes
        self.con_db.commit()

    def clean_orphaned_instances(self):
        '''Delete non existing instance entries

        This method check whether a pid exist on the os and if not then delete
        the orphaned entry from database.
        '''
        self.cursor.execute('SELECT pid FROM Instance')

        pids = self.cursor.fetchall()

        for pid in pids:
            if not self.check_pid(pid[0]):
                self.delete_instance(pid[0])

    def wake_instance(self):
        '''Wake up a pending instance

        This method is called at the end of any session from check_gmp.
        Get the oldest pending entries and wake them up.
        '''
        # How many processes are currently running?
        number_instances = self.has_entries(pending=0)

        # How many pending entries are waiting?
        number_pending_instances = self.has_entries(pending=1)

        if (number_instances < MAX_RUNNING_INSTANCES and
                number_pending_instances > 0):

            pending_entries = self.get_oldest_pending_entries(
                MAX_RUNNING_INSTANCES - number_instances)

            logger.debug('wake_instance: %i %i' % (
                number_instances, number_pending_instances))

            for entry in pending_entries:
                created_at = entry[0]
                pid = entry[1]
                # Change status to not pending and continue the process
                self.update_pending_status(created_at, False)
                self.start_process(pid)

    def start_process(self, pid):
        '''Continue a stopped process

        Send a continue signal to the process with given pid

        Arguments:
            pid {int} -- Process Identification Number
        '''
        logger.debug('Continue pid: %i' % (pid))
        os.kill(pid, signal.SIGCONT)

    def stop_process(self, pid):
        '''Stop a running process

        Send a stop signal to the process with given pid

        Arguments:
            pid {int} -- Process Identification Number
        '''
        os.kill(pid, signal.SIGSTOP)

    def check_pid(self, pid):
        '''Check for the existence of a process.

        Arguments:
            pid {int} -- Process Identification Number
        '''
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        else:
            return True


def main():
    parser = argparse.ArgumentParser(
        prog='check_gmp',
        description=help_text,
        formatter_class=RawTextHelpFormatter,
        add_help=False,
        epilog="""
usage: check_gmp [-h] [--version] [connection_type] ...
   or: check_gmp connection_type --help""")
    subparsers = parser.add_subparsers(metavar='[connection_type]')
    subparsers.required = False
    subparsers.dest = 'connection_type'

    parser.add_argument(
        '-h', '--help', action='help',
        help='Show this help message and exit.')

    parser.add_argument(
        '-V', '--version', action='version',
        version='%(prog)s {version}'.format(version=__version__),
        help='Show program\'s version number and exit')

    parser.add_argument(
        '-I', '--max-running-instances', default=10, type=int,
        help='Set the maximum simultaneous processes of check_gmp')

    parser.add_argument(
        '--cache', nargs='?', default=tmp_path_db,
        help='Path to cache file. Default: %s.' % tmp_path_db)

    parser.add_argument(
        '--clean', action='store_true',
        help='Activate to clean the database.')

    group = parser.add_mutually_exclusive_group(required=False)

    group.add_argument(
        '--days', type=int, help='Delete database entries that are older than'
        ' given days.')

    group.add_argument(
        '--ip', help='Delete database entry for given ip.')

    # TODO: Werror: Turn status UNKNOWN into status CRITICAL
    parent_parser = argparse.ArgumentParser(add_help=False)

    parent_parser.add_argument(
        '-u', '--gmp-username', help='GMP username.', required=False)

    parent_parser.add_argument(
        '-w', '--gmp-password', help='GMP password.', required=False)

    parent_parser.add_argument(
        '-F', '--hostaddress', required=False, default='',
        help='Report last report status of host <ip>.')

    parent_parser.add_argument(
        '-T', '--task', required=False,
        help='Report status of task <task>.')

    group = parent_parser.add_mutually_exclusive_group(required=False)

    group.add_argument(
        '--ping', action='store_true',
        help='Ping the gsm appliance.')

    group.add_argument(
        '--status', action='store_true',
        help='Report status of task.')

    group = parent_parser.add_mutually_exclusive_group(required=False)

    group.add_argument(
        '--trend', action='store_true',
        help='Report status by trend.')

    group.add_argument(
        '--last-report', action='store_true',
        help='Report status by last report.')

    parent_parser.add_argument(
        '--apply-overrides', action='store_true',
        help='Apply overrides.')

    parent_parser.add_argument(
        '--overrides', action='store_true',
        help='Include overrides.')

    parent_parser.add_argument(
        '-d', '--details', action='store_true',
        help='Include connection details in output.')

    parent_parser.add_argument(
        '-l', '--report-link', action='store_true',
        help='Include URL of report in output.')

    parent_parser.add_argument(
        '--dfn', action='store_true',
        help='Include DFN-CERT IDs on vulnerabilities in output.')

    parent_parser.add_argument(
        '--oid', action='store_true',
        help='Include OIDs of NVTs finding vulnerabilities in output.')

    parent_parser.add_argument(
        '--descr', action='store_true',
        help='Include descriptions of NVTs finding vulnerabilities in output.')

    parent_parser.add_argument(
        '--showlog', action='store_true',
        help='Include log messages in output.')

    parent_parser.add_argument(
        '--show-ports', action='store_true',
        help='Include port of given vulnerable nvt in output.')

    parent_parser.add_argument(
        '--scanend', action='store_true',
        help='Include timestamp of scan end in output.')

    parent_parser.add_argument(
        '--autofp', type=int, choices=[0, 1, 2], default=0,
        help='Trust vendor security updates for automatic false positive'
        ' filtering (0=No, 1=full match, 2=partial).')

    parent_parser.add_argument(
        '-e', '--empty-as-unknown', action='store_true',
        help='Respond with UNKNOWN on empty results.')

    parent_parser.add_argument(
        '-A', '--use-asset-management', action='store_true',
        help='Request host status via Asset Management.')

    parser_ssh = subparsers.add_parser(
        'ssh', help='Use SSH connection for gmp service.',
        parents=[parent_parser])

    parser_ssh.add_argument(
        '--hostname', '-H', required=True, help='Hostname or IP-Address.')

    parser_ssh.add_argument(
        '--port', required=False, default=22, help='Port. Default: 22.')

    parser_ssh.add_argument(
        '--ssh-user', default='gmp', help='SSH Username. Default: gmp.')

    parser_tls = subparsers.add_parser(
        'tls', help='Use TLS secured connection for gmp service.',
        parents=[parent_parser])

    parser_tls.add_argument(
        '--hostname', '-H', required=True, help='Hostname or IP-Address.')

    parser_tls.add_argument(
        '--port', required=False, default=9390, help='Port. Default: 9390.')

    parser_socket = subparsers.add_parser(
        'socket', help='Use UNIX-Socket connection for gmp service.',
        parents=[parent_parser])

    parser_socket.add_argument(
        '--sockpath', nargs='?', default='/usr/local/var/run/openvasmd.sock',
        help='UNIX-Socket path. Default: /usr/local/var/run/openvasmd.sock.')

    # Set arguments that every parser should have
    for p in [parser, parser_ssh, parser_socket, parser_tls]:
        p.add_argument(
            '--timeout', required=False, default=60, type=int,
            help='Wait <seconds> for response. Default: 60')

        p.add_argument(
            '--log', nargs='?', dest='loglevel', const='INFO',
            choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
            help='Activates logging. Default level: INFO.')

    global args
    args = parser.parse_args()

    # Set the max running instances variable
    if args.max_running_instances:
        global MAX_RUNNING_INSTANCES
        MAX_RUNNING_INSTANCES = args.max_running_instances
    # Sets the logging
    if args.loglevel is not None:
        # level = logging.getLevelName(args.loglevel)
        logging.basicConfig(filename='check_gmp.log', level=args.loglevel)

    # Set the report manager
    global im
    im = InstanceManager(args.cache)

    # Check if command holds clean command
    if args.clean:
        if args.ip:
            logger.info('Delete entry with ip %s' % args.ip)
            im.delete_entry_with_ip(args.ip)
        elif args.days:
            logger.info('Delete entries older than %s days' % args.days)
            im.delete_older_entries(args.days)
        sys.exit(1)

    # Set the host
    im.set_host(args.hostaddress)

    # Check if no more than 10 instances of check_gmp runs simultaneously
    im.check_instances()

    try:
        global conn
        conn = connect(args.hostname, args.port)
    except Exception as e:
        end_session('GMP CRITICAL: %s' % str(e), NAGIOS_CRITICAL)

    if args.ping:
        ping()

    # Get the gmp version number, so i can choose the right functions
    version = conn.get_version().xpath('version/text()')

    if version:
        version = float(version[0])
    else:
        version = 6.0

    try:
        conn.authenticate(args.gmp_username, args.gmp_password)
    except Exception as e:
        end_session('GMP CRITICAL:  %s' % str(e), NAGIOS_CRITICAL)

    if args.status:
        status(version)

    conn.close()


def connect(hostname, port):
    '''Connect to gvm

    According due the chosen connection type, this method will connect
    to the manager.

    Arguments:
        hostname {string} -- Hostname or ip
        port {int} -- Port

    Returns:
        GVMConnection -- Instance from GVMConnection class
    '''
    if 'socket' in args.connection_type:
        try:
            return UnixSocketConnection(
                sockpath=args.sockpath, shell_mode=True, timeout=args.timeout)
        except OSError as e:
            end_session(
                'GMP CRITICAL:  %s: %s' %
                (str(e), args.sockpath), NAGIOS_CRITICAL)

    elif 'tls' in args.connection_type:
        try:
            return TLSConnection(hostname=args.hostname, port=args.port,
                                 timeout=args.timeout, shell_mode=True)
        except OSError as e:
            end_session(
                'GMP CRITICAL:  %s: Host: %s Port: %s' %
                (str(e), args.hostname, args.port), NAGIOS_CRITICAL)

    elif 'ssh' in args.connection_type:
        try:
            return SSHConnection(hostname=args.hostname, port=args.port,
                                 timeout=args.timeout, ssh_user=args.ssh_user,
                                 ssh_password='', shell_mode=True)
        except Exception as e:
            end_session(
                'GMP CRITICAL:  %s: Host: %s Port: %s' %
                (str(e), args.hostname, args.port), NAGIOS_CRITICAL)


def ping():
    '''Checks for connectivity

    This function sends the get_version command and checks whether the status
    is ok or not.
    '''
    version = conn.get_version()
    status = version.xpath('@status')

    if '200' in status:
        end_session('GMP OK: Ping successful', NAGIOS_OK)
    else:
        end_session('GMP CRITICAL: Machine dead?', NAGIOS_CRITICAL)


def status(version):
    '''Returns the current status of a host

    This functions return the current state of a host.
    Either directly over the asset management or within a task.

    For a task you can explicitly ask for the trend.
    Otherwise the last report of the task will be filtered.

    In the asset management the report id in the details is taken
    as report for the filter.
    If the asset information contains any vulnerabilities, then will the
    report be filtered too. With additional parameters it is possible to add
    more information about the vulnerabilities.

    * DFN-Certs
    * Logs
    * Autofp
    * Scanend
    * Overrides
    '''
    params_used = 'task=%s autofp=%i overrides=%i apply_overrides=%i' \
        % (args.task, args.autofp, int(args.overrides), int(args.apply_overrides))

    if args.use_asset_management:
        report = conn.get_reports(
            type='assets', host=args.hostaddress,
            filter='sort-reverse=id result_hosts_only=1 min_cvss_base= '
                   'min_qod= levels=hmlgd autofp=%s notes=0 apply_overrides=%s overrides=%s'
                   ' first=1 rows=-1 delta_states=cgns'
                   % (args.autofp, int(args.apply_overrides), int(args.overrides)))

        report_id = report.xpath(
            'report/report/host/detail/name[text()="report/@id"]/../value/'
            'text()')

        last_scan_end = report.xpath('report/report/host/end/text()')

        if last_scan_end:
            last_scan_end = last_scan_end[0]

        if report_id:
            report_id = report_id[0]
        else:
            end_session('GMP UNKNOWN: Failed to get report_id'
                        ' via Asset Management', NAGIOS_UNKNOWN)

        low_count = int(report.xpath(
            'report/report/host/detail/name[text()="report/result_count/'
            'low"]/../value/text()')[0])
        medium_count = int(report.xpath(
            'report/report/host/detail/name[text()="report/result_count/'
            'medium"]/../value/text()')[0])
        high_count = int(report.xpath(
            'report/report/host/detail/name[text()="report/result_count/'
            'high"]/../value/text()')[0])

        if medium_count + high_count == 0:
            print('GMP OK: %i vulnerabilities found - High: 0 Medium: 0 '
                  'Low: %i' % (low_count, low_count))

            if args.report_link:
                print('https://%s/omp?cmd=get_report&report_id=%s' %
                      (args.hostname, report_id))

            if args.scanend:
                end = report.xpath('//end/text()')[0]
                print('SCAN_END: %s' % end)

            end_session('|High=%i Medium=%i Low=%i' %
                        (high_count, medium_count, low_count), NAGIOS_OK)

        else:
            full_report = None
            # if last_scan_end is newer then add the report to db
            if im.old_report(last_scan_end, params_used):
                levels = ''
                autofp = ''

                if not args.showlog:
                    levels = 'levels=hml'

                if args.autofp:
                    autofp = 'autofp=%i' % args.autofp

                full_report = conn.get_reports(
                    report_id=report_id,
                    filter='sort-reverse=id result_hosts_only=1 '
                           'min_cvss_base= min_qod= notes=0 apply_overrides=%s overrides=%s '
                           'first=1 rows=-1 delta_states=cgns %s %s =%s'
                           % (int(args.apply_overrides), int(args.overrides), autofp, levels,
                              args.hostaddress))

                full_report = full_report.xpath('report/report')

                if not full_report:
                    end_session('GMP UNKNOWN: Failed to get results list.',
                                NAGIOS_UNKNOWN)

                full_report = full_report[0]

                im.add_report(last_scan_end, params_used, full_report)
                logger.debug('Report added to db')
            else:
                full_report = im.load_local_report()

            filter_report(full_report)

    if args.task:
        task = conn.get_tasks(filter='permission=any owner=any rows=1 '
                                     'name=\"%s\"' % args.task)
        if args.trend:
            trend = task.xpath('task/trend/text()')

            if not trend:
                end_session('GMP UNKNOWN: Trend is not available.',
                            NAGIOS_UNKNOWN)

            trend = trend[0]

            if trend in ['up', 'more']:
                end_session(
                    'GMP CRITICAL: Trend is %s.' % trend, NAGIOS_CRITICAL)
            elif trend in ['down', 'same', 'less']:
                end_session(
                    'GMP OK: Trend is %s.' % trend, NAGIOS_OK)
            else:
                end_session(
                    'GMP UNKNOWN: Trend is unknown: %s' % trend,
                    NAGIOS_UNKNOWN)
        else:
            last_report_id = task.xpath('task/last_report/report/@id')

            if not last_report_id:
                end_session('GMP UNKNOWN: Report is not available',
                            NAGIOS_UNKNOWN)

            last_report_id = last_report_id[0]
            # pretty(task)
            last_scan_end = task.xpath(
                'task/last_report/report/scan_end/text()')

            if last_scan_end:
                last_scan_end = last_scan_end[0]
            else:
                last_scan_end = ''

            if im.old_report(last_scan_end, params_used):
                autofp = ''

                if args.autofp:
                    autofp = 'autofp=%i' % args.autofp

                # When i add the ip address in the filter by a big report with
                # a lot vulns, then the filtering is quick. But when the
                # report did not contain the host, then it takes a lot longer.
                # I dont know why. I add a check for the result count.
                # If > 500 then add the host else without host
                #
                #   <debug>0</debug>
                #   <hole>413</hole>
                #   <info>380</info>
                #   <log>9490</log>
                #   <warning>2332</warning>
                #   <false_positive>842</false_positive>

                result_count = task.xpath(
                    'task/last_report/report/result_count')
                result_count = result_count[0]
                # sum_results = result_count.xpath(
                #     'sum(hole/text() | warning/text() | info/text())')
                # host = ''
                # Host must be setted, otherwise we get results of other hosts in the reports
                # if sum_results > 200:
                host = args.hostaddress

                full_report = conn.get_reports(
                    report_id=last_report_id,
                    filter='sort-reverse=id result_hosts_only=1 '
                           'min_cvss_base= min_qod= levels=hmlgd autofp=%s '
                           'notes=0 apply_overrides=%s overrides=%s first=1 rows=-1 '
                           'delta_states=cgns host=%s'
                           % (args.autofp, int(args.overrides), int(args.apply_overrides), host))
                # pretty(report.xpath('report/report/filters'))

                im.add_report(last_scan_end, params_used, full_report)
                logger.debug('Report added to db')
            else:
                full_report = im.load_local_report()

            filter_report(full_report.xpath('report/report')[0])


def filter_report(report):
    '''Filter out the information in a report

    This function filters the results of a given report.

    Arguments:
        report {lxml object} -- Report in lxml object format
    '''
    report_id = report.xpath('@id')
    if report_id:
        report_id = report_id[0]
    results = report.xpath('//results')
    if not results:
        end_session('GMP UNKNOWN: Failed to get results list', NAGIOS_UNKNOWN)

    results = results[0]
    # Init variables
    any_found = False
    high_count = 0
    medium_count = 0
    low_count = 0
    log_count = 0
    error_count = 0

    nvts = {'high': [], 'medium': [], 'low': [], 'log': []}

    all_results = results.xpath('result')

    for result in all_results:
        if args.hostaddress:
            host = result.xpath('host/text()')
            if not host:
                end_session('GMP UNKNOWN: Failed to parse result host',
                            NAGIOS_UNKNOWN)

            if args.hostaddress != host[0]:
                continue
            any_found = True

        threat = result.xpath('threat/text()')
        if not threat:
            end_session('GMP UNKNOWN: Failed to parse result threat.',
                        NAGIOS_UNKNOWN)

        threat = threat[0]
        if threat in 'High':
            high_count += 1
            if args.oid:
                nvts['high'].append(retrieve_nvt_data(result))
        elif threat in 'Medium':
            medium_count += 1
            if args.oid:
                nvts['medium'].append(retrieve_nvt_data(result))
        elif threat in 'Low':
            low_count += 1
            if args.oid:
                nvts['low'].append(retrieve_nvt_data(result))
        elif threat in 'Log':
            log_count += 1
            if args.oid:
                nvts['log'].append(retrieve_nvt_data(result))
        else:
            end_session('GMP UNKNOWN: Unknown result threat: %s' % threat,
                        NAGIOS_UNKNOWN)

    errors = report.xpath('errors')

    if errors:
        errors = errors[0]
        if args.hostaddress:
            for error in errors.xpath('error'):
                host = error.xpath('host/text()')
                if args.hostaddress == host[0]:
                    error_count += 1
        else:
            error_count = errors.xpath('count/text()')[0]
            print_without_pipe(error_count)

    ret = 0
    if high_count > 0:
        ret = NAGIOS_CRITICAL
    elif medium_count > 0:
        ret = NAGIOS_WARNING

    if args.empty_as_unknown and \
            (not all_results or (not any_found and args.hostaddress)):
        ret = NAGIOS_UNKNOWN

    print('GMP %s: %i vulnerabilities found - High: %i Medium: %i '
          'Low: %i' % (NAGIOS_MSG[ret],
                       (high_count + medium_count + low_count),
                       high_count, medium_count, low_count))

    if not all_results:
        print('Report did not contain any vulnerabilities')

    elif not any_found and args.hostaddress:
        print('Report did not contain vulnerabilities for IP %s' %
              args.hostaddress)

    if int(error_count) > 0:
        if args.hostaddress:
            print_without_pipe('Report did contain %i errors for IP %s' %
                               (error_count, args.hostaddress))
        else:
            print_without_pipe('Report did contain %i errors' % int(error_count))

    # TODO Add multiple response data here from check_omp.c (errors)

    if args.report_link:
        print('https://%s/omp?cmd=get_report&report_id=%s' %
              (args.hostname, report_id))

    if args.oid:
        print_nvt_data(nvts)

    if args.scanend:
        end = report.xpath('//end/text()')[0]
        print('SCAN_END: %s' % end)

    if args.details:
        if args.hostname:
            print('GSM_Host: %s:%d' % (args.hostname, args.port))
        if args.gmp_username:
            print('GMP_User: %s' % args.gmp_username)
        if args.task:
            print_without_pipe('Task: %s' % args.task)

    end_session('|High=%i Medium=%i Low=%i' %
                (high_count, medium_count, low_count), ret)


def retrieve_nvt_data(result):
    '''Retrieve the nvt data out of the result object

    This function parse the xml tree to find the important nvt data.

    Arguments:
        result {lxml object} -- Result xml object

    Returns:
        Tuple -- List with oid, name, desc, port and dfn
    '''
    oid = result.xpath('nvt/@oid')
    name = result.xpath('nvt/name/text()')
    desc = result.xpath('description/text()')
    port = result.xpath('port/text()')

    if oid:
        oid = oid[0]

    if name:
        name = name[0]

    if desc:
        desc = desc[0]
    else:
        desc = ''

    if port:
        port = port[0]
    else:
        port = ''

    certs = result.xpath('nvt/cert/cert_ref')

    dfn_list = []
    for ref in certs:
        ref_type = ref.xpath('@type')[0]
        ref_id = ref.xpath('@id')[0]

        if ref_type in 'DFN-CERT':
            dfn_list.append(ref_id)

    return (oid, name, desc, port, dfn_list)


def print_nvt_data(nvts):
    '''Print nvt data

    Prints for each nvt found in the array the relevant data

    Arguments:
        nvts {object} -- Object holding all nvts
    '''
    for key, nvt_data in nvts.items():
        if key is 'log' and not args.showlog:
            continue
        for nvt in nvt_data:
            print_without_pipe('NVT: %s (%s) %s' % (nvt[0], key, nvt[1]))
            if args.show_ports:
                print_without_pipe('PORT: %s' % (nvt[3]))
            if args.descr:
                print_without_pipe('DESCR: %s' % nvt[2])

            if args.dfn and nvt[4]:
                dfn_list = ', '.join(nvt[4])
                if dfn_list:
                    print_without_pipe('DFN-CERT: %s' % dfn_list)


def end_session(msg, nagios_status):
    '''End the session

    Close the socket if open and print the last msg

    Arguments:
        msg {string} -- Message to print
        nagios_status {int} -- Exit status
    '''
    if conn:
        conn.close()

    print(msg)

    # Delete this instance
    im.delete_instance()

    # Activate some waiting instances if possible
    im.wake_instance()

    # Close the connection to database
    im.close_db()

    sys.exit(nagios_status)


def print_without_pipe(msg):
    '''Prints the message, but without any pipe symbol

    If any pipe symbol is in the msg string, then it will be replaced with
    broken pipe symbol.

    Arguments:
        msg {string} -- Message to print
    '''
    if '|' in msg:
        msg.replace('|', '¦')

    print(msg)


def pretty(xml):
    """Prints beautiful XML-Code

    This function gets an object of list<lxml.etree._Element>
    or directly a lxml element.
    Print it with good readable format.

    Arguments:
        xml {obj} -- list<lxml.etree._Element> or directly a lxml element
    """
    if type(xml) is list:
        for item in xml:
            if etree.iselement(item):
                print(etree.tostring(item, pretty_print=True).decode('utf-8'))
            else:
                print(item)
    elif etree.iselement(xml):
        print(etree.tostring(xml, pretty_print=True).decode('utf-8'))

"""ISO 8601 date time string parsing

Copyright (c) 2007 - 2015 Michael Twomey

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from datetime import (
    timedelta,
    tzinfo
)
from decimal import Decimal
import re

__all__ = ["parse_date", "ParseError", "UTC"]

if sys.version_info >= (3, 0, 0):
    _basestring = str
else:
    _basestring = basestring


# Adapted from http://delete.me.uk/2005/03/iso8601.html
ISO8601_REGEX = re.compile(
    r"""
    (?P<year>[0-9]{4})
    (
        (
            (-(?P<monthdash>[0-9]{1,2}))
            |
            (?P<month>[0-9]{2})
            (?!$)  # Don't allow YYYYMM
        )
        (
            (
                (-(?P<daydash>[0-9]{1,2}))
                |
                (?P<day>[0-9]{2})
            )
            (
                (
                    (?P<separator>[ T])
                    (?P<hour>[0-9]{2})
                    (:{0,1}(?P<minute>[0-9]{2})){0,1}
                    (
                        :{0,1}(?P<second>[0-9]{1,2})
                        ([.,](?P<second_fraction>[0-9]+)){0,1}
                    ){0,1}
                    (?P<timezone>
                        Z
                        |
                        (
                            (?P<tz_sign>[-+])
                            (?P<tz_hour>[0-9]{2})
                            :{0,1}
                            (?P<tz_minute>[0-9]{2}){0,1}
                        )
                    ){0,1}
                ){0,1}
            )
        ){0,1}  # YYYY-MM
    ){0,1}  # YYYY only
    $
    """,
    re.VERBOSE
)


class ParseError(Exception):
    """Raised when there is a problem parsing a date string"""

# Yoinked from python docs
ZERO = timedelta(0)


class Utc(tzinfo):
    """UTC Timezone

    """

    def utcoffset(self, dt):
        return ZERO

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return ZERO

    def __repr__(self):
        return "<iso8601.Utc>"

UTC = Utc()


class FixedOffset(tzinfo):
    """Fixed offset in hours and minutes from UTC

    """

    def __init__(self, offset_hours, offset_minutes, name):
        self.__offset_hours = offset_hours  # Keep for later __getinitargs__
        # Keep for later __getinitargs__
        self.__offset_minutes = offset_minutes
        self.__offset = timedelta(hours=offset_hours, minutes=offset_minutes)
        self.__name = name

    def __eq__(self, other):
        if isinstance(other, FixedOffset):
            return (
                (other.__offset == self.__offset)
                and
                (other.__name == self.__name)
            )
        if isinstance(other, tzinfo):
            return other == self
        return False

    def __getinitargs__(self):
        return (self.__offset_hours, self.__offset_minutes, self.__name)

    def utcoffset(self, dt):
        return self.__offset

    def tzname(self, dt):
        return self.__name

    def dst(self, dt):
        return ZERO

    def __repr__(self):
        return "<FixedOffset %r %r>" % (self.__name, self.__offset)


def to_int(d, key, default_to_zero=False, default=None, required=True):
    """Pull a value from the dict and convert to int

    :param default_to_zero: If the value is None or empty, treat it as zero
    :param default: If the value is missing in the dict use this default

    """
    value = d.get(key) or default
    if (value in ["", None]) and default_to_zero:
        return 0
    if value is None:
        if required:
            raise ParseError("Unable to read %s from %s" % (key, d))
    else:
        return int(value)


def parse_timezone(matches, default_timezone=UTC):
    """Parses ISO 8601 time zone specs into tzinfo offsets

    """

    if matches["timezone"] == "Z":
        return UTC
    # This isn't strictly correct, but it's common to encounter dates without
    # timezones so I'll assume the default (which defaults to UTC).
    # Addresses issue 4.
    if matches["timezone"] is None:
        return default_timezone
    sign = matches["tz_sign"]
    hours = to_int(matches, "tz_hour")
    minutes = to_int(matches, "tz_minute", default_to_zero=True)
    description = "%s%02d:%02d" % (sign, hours, minutes)
    if sign == "-":
        hours = -hours
        minutes = -minutes
    return FixedOffset(hours, minutes, description)


def parse_date(datestring, default_timezone=UTC):
    """Parses ISO 8601 dates into datetime objects

    The timezone is parsed from the date string. However it is quite common to
    have dates without a timezone (not strictly correct). In this case the
    default timezone specified in default_timezone is used. This is UTC by
    default.

    :param datestring: The date to parse as a string
    :param default_timezone: A datetime tzinfo instance to use when no timezone
                             is specified in the datestring. If this is set to
                             None then a naive datetime object is returned.
    :returns: A datetime.datetime instance
    :raises: ParseError when there is a problem parsing the date or
             constructing the datetime instance.

    """
    if not isinstance(datestring, _basestring):
        raise ParseError("Expecting a string %r" % datestring)
    m = ISO8601_REGEX.match(datestring)
    if not m:
        raise ParseError("Unable to parse date string %r" % datestring)
    groups = m.groupdict()

    tz = parse_timezone(groups, default_timezone=default_timezone)

    groups["second_fraction"] = int(
        Decimal(
            "0.%s" % (groups["second_fraction"] or 0)) * Decimal("1000000.0"))

    try:
        return datetime(
            year=to_int(groups, "year"),
            month=to_int(groups, "month", default=to_int(
                groups, "monthdash", required=False, default=1)),
            day=to_int(groups, "day", default=to_int(
                groups, "daydash", required=False, default=1)),
            hour=to_int(groups, "hour", default_to_zero=True),
            minute=to_int(groups, "minute", default_to_zero=True),
            second=to_int(groups, "second", default_to_zero=True),
            microsecond=groups["second_fraction"],
            tzinfo=tz,
        )
    except Exception as e:
        raise ParseError(e)

if __name__ == '__main__':
    main()
