"""Static website upload through the existing scoped connection. No cloud token.

Every path component is opened without following symlinks; uploaded bytes come
from the approved directory descriptor. Never build, execute, or copy a repo.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone

from .client import APIError
from .state import save_private, load_private
from .worker import Blocked

MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
EXTENSIONS = {'.html', '.css', '.js', '.mjs', '.json', '.txt', '.svg', '.png',
              '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.woff', '.woff2',
              '.mp3', '.ogg', '.wav', '.mp4', '.webm'}
FORBIDDEN = {'node_modules', 'functions', 'package.json', 'package-lock.json',
             'wrangler.json', 'wrangler.jsonc', 'firebase.json', 'service-worker.js', 'sw.js'}
SECRET = re.compile(rb'(-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"private_key"\s*:|[?&#]invite=[A-Za-z0-9_-]{32,}|(?:PANTHEON_[A-Z_]*(?:TOKEN|SECRET|KEY)|CLOUDFLARE_API_TOKEN)\s*[=:]\s*["\x27]?[A-Za-z0-9_-]{16,})', re.I)

TOOLS = [
    {'name': 'pantheon_site_deploy', 'description': 'Deploy a built static website using this chat member’s existing automatic public grant; no separate website opt-in is required. Call pantheon_site_status first and respect disabled, expired, private-only or removed grants. Upload only an authorized build-output directory containing index.html: no server code, Functions, secrets or private chat context. Does not build code or publish chat history. Reuse the same key only for identical bytes; inspect status after an uncertain response.',
     'inputSchema': {'type': 'object', 'properties': {
         'connection': {'type': 'string'}, 'directory': {'type': 'string'}, 'key': {'type': 'string'}},
         'required': ['connection', 'directory', 'key'], 'additionalProperties': False}},
    {'name': 'pantheon_site_status', 'description': 'Read this membership’s website grant and deployment status. reconcile=true reads provider evidence for a pending deployment; it never retries an upload or changes visibility.',
     'inputSchema': {'type': 'object', 'properties': {
         'connection': {'type': 'string'}, 'reconcile': {'type': 'boolean'}},
         'required': ['connection'], 'additionalProperties': False}},
]


def bundle(directory):
    root = Path(directory)
    if not root.is_absolute() or '..' in root.parts:
        raise Blocked('Website output must be an absolute directory without symlinks')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    fd = os.open('/', flags)
    files, manifest, seen = [], [], set()
    total = 0

    def walk(folder, prefix=''):
        nonlocal total
        for name in sorted(os.listdir(folder)):
            relative = prefix + name
            if (len(relative) > 200 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', relative)
                    or name.startswith(('.', '_')) or name.lower() in FORBIDDEN):
                raise Blocked('Website contains a forbidden path or provider configuration')
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=folder)
                try:
                    walk(child, relative + '/')
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Blocked('Website files must be regular files without links')
            if Path(name).suffix.lower() not in EXTENSIONS or relative.lower() in seen:
                raise Blocked('Unsupported or duplicate website file')
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(child, 'rb') as source:
                actual = os.fstat(source.fileno())
                if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                    raise Blocked('Website file changed during packaging')
                data = source.read(MAX_FILE + 1)
            total += len(data)
            if len(data) > MAX_FILE or total > MAX_TOTAL or len(files) >= 500:
                raise Blocked('Website exceeds the 500-file, 5 MiB/file or 20 MiB total limit')
            if SECRET.search(data):
                raise Blocked('Website contains a possible secret; remove it before uploading')
            seen.add(relative.lower())
            files.append({'path': relative, 'data': base64.b64encode(data).decode('ascii')})
            manifest.append([relative, hashlib.sha256(data).hexdigest()])
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        walk(fd)
    except OSError:
        raise Blocked('Cannot safely read the selected website directory') from None
    finally:
        os.close(fd)
    if not any(f['path'] == 'index.html' and f['data'] for f in files):
        raise Blocked('Website requires a nonempty index.html')
    manifest.sort()
    digest = hashlib.sha256(json.dumps(manifest, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return files, digest


def execute(service, name, args, binding):
    state, _, client = service.member(args['connection'], binding)
    manifest = client.request('/v1/manifest')
    if (manifest.get('website_publishing', {}).get('available') is not True
            or manifest.get('website_publishing', {}).get('protocol') != 'pantheon.sites.v1'):
        raise Blocked('Website publishing is not enabled on this Pantheon server')
    service.require_allowance()
    if name == 'pantheon_site_status':
        if args.get('reconcile'):
            return client.request('/v1/sites/current/reconcile', {})
        return client.request('/v1/sites/current')
    key = args['key']
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
        raise Blocked('Use a stable website deployment key of 16–128 letters, digits, underscores or hyphens')
    grant = client.request('/v1/sites/current')
    try:
        unexpired = datetime.fromisoformat(grant['expires'].replace('Z', '+00:00')) > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        unexpired = False
    if grant.get('enabled') is not True or not unexpired:
        raise Blocked('Website updates are disabled or this chat’s publishing window has ended')
    files, digest = bundle(args['directory'])
    service.require_allowance()
    # Private journal contains metadata only. Credentials and bundle bytes are
    # never copied to this receipt or native diagnostic logs.
    receipt = state.with_suffix('.site-operation.json')
    if receipt.exists():
        prior = load_private(receipt)
        if grant.get('pending') and (prior.get('key') != key or prior.get('digest') != digest):
            raise Blocked('A prior website deployment needs reconciliation. Preserve its key and files.')
    if grant.get('pending'):
        raise Blocked('Use pantheon_site_status with reconcile=true before deploying again')
    save_private(receipt, {'key': key, 'digest': digest, 'site': grant.get('id'), 'state': 'submitted'})
    try:
        result = client.request('/v1/sites/current/deploy', {'key': key, 'files': files})
    except APIError:
        raise Blocked('Website deployment was not confirmed. Use pantheon_site_status with reconcile=true; preserve the key and files. Do not start a replacement deployment.') from None
    save_private(receipt, {'key': key, 'digest': digest, 'site': grant.get('id'), 'state': result.get('state'), 'deployment': result.get('deployment')})
    return result
