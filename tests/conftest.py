# The piwheels project
#   Copyright (c) 2017 Ben Nuttall <https://github.com/bennuttall>
#   Copyright (c) 2017 Dave Jones <dave@waveform.org.uk>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


import os
from unittest import mock
from datetime import datetime, timedelta
from hashlib import sha256
from threading import Thread
from time import sleep

import zmq
import pytest
from sqlalchemy import create_engine

from piwheels import const
from piwheels.initdb import get_script, parse_statements
from piwheels.master.states import BuildState, FileState, DownloadState
from piwheels.master.the_oracle import TheOracle
from piwheels.master.seraph import Seraph


# The database tests all assume that a database (default: piwheels_test)
# exists, along with two users. An ordinary unprivileged user (default:
# piwheels) which will be used as if it were the piwheels user on the
# production database, and a postgres superuser (default: postgres) which will
# be used to set up structures in the test database and remove them between
# each test. The environment variables listed below can be used to configure
# the names of these entities for use by the test suite.

PIWHEELS_TESTDB = os.environ.get('PIWHEELS_TESTDB', 'piwheels_test')
PIWHEELS_USER = os.environ.get('PIWHEELS_USER', 'piwheels')
PIWHEELS_PASS = os.environ.get('PIWHEELS_PASS', 'piwheels')
PIWHEELS_SUPERUSER = os.environ.get('PIWHEELS_SUPERUSER', 'postgres')
PIWHEELS_SUPERPASS = os.environ.get('PIWHEELS_SUPERPASS', '')


@pytest.fixture()
def file_content(request):
    return b'\x01\x02\x03\x04\x05\x06\x07\x08' * 15432  # 123456 bytes


@pytest.fixture()
def file_state(request, file_content):
    h = sha256()
    h.update(file_content)
    return FileState(
        'foo-0.1-cp34-cp34m-linux_armv7l.whl', len(file_content),
        h.hexdigest().lower(), 'foo', '0.1', 'cp34', 'cp34m', 'linux_armv7l')


@pytest.fixture()
def file_state_hacked(request, file_content):
    h = sha256()
    h.update(file_content)
    return FileState(
        'foo-0.1-cp34-cp34m-linux_armv6l.whl', len(file_content),
        h.hexdigest().lower(), 'foo', '0.1', 'cp34', 'cp34m', 'linux_armv6l',
        transferred=True)


@pytest.fixture()
def file_state_universal(request, file_content):
    h = sha256()
    h.update(file_content)
    return FileState(
        'foo-0.1-py2.py3-none-any.whl', len(file_content),
        h.hexdigest().lower(), 'foo', '0.1', 'py2.py3', 'none', 'any')


@pytest.fixture()
def build_state(request, file_state):
    return BuildState(
        1, file_state.package_tag, file_state.package_version_tag,
        file_state.abi_tag, True, 300, 'Built successfully',
        {file_state.filename: file_state})


@pytest.fixture()
def build_state_hacked(request, file_state, file_state_hacked):
    return BuildState(
        1, file_state.package_tag, file_state.package_version_tag,
        file_state.abi_tag, True, 300, 'Built successfully', {
            file_state.filename: file_state,
            file_state_hacked.filename: file_state_hacked,
        })


@pytest.fixture()
def download_state(request, file_state):
    return DownloadState(
        file_state.filename, '123.4.5.6', datetime(2018, 1, 1, 0, 0, 0),
        'armv7l', 'Raspbian', '9', 'Linux', '', 'CPython', '3.5')


@pytest.fixture(scope='session')
def db_url(request):
    return 'postgres://{username}:{password}@/{db}'.format(
        username=PIWHEELS_USER,
        password=PIWHEELS_PASS,
        db=PIWHEELS_TESTDB
    )


@pytest.fixture(scope='session')
def db_super_url(request):
    return 'postgres://{username}:{password}@/{db}'.format(
        username=PIWHEELS_SUPERUSER,
        password=PIWHEELS_SUPERPASS,
        db=PIWHEELS_TESTDB
    )


@pytest.fixture(scope='session')
def db_engine(request, db_super_url):
    engine = create_engine(db_super_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(request, db_engine):
    conn = db_engine.connect()
    conn.execute("SET SESSION synchronous_commit TO OFF")  # it's only a test
    yield conn
    conn.close()


@pytest.fixture(scope='function')
def with_schema(request, db):
    with db.begin():
        # Wipe the public schema and re-create it with standard defaults
        db.execute("DROP SCHEMA public CASCADE")
        db.execute("CREATE SCHEMA public AUTHORIZATION postgres")
        db.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
        db.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        for stmt in parse_statements(get_script()):
            stmt = stmt.format(username=PIWHEELS_USER)
            db.execute(stmt)
    return 'schema'


@pytest.fixture()
def with_build_abis(request, db, with_schema):
    with db.begin():
        db.execute(
            "INSERT INTO build_abis VALUES ('cp34m'), ('cp35m')")
    return {'cp34m', 'cp35m'}


@pytest.fixture()
def with_package(request, db, with_build_abis, build_state):
    with db.begin():
        db.execute(
            "INSERT INTO packages(package) VALUES (%s)", build_state.package)
    return build_state.package


@pytest.fixture()
def with_package_version(request, db, with_package, build_state):
    with db.begin():
        db.execute(
            "INSERT INTO versions(package, version) "
            "VALUES (%s, %s)", build_state.package, build_state.version)
    return (build_state.package, build_state.version)


@pytest.fixture()
def with_build(request, db, with_package_version, build_state):
    with db.begin():
        build_id = db.execute(
            "INSERT INTO builds"
            "(package, version, built_by, built_at, duration, status, abi_tag) "
            "VALUES "
            "(%s, %s, %s, TIMESTAMP '2018-01-01 00:00:00', %s, true, %s) "
            "RETURNING (build_id)",
            build_state.package,
            build_state.version,
            build_state.slave_id,
            timedelta(seconds=build_state.duration),
            build_state.abi_tag).first()[0]
        db.execute(
            "INSERT INTO output VALUES (%s, 'Built successfully')", build_id)
    build_state.logged(build_id)
    return build_state


@pytest.fixture()
def with_files(request, db, with_build, file_state, file_state_hacked):
    with db.begin():
        db.execute(
            "INSERT INTO files "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            file_state.filename, with_build.build_id,
            file_state.filesize, file_state.filehash, file_state.package_tag,
            file_state.package_version_tag, file_state.py_version_tag,
            file_state.abi_tag, file_state.platform_tag)
        db.execute(
            "INSERT INTO files "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            file_state_hacked.filename, with_build.build_id,
            file_state_hacked.filesize, file_state_hacked.filehash,
            file_state_hacked.package_tag,
            file_state_hacked.package_version_tag,
            file_state_hacked.py_version_tag, file_state_hacked.abi_tag,
            file_state_hacked.platform_tag)
    return [file_state, file_state_hacked]


@pytest.fixture()
def with_downloads(request, db, with_files, download_state):
    dl = download_state
    with db.begin():
        db.execute(
            "INSERT INTO downloads "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            dl.filename, dl.host, dl.timestamp,
            dl.arch, dl.distro_name, dl.distro_version,
            dl.os_name, dl.os_version,
            dl.py_name, dl.py_version)
        db.execute(
            "INSERT INTO downloads "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            dl.filename, dl.host, dl.timestamp + timedelta(minutes=5),
            dl.arch, dl.distro_name, dl.distro_version,
            dl.os_name, dl.os_version,
            dl.py_name, dl.py_version)


@pytest.fixture(scope='function')
def master_config(request, tmpdir):
    config = mock.Mock()
    config.pypi_xmlrpc = 'https://pypi.org/pypi'
    config.pypi_simple = 'https://pypi.org/simple'
    config.dsn = 'postgres://{username}:{password}@/{db}'.format(
        username=PIWHEELS_USER,
        password=PIWHEELS_PASS,
        db=PIWHEELS_TESTDB
    )
    config.output_path = str(tmpdir)
    config.index_queue = 'inproc://tests-indexes'
    config.status_queue = 'inproc://tests-status'
    config.control_queue = 'inproc://tests-control'
    config.builds_queue = 'inproc://tests-builds'
    config.db_queue = 'inproc://tests-db'
    config.fs_queue = 'inproc://tests-fs'
    config.slave_queue = 'inproc://tests-slave-driver'
    config.file_queue = 'inproc://tests-file-juggler'
    config.import_queue = 'inproc://tests-imports'
    config.log_queue = 'inproc://tests-logger'
    config.stats_queue = 'inproc://tests-stats'
    return config


@pytest.fixture(scope='session')
def zmq_context(request):
    context = zmq.Context.instance()
    yield context
    context.destroy(linger=1000)
    context.term()


@pytest.fixture(scope='function')
def master_control_queue(request, zmq_context, master_config):
    queue = zmq_context.socket(zmq.PULL)
    queue.hwm = 1
    queue.bind(master_config.control_queue)
    yield queue
    queue.close()


@pytest.fixture(scope='function')
def master_status_queue(request, zmq_context):
    queue = zmq_context.socket(zmq.PULL)
    queue.hwm = 1
    queue.bind(const.INT_STATUS_QUEUE)
    yield queue
    queue.close()


@pytest.fixture()
def sock_push_pull(request, zmq_context):
    # XXX Could extend this to be a factory fixture (permitting multiple pairs)
    pull = zmq_context.socket(zmq.PULL)
    push = zmq_context.socket(zmq.PUSH)
    pull.hwm = 1
    push.hwm = 1
    pull.bind('inproc://push-pull')
    push.connect('inproc://push-pull')
    yield push, pull
    push.close()
    pull.close()


@pytest.fixture()
def sock_pair(request, zmq_context):
    # XXX Could extend this to be a factory fixture (permitting multiple pairs)
    sock1 = zmq_context.socket(zmq.PAIR)
    sock2 = zmq_context.socket(zmq.PAIR)
    sock1.hwm = 1
    sock2.hwm = 1
    sock1.bind('inproc://pair-pair')
    sock2.connect('inproc://pair-pair')
    yield sock1, sock2
    sock2.close()
    sock1.close()


class MockMessage:
    def __init__(self, action, message):
        assert action in ('send', 'recv')
        self.action = action
        self.message = message
        self.result = None

    def __repr__(self):
        if self.result is None:
            return '%s: %r' % (
                ('TX', 'RX')[self.action == 'recv'], self.message)
        else:
            return '%s: %s' % (
                ('!!', 'OK')[self.action == 'send' or self.message == self.result],
                self.result)


class MockTask(Thread):
    """
    Helper class for testing tasks which interface with REQ/REP sockets. This
    spawns a thread which can be tasked with expecting certain inputs and to
    respond with certain outputs. Typically used to emulate DbClient and
    FsClient to downstream tasks.
    """
    ident = 0

    def __init__(self, ctx, sock_type, sock_addr):
        address = 'inproc://mock-%d' % MockTask.ident
        super().__init__(target=self.loop, args=(ctx, address))
        MockTask.ident += 1
        self.sock_type = sock_type
        self.sock_addr = sock_addr
        self.control = ctx.socket(zmq.REQ)
        self.control.hwm = 1
        self.control.bind(address)
        self.sock = ctx.socket(sock_type)
        self.sock.hwm = 1
        self.sock.bind(sock_addr)
        self.daemon = True
        self.start()

    def __repr__(self):
        return '<MockTask sock_addr="%s">' % self.sock_addr

    def close(self):
        self.control.send_pyobj(['QUIT'])
        assert self.control.recv_pyobj() == ['OK']
        self.join(10)
        self.control.close()
        self.control = None
        if self.is_alive():
            raise RuntimeError('failed to terminate mock task %r' % self)
        self.sock.close()
        self.sock = None

    def expect(self, message):
        self.control.send_pyobj(['RECV', message])
        assert self.control.recv_pyobj() == ['OK']

    def send(self, message):
        self.control.send_pyobj(['SEND', message])
        assert self.control.recv_pyobj() == ['OK']

    def check(self, timeout=1):
        self.control.send_pyobj(['TEST', timeout])
        exc = self.control.recv_pyobj()
        if exc is not None:
            raise exc

    def reset(self):
        self.control.send_pyobj(['RESET'])
        assert self.control.recv_pyobj() == ['OK']

    def loop(self, ctx, address):
        queue = []
        done = []
        socks = {}

        def handle_queue():
            if self.sock in socks and queue[0].action == 'recv':
                queue[0].result = self.sock.recv_pyobj()
                done.append(queue.pop(0))
            elif queue[0].action == 'send':
                self.sock.send_pyobj(queue[0].message)
                queue[0].result = queue[0].message
                done.append(queue.pop(0))

        control = ctx.socket(zmq.REP)
        control.hwm = 1
        control.connect(address)
        try:
            poller = zmq.Poller()
            poller.register(control, zmq.POLLIN)
            poller.register(self.sock, zmq.POLLIN)
            while True:
                socks = dict(poller.poll(10))
                if control in socks:
                    msg, *args = control.recv_pyobj()
                    if msg == 'QUIT':
                        control.send_pyobj(['OK'])
                        break
                    elif msg == 'SEND':
                        queue.append(MockMessage('send', args[0]))
                        control.send_pyobj(['OK'])
                    elif msg == 'RECV':
                        queue.append(MockMessage('recv', args[0]))
                        control.send_pyobj(['OK'])
                    elif msg == 'TEST':
                        try:
                            timeout = timedelta(seconds=args[0])
                            start = datetime.utcnow()
                            while queue and datetime.utcnow() - start < timeout:
                                socks = dict(poller.poll(10))
                                handle_queue()
                            if queue:
                                assert False, 'Still waiting for %r' % queue[0]
                            for item in done:
                                assert item.message == item.result
                        except Exception as exc:
                            control.send_pyobj(exc)
                        else:
                            control.send_pyobj(None)
                    elif msg == 'RESET':
                        queue = []
                        done = []
                        control.send_pyobj(['OK'])
                if queue:
                    handle_queue()
        finally:
            control.close()


@pytest.fixture(scope='function')
def db_queue(request, zmq_context, master_config):
    task = MockTask(zmq_context, zmq.REP, master_config.db_queue)
    yield task
    task.close()


@pytest.fixture(scope='function')
def fs_queue(request, zmq_context, master_config):
    task = MockTask(zmq_context, zmq.REP, master_config.fs_queue)
    yield task
    task.close()
