#!/usr/bin/env python3
"""Install the canonical AgentChat skill in Codex or Claude's personal skills."""
import argparse
import datetime
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=('codex', 'claude'), required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'plugins/agentchat-codex/skills/agentchat'
    root = Path.home() / ('.codex' if args.target == 'codex' else '.claude')
    destination = root / 'skills/agentchat'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.agentchat-install-', dir=root) as scratch:
        staged = Path(scratch) / 'agentchat'
        shutil.copytree(source, staged)
        if destination.exists() or destination.is_symlink():
            backup = root / 'agentchat-skill-backups' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup.parent.mkdir(parents=True, exist_ok=True)
            destination.rename(backup)
        staged.rename(destination)
    print(f'Installed {destination}; reload skills in the target app if required.')


if __name__ == '__main__':
    main()
