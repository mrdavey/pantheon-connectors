"""Owner-invoked connector commands; no automatic installs or credential logging."""
import argparse
import pathlib
import secrets
import signal
import subprocess
import threading
from urllib.parse import urlsplit
from .client import Client
from .codex import AttachedAppServer, CodexAdapter
from .hermes import HermesSessionsAdapter
from .state import Journal, load_private, save_private
from .worker import Worker, Blocked

def inspection_browser_url(origin, credential):
    """Owner-selected Hosting origin; the capability stays in the URL fragment."""
    url = urlsplit(origin)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise Blocked("App origin must be an HTTPS origin without a path")
    return origin.rstrip("/") + "/app/invite#inspection=" + credential

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("operation", choices=["redeem", "run", "inspection"])
    parser.add_argument("binding_file", type=pathlib.Path)
    parser.add_argument("--app-origin", help="Hosting HTTPS origin for an owner-only Google account inspection link")
    args=parser.parse_args()
    config=load_private(args.binding_file)
    if args.operation=="redeem":
        if config.get("deliberate") is not True:
            raise Blocked("Owner's deliberate join instruction is required")
        if "credential" not in config:
            config["credential"]=secrets.token_urlsafe(32)
            save_private(args.binding_file, config)
        Client(config["url"], "").request("/v1/redeem",dict(invitation=config["invitation"],secret=config["credential"],name=config["name"],binding=config["binding"],deliberate=True))
        print("Membership redeemed; credential remains only in the private binding file")
        return
    if args.operation=="inspection":
        if args.app_origin:
            inspection_browser_url(args.app_origin, "validation")
        output=args.binding_file.with_suffix(".inspection.json")
        if output.exists():
            grant=load_private(output)
        else:
            grant=dict(url=config["url"],credential=secrets.token_urlsafe(32),key=secrets.token_urlsafe(24))
            save_private(output,grant)
        if grant["url"]!=config["url"]:raise Blocked("Inspection grant origin changed")
        result=Client(config["url"],config["credential"]).command("grant_inspection",secret=grant["credential"],hours=1,key=grant["key"])
        grant["grant_id"]=result["id"];save_private(output,grant)
        if args.app_origin:
            grant["browser_url"] = inspection_browser_url(args.app_origin, grant["credential"])
            save_private(output, grant)
            print("One-hour owner inspection link saved as browser_url in the private inspection file. Open it and sign in with Google to connect your account. Share only with the owner.")
        else:
            print("One-hour inspection capability saved beside the binding file; upload it in local Dispatch. Use --app-origin to save an owner-only Firebase account link. No agent or admin credential is shared.")
        return
    binding=config["binding"]
    rpc = None
    try:
        if binding["runtime"] == "codex":
            version=subprocess.run([config.get("codex_executable", "codex"),"--version"],capture_output=True,text=True,check=True).stdout.strip().removeprefix("codex-cli ")
            if version!=binding["version"]:
                raise Blocked("Runtime version differs from pinned binding")
            from .onboarding import trial_allowed
            if not trial_allowed(config):
                raise Blocked("A bounded owner-approved trial is required")
            rpc=AttachedAppServer(config["app_server_socket"], binding["version"], binding["profile"], config.get("codex_executable", "codex"))
            adapter=CodexAdapter(rpc,binding,config.get("owner_instruction", ""))
        elif binding["runtime"] == "hermes" and binding["surface"] == "sessions-api":
            native = load_private(config["hermes_access_file"])
            adapter = HermesSessionsAdapter(Client(native["url"], native["credential"], timeout=600), binding, config.get("owner_instruction", ""))
        else:
            raise Blocked("Selected adapter is not runnable; no substitute route selected")
        journal=Journal(args.binding_file.with_suffix(".sqlite"),binding)
        stop=threading.Event()
        signal.signal(signal.SIGTERM, lambda *_:stop.set())
        signal.signal(signal.SIGINT, lambda *_:stop.set())
        try:
            worker = Worker(Client(config["url"],config["credential"]),journal,adapter,binding)
            while not stop.is_set():
                from .onboarding import trial_allowed
                current = load_private(args.binding_file)
                if any(current.get(key) != config.get(key) for key in ("binding", "credential", "url", "app_server_socket")):
                    raise Blocked("Pinned receiver configuration changed")
                if not trial_allowed(current):
                    raise Blocked("Local trial authorization expired")
                if binding["runtime"] == "codex" and len(journal.accepted()) >= 8:
                    raise Blocked("Local trial delivery budget exhausted")
                worker.once()
                stop.wait(5)
        finally:
            journal.db.close()
    finally:
        if rpc: rpc.close()

if __name__=="__main__":
    try:main()
    except Exception:
        raise SystemExit("Connector stopped; check local binding, native runtime readiness, and durable pending state. No automatic rerouting occurred.") from None
