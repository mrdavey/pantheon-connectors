"""Local stdio MCP panel. No model calls, shell execution, or implicit installation."""
import json
import pathlib
import sys
from .codex_queue import Notices
from .state import load_private
from .worker import Blocked

URI = 'ui://pantheon/codex-panel'


def tool(name, properties=None, required=None):
    return dict(name=name, description='Private bounded Pantheon Codex panel operation.',
                inputSchema=dict(type='object', properties=properties or {}, required=required or [], additionalProperties=False),
                _meta={'ui': {'visibility': ['app']}})


TOOLS = [
    dict(tool('pantheon_codex_panel'), title='Pantheon trial', _meta={
        'ui': {'resourceUri': URI}, 'openai/ui': {'entrypoints': [{'type': 'thread'}]}}),
    tool('pantheon_panel_open'),
    tool('pantheon_panel_take', {'token': {'type': 'string'}}, ['token']),
    tool('pantheon_panel_result', {'token': {'type': 'string'}, 'id': {'type': 'string'}, 'accepted': {'type': 'boolean'}}, ['token', 'id', 'accepted']),
]


class Server:
    def __init__(self, setup_file):
        self.setup_file = setup_file

    def handle(self, method, params):
        if method == 'initialize':
            return dict(protocolVersion='2025-11-25', capabilities={'tools': {}, 'resources': {}},
                        serverInfo={'name': 'pantheon-codex', 'version': '0.4.0'})
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': TOOLS}
        resource = dict(uri=URI, name='Pantheon trial', mimeType='text/html;profile=mcp-app',
                        _meta={'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}}})
        if method == 'resources/list':
            return {'resources': [resource]}
        if method == 'resources/read' and params.get('uri') == URI:
            root = pathlib.Path(__file__).resolve().parents[2]
            html = (root / 'codex-ui' / 'panel.html').read_text()
            script = (root / 'codex-ui' / 'panel.js').read_text()
            return {'contents': [dict(resource, text=html.replace('/* PANTHEON_PANEL_SCRIPT */', script))]}
        if method != 'tools/call':
            raise Blocked('Unsupported panel method')
        name, args = params.get('name'), params.get('arguments', {})
        schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
        if not schema or not isinstance(args, dict) or set(args) != set(schema['required']):
            raise Blocked('Invalid panel arguments')
        for key, value in args.items():
            if type(value) is not {'string': str, 'boolean': bool}[schema['properties'][key]['type']]:
                raise Blocked('Invalid panel argument type')
        if name == 'pantheon_codex_panel':
            value = {'status': 'Open this panel only inside the selected Codex conversation. Pairing requires one approved model turn.'}
        else:
            config = load_private(self.setup_file)  # Re-read revocation/expiry every request.
            notices = Notices(config)
            if name == 'pantheon_panel_open':
                value = notices.panel_open()
            elif name == 'pantheon_panel_take':
                value = notices.panel_take(args['token'])
            else:
                value = notices.panel_result(args['token'], args['id'], args['accepted'])
        return {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value}


def serve(setup_file):
    server = Server(setup_file)
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if 'id' not in request:
                continue
            result = server.handle(request.get('method'), request.get('params', {}))
            response = dict(jsonrpc='2.0', id=request['id'], result=result)
        except Exception:
            response = dict(jsonrpc='2.0', id=request.get('id'), error={
                'code': -32602, 'message': 'Pantheon panel blocked; check selected setup, consent, pairing and expiry. No fallback retry.'})
        print(json.dumps(response), flush=True)


if __name__ == '__main__':
    serve(sys.argv[1])
