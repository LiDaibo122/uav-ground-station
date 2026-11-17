#!/usr/bin/env python3
"""Fetch pinned MIT sources; build the optional basic auction baseline locally."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-baseline', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((ROOT/'third_party/SOURCES.json').read_text())
    for repo in manifest['repositories']:
        path = ROOT/repo['directory']
        if not path.exists():
            subprocess.run(['git', 'clone', repo['upstream']+'.git', str(path)], check=True)
            subprocess.run(['git', '-C', str(path), 'checkout', '--detach', repo['commit']], check=True)
        sha = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        if sha != repo['commit']:
            raise RuntimeError('Source revision differs from manifest: '+str(path))
        print(repo['directory'], sha, repo['license'], flush=True)
    sslap = ROOT/'third_party/sslap'
    patch = ROOT/'third_party/patches/sslap-numpy124.patch'
    already = subprocess.run(['git', 'apply', '--reverse', '--check', str(patch)], cwd=sslap,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not already:
        subprocess.run(['git', 'apply', '--check', str(patch)], cwd=sslap, check=True)
        subprocess.run(['git', 'apply', str(patch)], cwd=sslap, check=True)
    if args.build_baseline:
        subprocess.run([sys.executable, 'setup.py', 'build_ext', '--inplace'], cwd=sslap, check=True)


if __name__ == '__main__':
    main()
