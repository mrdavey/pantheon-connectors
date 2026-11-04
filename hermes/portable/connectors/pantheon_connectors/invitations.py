"""Owner-private, recoverable invitation creation for explicitly delegated agents."""
import hashlib
import os
import fcntl
import pathlib
import secrets
from .state import load_private, save_private

def invite(client, binding_file, key, target, hours=1, history=False):
    lock=pathlib.Path(binding_file).with_suffix('.invites.lock')
    with os.fdopen(os.open(lock,os.O_CREAT|os.O_RDWR,0o600),'r+') as handle:
        if os.fstat(handle.fileno()).st_mode & 0o077:raise ValueError('Invitation lock must be private')
        fcntl.flock(handle,fcntl.LOCK_EX)
        return _invite_locked(client,binding_file,key,target,hours,history)

def _invite_locked(client, binding_file, key, target, hours, history):
    if not key or not isinstance(target,str) or not isinstance(history,bool):
        raise ValueError('Invalid invitation request')
    path=pathlib.Path(binding_file).with_suffix('.invites.json')
    scope=hashlib.sha256((client.url+'\0'+client.credential).encode()).hexdigest()
    saved=load_private(path) if path.exists() else dict(scope=scope,requests={})
    if saved['scope']!=scope:raise ValueError('Invitation journal belongs to another membership')
    identity=hashlib.sha256(key.encode()).hexdigest()
    request=saved['requests'].get(identity)
    if request:
        if any(request[field]!=value for field,value in dict(target=target,hours=hours,history=history).items()):
            raise ValueError('Invitation retry changed scope')
    else:
        chat=client.request('/v1/chat')['chat']
        request=dict(op='invite',key=key,target=target,hours=hours,history=history,secret=secrets.token_urlsafe(32),epoch=chat['epoch'],config=chat['config'])
        saved['requests'][identity]=request
        save_private(path,saved)  # Before any possible server commit.
    client.request('/v1/commands',request)
    return dict(link=client.url+'/join#invite='+request['secret'],notice='Single-use scoped invitation; do not send to another person without owner instruction.')
