import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pantheon_connectors import codex_compatibility as compatibility
from pantheon_connectors.worker import Blocked


def schema():
    string={'type':'string'}
    def obj(props, required=()):return dict(type='object',properties=props,required=list(required))
    definitions={
        'Turn':obj(dict(threadId=string,input=dict(type='array'),toolOutput={'$ref':'#/definitions/Output'}),('threadId','input')),
        'Output':obj(dict(name=string,namespace={'type':['string','null']},output={'anyOf':[string,{'type':'array'}]}),('name','output')),
        'Read':obj(dict(threadId=string,includeTurns={'type':'boolean'}),('threadId',)),
        'Loaded':obj(dict(cursor=string)),
        'Config':obj(dict(includeLayers={'type':'boolean'})),
    }
    return dict(definitions=definitions,oneOf=[dict(properties=dict(method=dict(enum=[m]),params={'$ref':'#/definitions/'+d})) for m,d in [('turn/start','Turn'),('thread/read','Read'),('thread/loaded/list','Loaded'),('config/read','Config')]])


class CapabilitiesTests(unittest.TestCase):
    def test_actual_contract_shape_and_fail_closed_changes(self):
        compatibility.validate_schema(schema())
        for mutate in [
            lambda d:d['oneOf'].pop(),
            lambda d:d['definitions']['Turn']['properties'].pop('toolOutput'),
            lambda d:d['definitions']['Turn']['properties']['input'].update(minItems=1),
            lambda d:d['definitions']['Output']['required'].append('callId'),
            lambda d:d['definitions']['Output']['properties']['output'].update(anyOf=[{'type':'array'}]),
            lambda d:d['definitions']['Output']['properties']['name'].update(pattern='unknown constraint'),
            lambda d:d['definitions']['Read']['properties'].pop('includeTurns'),
        ]:
            changed=copy.deepcopy(schema());mutate(changed)
            with self.assertRaises(Blocked):compatibility.validate_schema(changed)

    def test_export_is_inert_isolated_and_cache_invalidates_on_binary_change(self):
        compatibility._inspect.cache_clear()
        with tempfile.TemporaryDirectory() as temp:
            executable=Path(temp)/'codex';executable.write_text('fixture')
            calls=[]
            def run(args,**kw):
                calls.append(args)
                self.assertEqual(args[1:4],['app-server','generate-json-schema','--out'])
                self.assertNotEqual(kw['env']['CODEX_HOME'],str(Path.home()/'.codex'))
                out=Path(args[4]);out.mkdir();(out/'ClientRequest.json').write_text(json.dumps(schema()))
            with patch.object(compatibility.subprocess,'run',side_effect=run):
                compatibility.require_capabilities(str(executable));compatibility.require_capabilities(str(executable))
                self.assertEqual(len(calls),1)
                executable.write_text('updated fixture')
                compatibility.require_capabilities(str(executable));self.assertEqual(len(calls),2)
