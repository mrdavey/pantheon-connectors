"""Invoked by Go's real HTTP fixture; no native model or cloud operations."""
import json
import secrets
from pathlib import Path
import sys
import tempfile

from pantheon_connectors.broker.catalog import Catalog
from pantheon_connectors.broker.client import BrokerClient, CONFIG
from pantheon_connectors.broker.transport import HTTP


config = json.load(sys.stdin)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    path = root / ('a' * 32 + '.json')
    config['mcp'] = CONFIG
    catalog = Catalog(config['url'], cache_dir=root)
    make = lambda: BrokerClient(path, config, lambda: None, catalog=catalog, clock=lambda: config['now'])
    client = make()
    view = client.request('/v1/chat')
    assert view['chat']['id']
    # Tools added only to the server catalogue/handlers use the unchanged broker.
    context = client.tool('pantheon_context', {'connection': path.stem})
    assert context['objective'] == view['chat']['objective']
    assert context['config'] == view['chat']['config']
    assert context['member_id'] == view['member']
    status = client.tool('pantheon_chat_state', {'connection': path.stem})
    assert status == dict(chat_id=view['chat']['id'], member_id=view['member'],
        state=view['chat']['state'], epoch=view['chat']['epoch'], config=view['chat']['config'],
        result_final=view['result_final'])
    roster = client.tool('pantheon_members', {'connection': path.stem})
    assert [m['id'] for m in roster['members']] == sorted(view['chat']['members'])
    assert all(set(m) == {'id','name','runtime','surface','required','revoked',
                         'ready_version','report','report_source'} for m in roster['members'])
    pending = client.request('/v1/pending')
    probe = next(item for item in pending if item['message']['kind'] == 'probe')
    for item in pending:
        claim = client.command('claim', target=item['delivery']['id'], worker=secrets.token_urlsafe(32))
        client.command('receipt', ids=[item['message']['id']], attempt=claim['id'], text='harness')
    ready_args = dict(connection=path.stem, key='explicit-readiness',
        message_ids=[item['message']['id'] for item in pending], text=probe['message']['text'],
        objective_version=context['objective_version'], context_version=context['context_version'])
    # Same explicit tool works through the current broker and exported old broker;
    # it never changes the preapproved schema of existing command tools.
    ready = client.tool('pantheon_ack_ready', ready_args)
    assert make().tool('pantheon_ack_ready', ready_args) == ready
    after = client.request('/v1/chat')
    assert after['chat']['members'][view['member']]['ready'] == context['config']
    selector, token = client._token()
    HTTP(config['url']).initialize(token)
    first = client.command('post', key='python-broker-http-probe', text='synthetic Python broker message')
    retried = make().command('post', key='python-broker-http-probe', text='synthetic Python broker message')
    assert first == retried and first['id']
    source = make().tool('pantheon_message', {'connection': path.stem, 'id': first['id']})
    assert source['id'] == first['id']
    make().tool('pantheon_receipts', {'connection': path.stem, 'message_id': first['id']})
    history = make().tool('pantheon_history', {'connection': path.stem})
    # The unchanged broker carries the bounded page and opaque continuation.
    for i in range(12):
        client.command('post', key=f'python-history-page-{i}', text=f'history entry {i}')
    history = make().tool('pantheon_history', {'connection': path.stem})
    assert len(history['messages']) == 10 and history['next']
    seen = {entry['id'] for entry in history['messages']}
    while history['next']:
        history = make().request('/v1/history?cursor=' + history['next'])
        assert 0 < len(history['messages']) <= 10
        assert not seen.intersection(entry['id'] for entry in history['messages'])
        seen.update(entry['id'] for entry in history['messages'])
    assert first['id'] in seen
    assert not catalog.refresh(force=True), 'ETag must preserve the same snapshot'
    assert selector != path.stem
print('broker HTTP interoperability passed')
