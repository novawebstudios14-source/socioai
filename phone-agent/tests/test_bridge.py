import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from app import create_app, connect
from bridge import Bridge, CallbackError, EVENT, OWNER, SCOPE, canonical, digest, public_post, signed_headers, voice_command
import test_app as calltests


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.received = []
        def post(url, raw, headers):
            self.received.append((url, json.loads(raw), headers))
            return 200, json.dumps({'challenge': json.loads(raw).get('challenge')}).encode()
        self.config = {'TESTING': True, 'DATABASE': self.tmp.name+'/calls.db', 'PUBLIC_URL': 'https://example.com',
                       'BRIDGE_SQLITE_TEST_PATH': self.tmp.name+'/bridge.db', 'BRIDGE_ENCRYPTION_KEY': self.key,
                       'BRIDGE_FORM_KEY': 'test-form-key', 'BRIDGE_PAIRING_HASH': digest('test-pairing'), 'BRIDGE_POST': post}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.bridge = self.app.extensions['voice_bridge']
        self.secret = 'whsec_'+base64.b64encode(b'k'*32).decode()
        self.params = {'name': EVENT, 'arguments': {}, 'delivery': {'mode': 'webhook', 'url': 'https://receiver.example/events', 'secret': self.secret}}

    def tearDown(self):
        self.tmp.cleanup()

    def rpc(self, method, params=None, token=None):
        return self.client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':method,'params': params or {}},
                                headers={'Authorization':'Bearer '+token} if token else {})

    def token(self):
        self.bridge.store.mutate(lambda s: s.setdefault('access', {}).update({digest('test-access'): {'owner':OWNER,'scope':SCOPE,'resource':self.bridge.resource,'expires':time.time()+3600}}))
        return 'test-access'

    def test_direct_chat_control_auth_and_execution_states(self):
        path = '/control/voice-commands'
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(path+'?key=owner-control').status_code, 403)
        self.bridge.accept('cmd_direct', 'test-call', 'Faça um resumo')
        with patch.dict(os.environ, {'CONTROL_API_KEY':'owner-control'}):
            auth = {'Authorization':'Bearer owner-control'}
            self.assertEqual(self.client.get(path,headers={'Authorization':'Bearer wrong'}).status_code,403)
            pending = self.client.get(path,headers=auth)
            self.assertEqual(pending.json['commands'][0]['id'],'cmd_direct')
            self.assertEqual(pending.headers['Cache-Control'],'no-store')
            result = self.client.post(path,headers=auth,json={'action':'claim','command_id':'cmd_direct','run_id':'direct-chat-run-001'})
            self.assertTrue(result.json['acquired'])
            other = self.client.post(path,headers=auth,json={'action':'claim','command_id':'cmd_direct','run_id':'direct-chat-run-002'})
            self.assertFalse(other.json['acquired'])
            complete = {'action':'complete','command_id':'cmd_direct','run_id':'direct-chat-run-001','status':'completed','result':'Resumo entregue no chat'}
            self.assertEqual(self.client.post(path,headers=auth,json=complete).json['status'],'completed')
            self.assertEqual(self.client.post(path,headers=auth,json=complete).status_code,200)
            self.assertEqual(self.client.get(path,headers=auth).json,{'commands':[]})
            self.assertEqual(self.client.post(path,headers=auth,json={'action':'events/subscribe'}).status_code,400)
            self.assertEqual(self.client.post(path,headers=auth,json={'action':'claim','command_id':'missing','run_id':'direct-chat-run-003'}).status_code,409)

    def test_discovery_and_auth(self):
        self.assertIn('events', self.rpc('server/discover').json['result']['capabilities'])
        self.assertEqual(self.rpc('events/list').json['result']['events'][0]['name'], EVENT)
        self.assertEqual(self.rpc('events/subscribe', self.params).status_code, 401)
        self.assertTrue(self.rpc('tools/call', {'name':'list_voice_commands'}).json['result']['isError'])
        self.assertIn('oauth2', json.dumps(self.rpc('tools/list').json))

    def test_encryption_persistence_rotation_and_unsubscribe(self):
        token = self.token()
        first = self.rpc('events/subscribe', self.params, token).json['result']
        again = self.rpc('events/subscribe', self.params, token).json['result']
        self.assertEqual(first['id'], again['id'])
        app2 = create_app(self.config)
        b2 = app2.extensions['voice_bridge']
        command = b2.accept('cmd_one', 'CA'+'1'*32, 'Crie um resumo')
        self.assertTrue(command['chat_linked_at_confirmation'])
        from pathlib import Path
        self.assertNotIn(b'Crie um resumo', Path(self.tmp.name+'/bridge.db').read_bytes())
        b2.accept('cmd_one', 'CA'+'1'*32, 'texto alterado')
        self.assertEqual(len(b2.store.mutate(lambda s: s['commands'])), 1)
        self.params['delivery']['secret'] = 'whsec_'+base64.b64encode(b'n'*32).decode()
        self.rpc('events/subscribe', self.params, token)
        self.assertTrue(b2.deliver_once())
        headers = self.received[-1][2]
        self.assertEqual(len(headers['webhook-signature'].split(' ')), 2)
        event = self.received[-1][1]
        self.assertEqual(event['data']['text'], 'Crie um resumo')
        self.assertEqual(b2.store.mutate(lambda s: s['outbox']), {})
        self.rpc('events/unsubscribe', self.params, token)
        self.assertFalse(b2.accept('cmd_two','CA'+'1'*32,'Outro')['chat_linked_at_confirmation'])

    def test_expiration_retry_and_terminal_response(self):
        sub = self.bridge.subscribe(self.params)
        self.bridge.accept('cmd_retry', 'test', 'Teste')
        attempts = []
        def failing(url, raw, headers):
            attempts.append((json.loads(raw)['eventId'], headers))
            return 503,b''
        self.bridge.post = failing
        self.bridge.deliver_once()
        self.bridge.store.mutate(lambda s: [i.update(due=0) for i in s['outbox'].values()])
        self.bridge.deliver_once()
        self.assertEqual(attempts[0][0],attempts[1][0])
        self.bridge.post = lambda *args: (410,b'')
        self.bridge.store.mutate(lambda s: [i.update(due=0) for i in s['outbox'].values()])
        self.bridge.deliver_once()
        self.assertEqual(self.bridge.store.mutate(lambda s:s['subscriptions']), {})
        self.bridge.post = lambda u, raw, h: (200, json.dumps({'challenge':json.loads(raw)['challenge']}).encode())
        self.bridge.subscribe(self.params)
        self.bridge.store.mutate(lambda s: [v.update(expires=0) for v in s['subscriptions'].values()])
        self.assertFalse(self.bridge.accept('expired', 'test','Teste')['chat_linked_at_confirmation'])

    def test_callback_verification_and_filters(self):
        self.bridge.post = lambda *args: (200,b'{"challenge":"wrong"}')
        with self.assertRaises(CallbackError): self.bridge.subscribe(self.params)
        self.assertEqual(self.bridge.store.mutate(lambda s:s.get('subscriptions',{})),{})
        self.params['arguments']={'someone_else':True}
        with self.assertRaises(ValueError): self.bridge.subscribe(self.params)
        self.params['arguments']={}
        self.params['delivery']['secret']='whsec_invalid'
        with self.assertRaises(ValueError): self.bridge.subscribe(self.params)

    def test_signature_and_ssrf(self):
        sub={'id':'sub_1','secret':self.secret}
        body=b'{"a":1}'
        headers=signed_headers(sub,'evt_1',body)
        message=b'evt_1.'+headers['webhook-timestamp'].encode()+b'.'+body
        expected='v1,'+base64.b64encode(hmac.new(b'k'*32,message,hashlib.sha256).digest()).decode()
        self.assertEqual(headers['webhook-signature'],expected)
        for url in ['http://example.com/', 'https://127.0.0.1/', 'https://[::1]/', 'https://example.com:22/', 'https://x:y@example.com/']:
            with self.assertRaises(ValueError): public_post(url,b'',{})
        with patch('bridge.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.2',443))]), patch('bridge.socket.create_connection') as socket:
            with self.assertRaises(ValueError): public_post('https://example.com/',b'',{})
            socket.assert_not_called()

    def test_confirmation_cancel_and_claim(self):
        m=[]
        voice_command(self.bridge,m,'call',1,'comando para o chat crie um arquivo')
        self.assertEqual(self.bridge.store.mutate(lambda s:s.get('commands',{})),{})
        voice_command(self.bridge,m,'call',2,'sim talvez')
        self.assertEqual(self.bridge.store.mutate(lambda s:s.get('commands',{})),{})
        voice_command(self.bridge,m,'call',3,'cancelar comando')
        voice_command(self.bridge,m,'call',4,'comando para o chat')
        voice_command(self.bridge,m,'call',5,'Crie um resumo')
        response=voice_command(self.bridge,m,'call',6,'confirmar comando')
        self.assertIn('execute meu último comando',response)
        cmd=self.bridge.store.mutate(lambda s:list(s['commands'].values())[0])
        self.assertEqual(cmd['text'],'Crie um resumo')
        ident=cmd['id']
        self.assertTrue(self.bridge.invoke('claim_voice_command',{'command_id':ident,'run_id':'r'*16})['structuredContent']['acquired'])
        self.assertFalse(self.bridge.invoke('claim_voice_command',{'command_id':ident,'run_id':'s'*16})['structuredContent']['acquired'])
        args={'command_id':ident,'run_id':'r'*16,'status':'completed','result':'Resumo pronto'}
        self.bridge.invoke('complete_voice_command',args)
        self.assertEqual(self.bridge.invoke('complete_voice_command',args)['structuredContent']['status'],'completed')
        args['result']='Outro resultado'
        with self.assertRaises(ValueError): self.bridge.invoke('complete_voice_command',args)

    def test_oauth_pkce_csrf_resource_and_refresh(self):
        redirect='https://chatgpt.com/connector_platform_oauth_redirect'
        reg=self.client.post('/oauth/register',json={'redirect_uris':[redirect]}).json
        verifier='v'*43
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        args={'client_id':reg['client_id'],'redirect_uri':redirect,'response_type':'code','scope':SCOPE,'resource':self.bridge.resource,'state':'opaque','code_challenge_method':'S256','code_challenge':challenge}
        page=self.client.get('/oauth/authorize',query_string=args,base_url='https://example.com')
        import re
        ticket=re.search(r'name="ticket" value="([^"]+)"',page.text)[1]
        other=self.app.test_client()
        self.assertEqual(other.post('/oauth/authorize',data={'ticket':ticket,'pairing_code':'test-pairing'},base_url='https://example.com').status_code,403)
        res=self.client.post('/oauth/authorize',data={'ticket':ticket,'pairing_code':'test-pairing'},base_url='https://example.com')
        values=parse_qs(urlsplit(res.location).query)
        self.assertEqual(values['iss'][0], 'https://example.com')
        exchange={'grant_type':'authorization_code','code':values['code'][0],'client_id':reg['client_id'],'redirect_uri':redirect,'resource':self.bridge.resource,'code_verifier':'x'*43}
        self.assertEqual(self.client.post('/oauth/token',data=exchange).status_code,400)
        exchange['code_verifier']=verifier
        response=self.client.post('/oauth/token',data=exchange)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/oauth/token',data=exchange).status_code,400)
        token=response.json
        self.assertIn('result',self.rpc('tools/call',{'name':'list_voice_commands'},token['access_token']).json)
        refresh={'grant_type':'refresh_token','refresh_token':token['refresh_token'],'resource':self.bridge.resource,'client_id':reg['client_id']}
        self.assertEqual(self.client.post('/oauth/token',data=refresh).status_code,200)
        self.assertEqual(self.client.post('/oauth/token',data=refresh).status_code,400)
        self.assertEqual(self.client.post('/oauth/register',json={'redirect_uris':['https://evil.example/callback']}).status_code,400)

    def test_storage_missing_fails_closed(self):
        other=create_app({'TESTING':True,'DATABASE':self.tmp.name+'/other.db','PUBLIC_URL':'https://example.com'})
        client=other.test_client()
        self.assertEqual(client.post('/oauth/register',json={'redirect_uris':['https://chatgpt.com/connector_platform_oauth_redirect']}).status_code,503)
        self.assertFalse(client.get('/health').json['bridge_configured'])


class VoiceBridgeIntegrationTest(unittest.TestCase):
    setUp = calltests.CallsTest.setUp
    tearDown = calltests.CallsTest.tearDown
    send = calltests.CallsTest.send

    def test_phone_confirm_retry_creates_exactly_one_command(self):
        self.app.config['BRIDGE_ENABLED']=True
        bridge=self.app.extensions['voice_bridge']
        from bridge import Store
        bridge.store=Store(None,Fernet.generate_key(),self.tmp.name+'/bridge.db')
        self.send()
        reply=self.send(1,'comando para o chat pesquise preços de café')
        self.assertIn('confirmar comando',reply.text)
        first=self.send(2,'confirmar comando')
        retry=self.send(2,'confirmar comando')
        self.assertEqual(first.data,retry.data)
        self.assertEqual(len(bridge.store.mutate(lambda s:s['commands'])),1)
