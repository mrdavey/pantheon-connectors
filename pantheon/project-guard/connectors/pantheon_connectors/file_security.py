"""Descriptor-bound access to a folder selected in native Codex setup."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import stat
from unicodedata import normalize

from .feature_defaults import folders as default_folders
from .worker import Blocked

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
PRIVATE_NAMES = {
    '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.config', '.hermes', '.codex',
    '.claude', '.git', '.git-credentials', '.docker', '.netrc', '.npmrc', '.pypirc',
    '.password-store', '.token', 'keychains', 'keyrings',
    'application_default_credentials.json', 'service-account.json',
    'service_account.json', 'client_secret.json',
}
PRIVATE_STEMS = {
    'auth', 'credentials', 'token', 'tokens', 'access_token', 'access_tokens',
    'refresh_token', 'refresh_tokens',
}
CREDENTIAL_EXTENSIONS = {'json', 'txt', 'db', 'sqlite', 'yaml', 'yml', 'toml', 'ini', 'xml'}
KEY_STEMS = {'private_key', 'private-key', 'id_rsa', 'id_dsa', 'id_ecdsa',
             'id_ecdsa_sk', 'id_ed25519', 'id_ed25519_sk'}
PRIVATE_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.ppk')
PATH_CHANGED = 'Local path changed during verification. Choose a stable path in the folder selected during native setup.'


def absolute_parts(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
        raise Blocked('Choose an explicit absolute local path')
    parts = tuple(value.split('/')[1:])
    if any(part in ('', '.', '..') for part in parts):
        raise Blocked('Choose a normalized absolute local path without traversal')
    return parts


def folded(parts):
    return tuple(normalize('NFC', part).casefold() for part in parts)


def private_name(name):
    stem, *extensions = name.split('.')
    return (name in PRIVATE_NAMES or stem in KEY_STEMS
            or (stem in PRIVATE_STEMS and (not extensions or extensions[0] in CREDENTIAL_EXTENSIONS))
            or name.startswith('.env') or name.endswith(PRIVATE_SUFFIXES))


class FileBoundary:
    def __init__(self, folder, capability, native_home):
        self.root = absolute_parts(folder)
        native_parts = native_home.parts[1:]
        self.native_home = folded(native_parts)
        managed = default_folders(native_home / 'pantheon')[capability]
        self.managed = folder == managed
        # Also protect configured homes whose names do not identify them as
        # credentials. Resolve these profile roots only, never file arguments.
        configured = (('HERMES_HOME', '~/.hermes'), ('CLAUDE_CONFIG_DIR', '~/.claude'),
                      ('XDG_CONFIG_HOME', '~/.config'))
        self.other_profiles = []
        for key, default in configured:
            path = Path(os.environ.get(key) or default).expanduser().resolve()
            profile = folded(path.parts[1:])
            if not (key == 'XDG_CONFIG_HOME' and self.managed
                    and self.native_home[:len(profile)] == profile):
                self.other_profiles.append(profile)
        self.check_names(self.root)

    def check_names(self, parts):
        names = folded(parts)
        if (any(names[:len(profile)] == profile for profile in self.other_profiles)
                or (not self.managed and names[:len(self.native_home)] == self.native_home)
                or any(private_name(name) for index, name in enumerate(names)
                       if not (self.managed and index < len(self.native_home) and name in ('.codex', '.config')))):
            raise Blocked('Credential and profile paths are blocked. Choose a shareable path in the folder selected during native setup.')

    def target(self, value, *, directory=False):
        parts = absolute_parts(value)
        if parts[:len(self.root)] != self.root or (not directory and parts == self.root):
            raise Blocked('Choose a path inside the folder selected during native setup')
        self.check_names(parts)
        return parts

    @contextmanager
    def directory(self, parts):
        # Retain the whole named chain. Relative opens cannot be redirected to a
        # replacement ancestor. Rechecking every link detects detached parents.
        with ExitStack() as stack:
            fd = os.open('/', DIRECTORY_FLAGS)
            stack.callback(os.close, fd)
            current = Directory(fd)
            for name in parts:
                current = stack.enter_context(current.child(name))
            current.verify()
            yield current


def inode(info):
    return info.st_dev, info.st_ino


def file_identity(info):
    return (*inode(info), info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class Directory:
    def __init__(self, fd, parent=None, name=None):
        self.fd = fd
        self.parent = parent
        self.name = name
        self.inode = inode(os.fstat(fd))

    def stat(self, name):
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    @contextmanager
    def child(self, name, *, expected=None):
        before = expected if expected is not None else self.stat(name)
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=self.fd)
        try:
            child = Directory(fd, self, name)
            if inode(before) != child.inode:
                raise Blocked(PATH_CHANGED)
            yield child
        finally:
            os.close(fd)

    def verify(self):
        current = self
        while current.parent is not None:
            try:
                named = current.parent.stat(current.name)
            except FileNotFoundError:
                raise Blocked(PATH_CHANGED) from None
            if not stat.S_ISDIR(named.st_mode) or inode(named) != current.inode:
                raise Blocked(PATH_CHANGED)
            current = current.parent


def verified_file(directory, name, fd):
    try:
        named = directory.stat(name)
    except FileNotFoundError:
        raise Blocked(PATH_CHANGED) from None
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode) or file_identity(named) != file_identity(opened):
        raise Blocked(PATH_CHANGED)
    directory.verify()
    return opened


def read_regular(directory, name, limit, *, minimum=0, durable=False):
    before = directory.stat(name)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise Blocked('Choose a regular file without symlinks or hardlinks')
    if not minimum <= before.st_size <= limit:
        raise Blocked(f'Local file must contain {minimum:,} to {limit:,} bytes')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fd)
    with os.fdopen(fd, 'rb') as source:
        if file_identity(verified_file(directory, name, fd)) != file_identity(before):
            raise Blocked(PATH_CHANGED)
        data = source.read(limit + 1)
        if len(data) != before.st_size or file_identity(verified_file(directory, name, fd)) != file_identity(before):
            raise Blocked(PATH_CHANGED)
        if durable:
            os.fsync(fd)
            os.fsync(directory.fd)
            if file_identity(verified_file(directory, name, fd)) != file_identity(before):
                raise Blocked(PATH_CHANGED)
    return data
