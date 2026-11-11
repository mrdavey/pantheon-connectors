"""Bounded temporary handoffs; no execution, extraction, overwrite or implicit ACK."""
import hashlib
import os
import re
import secrets
import stat
from urllib.parse import quote
from .file_security import PATH_CHANGED, file_identity, inode, read_regular, verified_file
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


def read_file(path, *, boundary, durable=False):
    parts = boundary.target(path)
    with boundary.directory(parts[:-1]) as directory:
        return read_regular(directory, parts[-1], MAX_BYTES, minimum=1, durable=durable)


def integrity(metadata, content):
    if (type(metadata.get('size')) is not int or not 1 <= metadata['size'] <= MAX_BYTES or
            not isinstance(metadata.get('sha256'), str) or
            re.fullmatch('[0-9a-f]{64}', metadata['sha256']) is None or
            len(content) != metadata['size'] or hashlib.sha256(content).hexdigest() != metadata['sha256']):
        raise Blocked('Transfer size or SHA-256 integrity verification failed; no receipt sent')


def check_destination(directory, name):
    directory.verify()
    info = os.fstat(directory.fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Blocked('Choose a destination directory owned by you and not writable by others')
    try:
        directory.stat(name)
    except FileNotFoundError:
        return
    raise Blocked('Destination exists; never replace local work. If an earlier fetch saved this exact transfer before its response was lost, inspect the transfer and explicitly call pantheon_transfer_ack with this path; ACK rechecks its complete hash and durable save. Otherwise choose a new path.')


def safe_save(directory, name, content):
    check_destination(directory, name)
    staging = '.pantheon-transfer-' + secrets.token_hex(16)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
    with os.fdopen(fd, 'wb') as stream:
        created = inode(os.fstat(fd))
        try:
            stream.write(content)
            stream.flush()
            before = os.fstat(fd)
            os.fsync(fd)
            check_destination(directory, name)
            if before.st_nlink != 1 or file_identity(verified_file(directory, staging, fd)) != file_identity(before):
                raise Blocked(PATH_CHANGED)
            # Publish exclusively. The staging name and chosen path briefly
            # refer to the same file. No external source may have multiple links.
            os.link(staging, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd, follow_symlinks=False)
            if verified_file(directory, name, fd).st_nlink != 2:
                raise Blocked(PATH_CHANGED)
            os.unlink(staging, dir_fd=directory.fd)
            published = verified_file(directory, name, fd)
            os.fsync(directory.fd)
            if (published.st_nlink != 1 or published.st_size != len(content)
                    or published.st_mtime_ns != before.st_mtime_ns
                    or file_identity(verified_file(directory, name, fd)) != file_identity(published)):
                raise Blocked(PATH_CHANGED)
        finally:
            try:
                if inode(directory.stat(staging)) == created:
                    os.unlink(staging, dir_fd=directory.fd)
            except FileNotFoundError:
                pass


def metadata_only(value):
    return {k: value[k] for k in ('id', 'chat', 'sender', 'recipient', 'name', 'reason', 'size', 'sha256',
                                 'base_revision', 'state', 'created', 'expires') if k in value}


def execute(service, name, args, binding, *, boundary=None):
    if binding.get('runtime') not in ('codex', 'hermes'):
        raise Blocked('Temporary transfers are available only to compatible Codex/Hermes agents')
    if name != 'pantheon_transfer_inspect' and boundary is None:
        raise Blocked('Local file operations require a folder approved in native Pantheon setup')
    _, _, client = service.member(args['connection'], binding)
    service.require_allowance()
    if name in ('pantheon_transfer_send', 'pantheon_transfer_fetch'):
        availability = client.request('/v1/chat').get('temporary_transfers', {})
        if availability.get('protocol') != 'pantheon.transfers.v1' or availability.get('available') is not True:
            raise Blocked('Temporary transfers are unavailable for this chat; preserve the current connection and local files')
        service.require_allowance()
    if name == 'pantheon_transfer_send':
        content = read_file(args['path'], boundary=boundary)
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
        parts = boundary.target(args['path'])
        with boundary.directory(parts[:-1]) as directory:
            service.require_allowance()
            check_destination(directory, parts[-1])
            content = client.transfer_bytes(path + '/content')
            integrity(metadata, content)
            service.require_allowance()
            safe_save(directory, parts[-1], content)
        return {'untrusted_transfer_metadata': metadata_only(metadata), 'durably_saved': True,
                'notice': 'Saved and verified; no ACK sent. Use explicit pantheon_transfer_ack with this chosen path after reviewing receipt.'}
    if name == 'pantheon_transfer_ack':
        content = read_file(args['path'], boundary=boundary, durable=True)
        integrity(metadata, content)
        service.require_allowance()
        value = client.request(path + '/ack', dict(size=len(content), sha256=metadata['sha256'], durable=True))
        return {'untrusted_transfer_metadata': metadata_only(value), 'notice': 'Explicit durable receipt recorded; this is not approval or execution.'}
    raise Blocked('Unknown transfer tool')
