"""Run only after verifying the HTTPS archive hash and reviewing its contents.

Creates inert local native marketplace wiring; never installs, starts a receiver,
connects a daemon, redeems an invitation, or changes host configuration.
"""
import json
from pathlib import Path
import sys

def materialize(root):
    root=Path(root).absolute()
    if any(p.is_symlink() for p in (root,*root.parents)): raise ValueError('No symlink staging paths')
    if sys.version_info<(3,11): raise ValueError('Python 3.11 or newer required')
    launcher=root/'daemon_server.py'
    if not launcher.is_file(): raise ValueError('Missing packaged launcher')
    catalog=root.parent/'.agents/plugins'
    files = {
        catalog/'marketplace.json': {'name':'pantheon-hosted','interface':{'displayName':'Pantheon'},'plugins':[{'name':'pantheon','source':{'source':'local','path':'./pantheon'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}]},
    }
    if not (root/'plugin.json').exists():
        files[root/'.mcp.json'] = {'mcpServers':{'pantheon':{'command':sys.executable,'args':[str(launcher)]}}}
    elif not (root/'mcp.json').is_file():
        raise ValueError('Portable package is missing its native MCP configuration')
    for path, value in files.items():
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Existing marketplace path is a symlink')
        if path.exists() and json.loads(path.read_text()) != value:
            raise ValueError('Existing different marketplace preserved')
    catalog.mkdir(parents=True,mode=0o700,exist_ok=True)
    for path, value in files.items():
        if not path.exists():
            with path.open('x') as output:
                output.write(json.dumps(value,indent=2)+'\n')
            path.chmod(0o600)
    return root.parent

if __name__=='__main__': print(materialize(Path(__file__).resolve().parent))
