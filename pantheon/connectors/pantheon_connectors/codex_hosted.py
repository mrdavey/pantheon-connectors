"""Portable native MCP router: state is private to calling thread and invitation."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from .codex_audit import Audit
from .version import CODEX_VERSION
from .codex_daemon import native_identity
from .codex_delivery import DEFAULT_DAEMON, GUI_QUEUE, NATIVE_SETUP, descriptor, selected
from .codex_mcp import Server, WAKE_TOOLS
from .codex_setup import prepare, cli_compatibility
from .codex_compatibility import require_capabilities
from .hosted_discovery import TRUSTED_ORIGIN, fetch_discovery, validate_hosted_admission
from .onboarding import TOOLS, Onboarding, invitation, private_directory, private_lock, validate_arguments
from .state import load_private, save_private
from .worker import Blocked
from . import notification_policy, runtime_policy
from .broker import connect
from .broker import codex_setup_policy

PREPARE = dict(name='pantheon_prepare', description='Check public discovery and CLI availability, or reuse exact saved setup without daemon inspection. Fresh setup requires inspect_default_daemon:true only after scoped human consent. Never redeems or activates runtime.', inputSchema=dict(type='object',properties={'link':{'type':'string'},'deliberate':{'type':'boolean'},'inspect_default_daemon':{'type':'boolean'}},required=['link','deliberate'],additionalProperties=False))
AUTHORIZE = dict(
    name='pantheon_authorize',
    description='Only after explicit human approval of daemon access, provider usage and connection lifetime or finite duration. New preparations have no notification quota. Activates this invitation scope; does not join or start a receiver.',
    inputSchema={
        'type':'object',
        'properties': {
            'connection':{'type':'string'}, 'deliberate':{'type':'boolean'},
            'expires_at':{'type':'number'}, 'runtime_policy':runtime_policy.SCHEMA,
            'max_notices':{'type':'integer','minimum':1,'maximum':8},
            'notification_policy':notification_policy.SCHEMA,
        },
        'required':['connection','deliberate'],
        'oneOf':[{'required':['expires_at']},{'required':['runtime_policy']}],
        'not':{'required':['max_notices','notification_policy']},
        'additionalProperties':False,
    })


def unavailable_review(*_):
    raise Blocked('Native setup form is unavailable; no approval was recorded')


def runtime_active(config):
    mode = selected(config)
    return runtime_policy.allowed(config) and (mode == GUI_QUEUE or
        (config.get('allow_daemon_access') is True and config.get('allow_tool_output') is True))

class HostedServer:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'pantheon' / 'hosted'
        self.children = {}
        self.lock = threading.RLock()
        self.catalogue = None
        self.setup_policy = codex_setup_policy.Policy(self.root.parent / 'setup-policy.json')
        # Native tool approval settings are loaded with the host conversation.
        # Saving preferences cannot refresh an already-running host. Fence this
        # process until normal native reload starts a server with that policy.
        try:
            loaded = self.setup_policy.read()
            self.loaded_setup_id = loaded['id'] if self.setup_policy.active(loaded) else None
        except (Blocked, OSError, ValueError, KeyError, TypeError):
            self.loaded_setup_id = None  # Keep status/revoke diagnostics available.
        self.elicit = unavailable_review
        metadata = descriptor()
        self.version = metadata.get('package_version', CODEX_VERSION) if metadata else CODEX_VERSION
        self.setup_enabled = bool(metadata and metadata.get('setup_contract') == codex_setup_policy.CONTRACT)
        if metadata and metadata.get('broker_contract') == 'pantheon.broker.v1':
            from .broker.adapter import ToolSnapshot
            self.catalogue = ToolSnapshot(TRUSTED_ORIGIN, TOOLS)

    def start_notifications(self, notify):
        if self.catalogue:
            self.catalogue.watch(self.broker_active, notify)

    def broker_active(self):
        with self.lock:
            paths = [child.setup_file for child in self.children.values()]
        for path in paths:
            try:
                config = load_private(path)
                if config.get('mcp') == {'contract': 'pantheon.broker.v1'} and runtime_policy.allowed(config):
                    return True
            except (OSError, ValueError):
                pass
        return False

    def location(self, thread, connection):
        if not re.fullmatch(r'[0-9a-f]{32}', connection): raise Blocked('Invalid invitation connection')
        return self.root / hashlib.sha256(thread.encode()).hexdigest() / connection / 'setup.json'

    def handle(self, method, params, *, _setup_connect=False):
        if method == 'initialize': return dict(protocolVersion=params.get('protocolVersion','2025-06-18'),serverInfo=dict(name='pantheon',version=self.version),capabilities={'tools':{'listChanged': True} if self.catalogue else {}})
        if method == 'ping': return {}
        if method == 'tools/list':
            if self.catalogue:
                self.catalogue.refresh()
            tools=[{**tool,'inputSchema':dict(type='object',properties={'connection':{'type':'string'}},required=['connection'],additionalProperties=False)} if tool['name']=='pantheon_doctor' else tool for tool in (self.catalogue.tools() if self.catalogue else TOOLS)]
            setup = [codex_setup_policy.TOOL] if self.setup_enabled else [PREPARE, AUTHORIZE]
            connector = codex_setup_policy.CONNECT_TOOL if self.setup_enabled else connect.TOOL
            return {'tools':[connector]+setup+tools+WAKE_TOOLS}
        if method != 'tools/call': raise Blocked('Unsupported plugin method')
        thread = native_identity(params.get('_meta'))
        name,args=params.get('name'),params.get('arguments',{})
        if not isinstance(args,dict): raise Blocked('Expected tool arguments')
        metadata=descriptor()
        if not metadata or metadata.get('version')!=3 or metadata.get('service_origin')!=TRUSTED_ORIGIN: raise Blocked('Portable package trust configuration is missing')
        if self.setup_enabled:
            if name == 'pantheon_setup':
                if ('action' not in args or set(args) - {'action', 'expires_at'}
                        or args['action'] not in ('status', 'review', 'revoke', 'change')
                        or ('expires_at' in args and args['action'] not in ('review', 'change'))):
                    raise Blocked('Use setup status, review, change or revoke; model approval arguments are not accepted')
                if 'expires_at' in args:
                    codex_setup_policy.validate_deadline(args['expires_at'])
                if args['action'] == 'status':
                    return self.result(self.setup_status(thread))
                if args['action'] == 'revoke':
                    return self.result(self.setup_policy.revoke())
                return self.result(self.review_setup(thread, change=args['action'] == 'change', expires_at=args.get('expires_at')))
            if name in ('pantheon_prepare', 'pantheon_authorize') and not _setup_connect:
                raise Blocked('Use pantheon_connect; this package derives fresh grants only from native setup review')
        if 'link' in args:
            token=invitation(args['link'],TRUSTED_ORIGIN)
            connection=hashlib.sha256((TRUSTED_ORIGIN+'\0'+token).encode()).hexdigest()[:32]
        else: connection=args.get('connection','')
        path=self.location(thread,connection)
        if name=='pantheon_preview':
            validate_arguments(name,args)
            # Public invitation inspection needs no saved credentials or daemon access.
            return self.result(Onboarding({'url':TRUSTED_ORIGIN}).preview(args['link']))
        with self.lock:
            if name == 'pantheon_connect':
                (codex_setup_policy.validate_connect if self.setup_enabled else connect.validate)(args)
                for directory in [self.root.parent, self.root, path.parent.parent, path.parent]:
                    private_directory(directory.absolute())
                def inspect():
                    if not path.exists() and not path.is_symlink():
                        return {}
                    config = load_private(path)
                    if config['binding']['session'] != thread or config.get('invitation_connection') != connection:
                        raise Blocked('Existing setup binding differs')
                    grant = config.get('hosted_runtime_grant')
                    return dict(previous=grant is not None,
                        connection=connection,
                        active=bool(grant and runtime_active(config)),
                        lifetime=({'runtime_policy': grant['runtime_policy']} if grant and 'runtime_policy' in grant else {'expires_at': grant['expires_at']} if grant else None),
                        provider={'provider': config.get('reviewed_provider'), 'account': config.get('reviewed_account')})
                def call(tool, values):
                    return self.handle('tools/call', {**params, 'name': tool, 'arguments': values}, _setup_connect=True)['structuredContent']
                review = connect.Review(path.parent.parent / 'connect-review.json', {'runtime': 'codex', 'session': thread})
                if self.setup_enabled:
                    prior = inspect()
                    if prior.get('previous') and not prior.get('active'):
                        return self.result(dict(status='runtime_inactive', next_action='stop_inactive_grant'))
                    if not self.setup_policy.recall(thread):
                        reviewed = self.review_setup(thread, expires_at=args.get('expires_at'))
                        if reviewed['status'] != 'setup_active':
                            return self.result(reviewed)
                lock = self.setup_policy.path.with_suffix('.lock') if self.setup_enabled else path.parent.parent / 'connect.lock'
                with private_lock(lock):
                    if self.setup_enabled:
                        status = self.setup_status(thread)
                        if status['status'] != 'setup_active':
                            return self.result(status)
                        args = self.setup_policy.connect_arguments(thread, args)
                    value = connect.run(args, inspect=inspect,
                        recall=(lambda: self.setup_policy.recall(thread)) if self.setup_enabled else review.recall,
                        remember=(lambda *_: None) if self.setup_enabled else review.remember,
                        preview=lambda: call('pantheon_preview', {'link': args['link']}),
                        prepare=lambda approved: call('pantheon_prepare', dict(link=args['link'], deliberate=True, inspect_default_daemon=approved)),
                        authorize=lambda consent: call('pantheon_authorize', dict(connection=connection, deliberate=True,
                            **{k: consent[k] for k in ('runtime_policy', 'expires_at') if k in consent})),
                        join=lambda: call('pantheon_join', dict(link=args['link'], deliberate=True,
                            **({'name': args['name']} if 'name' in args else {}))))
                return self.result(value)
            if name=='pantheon_prepare':
                if (not {'link','deliberate'} <= set(args) or set(args)-{'link','deliberate','inspect_default_daemon'}
                        or args['deliberate'] is not True
                        or ('inspect_default_daemon' in args and type(args['inspect_default_daemon']) is not bool)):
                    raise Blocked('Deliberate origin selection and an optional boolean inspection consent are required')
                # Create ancestors privately too, never inherit a permissive default mkdir.
                for directory in [self.root.parent,self.root,self.root/hashlib.sha256(thread.encode()).hexdigest(),path.parent]: private_directory(directory.absolute())
                audit=Audit(path,plugin_version=metadata['package_version'])
                with audit.operation(name,thread=thread,connection=connection):
                    from . import hosted_v3
                    broker = metadata.get('broker_contract') == hosted_v3.CONTRACT
                    discovery = hosted_v3.fetch(TRUSTED_ORIGIN, 'codex') if broker else fetch_discovery(TRUSTED_ORIGIN,'codex')
                    package=discovery['packages']['codex']
                    installed = hosted_v3.installed_codex(metadata) if broker else None
                    if broker:
                        hosted_v3.check_installed(discovery, 'codex', installed)
                    elif package['version']!=metadata.get('package_version') or package['source_revision']!=metadata.get('source_revision'):
                        raise Blocked('Installed package differs from hosted discovery')
                    if path.exists():
                        with private_lock(path.parent/'setup.lock'):
                            config=load_private(path)
                            if config['binding']['session']!=thread or config.get('invitation_connection')!=connection: raise Blocked('Existing setup binding differs')
                            saved = config.get('hosted_onboarding', {})
                            if broker and saved.get('schema') != hosted_v3.SCHEMA:
                                raise Blocked('Existing preparation has a different connector contract; preserve its grant for deliberate migration')
                            if not broker and saved.get('packages', {}).get('codex') != package:
                                raise Blocked('Existing preparation package differs; preserve its grant and membership for explicit recovery')
                            refreshed = hosted_v3.reprepare_config(config, discovery, installed, config['binding']) if broker else dict(config, hosted_onboarding=discovery)
                            validate_hosted_admission(refreshed,refreshed['binding'])
                            if not broker and not runtime_policy.same_scope(refreshed, config):
                                raise Blocked('Hosted discovery scope changed; preserve existing setup for review')
                            if refreshed != config:
                                save_private(path, refreshed)
                            config = refreshed
                        value=dict(status='existing_setup_preserved',runtime_enabled=config.get('allow_runtime_turns') is True)
                    else:
                        if args.get('inspect_default_daemon') is not True:
                            executable, version = cli_compatibility()
                            require_capabilities(executable)
                            return self.result(dict(status='inspection_consent_required',connection=connection,
                                cli_version=version,offline_capabilities_verified=True,native_owner_verified=False,runtime_authorized=False,
                                runtime_options=['connection','finite'],next_action='review_missing_consent',
                                next='Preview the room without daemon access. Obtain only missing scoped consent: inspect and access this conversation through the broader local daemon API, then join and autonomously fetch/ACK/reply using its existing model/provider until disconnect or room closure/expiry (or an explicitly requested finite window). If inspection reveals a different scope, stop and review only that difference. Paste-link intent alone is not this consent.'))
                        mode = self.setup_policy.read().get('delivery', DEFAULT_DAEMON) if self.setup_enabled else DEFAULT_DAEMON
                        value=prepare(path,TRUSTED_ORIGIN,'default',delivery=mode,native_thread=thread,inspect_default_daemon=mode == DEFAULT_DAEMON)
                        config=load_private(path);config.update(hosted_onboarding=discovery,hosted_consent=True,invitation_connection=connection,connection_runtime_supported=True)
                        if broker:
                            config = hosted_v3.prepare_config(config, discovery, installed, config['binding'])
                        validate_hosted_admission(config,config['binding']);save_private(path,config)
                    value.pop('setup_file',None)
                    existing_grant = config.get('hosted_runtime_grant') is not None
                    authorized = runtime_active(config)
                    value.update(connection=connection,
                        reviewed_provider=config.get('reviewed_provider'),
                        account_scope=dict(login_metadata_pinned='reviewed_account' in config,exact_account_verified=False,
                            limitation='The native API does not identify every provider account/workspace. Exact-account automatic delivery remains unqualified.'),
                        runtime_authorized=authorized,
                        runtime_enabled=authorized,
                        native_owner_verified=value['status'] != 'existing_setup_preserved' and selected(config) == DEFAULT_DAEMON,
                        next_action=('join_with_existing_checks' if authorized else 'stop_inactive_grant') if existing_grant else 'authorize_with_settled_consent',
                        runtime_options=[] if existing_grant else (['connection','finite'] if config.get('connection_runtime_supported') is True else ['finite']),
                        next=(('Reuse the existing approval without another confirmation; join/recovery must still verify the pinned owner, provider and membership. No new daemon inspection was performed.' if authorized else
                               'Existing approval is inactive; stop. Do not renew, reactivate or ask to repeat the same approval. Report the expiry/disabled state and preserve it.') if existing_grant else
                              'If the observed provider and scope match the human review, authorize and join without another confirmation. Ask only for missing or materially changed scope. Preparation alone enables no runtime.'))
                    if selected(config) == GUI_QUEUE:
                        value['account_scope'] = dict(login_metadata_pinned=False, exact_account_verified=False,
                            limitation='The queue uses this desktop conversation\'s existing configuration; provider/account identity is not exposed by this route.')
                        if authorized:
                            value['next'] = 'Reuse the original approval; verify the pinned queue helper, native thread/home and membership. No daemon or provider/account inspection is available on this route.'
                    from .broker.bootstrap import prepared
                    prepared(config, connection, config['binding'])
                    return self.result(value)
            child=self.children.get((thread,connection))
            if child is None:
                try:
                    config=load_private(path)
                except FileNotFoundError:
                    raise Blocked('No prepared Pantheon setup exists for this invitation in this native conversation. Prepare this exact invitation here first, reusing settled consent; do not reuse another connection handle.') from None
                if config['binding']['session']!=thread or config.get('invitation_connection')!=connection: raise Blocked('No matching prepared invitation')
                child=Server(path);child.audit=Audit(path,plugin_version=metadata['package_version']);self.children[(thread,connection)]=child
            if name=='pantheon_authorize':
                with child.audit.operation(name,thread=thread,connection=connection):
                    fields=set(args)
                    if (not {'connection','deliberate'} <= fields or args['deliberate'] is not True
                            or fields - {'connection','deliberate','expires_at','runtime_policy','max_notices','notification_policy'}
                            or ('expires_at' in args) == ('runtime_policy' in args)
                            or {'max_notices','notification_policy'} <= fields):
                        raise Blocked('Explicit runtime lifetime and notification consent required')
                    connection_lifetime = 'runtime_policy' in args
                    if connection_lifetime and args['runtime_policy'] != {'mode':'connection'}:
                        raise Blocked('Unsupported runtime policy')
                    expiry = 0 if connection_lifetime else args['expires_at']
                    proposed={k:args[k] for k in ('max_notices','notification_policy') if k in args}
                    if not proposed:
                        prepared = load_private(path)
                        if prepared.get('notification_policy') != {'mode':'unlimited'}:
                            raise Blocked('Legacy setup needs its original explicit quota; new preparation defaults to no quota')
                        proposed = {'notification_policy':{'mode':'unlimited'}}
                    notification_policy.policy(proposed, 'codex')
                    if not connection_lifetime and (type(expiry) not in (int,float) or not math.isfinite(expiry) or not time.time()<expiry<=time.time()+3600): raise Blocked('Consent must expire within one hour; notification policy does not extend runtime')
                    with private_lock(path.parent/'setup.lock'):
                        config=load_private(path);validate_hosted_admission(config,config['binding'])
                        grant=dict(connection=connection,expires_at=expiry,**proposed)
                        if connection_lifetime:
                            grant['runtime_policy'] = {'mode':'connection'}
                        previous=config.get('hosted_runtime_grant')
                        if previous is not None:
                            expected = dict(previous)
                            if connection_lifetime: expected.pop('runtime_started_at', None)
                            if expected != grant: raise Blocked('Existing consent window and budget cannot be renewed or reset')
                            active = runtime_active(config)
                            return self.result(dict(status='existing_authorization_preserved',connection=connection,expires_at=expiry,**proposed,**({'runtime_policy':{'mode':'connection'}} if connection_lifetime else {}),runtime_authorized=active,next_action='join_with_existing_checks' if active else 'stop_inactive_grant',receiver_started=False))
                        if self.setup_enabled:
                            preference = self.setup_policy.read()
                            if preference['expires_at'] and (connection_lifetime or expiry > preference['expires_at']):
                                raise Blocked('Chat consent exceeds the fixed setup window; preserve its original deadline')
                            config = self.setup_policy.attach(path, config, thread)
                        if connection_lifetime:
                            if (config.get('connection_runtime_supported') is not True
                                    or config.get('allow_runtime_turns') is not False
                                    or config.get('trial_expires_at') != 0 or 'runtime_policy' in config):
                                raise Blocked('Connection lifetime requires a fresh disabled preparation; preserve previous consent')
                            started = time.time()
                            grant['runtime_started_at'] = started
                            config.update(runtime_policy={'mode':'connection'}, runtime_started_at=started)
                        daemon = selected(config) == DEFAULT_DAEMON
                        config.update(allow_runtime_turns=True,allow_daemon_access=daemon,allow_tool_output=daemon,trial_expires_at=expiry,hosted_runtime_grant=grant)
                        config.pop('max_notices',None);config.pop('notification_policy',None)
                        config.update(proposed)
                        save_private(path,config)
                    return self.result(dict(status='authorized',connection=connection,expires_at=expiry,**proposed,**({'runtime_policy':{'mode':'connection'}} if connection_lifetime else {}),receiver_started=False))
            if name=='pantheon_doctor': params={**params,'arguments':{}}
            result = child.handle(method,params)
            if name == 'pantheon_doctor' and self.catalogue:
                from .broker.bootstrap import read
                config = load_private(path)
                result['structuredContent']['bootstrap'] = read(config, connection, config['binding'])
                result['structuredContent']['catalogue'] = dict(status=self.catalogue.status,
                    revision=self.catalogue.revision, refresh='native_turn_boundary_or_supported_reload',
                    immediate_refresh_qualified=False,
                    notice='Preserve this conversation and its connection. A tool-list notification does not prove the current model turn has refreshed its tools.')
                result['content'] = [{'type': 'text', 'text': json.dumps(result['structuredContent'])}]
            return result

    def review_setup(self, thread, *, change=False, expires_at=None):
        def inspect(path, delivery=DEFAULT_DAEMON):
            prepare(path, TRUSTED_ORIGIN, 'default', delivery=delivery,
                    native_thread=thread, inspect_default_daemon=delivery == DEFAULT_DAEMON)
            return load_private(path)
        result = self.setup_policy.review(thread, self.elicit, inspect, change=change, expires_at=expires_at,
                                         choose_host=(descriptor() or {}).get('delivery') == NATIVE_SETUP)
        return {**result, **self.setup_status(thread)} if result['status'] == 'setup_active' else result

    def setup_status(self, thread):
        status = self.setup_policy.status(thread)
        value = self.setup_policy.read()
        if value:
            status['delivery'] = value.get('delivery', DEFAULT_DAEMON)
        if status['status'] == 'setup_active' and self.setup_policy.read()['id'] != self.loaded_setup_id:
            if status['delivery'] == GUI_QUEUE:
                return {**status, 'status': 'native_reload_required', 'membership_started': False,
                    'next_action': 'end_turn_then_reload_same_conversation',
                    'notice': 'Setup saved. End this turn. Restart the Codex desktop app normally, reopen this same chat, then Continue. Reuse the original invitation and deadline; no chat grant, membership or receiver has started. Do not start a CLI copy of this conversation or retry tools to bypass reload.'}
            return {**status, 'status': 'native_reload_required', 'membership_started': False,
                    'next_action': 'end_turn_then_reload_same_conversation',
                    'notice': 'Setup saved. End this turn with the normal native reload instruction: /quit, then codex resume with this conversation\'s trusted native ID, then Continue. Wait for the old CLI to exit before resuming. Reuse the original invitation and deadline; no chat grant, membership or receiver has started. Do not retry tools or restart the shared daemon to bypass reload.'}
        return status

    @staticmethod
    def result(value): return dict(content=[dict(type='text',text=json.dumps(value))],structuredContent=value)
    def close(self):
        if self.catalogue:
            self.catalogue.close()
        for child in self.children.values(): child.close()
