"""Trusted HTTPS discovery and inert package staging. No installation or grants."""
import hashlib
import gzip
import io
import math
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
from urllib.parse import urlsplit
import urllib.request
from .client import Client
from .tls import client_context
from .worker import Blocked

TRUSTED_ORIGIN = "https://pantheon.talk"
SCHEMA = "pantheon.hosted-onboarding.v2"
LEGACY_SCHEMA = "pantheon.hosted-onboarding.v1"
_NATIVE_SURFACES = {"codex": "app-server-tool-output", "hermes": "native-adapter"}
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def https_origin(value):
    if not isinstance(value, str):
        raise Blocked("Expected a trusted HTTPS service origin")
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.path or p.query or p.fragment or p.netloc != p.netloc.lower()
            or p.port not in (None, 443)):
        raise Blocked("Expected a trusted HTTPS service origin")
    return value


def validate_discovery(value, trusted_origin, runtime, *, now=None):
    base = https_origin(trusted_origin)
    if not isinstance(value, dict) or value.get("schema") not in (SCHEMA, LEGACY_SCHEMA) or value.get("service_origin") != base:
        raise Blocked("Hosted discovery does not match the trusted service")
    expiry = value.get("expires_at")
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= (time.time() if now is None else now):
        raise Blocked("Hosted discovery expired or has no finite expiry")
    admission = value.get("admission", {})
    if (not isinstance(admission, dict) or admission.get("enabled") is not True
            or not isinstance(admission.get("runtimes"), list) or runtime not in admission["runtimes"]):
        raise Blocked("Hosted admission is not enabled for this connector")
    packages = value.get("packages")
    package = packages.get(runtime) if isinstance(packages, dict) else None
    if not isinstance(package, dict) or package.get("runtime") != runtime or package.get("format") != "tar.gz":
        raise Blocked("Hosted package descriptor is unavailable")
    for field, pattern in (("sha256", r"[0-9a-f]{64}"), ("source_revision", r"[0-9a-f]{40}"),
                           ("version", r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")):
        if not isinstance(package.get(field), str) or not re.fullmatch(pattern, package[field]):
            raise Blocked("Hosted package integrity/version fields are invalid")
    artifact = package.get("artifact_url")
    if not isinstance(artifact, str):
        raise Blocked("Hosted artifact URL is missing")
    p = urlsplit(artifact)
    if (p.scheme + "://" + p.netloc != base or p.username or p.password or p.query or p.fragment
            or package["sha256"] not in p.path.split("/") or "%" in p.path
            or any(part in (".", "..") for part in p.path.split("/"))):
        raise Blocked("Artifact must be immutable same-origin HTTPS")
    host = package.get("host")
    legacy = value["schema"] == LEGACY_SCHEMA
    if (not legacy and runtime == 'codex' and isinstance(host, dict)
            and host.get('compatibility') == 'native-capabilities'):
        if (set(host) != {'compatibility', 'tested_version'}
                or not isinstance(host['tested_version'], str) or not 0 < len(host['tested_version']) <= 128):
            raise Blocked("Hosted native compatibility is invalid")
        return value
    fields = {"runtime_version", "surface"} if legacy else {"runtime_version"}
    if (runtime not in _NATIVE_SURFACES or not isinstance(host, dict) or set(host) != fields
            or not isinstance(host.get("runtime_version"), str) or not 0 < len(host["runtime_version"]) <= 128
            or (legacy and host.get("surface") != _NATIVE_SURFACES[runtime])):
        raise Blocked("Hosted native compatibility is invalid")
    return value


def matches_native_host(package, binding, *, surfaces=None):
    """Compatibility is local policy, never an operator-selectable mechanism."""
    runtime = package.get("runtime")
    return (runtime in _NATIVE_SURFACES and binding.get("runtime") == runtime
            and (package['host'].get('runtime_version') == binding.get('version')
                 or (runtime == 'codex' and package['host'].get('compatibility') == 'native-capabilities'
                     and isinstance(binding.get('version'), str) and 0 < len(binding['version']) <= 128))
            and binding.get("surface") in (surfaces if surfaces is not None else [_NATIVE_SURFACES[runtime]]))


def fetch_discovery(trusted_origin, runtime, *, client_factory=Client):
    https_origin(trusted_origin)
    return validate_discovery(client_factory(trusted_origin, "").request("/v1/onboarding"), trusted_origin, runtime)


def validate_hosted_admission(config, binding, *, client_factory=Client):
    if config.get('hosted_onboarding', {}).get('schema') == 'pantheon.hosted-onboarding.v3':
        from .hosted_v3 import admission
        return admission(config, binding, client_factory=client_factory)
    if config.get("hosted_consent") is not True:
        raise Blocked("Deliberate consent for this hosted service is required")
    runtime, base = binding["runtime"], config["url"]
    saved = validate_discovery(config.get("hosted_onboarding"), base, runtime)
    fresh = fetch_discovery(base, runtime, client_factory=client_factory)
    old, new = saved["packages"][runtime], fresh["packages"][runtime]
    if old != new:
        raise Blocked("Hosted package changed; review and prepare the new package explicitly")
    if not matches_native_host(new, binding):
        raise Blocked("Native host does not match the reviewed hosted package")
    return fresh


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_package(descriptor, runtime, output, *, opener=None, trusted_origin=TRUSTED_ORIGIN):
    """Download, hash then stage a new private tree; never execute or install it.

    Caller must first validate discovery against its independently trusted origin.
    Revalidation here rejects expiry/revocation in the supplied snapshot; live
    admission is checked again before redemption. Failed extraction is preserved
    for inspection; existing destinations are never overwritten.
    """
    if descriptor.get('schema') == 'pantheon.hosted-onboarding.v3':
        from .hosted_v3 import validate
        validate(descriptor, trusted_origin, runtime)
    else:
        validate_discovery(descriptor, trusted_origin, runtime)
    package = descriptor["packages"][runtime]
    opener = opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=client_context()))
    with opener.open(package["artifact_url"], timeout=20) as response:
        if response.geturl() != package["artifact_url"]:
            raise Blocked("Artifact redirects are prohibited")
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != package["sha256"]:
        raise Blocked("Artifact exceeds the limit or fails SHA-256 verification")
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)) or output.exists():
        raise Blocked("Package staging requires a new nonsymlink directory")
    # Bound the entire decompressed stream, including PAX headers/padding,
    # before tarfile can allocate memory for attacker-sized metadata.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(MAX_EXPANDED + 1)
    if len(expanded) > MAX_EXPANDED:
        raise Blocked("Expanded archive stream exceeds the staging limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        members, names, total = [], set(), 0
        for entry in archive:
            name = PurePosixPath(entry.name)
            if (len(members) >= 2048 or not entry.name or name.is_absolute() or ".." in name.parts
                    or ".git" in name.parts
                    or "\\" in entry.name or str(name) in (".", "") or str(name) in names
                    or not (entry.isfile() or entry.isdir()) or entry.size < 0):
                raise Blocked("Unsafe or duplicate package archive entry")
            names.add(str(name))
            total += entry.size
            if total > MAX_EXPANDED:
                raise Blocked("Expanded package exceeds the staging limit")
            members.append(entry)
        files = {str(PurePosixPath(entry.name)) for entry in members if entry.isfile()}
        if any(str(parent) in files for entry in members for parent in PurePosixPath(entry.name).parents):
            raise Blocked("Archive file conflicts with a parent directory")
        # Validate all entries before making any file executable or writing output.
        output.mkdir(mode=0o700, parents=False)
        for entry in members:
            target = output / entry.name
            if entry.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(entry) as source, target.open("xb") as dest:
                    while chunk := source.read(1024 * 1024):
                        dest.write(chunk)
                target.chmod(0o700 if entry.mode & 0o111 else 0o600)
    return {"directory": str(output), "sha256": package["sha256"], "installed": False}
