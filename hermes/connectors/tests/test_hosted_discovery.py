import copy
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from pantheon_connectors.hosted_discovery import (TRUSTED_ORIGIN, SCHEMA, validate_discovery,
    fetch_discovery, validate_hosted_admission, download_package)
from pantheon_connectors.worker import Blocked


def descriptor(runtime='hermes'):
    digest = 'a' * 64
    return dict(schema=SCHEMA, service_origin=TRUSTED_ORIGIN, expires_at=time.time()+300,
        admission=dict(enabled=True, runtimes=[runtime]), packages={runtime: dict(runtime=runtime,
        version='0.3.17', artifact_url=TRUSTED_ORIGIN+'/plugins/'+digest+'/plugin.tar.gz',
        sha256=digest, source_revision='b'*40, format='tar.gz',
        host=dict(runtime_version='git:fixture'))})


class DiscoveryTests(unittest.TestCase):
    def test_valid_and_fail_closed_fields(self):
        d=descriptor()
        self.assertEqual(validate_discovery(d, TRUSTED_ORIGIN, 'hermes'), d)
        for field,value in [('schema','other'), ('service_origin','https://evil.example'), ('expires_at',True),
                            ('expires_at',float('inf')), ('expires_at',0), ('admission',{'enabled':False})]:
            with self.subTest(field=field,value=value), self.assertRaises(Blocked):
                validate_discovery(dict(d, **{field:value}), TRUSTED_ORIGIN, 'hermes')
        for url in ['file:///private/package', 'https://evil.example/'+ 'a'*64,
                    TRUSTED_ORIGIN+'/plugins/latest', d['packages']['hermes']['artifact_url']+'?token=x',
                    TRUSTED_ORIGIN+'/plugins/../'+'a'*64+'/x']:
            v=copy.deepcopy(d);v['packages']['hermes']['artifact_url']=url
            with self.subTest(url=url),self.assertRaises(Blocked): validate_discovery(v,TRUSTED_ORIGIN,'hermes')

    def test_fresh_admission_checks_consent_exact_host_and_package(self):
        d=descriptor(); calls=[]
        class Fake:
            def request(self,path): calls.append(path);return d
        factory=lambda origin,credential: Fake()
        binding=dict(runtime='hermes',version='git:fixture',surface='native-adapter')
        config=dict(url=TRUSTED_ORIGIN,hosted_onboarding=copy.deepcopy(d),hosted_consent=True)
        validate_hosted_admission(config,binding,client_factory=factory)
        self.assertEqual(calls,['/v1/onboarding'])
        for changed in [dict(binding,version='different'),dict(binding,surface='cli')]:
            with self.assertRaises(Blocked):validate_hosted_admission(config,changed,client_factory=factory)
        with self.assertRaises(Blocked):validate_hosted_admission(dict(config,hosted_consent=False),binding,client_factory=factory)
        d['packages']['hermes']['source_revision']='c'*40
        with self.assertRaises(Blocked):validate_hosted_admission(config,binding,client_factory=factory)

    def stage(self, entries, output, *, corrupt=False, redirect=False):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w:gz') as archive:
            for name,kind in entries:
                entry=tarfile.TarInfo(name);entry.type=kind
                if kind==tarfile.REGTYPE:
                    entry.size=3;archive.addfile(entry,io.BytesIO(b'abc'))
                else:
                    entry.linkname='/tmp/escape';archive.addfile(entry)
        blob=data.getvalue();d=descriptor();p=d['packages']['hermes'];p['sha256']=hashlib.sha256(blob).hexdigest()
        p['artifact_url']=TRUSTED_ORIGIN+'/plugins/'+p['sha256']+'/plugin.tar.gz'
        class Response(io.BytesIO):
            def geturl(self): return 'https://evil.example' if redirect else p['artifact_url']
        class Opener:
            def open(self,*args,**kwargs):return Response(blob+b'bad' if corrupt else blob)
        return download_package(d,'hermes',output,opener=Opener())

    def test_hash_verified_before_safe_private_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp).resolve()/'package'
            self.stage([('plugin/file.py',tarfile.REGTYPE)],out)
            self.assertEqual((out/'plugin/file.py').read_bytes(),b'abc')
            self.assertEqual(out.stat().st_mode & 0o777,0o700)
            with self.assertRaises(Blocked): self.stage([('x',tarfile.REGTYPE)],out)

    def test_archive_rejects_redirect_hash_links_traversal_duplicate_without_writes(self):
        cases=[([('../escape',tarfile.REGTYPE)],{}),([('/escape',tarfile.REGTYPE)],{}),
               ([('link',tarfile.SYMTYPE)],{}),([('hard',tarfile.LNKTYPE)],{}),
               ([('x',tarfile.REGTYPE),('x',tarfile.REGTYPE)],{}),
               ([('.git/config',tarfile.REGTYPE)],{}),
               ([('x',tarfile.REGTYPE),('x/y',tarfile.REGTYPE)],{}),
               ([('x',tarfile.REGTYPE)],{'corrupt':True}),([('x',tarfile.REGTYPE)],{'redirect':True})]
        for entries,kwargs in cases:
            with self.subTest(entries=entries,kwargs=kwargs), tempfile.TemporaryDirectory() as temp:
                out=Path(temp).resolve()/'package'
                with self.assertRaises(Blocked):self.stage(entries,out,**kwargs)
                self.assertFalse(out.exists())

    def test_expansion_limit_includes_tar_metadata_and_padding(self):
        with tempfile.TemporaryDirectory() as temp, patch('pantheon_connectors.hosted_discovery.MAX_EXPANDED', 100):
            out=Path(temp).resolve()/'package'
            with self.assertRaises(Blocked):self.stage([('x',tarfile.REGTYPE)],out)
            self.assertFalse(out.exists())

    def test_neutral_metadata_and_legacy_compatibility(self):
        d=descriptor()
        self.assertNotIn('surface',d['packages']['hermes']['host'])
        d['packages']['hermes']['host']['surface']='native-adapter'
        with self.assertRaises(Blocked):validate_discovery(d,TRUSTED_ORIGIN,'hermes')
        d['schema']='pantheon.hosted-onboarding.v1'
        validate_discovery(d,TRUSTED_ORIGIN,'hermes')
        d['packages']['hermes']['host']['surface']='other'
        with self.assertRaises(Blocked):validate_discovery(d,TRUSTED_ORIGIN,'hermes')

    def test_codex_capability_policy_does_not_whitelist_tested_version(self):
        from pantheon_connectors.hosted_discovery import matches_native_host
        d = descriptor('codex'); p = d['packages']['codex']
        p['host'] = dict(compatibility='native-capabilities', tested_version='0.162.1')
        validate_discovery(d, TRUSTED_ORIGIN, 'codex')
        for version in ['0.162.1', '0.162.2', '0.200.0', '1.0.0-alpha.2']:
            binding = dict(runtime='codex', version=version, surface='app-server-tool-output')
            self.assertTrue(matches_native_host(p, binding))
            self.assertFalse(matches_native_host(p, dict(binding, surface='native-queue')))
        for host in [dict(p['host'], runtime_version='0.162.1'), dict(p['host'], surface='app-server-tool-output'), dict(compatibility='guess',tested_version='0.162.1')]:
            bad=copy.deepcopy(d);bad['packages']['codex']['host']=host
            with self.assertRaises(Blocked):validate_discovery(bad,TRUSTED_ORIGIN,'codex')
        d['schema']='pantheon.hosted-onboarding.v1'
        with self.assertRaises(Blocked):validate_discovery(d,TRUSTED_ORIGIN,'codex')


class HermesLatestHostTests(unittest.TestCase):
    def test_updated_host_is_checked_by_capabilities_and_binding_not_tested_revision(self):
        from pantheon_connectors.hosted_discovery import matches_native_host
        d = descriptor('hermes')
        p = d['packages']['hermes']
        p['host'] = dict(compatibility='native-capabilities', tested_version='historical-evidence')
        validate_discovery(d, TRUSTED_ORIGIN, 'hermes')
        for version in ('git:'+'1'*40, 'git:'+'2'*40, 'hermes:future-release'):
            binding = dict(runtime='hermes', version=version, surface='native-adapter')
            self.assertTrue(matches_native_host(p, binding))
            self.assertFalse(matches_native_host(p, dict(binding, surface='native-queue')))
            self.assertFalse(matches_native_host(p, dict(binding, version='')))
