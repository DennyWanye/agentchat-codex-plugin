#!/usr/bin/env python3
"""Bounded local soak; no model calls, credentials or network mutations."""
import argparse
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from bridge.daemon import BridgeDaemon, Handler
from bridge.runtime import run_bounded
from bridge.store import InboxStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--messages', type=int, default=4000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1000 <= args.messages <= 10000:
        parser.error('messages must be between 1000 and 10000')
    process = psutil.Process()
    tracemalloc.start()
    samples = []
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='agentchat-soak-') as scratch:
        store = InboxStore(Path(scratch) / 'inbox.sqlite3')
        class Client:
            count = 0
            acks = 0
            def receive(self, **kwargs):
                self.count += 1
                return {'delivery_id': f'd{self.count}', 'lease_token': 'fixture', 'envelope': {
                    'message_id': f'm{self.count}', 'message': {'text': 'x' * 8192}}}
            def ack(self, *args):
                self.acks += 1
        client = Client()
        handler = Handler()
        handler.run = lambda *args: {'status': 'fixture-accepted'}
        daemon = BridgeDaemon(client, store, handler)
        try:
            for i in range(1, args.messages + 1):
                daemon.process_once()
                if i % 100 == 0:
                    assert run_bounded([sys.executable, '-c', 'print("ok")']).returncode == 0
                    store.maintain()
                if i % 500 == 0:
                    gc.collect()
                    current, peak = tracemalloc.get_traced_memory()
                    samples.append({'messages': i, 'rss_bytes': process.memory_info().rss,
                        'python_heap_bytes': current, 'python_heap_peak_bytes': peak,
                        'fds': process.num_fds(), 'children': len(process.children()),
                        'disk_bytes': sum(p.stat().st_size for p in Path(scratch).iterdir())})
            assert client.acks == args.messages
        finally:
            store.close()
    baseline, final = samples[0], samples[-1]
    result = {'scope': 'local fixture deliveries plus bounded subprocesses; no live model stress',
              'messages': args.messages, 'duration_seconds': round(time.monotonic() - start, 2),
              'samples': samples, 'rss_growth_after_warmup_bytes': final['rss_bytes'] - baseline['rss_bytes'],
              'heap_growth_after_warmup_bytes': final['python_heap_bytes'] - baseline['python_heap_bytes'],
              'passed': final['fds'] == baseline['fds'] and final['children'] == 0
                        and final['python_heap_bytes'] - baseline['python_heap_bytes'] < 2 * 1024**2
                        and final['rss_bytes'] - baseline['rss_bytes'] < 16 * 1024**2}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
