from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid
from pathlib import Path

import pytest

from bridge.claude import ClaudeAdapter, sessions
from bridge.client import AgentChatClient, Credentials
from bridge.config import BridgeConfig
from bridge.daemon import BridgeDaemon, Handler
from bridge.errors import ProtocolError, RemoteError
from bridge.runtime import DispatchUncertain, profile_lock, run_bounded
from bridge.service import LaunchAgentService
from bridge.store import InboxStore
from bridge.transport import JsonRpcTransport


class Repeater:
    def __init__(self):
        self.acks = []
        self.response = {'type': 'message', 'delivery_id': 'd1', 'lease_token': 'l1',
                         'envelope': {'message_id': 'm1', 'message': {'text': 'hello'}}}

    def receive(self, **kwargs):
        return self.response

    def ack(self, *args):
        self.acks.append(args)


def test_crash_intent_prevents_second_host_call_and_can_be_resolved(tmp_path):
    client = Repeater()
    with_store = InboxStore(tmp_path / 'inbox.sqlite3')
    calls = []
    handler = Handler()
    def uncertain(*args):
        calls.append(1)
        raise DispatchUncertain('test timeout')
    handler.run = uncertain
    daemon = BridgeDaemon(client, with_store, handler)
    with pytest.raises(DispatchUncertain):
        daemon.process_once()
    with pytest.raises(DispatchUncertain):
        daemon.process_once()
    assert calls == [1] and not client.acks
    with_store.resolve('d1', retry=False)
    assert daemon.process_once().status == 'acked'
    assert len(calls) == 1 and len(client.acks) == 1
    with_store.close()


def test_new_delivery_id_keeps_canonical_pointer(tmp_path):
    store = InboxStore(tmp_path / 'inbox.sqlite3')
    store.put('old', 'm1', 'old-lease', {'message_id': 'm1'})
    client = Repeater()
    seen = []
    handler = Handler()
    handler.run = lambda env, msg: seen.append(env['delivery_id']) or {'status': 'queued'}
    assert BridgeDaemon(client, store, handler).process_once().status == 'acked'
    assert seen == ['old']
    assert client.acks[0][0:2] == ('d1', 'l1')
    store.close()


def test_profile_lock_excludes_second_receiver_and_releases(tmp_path):
    with profile_lock(tmp_path):
        with pytest.raises(RuntimeError, match='already has a receiver'):
            with profile_lock(tmp_path):
                pass
    with profile_lock(tmp_path):
        pass


def test_process_output_flood_is_bounded():
    with pytest.raises(DispatchUncertain, match='output limit'):
        run_bounded([sys.executable, '-c', 'import os\nwhile True: os.write(1,b"x"*8192)'], output_limit=16384)


def test_child_that_does_not_read_stdin_times_out():
    before = time.monotonic()
    with pytest.raises(DispatchUncertain, match='timed out'):
        run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'], input='x' * 524288, timeout=0.15)
    assert time.monotonic() - before < 3


def test_process_group_cleanup_kills_descendant(tmp_path):
    pid_file = tmp_path / 'pid'
    code = 'import subprocess,time,pathlib; p=subprocess.Popen(["sleep","30"]); pathlib.Path(' + repr(str(pid_file)) + ').write_text(str(p.pid)); time.sleep(30)'
    with pytest.raises(DispatchUncertain):
        run_bounded([sys.executable, '-c', code], timeout=0.3)
    pid = int(pid_file.read_text())
    for _ in range(30):
        state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith('Z'):
            break
        time.sleep(0.05)
    assert not state or state.startswith('Z')


def test_http_errors_are_closed_on_every_retry(monkeypatch):
    streams = []
    def failure(*args, **kwargs):
        body = io.BytesIO(b'error')
        streams.append(body)
        raise urllib.error.HTTPError('https://example.invalid', 503, 'unavailable', {}, body)
    monkeypatch.setattr('urllib.request.urlopen', failure)
    transport = JsonRpcTransport('https://example.invalid', retries=3, backoff=0)
    with pytest.raises(RemoteError):
        transport.call('receive_message')
    assert len(streams) == 4 and all(s.closed for s in streams)


def test_http_response_limit_closes_response(monkeypatch):
    class Response(io.BytesIO):
        status = 200
    response = Response(b'x' * 100)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: response)
    transport = JsonRpcTransport('https://example.invalid')
    transport.MAX_RESPONSE_BYTES = 32
    with pytest.raises(ProtocolError, match='exceeds'):
        transport.call('receive_message')
    assert response.closed


def test_inbox_retention_preserves_pending_and_listing_has_no_bodies(tmp_path):
    store = InboxStore(tmp_path / 'inbox.sqlite3')
    for i, status in enumerate(('pending', 'acked', 'failed')):
        store.put(f'd{i}', f'm{i}', 'lease-secret', {'text': 'x' * 100000})
        store.mark(f'd{i}', status)
    store._db.execute("UPDATE deliveries SET handled_at=datetime('now','-9 days')")
    store._db.commit()
    assert all(not item.payload and not item.lease_token for item in store.list())
    store.maintain()
    assert store.counts() == {'pending': 1, 'acked': 0, 'failed': 0}
    assert len(store.get('d0').payload['text']) == 100000
    store.close()


def test_inbox_row_quota_does_not_evict_or_block_duplicate(tmp_path):
    store = InboxStore(tmp_path / 'inbox.sqlite3')
    store._db.executemany("INSERT INTO deliveries(delivery_id,message_id,payload,status) VALUES(?,?,?,'pending')",
                          [(f'd{i}', f'm{i}', '{}') for i in range(10000)])
    store._db.commit()
    with pytest.raises(RuntimeError, match='quota'):
        store.put('overflow', 'overflow', 'l', {})
    assert store.put('d0', 'm0', 'new-lease', {}).lease_token == 'new-lease'
    assert store.counts()['pending'] == 10000
    store.close()


def test_launch_profiles_are_isolated(tmp_path):
    a = LaunchAgentService(BridgeConfig.from_env(state_dir=tmp_path / 'a'))
    b = LaunchAgentService(BridgeConfig.from_env(state_dir=tmp_path / 'b'))
    assert a.label != b.label and a.plist_path != b.plist_path


def test_claude_exact_session_frame_and_socket_cleanup():
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='ac-') as temp:
        root = Path(temp)
        registry = root / 'sessions'
        registry.mkdir(mode=0o700)
        peer_path = root / 'peer.sock'
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(peer_path)); peer_path.chmod(0o600); server.listen()
        sid = str(uuid.uuid4())
        record = registry / 'session.json'
        record.write_text(json.dumps({'pid': os.getpid(), 'sessionId': sid, 'peerProtocol': 1,
                                      'version': 'fixture-1', 'messagingSocketPath': str(peer_path)}))
        seen = []
        def receiver():
            with server.accept()[0] as peer:
                data = b''
                while chunk := peer.recv(16384):
                    data += chunk
                seen.append(json.loads(data))
        thread = threading.Thread(target=receiver)
        thread.start()
        try:
            result = ClaudeAdapter(sid, registry).send('local pointer', 'm1')
            thread.join(2)
            assert not thread.is_alive()
            assert result['status'] == 'submitted_unconfirmed'
            assert seen[0]['session_id'] == sid and seen[0]['priority'] == 'later'
            assert seen[0]['message']['content'].startswith('<cross-session-message')
            assert 'permissionMode' not in seen[0] and 'from' not in seen[0]
            with pytest.raises(RuntimeError, match='offline'):
                ClaudeAdapter(str(uuid.uuid4()), registry).preflight()
            record.write_text(record.read_text().replace(sid, str(uuid.uuid4())))
            with pytest.raises(RuntimeError, match='offline'):
                ClaudeAdapter(sid, registry).preflight()
        finally:
            server.close()


def test_sigterm_releases_receiver_lock_and_database(tmp_path):
    code = '''
import sys,time
from pathlib import Path
from bridge.runtime import profile_lock,stop_signals,StopBridge
from bridge.store import InboxStore
p=Path(sys.argv[1])
try:
 with profile_lock(p),stop_signals():
  store=InboxStore(p/'inbox.sqlite3')
  try:
   (p/'ready').touch()
   time.sleep(60)
  finally: store.close()
except StopBridge: pass
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)])
    try:
        for _ in range(100):
            if (tmp_path / 'ready').exists(): break
            time.sleep(0.02)
        assert (tmp_path / 'ready').exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=3) == 0
        with profile_lock(tmp_path):
            store = InboxStore(tmp_path / 'inbox.sqlite3')
            store.put('d', 'm', 'l', {})
            store.close()
    finally:
        if child.poll() is None:
            child.kill(); child.wait()


def test_pair_does_not_overwrite_active_identity(tmp_path):
    client = AgentChatClient(BridgeConfig.from_env(state_dir=tmp_path))
    client.credentials = Credentials('access', 'refresh', 'instance')
    with pytest.raises(ValueError, match='already paired'):
        client.pair('never-transmitted')
    assert client.credentials.access_token == 'access'


def test_daemon_renews_presence_after_long_poll(tmp_path, monkeypatch):
    class Done(BaseException): pass
    clock = [0.0]
    class Client:
        registrations = 0
        def ensure_registered(self):
            self.registrations += 1
    client = Client()
    store = InboxStore(tmp_path / 'inbox.sqlite3')
    daemon = BridgeDaemon(client, store, Handler())
    calls = [0]
    def poll(**kwargs):
        calls[0] += 1
        if calls[0] > 3: raise Done()
        clock[0] += 25
    daemon.process_once = poll
    monkeypatch.setattr('bridge.daemon.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bridge.daemon.time.sleep', lambda _: None)
    try:
        with pytest.raises(Done): daemon.run_forever()
        assert client.registrations == 4
    finally:
        store.close()
