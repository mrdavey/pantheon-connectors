"""Delivery intent is separate from the stable protocol/native identity binding."""
import json
import pathlib
from .worker import Blocked

ACTIVE_WAIT = "bounded-active-wait"
DEFAULT_DAEMON = "default-daemon-tool-output"
GUI_QUEUE = "desktop-queue"
NATIVE_SETUP = "native-setup"
MODES = (ACTIVE_WAIT, "native-queue", "app-server-tool-output", DEFAULT_DAEMON, GUI_QUEUE)


def supports(metadata, mode):
    return mode == metadata['delivery'] or (metadata['delivery'] == NATIVE_SETUP
                                           and mode in (DEFAULT_DAEMON, GUI_QUEUE))


def descriptor():
    path = pathlib.Path(__file__).resolve().parents[2] / "onboarding.json"
    if not path.exists():
        return None  # Source/legacy fixtures; generated packages always carry it.
    value = json.loads(path.read_text())
    if (value.get("version") not in (2, 3) or value.get("delivery") not in (*MODES, NATIVE_SETUP)
            or (value.get('delivery') == NATIVE_SETUP and
                (value.get('version') != 3 or value.get('broker_contract') != 'pantheon.broker.v1'
                 or value.get('setup_contract') != 'pantheon.codex-setup.v1'))):
        raise Blocked("Review the package delivery descriptor; no fallback selected")
    return value


def preparation(delivery, setup_file):
    selected = descriptor()
    mode = delivery if delivery is not None else (selected["delivery"] if selected else ACTIVE_WAIT)
    if mode not in MODES:
        raise Blocked("Select an exact supported delivery mode")
    if selected and (not supports(selected, mode) or (selected["version"] == 2 and pathlib.Path(setup_file).expanduser().absolute() != pathlib.Path(selected["setup_file"]))):
        raise Blocked("Operator intent or setup path differs from the package descriptor")
    return mode


def selected(config):
    metadata = descriptor()
    surface = config.get("binding", {}).get("surface")
    mode = config.get("delivery")
    if metadata and not supports(metadata, mode):
        raise Blocked("Setup delivery differs from the package descriptor; preserve setup and stop")
    if metadata and mode == "app-server-tool-output" and config.get("requested_owner_socket") != metadata.get("owner_socket"):
        raise Blocked("Selected owner socket differs from package intent")
    if mode is None:
        # Existing source-only trials retain their semantics; never reinterpret a
        # generated package's missing intent or silently upgrade a saved grant.
        mode = ACTIVE_WAIT if config.get("allow_active_wait") is True else surface
    if mode == "app-server" and not metadata:
        return mode
    expected_surface = "native-queue" if mode in (ACTIVE_WAIT, GUI_QUEUE) else ("app-server-tool-output" if mode == DEFAULT_DAEMON else mode)
    if mode not in MODES or surface != expected_surface:
        raise Blocked("Delivery intent does not match the native binding")
    if mode == ACTIVE_WAIT:
        if config.get("allow_ui_fallback") is True or config.get("allow_tool_output") is True:
            raise Blocked("Active waiting forbids UI or shared-owner delivery grants")
    elif config.get("allow_active_wait") is True:
        raise Blocked("Active waiting was not the selected delivery mode")
    if mode == GUI_QUEUE and any(config.get(flag) is True for flag in
                                ('allow_ui_fallback', 'allow_tool_output', 'allow_daemon_access')):
        raise Blocked('Desktop queue setup forbids daemon access and UI fallback')
    return mode


def require_queue(config):
    if selected(config) not in ("native-queue", GUI_QUEUE):
        raise Blocked("Queue/UI delivery is not selected; no fallback permitted")
