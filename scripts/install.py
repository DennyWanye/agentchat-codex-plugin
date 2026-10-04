#!/usr/bin/env python3
"""Install this checkout's bridge and canonical skill; never pair or bind implicitly."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('codex', 'claude'), required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    uv = shutil.which('uv')
    if not uv:
        candidate = Path.home() / '.local/bin/uv'
        if candidate.is_file() and os.access(candidate, os.X_OK):
            uv = str(candidate)
    if not uv:
        parser.error('Install uv first: https://docs.astral.sh/uv/getting-started/installation/')
    subprocess.run([uv, 'tool', 'install', '--python', '3.11', '--force',
                    '--reinstall', '--refresh', str(source)], check=True, timeout=300)
    subprocess.run([sys.executable, str(source / 'scripts/install_skill.py'),
                    '--target', args.target], check=True, timeout=30)
    bin_dir = subprocess.check_output([uv, 'tool', 'dir', '--bin'], text=True, timeout=15).strip()
    executable = Path(bin_dir) / 'agentchat-bridge'
    subprocess.run([str(executable), '--help'], check=True, stdout=subprocess.DEVNULL, timeout=15)
    print(json.dumps({'installed': True, 'target': args.target,
                      'bridge': str(executable), 'connection': 'not_configured_by_installer'}, indent=2))


if __name__ == '__main__':
    main()
