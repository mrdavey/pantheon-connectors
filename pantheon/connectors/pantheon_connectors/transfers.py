"""Bounded temporary handoffs; no execution, extraction, overwrite or implicit ACK."""
import contextlib
import hashlib
import os
import re
import secrets
import stat
from urllib.parse import quote
from .worker import Blocked

MAX_BYTES = 1_000_000


def tool(name, description, required, optional=()):
    fields = ('connection', *required, *optional)
    return dict(name=name, description='Requires scoped server enablement. ' + description,
                inputSchema=dict(type='object', properties={k: {'type': 'string'} for k in fields},
                                 required=['connection', *required], additionalProperties=False))


TOOLS = [
    tool('pantheon_transfer_send', 'Upload one explicitly chosen absolute local file to one recipient. Reuse the exact key and arguments on interrupted retry; lifetime never extends. Output is metadata only.',
         ('path', 'recipient', 'name', 'reason', 'key'), ('base_revision',)),
    tool('pantheon_transfer_inspect', 'Inspect authorized transfer metadata; no bytes or receipt.', ('transfer_id',)),
    tool('pantheon_transfer_fetch', 'Save a complete verified file to an explicitly chosen NEW absolute path. Never overwrite, execute or extract. Does NOT acknowledge; inspect the result then explicitly ACK.', ('transfer_id', 'path')),
    tool('pantheon_transfer_ack', 'Explicitly confirm durable receipt after fetch. Reverify the chosen local file and fsync before ACK permits server deletion. Does not imply acceptance or execution.', ('transfer_id', 'path')),
]
NAMES = {t['name'] for t in TOOLS}


@contextlib.contextmanager
def parent(path):
    """Walk with directory descriptors: reject symlinks in every path component."""
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
        raise Blocked('Choose an explicit absolute local path')
    parts = path.split('/')[1:]
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise Blocked('Choose a normalized absolute local path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd, parts[-1]
    finally:
        os.close(fd)


def read_file(path, *, durable=False):
    with parent(path) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_BYTES:
                raise Blocked('Transfer requires a regular file of 1 to 1,000,000 bytes')
            content = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != before.st_size or len(content) > MAX_BYTES or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise Blocked('Local file changed during verification; preserve original transfer arguments')
            if durable:
                os.fsync(stream.fileno())
                os.fsync(directory)
                # fsync may block: the verified inode can be changed or unlinked
                # while it runs. Never ACK an old open descriptor whose chosen
                # path no longer names the complete, unchanged saved file.
                synced = os.fstat(stream.fileno())
                try:
                    named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    raise Blocked('Saved file disappeared during durable verification; no receipt sent') from None
                def identity(value):
                    return (value.st_dev, value.st_ino, value.st_size,
                            value.st_mtime_ns, value.st_ctime_ns)
                if (not stat.S_ISREG(named.st_mode) or synced.st_nlink < 1 or
                        identity(before) != identity(synced) or identity(synced) != identity(named)):
                    raise Blocked('Saved file changed during durable verification; no receipt sent')
            return content


def integrity(metadata, content):
    if (type(metadata.get('size')) is not int or not 1 <= metadata['size'] <= MAX_BYTES or
            not isinstance(metadata.get('sha256'), str) or
            re.fullmatch('[0-9a-f]{64}', metadata['sha256']) is None or
            len(content) != metadata['size'] or hashlib.sha256(content).hexdigest() != metadata['sha256']):
        raise Blocked('Transfer size or SHA-256 integrity verification failed; no receipt sent')


def safe_save(path, content):
    with parent(path) as (directory, name):
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Blocked('Choose a destination directory owned by you and not writable by others')
        # Refuse existing destinations, including broken links, before staging.
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Blocked('Destination exists; never replace local work. If an earlier fetch saved this exact transfer before its response was lost, inspect the transfer and explicitly call pantheon_transfer_ack with this path; ACK rechecks its complete hash and durable save. Otherwise choose a new path.')
        staging = '.pantheon-transfer-' + secrets.token_hex(16)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic exclusive publication; unlike rename this cannot replace a racer.
            os.link(staging, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(staging, dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(staging, dir_fd=directory)
            except FileNotFoundError:
                pass


def metadata_only(value):
    return {k: value[k] for k in ('id', 'chat', 'sender', 'recipient', 'name', 'reason', 'size', 'sha256',
                                 'base_revision', 'state', 'created', 'expires') if k in value}


def execute(service, name, args, binding):
    if binding.get('runtime') not in ('codex', 'hermes'):
        raise Blocked('Temporary transfers are available only to compatible Codex/Hermes agents')
    _, _, client = service.member(args['connection'], binding)
    service.require_allowance()
    if name in ('pantheon_transfer_send', 'pantheon_transfer_fetch'):
        availability = client.request('/v1/chat').get('temporary_transfers', {})
        if availability.get('protocol') != 'pantheon.transfers.v1' or availability.get('available') is not True:
            raise Blocked('Temporary transfers are unavailable for this chat; preserve the current connection and local files')
        service.require_allowance()
    if name == 'pantheon_transfer_send':
        content = read_file(args['path'])
        payload = {k: args[k] for k in ('recipient', 'name', 'reason', 'key', 'base_revision') if k in args}
        payload.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        service.require_allowance()
        metadata = client.request('/v1/transfers', payload)
        integrity(metadata, content)
        service.require_allowance()
        value = client.transfer_bytes('/v1/transfers/' + quote(metadata['id'], safe='') + '/content', content)
        return {'untrusted_transfer_metadata': metadata_only(value), 'notice': 'Temporary handoff only; preserve your authoritative local copy.'}
    path = '/v1/transfers/' + quote(args['transfer_id'], safe='')
    metadata = client.request(path)
    service.require_allowance()
    if name == 'pantheon_transfer_inspect':
        return {'untrusted_transfer_metadata': metadata_only(metadata)}
    if name == 'pantheon_transfer_fetch':
        content = client.transfer_bytes(path + '/content')
        integrity(metadata, content)
        service.require_allowance()
        safe_save(args['path'], content)
        return {'untrusted_transfer_metadata': metadata_only(metadata), 'durably_saved': True,
                'notice': 'Saved and verified; no ACK sent. Use explicit pantheon_transfer_ack with this chosen path after reviewing receipt.'}
    if name == 'pantheon_transfer_ack':
        content = read_file(args['path'], durable=True)
        integrity(metadata, content)
        service.require_allowance()
        value = client.request(path + '/ack', dict(size=len(content), sha256=metadata['sha256'], durable=True))
        return {'untrusted_transfer_metadata': metadata_only(value), 'notice': 'Explicit durable receipt recorded; this is not approval or execution.'}
    raise Blocked('Unknown transfer tool')
