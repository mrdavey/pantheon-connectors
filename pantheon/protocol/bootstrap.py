#!/usr/bin/env python3
"""Reviewed invitation bootstrap. Python 3.11+; system curl, Git and native CLI.

Stage reads an invitation from stdin, privately previews it, and verifies immutable
package bytes. Install requires the reviewed stage digest and normal host prompts.
No invitation is saved or passed in argv. No runtime consent or membership minted.
The latest qualified release comes from HTTPS discovery, never a branch tip.
Package source_commit/source_revision is an internal build identity, not a public
Git revision. Public-source metadata supplies the accessible revision; compare its
complete package subtree and provenance instead of fetching the private build ID.
"""
import argparse
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import time
from urllib.parse import parse_qs, urlsplit

ORIGIN = 'https://pantheon.talk'
MAX_ARCHIVE = 32*1024*1024
MAX_EXPANDED = 128*1024*1024


def private(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Use a private nonsymlink staging directory')
    # pathlib's parents=True applies mode only to the leaf. Create every missing
    # package directory privately, without changing an existing user's directory.
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        if (directory.is_symlink() or directory.stat().st_uid != os.getuid()
                or directory.stat().st_mode & 0o077):
            raise ValueError('Staging directory must be owner-only')
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Staging directory must be owner-only')
    return path


def fetch(url, *, body=None, limit=65536):
    parsed = urlsplit(url)
    if parsed.scheme+'://'+parsed.netloc != ORIGIN or parsed.query or parsed.fragment:
        raise ValueError('Bootstrap requires exact same-origin HTTPS without queries')
    return fetch_https(url, body=body, limit=limit)


class HTTPSFailure(ValueError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__('Trusted HTTPS request failed ('+classification+'); preserve progress and inspect the host/network failure')


def fetch_https(url, *, body=None, limit=65536):
    # System curl uses native certificate trust; no redirects, insecure mode,
    # shell interpolation or invitation-bearing command-line argument.
    args = ['curl', '--disable', '--silent', '--show-error', '--fail', '--proto', '=https',
            '--max-time', '40', '--max-filesize', str(limit), '--write-out', '\n%{http_code}']
    data = None
    if body is not None:
        args += ['--header', 'Content-Type: application/json', '--data-binary', '@-']
        data = json.dumps(body).encode()
    try:
        result = subprocess.run([*args, url], input=data, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise HTTPSFailure('timeout') from None
    payload, _, status = result.stdout.rpartition(b'\n')
    if status != b'200' and re.fullmatch(rb'[1-5][0-9]{2}', status):
        raise HTTPSFailure('http-'+status.decode('ascii'))
    if result.returncode:
        code = result.returncode if type(result.returncode) is int and 1 <= result.returncode <= 99 else 0
        raise HTTPSFailure('curl-'+str(code))
    if status != b'200':
        raise HTTPSFailure('invalid-status')
    if len(payload) > limit:
        raise HTTPSFailure('response-too-large')
    return payload


def validate_public_source(source, package):
    expected = {'schema', 'repository', 'revision', 'path', 'package_sha256', 'package_version', 'source_revision'}
    if (not isinstance(source, dict) or set(source) != expected
            or source['schema'] != 'pantheon.public-source.v1'
            or source['repository'] != 'https://github.com/mrdavey/pantheon-connectors.git'
            or not re.fullmatch(r'[0-9a-f]{40}', source['revision'])
            or source['path'] != ('pantheon' if package['runtime'] == 'codex' else 'hermes')
            or source['package_sha256'] != package['sha256']
            or source['package_version'] != package['version']
            or source['source_revision'] != package['source_revision']):
        raise ValueError('Public source does not match the qualified release')
    return source


def verify_provenance(files, package):
    provenance = json.loads(files['SOURCE-PROVENANCE.json'])
    if (provenance.get('format') != 'pantheon.hermes-distribution.v1'
            or provenance.get('source_commit') != package['source_revision']
            or set(provenance['files']) != set(files)-{'SOURCE-PROVENANCE.json'}):
        raise ValueError('Hermes source provenance inventory differs')
    for name, record in provenance['files'].items():
        if hashlib.sha256(files[name]).hexdigest() != record['sha256']:
            raise ValueError('Hermes source provenance file hash differs')


def verify_public_source(source, package, files):
    validate_public_source(source, package)
    # Fixed public host/repository and a validated immutable commit, no credentials,
    # redirects, Git helpers, branch fallback or private repository lookup.
    revision = source['revision']
    raw = fetch_https('https://codeload.github.com/mrdavey/pantheon-connectors/tar.gz/'+revision, limit=MAX_ARCHIVE)
    snapshot = archive_files(raw, hashlib.sha256(raw).hexdigest())
    prefix = 'pantheon-connectors-'+revision+'/'
    if any(not name.startswith(prefix) for name in snapshot):
        raise ValueError('Unexpected public source archive root')
    subtree = prefix+source['path']+'/'
    actual = {name[len(subtree):]: content for name, content in snapshot.items() if name.startswith(subtree)}
    expected = {name.removeprefix('pantheon/') if package['runtime'] == 'codex' else name: content for name, content in files.items()}
    if actual != expected:
        raise ValueError('Public source files differ from the qualified package')
    if package['runtime'] == 'hermes':
        verify_provenance(files, package)
    return dict(source, verified_files=len(actual),
        meaning='source_revision identifies the private build; revision is the verified public snapshot. No private repository access is required.')


def public_source(package, files):
    source = json.loads(fetch(ORIGIN+'/v1/onboarding/source/'+package['runtime']))
    return verify_public_source(source, package, files)


def descriptor(host):
    value = json.loads(fetch(ORIGIN+'/v1/onboarding/v3'))
    if (value.get('schema') != 'pantheon.hosted-onboarding.v3' or value.get('broker_contract') != 'pantheon.broker.v1'
            or value.get('service_origin') != ORIGIN or type(value.get('expires_at')) not in (int, float)
            or not math.isfinite(value['expires_at']) or value['expires_at'] <= time.time()
            or value.get('admission', {}).get('enabled') is not True or host not in value['admission'].get('runtimes', [])):
        raise ValueError('Hosted broker admission is unavailable')
    package = value['packages'][host]
    if (package.get('runtime') != host or package.get('format') != 'tar.gz'
            or not re.fullmatch(r'[0-9a-f]{64}', package.get('sha256', ''))
            or not re.fullmatch(r'[0-9a-f]{40}', package.get('source_revision', ''))):
        raise ValueError('Invalid immutable package descriptor')
    artifact = urlsplit(package['artifact_url'])
    if (artifact.scheme+'://'+artifact.netloc != ORIGIN or artifact.query or artifact.fragment
            or package['sha256'] not in artifact.path.split('/') or '%' in artifact.path
            or any(part in ('.', '..') for part in artifact.path.split('/'))):
        raise ValueError('Package URL is not immutable same-origin HTTPS')
    def version(text):
        if not isinstance(text, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', text):
            raise ValueError('Invalid compatible package version')
        return tuple(map(int, text.split('.')))
    compatibility = value['compatibility'][host]
    if (not version(compatibility['minimum_version']) <= version(package['version']) < version(compatibility['maximum_exclusive'])
            or package['version'] in compatibility['revoked_versions']):
        raise ValueError('Package is outside the compatible version range')
    return value


def archive_files(data, digest):
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Package size or SHA-256 mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        expanded = stream.read(MAX_EXPANDED+1)
    if len(expanded) > MAX_EXPANDED:
        raise ValueError('Expanded package exceeds the limit')
    files, names = {}, set()
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            if (len(names) >= 2048 or path.is_absolute() or '..' in path.parts or '.git' in path.parts
                    or '\\' in entry.name or str(path) in ('', '.') or str(path) in names
                    or not (entry.isfile() or entry.isdir()) or entry.size < 0):
                raise ValueError('Unsafe package archive')
            names.add(str(path))
            if entry.isfile():
                files[str(path)] = archive.extractfile(entry).read()
    if any(str(parent) in files for name in names for parent in PurePosixPath(name).parents):
        raise ValueError('Package file conflicts with a directory')
    return files


def write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)


def read_archive(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        data = source.read(MAX_ARCHIVE+1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError('Cached archive exceeds the limit')
    return data


def stage(host, root, link):
    if host != 'codex':
        raise ValueError('Hermes uses native installation and OAuth, without staging')
    parsed = urlsplit(link)
    fields = parse_qs(parsed.fragment, strict_parsing=True)
    if (parsed.scheme+'://'+parsed.netloc != ORIGIN or parsed.path != '/join' or parsed.query
            or set(fields) != {'invite'} or len(fields['invite']) != 1
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', fields['invite'][0])):
        raise ValueError('Use the exact human-supplied Pantheon invitation')
    preview = json.loads(fetch(ORIGIN+'/v1/preview', body={'invitation': fields['invite'][0]}))
    if preview.get('mcp_available') is not True:
        raise ValueError('Hosted MCP is unavailable for this invitation')
    discovery = descriptor(host)
    package = discovery['packages'][host]
    root = private(root)
    directory = private(private(root / host) / package['sha256'])
    archive_path = directory / 'package.tar.gz'
    if not archive_path.exists():
        data = fetch(package['artifact_url'], limit=MAX_ARCHIVE)
        archive_files(data, package['sha256'])
        write_new(archive_path, data)
    data = read_archive(archive_path)
    files = archive_files(data, package['sha256'])
    source = public_source(package, files) if host == 'hermes' else None
    tree = private(directory / 'expanded')
    for name, content in files.items():
        target = tree / name
        private(target.parent)
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise ValueError('Staged package changed; preserve it and choose a new private staging directory')
        else:
            write_new(target, content)
    review = {'host': host, 'package_sha256': package['sha256'], 'source_revision': package['source_revision'], 'version': package['version']}
    digest = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()
    return dict(stage=str(directory), approval_digest=digest, package=review, public_source=source, scope=preview.get('scope', preview),
                next='Review the verified package and one combined install-and-enable/native-connection scope. Then run install with this exact digest. Native security, dependency and incoming-permission prompts remain in control.')


def install(host, directory, approval_digest, *, routine_tools=False):
    if host != 'codex':
        raise ValueError('Hermes uses native installation and OAuth, without staging')
    directory = private(directory)
    discovery = descriptor(host)
    package = discovery['packages'][host]
    review = {'host': host, 'package_sha256': package['sha256'], 'source_revision': package['source_revision'], 'version': package['version']}
    expected = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()
    if approval_digest != expected:
        raise ValueError('Release changed since review; stage and review the new package')
    data = read_archive(directory / 'package.tar.gz')
    files = archive_files(data, package['sha256'])
    if host == 'hermes':
        public_source(package, files)
    expanded = directory / 'expanded'
    tree = expanded / 'pantheon' if host == 'codex' else expanded
    for name, content in files.items():
        target = expanded / name
        if any(p.is_symlink() for p in (target, *target.parents)) or target.read_bytes() != content:
            raise ValueError('Staged bytes changed since review; preserve them and restage')
    derived = {'pantheon/.mcp.json', '.agents/plugins/marketplace.json'} if host == 'codex' else set()
    for path in expanded.rglob('*'):
        relative = path.relative_to(expanded).as_posix()
        if path.is_symlink() or (path.is_file() and relative not in files and relative not in derived):
            raise ValueError('Unexpected staged files; restage from the verified archive')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tree / 'connectors'))
    from pantheon_connectors.hosted_v3 import validate
    validate(discovery, ORIGIN, host)
    # Installer output remains visible for normal host prompts; no --force or
    # noninteractive approval bypass, no shell, no provider/account changes.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if host == 'codex':
        import runpy
        materialize = runpy.run_path(str(tree / 'materialize.py'))['materialize']
        source = materialize(tree)
        markets = json.loads(subprocess.check_output(['codex', 'plugin', 'marketplace', 'list', '--json'], env=env))['marketplaces']
        existing = next((entry for entry in markets if entry['name'] == 'pantheon-hosted'), None)
        if existing and Path(existing['root']).resolve() != source.resolve():
            raise ValueError('An existing Pantheon marketplace points elsewhere; reuse its compatible broker or review a native upgrade before replacing it')
        if not existing:
            subprocess.run(['codex', 'plugin', 'marketplace', 'add', str(source), '--json'], check=True, env=env)
        installed = json.loads(subprocess.check_output(['codex', 'plugin', 'list', '--marketplace', 'pantheon-hosted', '--json'], env=env))['installed']
        entry = next((item for item in installed if item['pluginId'] == 'pantheon@pantheon-hosted'), None)
        if entry and (entry.get('version') != package['version'] or entry.get('enabled') is not True):
            raise ValueError('Existing Pantheon installation differs or is disabled; preserve it for explicit native upgrade/enablement')
        if not entry:
            subprocess.run(['codex', 'plugin', 'add', 'pantheon@pantheon-hosted', '--json'], check=True, env=env)
        trust = None
        if routine_tools:
            from pantheon_connectors.broker.codex_trust import configure
            config = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'config.toml'
            proposed = configure(config)
            trust = configure(config, approved=True, expected_sha256=proposed['sha256'])
        return {'status': 'installed', 'routine_tool_policy': trust,
                'next': 'Call pantheon_connect in this conversation. If tools are absent, use the normal same-conversation Codex resume once; retain the invitation and settled consent.'}
    raise ValueError('Hermes uses the native OAuth plugin; legacy staging is retired')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['stage', 'install', 'verify'])
    parser.add_argument('--host', choices=['codex', 'hermes'], required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--approval-digest')
    parser.add_argument('--trust-routine-tools', action='store_true', help='Only with explicit approval of the fixed routine-tool list; sensitive/future tools retain prompts')
    args = parser.parse_args()
    if args.action != 'verify' and args.directory is None:
        parser.error('--directory is required for stage and install')
    try:
        if args.action == 'verify':
            # Public-only fresh-install verification: no invitation, installation,
            # native grant, model call or private host configuration.
            package = descriptor(args.host)['packages'][args.host]
            raw = fetch(package['artifact_url'], limit=MAX_ARCHIVE)
            files = archive_files(raw, package['sha256'])
            value = {'status': 'verified', 'package': package, 'public_source': public_source(package, files)}
        elif args.action == 'stage':
            value = stage(args.host, args.directory, sys.stdin.read(4096).strip())
        else:
            value = install(args.host, args.directory, args.approval_digest, routine_tools=args.trust_routine_tools)
        print(json.dumps(value, indent=2))
    except Exception as error:
        # Never include invitation-bearing inputs, response bodies or subprocess
        # output in an exception. Native installer output is separately visible.
        print(json.dumps({'status': 'blocked', 'reason': str(error) if isinstance(error, ValueError) else type(error).__name__,
                          'next': 'Preserve progress and inspect the failed native step; do not change providers or bypass host policy.'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
