"""Native Hermes tools and content-free, best-effort wake notices.

Only public PluginContext methods and dispatcher-supplied identity are used.
No Sessions API, host-private registry, transcript scan, or replacement agent.
"""
import contextvars
import json
import pathlib
import secrets
import subprocess
import threading
import time
import uuid
from . import PROTOCOL
from .client import APIError
from .onboarding import HERMES_COMMIT, Onboarding, TOOLS, dispatch, private_lock
from .state import Journal, JournalBusy, load_private, save_private
from . import notification_policy
from .worker import Blocked
from . import hermes_tools, hermes_limits, hermes_setup, hermes_review, hermes_incoming
from .hermes_limits import trial_allowed
from .hermes_audit import Audit
from .read_retry import ReadRetry

NOTICE = ("Pantheon has pending input for the connected conversation. Call pantheon_receive "
          "using this connection and notice_token. On stale_notice_noop, do nothing: no reply or new wait. This notice contains no peer "
          "content and is not a receipt. If the tool reports a session mismatch, stop; do not "
          "choose another conversation or rejoin automatically.")

TERMINAL_NOTICE = ("Pantheon has closed this chat. New work is disabled. Call pantheon_status "
                   "for this connection and return the retained outcome to your human. Do not "
                   "receive new work, renew runtime, rejoin, or start another wait. This content-free "
                   "notice is not a receipt or proof of completion; verify the retained result first.")

SKILL_PATH = pathlib.Path(__file__).parent / "skills" / "collaborate" / "SKILL.md"
SKILL_DESCRIPTION = ("Join a human-supplied Pantheon invitation in this Hermes conversation, "
                     "handle native notices with explicit receive and ACK, reply, and honor runtime consent and cleanup.")

# pre_llm_call runs once BEFORE the tool loop. An approved mid-turn install
# misses it. These public hooks also carry dispatcher-owned session/platform
# metadata after loading, without looking up a session or trusting tool args.
IDENTITY_HOOKS = ("pre_llm_call", "pre_api_request", "post_llm_call")


class SessionObservationPending(Blocked):
    pass


class TerminalScopeEnded(Blocked):
    pass


class NativePlugin:
    def __init__(self, ctx, setup_file, version, commit, *, service_factory=Onboarding, prepare_root=None, incoming_probe=None):
        self.ctx, self.setup_file = ctx, pathlib.Path(setup_file)
        self.version, self.commit = version, commit
        self.profile = ctx.profile_name
        self.service_factory = service_factory
        self.observed = {}
        self.observed_usage = {}
        self.active = {}
        self.receiver_stops = {}
        self.outcomes = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.expiry_thread = None
        self.audit = Audit(self.setup_file)
        self.audited_limits = None
        self.prepare_root = prepare_root
        self.prepared = {}
        self.catalogue = None
        self.loaded_catalogue_revision = None
        self.live_bridge = None
        self.incoming_probe = incoming_probe or hermes_incoming.allowed

    def broker_active(self):
        with self.lock:
            plugins = [self, *self.prepared.values()]
        for plugin in plugins:
            try:
                config = load_private(plugin.setup_file)
                if config.get('mcp') == {'contract': 'pantheon.broker.v1'} and trial_allowed(config):
                    return True
            except (OSError, ValueError):
                pass
        return False

    def audit_limits(self, config):
        try:
            limits = hermes_limits.describe(config)
        except Exception:
            return  # Diagnostics must not change admission/cleanup semantics.
        snapshot = {key: limits[key] for key in ("duration_seconds", "notice_limit", "notification_policy", "trial_expires_at", "runtime_policy", "runtime_authorized")}
        if snapshot != self.audited_limits:
            if self.audit.record("limits_observed", limits=snapshot):
                self.audited_limits = snapshot

    def observe(self, session_id="", platform="", model=None, provider=None, **_):
        # Host hook metadata, never a model argument or a chosen "latest" session.
        with self.lock:
            if session_id and self.ctx.profile_name == self.profile and not self.stop.is_set():
                self.observed[session_id] = platform
                # Public hooks only; never inspect request/auth payloads.
                usage = dict(self.observed_usage.get(session_id, {}))
                if model is not None:
                    usage['model'] = model if isinstance(model, str) and 0 < len(model) <= 256 else None
                if provider is not None:
                    usage['model_provider'] = provider if isinstance(provider, str) and 0 < len(provider) <= 256 else None
                self.observed_usage[session_id] = usage
                for child in self.prepared.values():
                    child.observe(session_id=session_id, platform=platform, model=model, provider=provider)

    def usage(self, binding):
        with self.lock:
            usage = dict(self.observed_usage.get(binding['session'], {}))
        if not all(isinstance(usage.get(k), str) and usage[k] for k in ('model', 'model_provider')):
            raise Blocked('The native host has not exposed provider/model metadata for this conversation. Keep setup disabled; use the same-conversation native observation path, never guessed configuration.')
        return usage

    def check_usage(self, config, binding):
        if 'reviewed_provider' in config and self.usage(binding) != config['reviewed_provider']:
            raise Blocked('Native provider/model changed since approval; stop this connection and review only the changed scope')

    def route_prepared(self, name, args, session_id):
        binding = self.binding(session_id)
        if name == 'pantheon_connect':
            return self.connect(args, session_id, binding)
        if name == "pantheon_prepare":
            self.preflight(binding)
            setup, result = hermes_setup.prepare(self.prepare_root, binding, args, usage=self.usage(binding))
        else:
            # Existing explicitly configured loopback invitations keep their path.
            if isinstance(args, dict) and str(args.get("link", "")).startswith("http://127.0.0.1:"):
                return None
            # A hosted preparation must not hide a prior explicit legacy
            # connection in this same conversation. Only an existing exact
            # native binding may select it; never discover another state tree.
            connection = args.get("connection") if isinstance(args, dict) else None
            if isinstance(connection, str) and len(connection) == 32 and all(c in "0123456789abcdef" for c in connection) and self.setup_file.exists():
                legacy = load_private(self.setup_file)
                saved = pathlib.Path(legacy["state_dir"]) / (connection + ".json")
                if saved.exists() or saved.is_symlink():
                    self.service_factory(legacy).member(connection, binding, allow_expired=True)
                    return None
            setup = hermes_setup.select(self.prepare_root, binding, args)
            if setup is None:
                return None
            result = None
        with self.lock:
            if self.stop.is_set():
                raise Blocked("Native plugin was unloaded")
            child = self.prepared.get(setup)
            if child is None:
                child = NativePlugin(self.ctx, setup, self.version, self.commit, service_factory=self.service_factory,
                                     incoming_probe=self.incoming_probe)
                child.catalogue = self.catalogue
                child.loaded_catalogue_revision = self.loaded_catalogue_revision
                child.observed = dict(self.observed)
                child.observed_usage = {key: dict(value) for key, value in self.observed_usage.items()}
                self.prepared[setup] = child
                if load_private(setup).get("allow_runtime_turns") is True:
                    child.watch_expiry()
        if result is not None:
            child.audit.record("tool_finished", operation=name, session=session_id, outcome="completed")
            return json.dumps(result)
        return child.handle(name, args, session_id=session_id)

    def connect(self, args, session_id, binding):
        from .broker import connect
        from .onboarding import private_directory, private_lock
        connect.validate(args)
        self.preflight(binding)
        usage = self.usage(binding)
        connection = hermes_setup.handle_for(args['link'])
        private_directory(self.prepare_root)
        directory = private_directory(hermes_setup.conversation_directory(self.prepare_root, binding))
        setup = directory / connection / 'setup.json'
        def inspect():
            if not setup.exists() and not setup.is_symlink():
                return {}
            config = load_private(setup)
            if config.get('prepared_binding') != binding or config.get('prepared_connection') != connection:
                raise Blocked('Existing setup binding differs')
            self.check_usage(config, binding)
            grant = hermes_limits.anchored_grant(config)
            lifetime = None
            if grant:
                lifetime = ({'runtime_policy': grant['runtime_policy']} if 'runtime_policy' in grant else
                            {'expires_at': grant.get('approved_expires_at', grant['trial_expires_at'])})
            return dict(previous=bool(grant), active=bool(grant and trial_allowed(config)),
                        lifetime=lifetime, provider=usage, connection=connection)
        def call(tool, values):
            return json.loads(self.route_prepared(tool, values, session_id))
        def authorize(consent):
            values = dict(connection=connection, deliberate=True,
                **{k: consent[k] for k in ('runtime_policy', 'expires_at') if k in consent})
            if 'expires_at' in consent:
                values['link'] = args['link']
            return call('pantheon_authorize', values)
        def prepare(_approved):
            # connect.run reaches this only with explicit or still-valid saved consent.
            # Missing host permission must precede local grant writes and redemption.
            self.require_incoming()
            return call('pantheon_prepare', dict(link=args['link'], deliberate=True))
        review = connect.Review(directory / 'connect-review.json', binding)
        with self.lock, private_lock(directory / 'connect.lock'):
            value = connect.run(args, inspect=inspect, recall=review.recall, remember=review.remember,
                preview=lambda: self.service_factory({'url': hermes_setup.TRUSTED_ORIGIN}).preview(args['link']),
                prepare=prepare,
                authorize=authorize,
                join=lambda: call('pantheon_join', dict(link=args['link'], deliberate=True,
                    **({'name': args['name']} if 'name' in args else {}))))
        from .feature_defaults import folders
        value['default_folders'] = folders(pathlib.Path(self.prepare_root) / 'features')
        return json.dumps(value)

    def require_incoming(self):
        hermes_incoming.require(self.profile, self.incoming_probe)

    def binding(self, session_id):
        if not session_id or self.ctx.profile_name != self.profile:
            raise Blocked("Native session/profile metadata is unavailable or changed")
        return dict(runtime="hermes", version=self.version, surface="native-adapter", profile=self.profile,
                    session=session_id, return_route="hermes:" + session_id)

    def preflight(self, binding):
        if self.stop.is_set():
            raise Blocked("Native plugin was unloaded; use only the currently loaded instance")
        if self.commit != HERMES_COMMIT:
            raise Blocked("Hermes build differs from the inspected plugin contract")
        if binding != self.binding(binding["session"]):
            raise Blocked("Native runtime identity changed")
        with self.lock:
            platform = self.observed.get(binding["session"])
        if platform is None:
            raise SessionObservationPending("This conversation's host identity has not yet been observed; mid-turn loading can cause this. "
                "Keep runtime/injection off. After this turn ends, check pantheon_doctor once in the same conversation; "
                "do not reinstall, restart, choose another session, or grant access to fix this state.")
        if platform not in ("tui", "desktop"):
            raise Blocked("This trial needs an observed TUI/Desktop session; CLI and new-channel routing are not silently substituted")
        if not callable(getattr(self.ctx, "inject_message", None)):
            raise Blocked("Native notification hook is unavailable")

    def doctor(self, session_id):
        result = dict(runtime="hermes", version=self.version, profile=self.profile,
                      native_session_known=bool(session_id), receipt_mode="explicit_scoped_receive_then_agent_ack",
                      wake_mode="content_free_native_notice", released=False)
        try:
            binding = self.binding(session_id)
            self.preflight(binding)
            result["native_binding_observed"] = True
            config = load_private(self.setup_file)
            if config.get("prepared_binding",binding) != binding:
                raise Blocked("Prepared setup belongs to another native conversation; preserve its scope")
            result["trial_authorized"] = trial_allowed(config)
            result["limits"] = hermes_limits.describe(config)
            result["cleanup"] = config.get("cleanup", {"status": "not_recorded"})
            result["onboarding"] = hermes_review.progress(config,binding)
            if config.get('mcp') is not None and config.get('prepared_connection'):
                from .broker.bootstrap import read
                result['bootstrap'] = read(config, config['prepared_connection'], binding)
            result['provider_scope'] = dict(reviewed=config.get('reviewed_provider'), account_verified=False,
                limitation='The pinned Hermes hooks expose provider/model, not a stable account identity or atomic dispatch-time account check.')
            self.check_usage(config, binding)
            if result['trial_authorized']:
                self.require_incoming()
                result['host_incoming_permission'] = 'verified_enabled'
            result["status"] = "trial_preflight_passed" if result["trial_authorized"] else "trial_approval_required"
        except hermes_incoming.PermissionRequired as error:
            result.update(error.result())
        except SessionObservationPending as error:
            result.update(status="session_observation_pending", native_binding_observed=False,
                          next_action="check_doctor_next_turn_same_conversation", reason=str(error))
        except Blocked as error:
            result.update(status="blocked",next_action="report_specific_blocker_preserve_scope_without_restarting", reason=str(error))
        except FileNotFoundError:
            result.update(status="setup_required", next_action="prepare_original_invitation_without_an_extra_confirmation",
                          reason="Use pantheon_prepare with the human-supplied invitation; preparation stays disabled and needs no separate start or duration question.")
        except Exception:
            result.update(status="blocked",next_action="preserve_saved_setup_report_specific_loading_error",
                          reason="Saved setup is unavailable or invalid; preserve it and report the specific loading blocker.")
        result["limitations"] = ["Incoming permission is a distinct scope: reuse applicable approval or include only missing scope in one review. Native host prompts remain mandatory; scheduling may be refused.",
            "A queued notice is not durable receipt; restart/rollover/lost notice requires an explicit receive or recovery decision.",
            "No in-flight cancellation or independently verified human handback."]
        with self.lock:
            result["receivers"] = dict(self.outcomes)
        result["audit"] = self.audit.health()
        if self.catalogue:
            result['catalogue'] = dict(status=self.catalogue.status, revision=self.loaded_catalogue_revision,
                refresh='portable_mcp_live_tools', reload_required=False,
                notice='Use the Pantheon portable MCP tools through native Tool Search. The MCP catalogue refreshes independently; retain this conversation, connection and original consent.')
        return result

    def handle(self, name, args, *, session_id="", **_):
        if self.prepare_root is not None:
            try:
                result = self.route_prepared(name, args, session_id)
                if result is not None:
                    return result
            except hermes_incoming.PermissionRequired as error:
                return json.dumps(error.result())
            except (Blocked, APIError) as error:
                return json.dumps({"status": "blocked", "reason": error.guidance() if isinstance(error, APIError) else str(error),
                    "next_action":"report_specific_blocker_preserve_scope_without_restarting"})
            except Exception:
                return json.dumps({"status": "blocked", "reason": "Prepared state is unavailable; preserve it for recovery"})
        request, started = uuid.uuid4().hex, time.monotonic()
        values = args if isinstance(args, dict) else {}
        metadata = dict(operation=name, command=values.get("op"), connection=values.get("connection"), session=session_id,
                        request=request, key=values.get("key"), notice_token=values.get("notice_token"))
        self.audit.record("tool_started", **metadata)
        result = self._handle(name, args, session_id=session_id, audit_request=request)
        try:
            value = json.loads(result)
            ids = list(values.get("message_ids", [])) if isinstance(values.get("message_ids"), list) else []
            if isinstance(value.get("id"), str): ids.append(value["id"])
            for row in value.get("messages", []):
                identity = row.get("untrusted_member_content", {}).get("message", {}).get("id")
                if isinstance(identity, str): ids.append(identity)
            if isinstance(value.get("connection"), str): metadata["connection"] = value["connection"]
            outcome = value.get("status")
            if outcome not in ("blocked", "stale_notice_noop", "notice_budget_exhausted", "runtime_not_authorized"):
                outcome = "completed"
            self.audit.record("tool_finished", **metadata, outcome=outcome, message_ids=ids,
                              duration_ms=int((time.monotonic()-started)*1000))
        except Exception:
            self.audit.record("tool_finished", **metadata, outcome="error")
        return result

    def _handle(self, name, args, *, session_id="", audit_request=None):
        try:
            if name == "pantheon_doctor":
                return json.dumps(self.doctor(session_id))
            if self.stop.is_set():
                raise Blocked("Native plugin was unloaded; use only the currently loaded instance")
            binding = self.binding(session_id)
            if name != "pantheon_preview":
                # Saved membership is not evidence that this loaded instance has
                # observed the current host surface. Recheck every scoped operation.
                self.preflight(binding)
            config = load_private(self.setup_file)
            if config.get("prepared_binding", binding) != binding:
                raise Blocked("Prepared setup belongs to another native conversation")
            if name == "pantheon_authorize":
                self.require_incoming()
                self.check_usage(config, binding)
                value = hermes_setup.authorize(self.setup_file, binding, args, preview=self.service_factory(config).preview)
                value["onboarding"] = hermes_review.progress(load_private(self.setup_file),binding)
                value["next_action"] = value["onboarding"]["next_action"]
                return json.dumps(value)
            if name in ('pantheon_ack', 'pantheon_status') and not trial_allowed(config):
                recovered = self.terminal_cleanup(name, args, session_id)
                if recovered is not None:
                    return json.dumps(recovered)
            withdrawing = name == "pantheon_command" and isinstance(args, dict) and args.get("op") == "withdraw"
            terminal_report = (name == "pantheon_command" and isinstance(args, dict)
                and args.get("op") == "report" and args.get("target") in ("completed", "failed", "time_ran_out", "disconnected"))
            if not withdrawing and not terminal_report and name != 'pantheon_preview':
                self.check_usage(config, binding)
            if not withdrawing:
                self.audit_limits(config)
            if not withdrawing and name not in ("pantheon_preview", "pantheon_join", "pantheon_status") and not trial_allowed(config):
                self.expire(config)
            if name == "pantheon_join" and args.get("deliberate") is True and not trial_allowed(config):
                raise Blocked("A valid owner-approved anchored Hermes allowance is required before joining")
            if name == 'pantheon_join' or (name == 'pantheon_command' and args.get('op') == 'ready'):
                self.require_incoming()
            service = self.service_factory(load_private(self.setup_file))
            def allowance():
                current = load_private(self.setup_file)
                self.check_usage(current, binding)
                return trial_allowed(current)
            service.allowance_check = allowance
            if (hermes_limits.anchored_grant(config) and not withdrawing and not terminal_report
                    and name not in ("pantheon_preview", "pantheon_join") and trial_allowed(config)):
                self.require_live_connection(service, args.get("connection"), binding, config)
            if name in {tool["name"] for tool in hermes_tools.TOOLS}:
                hermes_tools.validate(name, args)
                connection = args["connection"]
                if name == "pantheon_status":
                    limits = self.connection_limits(service, connection, binding)
                    if not limits["runtime_authorized"]:
                        return json.dumps(dict(status="runtime_not_authorized", limits=limits,
                            receiver=self.receiver_state(connection), audit=self.audit.health(), notice="Local-only status; no server read or renewal."))
                if name == "pantheon_resume":
                    _, view = hermes_tools.view(service, connection, binding)
                    if view["chat"]["state"] not in ("waiting", "active"):
                        raise Blocked("Chat is not accepting receiver recovery")
                    self.preflight(binding)
                    service.require_allowance()
                    limits = self.connection_limits(service, connection, binding)
                    if limits["budget_exhausted"]:
                        return json.dumps(dict(status="notice_budget_exhausted", limits=limits,
                            receiver=self.receiver_state(connection), notice="No receiver started; approved budget is exhausted. Existing queued notices can still be received within runtime allowance."))
                    started = self.start(connection, binding)
                    return json.dumps(dict(status="receiver_started" if started else "receiver_already_present",
                        receiver=self.receiver_state(connection), limits=self.connection_limits(service, connection, binding),
                        notice="Existing notice and budget fences remain intact. No delivery, ACK or renewal is implied."))
                value = hermes_tools.read(service, name, args, binding)
                if name == "pantheon_status":
                    value["receiver"] = self.receiver_state(connection)
                    value["limits"] = limits
                    value["audit"] = self.audit.health()
                return json.dumps(value)
            if name == "pantheon_command" and args.get("op") == "withdraw":
                # Stop before the remote request: a lost response must not leave
                # the receiver running. Explicit resume verifies server membership.
                from .onboarding import validate_arguments
                validate_arguments(name, args)
                service.member(args["connection"], binding, allow_expired=True)
                self.stop_connection(args["connection"])
                if hermes_limits.anchored_grant(config):
                    self.fence_connection_runtime(config)
            value = dispatch(service, name, args, binding, self.preflight, lambda: self.doctor(session_id))
            if name == "pantheon_join" and value.get("connection"):
                if "hermes_finite_runtime_grant" in config:
                    chat = self.require_live_connection(service, value["connection"], binding, config)
                    value["runtime"] = hermes_setup.finite_disclosure(config,
                        chat.get("expiry") if chat.get("state") == "active" else None)
                self.start(value["connection"], binding)
            return json.dumps(value)
        except hermes_incoming.PermissionRequired as error:
            self.audit.record('tool_error', operation=name, session=session_id, request=audit_request,
                              outcome='blocked', error=error)
            return json.dumps(error.result())
        except (Blocked, APIError) as error:
            # Closure can race an already-running model turn between receive and
            # explicit ACK. Only a verified terminal fence permits this separate
            # recovery path; transport/auth failures never trigger fallback.
            if isinstance(error, Blocked):
                try:
                    recovered = self.terminal_cleanup(name, args, session_id)
                    if recovered is not None:
                        return json.dumps(recovered)
                except (Blocked, APIError) as cleanup_error:
                    error = cleanup_error
                except Exception:
                    pass  # Preserve the original blocked result on broken local evidence.
            self.audit.record("tool_error", operation=name, session=session_id, request=audit_request,
                              outcome="blocked", error=error, http_status=getattr(error, "status", None))
            return json.dumps({"status": "blocked", "reason": error.guidance() if isinstance(error, APIError) else str(error),
                    "next_action":"report_specific_blocker_preserve_scope_without_restarting",
                    **({'retry_same_join': True} if name == 'pantheon_join'
                       and isinstance(error, APIError) and error.retryable() else {})})
        except Exception as error:
            self.audit.record("tool_error", operation=name, session=session_id, request=audit_request,
                              outcome="error", error=error)
            return json.dumps({"status": "blocked", "reason": "Check private local setup and saved binding; no automatic rerouting occurred"})

    def start(self, connection, binding):
        self.require_incoming()
        with self.lock:
            if self.stop.is_set() or connection in self.active:
                return False
            self.receiver_stops[connection] = threading.Event()
            context = contextvars.copy_context()
            thread = threading.Thread(target=lambda: context.run(self.run, connection, binding), daemon=True,
                                      name="pantheon-native-receiver")
            self.active[connection] = thread
            self.watch_expiry()
            thread.start()
            self.audit.record("receiver_started", connection=connection, session=binding["session"])
            return True

    def connection_limits(self, service, connection, binding):
        path, _, _ = service.member(connection, binding, allow_expired=True)
        used = hermes_tools.saved_notice_count(path.with_suffix(".sqlite"), binding)
        return hermes_limits.describe(load_private(self.setup_file), used)

    def receiver_state(self, connection):
        with self.lock:
            thread = self.active.get(connection)
            event = self.receiver_stops.get(connection)
            return dict(running=bool(thread and thread.is_alive()),
                stop_requested=self.stop.is_set() or bool(event and event.is_set()),
                last_outcome=self.outcomes.get(connection, "not_started_in_this_loaded_instance"))

    def stop_connection(self, connection):
        with self.lock:
            self.receiver_stops.setdefault(connection, threading.Event()).set()
            self.outcomes[connection] = "withdrawal_requested"
            self.audit.record("receiver_stopped", connection=connection, operation="withdraw")

    def connection_stopped(self, connection):
        event = self.receiver_stops.get(connection)
        return bool(event and event.is_set())

    def watch_expiry(self):
        # Independent of network requests/receiver failures. No remote calls or
        # model requests; the loaded plugin owns and stops this thread.
        with self.lock:
            if self.stop.is_set() or (self.expiry_thread is not None and self.expiry_thread.is_alive()):
                return
            def monitor():
                while not self.stop.is_set():
                    try:
                        config = load_private(self.setup_file)
                        if not trial_allowed(config):
                            self.expire(config)
                            with self.lock:
                                self.outcomes["allowance"] = "trial_expired_host_cleanup_required"
                            return
                    except Exception as error:
                        with self.lock:
                            self.outcomes["allowance"] = "cleanup_blocked_check_private_setup"
                        self.audit.record("receiver_state", outcome="blocked", error=error)
                        return
                    self.stop.wait(.25)
            self.expiry_thread = threading.Thread(target=monitor, daemon=True, name="pantheon-trial-expiry")
            self.expiry_thread.start()

    def expire(self, config):
        """Fence the private allowance, without changing host profile permissions.

        No model turn, host API, membership withdrawal or renewed grant is needed.
        Host restoration has no supported plugin API and remains explicit cleanup.
        """
        with private_lock(self.setup_file.with_suffix(".cleanup.lock")):
            current = load_private(self.setup_file)
            if current != config or trial_allowed(current):
                return  # Do not overwrite a concurrent owner decision.
            if current.get('hermes_terminal_scope') and current.get('allow_runtime_turns') is False:
                return  # Closure already fenced work; preserve its original handback bound.
            if (current.get("cleanup", {}).get("status") == "runtime_fenced_host_cleanup_required"
                    and current.get("allow_runtime_turns") is False and current.get("trial_expires_at") == 0):
                return
            current.update(allow_runtime_turns=False, trial_expires_at=0,
                cleanup=dict(status="runtime_fenced_host_cleanup_required",
                    runtime_deadline=current.get("trial_expires_at"),
                    membership="withdraw_required_unless_already_withdrawn",
                    host="Restore prior injection permission when no other approved connection needs it; keep installed components idle for later deliberate invitations. No automatic host restoration API."))
            save_private(self.setup_file, current)
            self.audit.record("runtime_fenced", outcome="runtime_fenced_host_cleanup_required")

    def terminal_cleanup(self, name, args, session_id):
        if name not in ('pantheon_ack', 'pantheon_status'):
            return None
        if self.stop.is_set():
            raise Blocked('Native plugin was unloaded; use only the currently loaded instance')
        binding = self.binding(session_id)
        config = load_private(self.setup_file)
        if (not isinstance(args, dict) or config.get('allow_runtime_turns') is not False
                or config.get('prepared_binding', binding) != binding
                or config.get('hermes_terminal_scope') != {
                    'connection': args.get('connection'), 'binding': binding}):
            return None
        self.preflight(binding)
        self.check_usage(config, binding)
        from . import terminal_cleanup
        service = self.service_factory(config)
        if name == 'pantheon_status':
            hermes_tools.validate(name, args)
            return terminal_cleanup.status(service, args['connection'], binding)
        from .onboarding import validate_arguments
        validate_arguments(name, args)
        return terminal_cleanup.ack(service, args['connection'], binding, args['message_ids'], args['key'])

    def fence_connection_runtime(self, config, terminal_scope=None, terminal_notice_until=None):
        # Only the new explicit grant is fenced here; historical timers are unchanged.
        with private_lock(self.setup_file.with_suffix(".cleanup.lock")):
            current = load_private(self.setup_file)
            if current != config or not hermes_limits.anchored_grant(current):
                return
            current["allow_runtime_turns"] = False
            if terminal_scope is None:
                current.pop('hermes_terminal_scope', None)
                current.pop('hermes_terminal_notice_until', None)
            else:
                current['hermes_terminal_scope'] = terminal_scope
                if terminal_notice_until is not None:
                    current['hermes_terminal_notice_until'] = terminal_notice_until
            save_private(self.setup_file, current)
            self.audit.record("runtime_fenced", outcome="connection_scope_ended_host_cleanup_required")

    def require_live_connection(self, service, connection, binding, config):
        _, member, client = service.member(connection, binding)
        if any(member.get(key) != config.get(key) for key in
               ("runtime_policy", "runtime_started_at", "connection_runtime_supported")):
            raise Blocked("Membership runtime scope differs from its original connection grant")
        if ("hermes_finite_runtime_grant" in config and member.get("trial_expires_at") !=
                config["hermes_finite_runtime_grant"]["trial_expires_at"]):
            raise Blocked("Membership deadline differs from its original finite approval")
        try:
            view = client.request("/v1/chat")
        except APIError as error:
            if error.status in (401,403):
                self.fence_connection_runtime(config)
            raise
        chat = view.get("chat", {})
        own = chat.get("members", {}).get(view.get("member"), {})
        active = chat.get("state") in ("waiting", "active")
        if (view.get("role") != "agent" or own.get("revoked") is not False
                or own.get("binding") != binding):
            self.fence_connection_runtime(config)
            raise Blocked("Connection authority ended: room closed/expired or membership revoked/changed")
        if not active:
            terminal_scope = {'connection': connection, 'binding': binding} if chat.get('state') == 'closed' else None
            # One final, content-free handback notice may be queued only shortly
            # after observed closure and still inside the original approval.
            # Fencing new work happens first; this never extends a runtime grant.
            until = time.time() + 60
            if 'hermes_finite_runtime_grant' in config:
                until = min(until, config['hermes_finite_runtime_grant']['trial_expires_at'])
            if config.get('hermes_room_expires_at') is not None:
                until = min(until, config['hermes_room_expires_at'])
            if chat.get('expiry'):
                until = min(until, hermes_setup.expiry_timestamp(chat['expiry']))
            self.fence_connection_runtime(config, terminal_scope, until if terminal_scope else None)
            raise TerminalScopeEnded("Chat ended. New work is disabled; use pantheon_status for the retained result and ACK only already received messages.")
        if "hermes_finite_runtime_grant" in config and chat.get("state") == "active":
            # A later room extension must not move an already disclosed shorter end.
            observed = hermes_setup.expiry_timestamp(chat.get("expiry"))
            with private_lock(self.setup_file.with_suffix(".cleanup.lock")):
                current = load_private(self.setup_file)
                if current == config:
                    original = current["hermes_finite_runtime_grant"]["trial_expires_at"]
                    end = min(original,current.get("hermes_room_expires_at",original),observed)
                    if end < original and current.get("hermes_room_expires_at") != end:
                        current["hermes_room_expires_at"] = end
                        save_private(self.setup_file,current)
                        config["hermes_room_expires_at"] = end
        service.require_allowance()
        return chat

    @staticmethod
    def check_runtime_grant(journal, config):
        if not hermes_limits.anchored_grant(config):
            return
        current = json.dumps(hermes_limits.anchored_grant(config), sort_keys=True)
        saved = journal.control("hermes-runtime-grant")
        if saved is not None and saved != current:
            raise Blocked("Runtime approval changed under an existing journal; preserve its original scope")
        if saved is None:
            if hermes_tools.notice_count(journal.control("notice-count")):
                raise Blocked("Existing notification evidence cannot gain a connection-lifetime grant")
            journal.control("hermes-runtime-grant", current)

    @staticmethod
    def check_notice_policy(journal, config, count):
        current = notification_policy.policy(config, "hermes")
        saved = journal.control("notification-policy")
        if saved is not None:
            if json.loads(saved) != current:
                raise Blocked("Notification policy changed under an existing journal; preserve its scope")
        elif "notification_policy" in config:
            if count and current["mode"] == "unlimited":
                raise Blocked("Existing limited journal cannot silently become unlimited")
            journal.control("notification-policy", json.dumps(current, sort_keys=True))

    def tick(self, connection, binding):
        if self.stop.is_set():
            return "plugin_unloaded"
        if self.connection_stopped(connection):
            return "receiver_stopped"
        config = load_private(self.setup_file)
        self.audit_limits(config)
        if not trial_allowed(config):
            if config.get('hermes_terminal_scope') == {'connection': connection, 'binding': binding}:
                return 'terminal_scope_ended'
            self.expire(config)
            return "trial_expired"
        self.preflight(binding)
        self.check_usage(config, binding)
        service = self.service_factory(config)
        path, _, client = service.member(connection, binding)
        if hermes_limits.anchored_grant(config):
            self.require_live_connection(service, connection, binding, config)
        journal = Journal(path.with_suffix(".sqlite"), binding)
        try:
            with journal.exclusive():
                self.check_runtime_grant(journal, config)
                outstanding = journal.control("hermes-notice")
                if outstanding and json.loads(outstanding):
                    notice = json.loads(outstanding)
                    if notice.get('fetched') is not True:
                        return "idle_or_notice_unresolved"
                    remaining = client.request('/v1/pending')
                    if any(e['protocol'] != PROTOCOL or e['binding'] != binding for e in remaining):
                        raise Blocked('Pending native binding changed')
                    if any(e['delivery']['id'] in notice['deliveries'] for e in remaining):
                        return "idle_or_notice_unresolved"
                    journal.complete_notice(notice)
                # Legacy accepted/uncertain notices have no cancellation handle.
                # Never upgrade them into another wake automatically.
                if outstanding is None and journal.db.execute(
                        "SELECT 1 FROM controls WHERE id LIKE 'notice-%' AND id != 'notice-count' LIMIT 1").fetchone():
                    return "legacy_notice_unresolved"
                count = hermes_tools.notice_count(journal.control("notice-count"))
                self.check_notice_policy(journal, config, count)
                if notification_policy.exhausted(config, "hermes", count):
                    return "notice_budget_exhausted"
                pending = client.request("/v1/pending")
                if any(e["protocol"] != PROTOCOL or e["binding"] != binding for e in pending):
                    raise Blocked("Pending native binding changed")
                candidate = next((e for e in pending if e["delivery"]["state"] == "pending"
                                  and journal.control("notice-" + e["delivery"]["id"]) is None), None)
                if candidate is None:
                    return "idle_or_notice_unresolved"
                # The HTTP request can outlive unload, observation or allowance.
                # Serialize the final check/enqueue with close: after close returns,
                # a retained old context cannot schedule another notice on reload.
                with self.lock:
                    if self.stop.is_set():
                        return "plugin_unloaded"
                    if self.connection_stopped(connection):
                        return "receiver_stopped"
                    self.preflight(binding)
                    latest = load_private(self.setup_file)
                    self.check_usage(latest, binding)
                    if not trial_allowed(latest):
                        self.expire(latest)
                        return "trial_expired"
                    if notification_policy.exhausted(latest, "hermes", count):
                        return "notice_budget_exhausted"
                    self.check_notice_policy(journal, latest, count)
                    if hermes_limits.anchored_grant(latest):
                        self.require_live_connection(service, connection, binding, latest)
                    key = "notice-" + candidate["delivery"]["id"]
                    notice = dict(token=secrets.token_urlsafe(24),
                                  deliveries=[e["delivery"]["id"] for e in pending], outcome="outcome_unknown")
                    journal.control("hermes-notice", json.dumps(notice))
                    journal.control(key, "outcome_unknown")  # Durable BEFORE enqueue; never retry an uncertain notice.
                    journal.control("notice-count", str(count + 1))
                    self.audit.record("notice_attempt", connection=connection, session=binding["session"],
                                      notice_token=notice["token"], count=count+1, outcome="outcome_unknown",
                                      message_ids=[e.get("message", {}).get("id") for e in pending])
                    try:
                        accepted = self.ctx.inject_message(NOTICE + " Connection: " + connection + " notice_token: " + notice["token"], session_key=binding["session"])
                    except Exception:
                        raise Blocked('Native notice outcome is uncertain; preserve its journal and reconcile without replay') from None
                    notice["outcome"] = "scheduled_not_received" if accepted is True else "native_host_unavailable"
                    journal.control("hermes-notice", json.dumps(notice))
                    journal.control(key, notice["outcome"])
                    self.audit.record("notice_result", connection=connection, session=binding["session"],
                                      notice_token=notice["token"], count=count+1, outcome=notice["outcome"])
                # No claim, receipt, ACK, readiness, peer content, or alternate origin.
                return "scheduled_not_received" if accepted is True else "native_host_unavailable"
        except JournalBusy:
            # An explicit receive can own this same journal while the receiver
            # polls. Leave every fence/count intact and use run's normal wait.
            # The next tick rechecks expiry, stop state and native identity.
            return "journal_busy_retry"
        finally:
            journal.db.close()

    def terminal_notice(self, connection, binding):
        """At most one final notice; never replay an uncertain native enqueue."""
        from .terminal_cleanup import closed_view
        with self.lock:
            if self.stop.is_set() or self.connection_stopped(connection):
                return 'receiver_stopped'
            config = load_private(self.setup_file)
            until = config.get('hermes_terminal_notice_until')
            if (config.get('allow_runtime_turns') is not False
                    or config.get('hermes_terminal_scope') != {'connection': connection, 'binding': binding}
                    or not hermes_limits.finite(until) or time.time() >= until
                    or not (hermes_limits.finite_grant_matches(config)
                            or hermes_limits.connection_grant_matches(config))):
                return 'terminal_notice_unavailable'
            self.preflight(binding)
            self.check_usage(config, binding)
            path, _, _ = closed_view(self.service_factory(config), connection, binding)
            journal = Journal(path.with_suffix('.sqlite'), binding)
            try:
                with journal.exclusive():
                    self.check_runtime_grant(journal, config)
                    if journal.control('hermes-terminal-notice') is not None:
                        return 'terminal_notice_already_attempted'
                    count = hermes_tools.notice_count(journal.control('notice-count'))
                    self.check_notice_policy(journal, config, count)
                    if notification_policy.exhausted(config, 'hermes', count):
                        return 'notice_budget_exhausted'
                    # Authority reads may outlive the deadline or an owner edit.
                    if load_private(self.setup_file) != config or time.time() >= until:
                        return 'terminal_notice_unavailable'
                    self.preflight(binding)
                    self.check_usage(config, binding)
                    intent = dict(outcome='outcome_unknown', until=until)
                    journal.control('hermes-terminal-notice', json.dumps(intent))
                    journal.control('notice-count', str(count + 1))
                    self.audit.record('notice_attempt', connection=connection, session=binding['session'],
                                      count=count + 1, outcome='outcome_unknown')
                    try:
                        accepted = self.ctx.inject_message(TERMINAL_NOTICE + ' Connection: ' + connection,
                                                           session_key=binding['session'])
                    except Exception:
                        raise Blocked('Terminal notice outcome is uncertain; preserve its journal without replay') from None
                    outcome = 'scheduled_not_received' if accepted is True else 'native_host_unavailable'
                    journal.control('hermes-terminal-notice', json.dumps(dict(intent, outcome=outcome)))
                    self.audit.record('notice_result', connection=connection, session=binding['session'],
                                      count=count + 1, outcome=outcome)
                    return 'terminal_notice_' + outcome
            finally:
                journal.db.close()

    def run(self, connection, binding):
        retry = ReadRetry()
        try:
            while not self.stop.is_set() and not self.connection_stopped(connection):
                try:
                    try:
                        result = self.tick(connection, binding)
                    except TerminalScopeEnded:
                        result = 'terminal_scope_ended'
                    if result == 'terminal_scope_ended':
                        result = self.terminal_notice(connection, binding)
                    retry.reset()
                    delay = 5
                except APIError as error:
                    delay = retry.delay(error)
                    result = 'transport_retry'
                except JournalBusy:
                    delay = 1
                    result = 'journal_busy_retry'
                with self.lock:
                    previous = self.outcomes.get(connection)
                    self.outcomes[connection] = result
                if previous != result:
                    self.audit.record("receiver_state", connection=connection, session=binding["session"], outcome=result)
                if result == "trial_expired":
                    # The independent expiry watcher already fenced local work.
                    # Publish bounded metadata on the receiver, never that watcher.
                    from .lifecycle_status import report_terminal
                    config = load_private(self.setup_file)
                    grant = config.get("hermes_finite_runtime_grant", {})
                    end = grant.get("trial_expires_at") or config.get("cleanup", {}).get("runtime_deadline")
                    if isinstance(end, (int, float)) and 0 < end <= time.time():
                        report_terminal(self.service_factory(config), connection, binding, "time_ran_out", str(end))
                if result.startswith('terminal_notice_') or result in ("plugin_unloaded", "receiver_stopped", "trial_expired", "notice_budget_exhausted", "native_host_unavailable", "legacy_notice_unresolved"):
                    return
                self.receiver_stops[connection].wait(delay)
        except Exception as error:
            # Never log payloads, model inputs, or credentials.
            with self.lock:
                self.outcomes[connection] = "blocked_check_setup_binding_and_native_host"
            self.audit.record("receiver_state", connection=connection, session=binding["session"],
                              outcome="blocked_check_setup_binding_and_native_host", error=error,
                              http_status=getattr(error, "status", None))
            return
        finally:
            with self.lock:
                self.active.pop(connection, None)
            self.audit.record("receiver_stopped", connection=connection, session=binding["session"])

    def close(self):
        with self.lock:
            self.stop.set()
            for child in self.prepared.values():
                child.close()
            for event in self.receiver_stops.values():
                event.set()
            self.observed.clear()
            self.observed_usage.clear()
            self.audit.record("plugin_unloaded", outcome="plugin_unloaded")
        if self.live_bridge:
            self.live_bridge.close()


def register(ctx):
    """Called only by Hermes's approved native plugin loader."""
    import hermes_constants
    # Inspect the pinned installed source using Git, not host-private registries.
    root = pathlib.Path(hermes_constants.__file__).resolve().parent
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True).stdout.strip()
    home = hermes_constants.get_hermes_home()
    setup = ctx.get_config("setup_file", str(home / "pantheon" / "setup.json"))
    plugin = NativePlugin(ctx, setup, "git:" + commit, commit if not dirty else "dirty",
                          prepare_root=home / "pantheon" / "hosted")
    # Public content API: a useful workflow, read-only and owned/unloaded by Hermes.
    # Registration alone does not start a receiver, redeem, inject, or grant access.
    ctx.register_skill("collaborate", SKILL_PATH, SKILL_DESCRIPTION)
    for hook in IDENTITY_HOOKS:
        ctx.register_hook(hook, plugin.observe)
    ctx.on_unload(plugin.close)
    if plugin.setup_file.exists():
        try:
            if load_private(plugin.setup_file).get("allow_runtime_turns") is True:
                plugin.watch_expiry()
        except (OSError, ValueError):
            plugin.outcomes["allowance"] = "cleanup_blocked_check_private_setup"
    from .broker.package import metadata
    package = metadata()
    tools = TOOLS
    if package:
        from .broker.adapter import ToolSnapshot
        from .hosted_discovery import TRUSTED_ORIGIN
        plugin.catalogue = ToolSnapshot(TRUSTED_ORIGIN, TOOLS)
        # Registration also runs in Hermes's network-disabled Plugin Doctor.
        # Fetch only when a native Pantheon call is actually being attested;
        # the portable MCP process independently refreshes its advertised tools.
        plugin.loaded_catalogue_revision = plugin.catalogue.revision
        tools = plugin.catalogue.tools()
        # The portable companion can go live through the normal host installer.
        # Its calls acquire native identity only through this public hook.
        from .broker.hermes_live import NativeBridge
        plugin.live_bridge = NativeBridge(plugin, home)
        ctx.register_hook('pre_tool_call', plugin.live_bridge.attest)
        plugin.catalogue.watch(plugin.broker_active, lambda: None)
        ctx.on_unload(plugin.catalogue.close)
    names = {tool['name'] for tool in tools}
    from .broker.connect import TOOL as CONNECT_TOOL
    for tool in [CONNECT_TOOL, *tools, *(tool for tool in hermes_tools.TOOLS if tool['name'] not in names), *hermes_setup.TOOLS]:
        def handler(args, session_id="", _name=tool["name"], **kwargs):
            return plugin.handle(_name, args, session_id=session_id, **kwargs)
        ctx.register_tool(name=tool["name"], toolset="pantheon", schema={"name": tool["name"],
            "description": ("Join the human-supplied invitation only with current scoped runtime consent. "
                            "Disclose the prepared 60-minute default with joining approval; reuse the exact native conversation, never another."
                            if tool["name"] == "pantheon_join" else tool["description"]),
            "parameters": tool["inputSchema"]}, handler=handler)
    return plugin
