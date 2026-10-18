import io
import json
import os
import unittest
from unittest.mock import patch

from conversation import completion, converse
from bridge import voice_command


class ConversationTest(unittest.TestCase):
    def reply(self, messages, speech, payload):
        with patch('conversation.completion', return_value=json.dumps(payload)) as model:
            result = converse(messages, 'call', 2, speech)
            return result, model.call_args.args[0]

    def test_casual_question_uses_history_without_creating_task(self):
        messages = [{'role':'user','content':'Meu nome é Lucas.'}, {'role':'assistant','content':'Oi, Lucas.'}]
        result, sent = self.reply(messages, 'Como me chamo?', {'reply':'Você se chama Lucas.', 'task':None})
        self.assertEqual(result, 'Você se chama Lucas.')
        self.assertEqual(sent[-3:], messages + [{'role':'user','content':'Como me chamo?'}])
        self.assertEqual(len(messages), 2)

    def test_clarification_preserves_pending_and_correction_replaces_it(self):
        messages = [{'role':'voice_pending','id':'old','content':'Pesquise notebooks até quatro mil reais'}]
        self.reply(messages, 'Qual foi meu orçamento?', {'reply':'Quatro mil reais.', 'task':None})
        self.assertEqual(messages[0]['id'], 'old')
        result, sent = self.reply(messages, 'Mude para três mil', {'reply':'Certo.', 'task':'Pesquise notebooks até três mil reais'})
        self.assertIn('três mil', result)
        self.assertIn('Posso enviar', result)
        self.assertEqual(len(messages), 1)
        self.assertNotEqual(messages[0]['id'],'old')
        self.assertTrue(all(m['role'] in ('system','assistant','user') for m in sent))

    def test_confirmation_requires_application_not_model_and_cannot_queue_twice(self):
        from unittest.mock import Mock
        bridge=Mock()
        bridge.accept.return_value={'chat_linked_at_confirmation':False}
        messages=[]
        with patch('conversation.completion', return_value='{"reply":"Certo","task":"Pesquise notebooks"}'):
            voice_command(bridge,messages,'call',1,'Pesquise notebooks',conversation=converse)
        bridge.accept.assert_not_called()
        with patch('conversation.completion', side_effect=AssertionError('Confirmation is deterministic')):
            voice_command(bridge,messages,'call',2,'Pode enviar',conversation=converse)
        bridge.accept.assert_called_once()
        self.assertEqual(messages,[])

    def test_invalid_output_and_timeout_do_not_alter_pending(self):
        pending={'role':'voice_pending','id':'old','content':'Pedido original'}
        for raw in ('invalid', '{"reply":"sim","task":123}', '{"reply":"sim","task":"novo","cancel_pending":true}'):
            messages=[pending.copy()]
            with patch('conversation.completion', return_value=raw), self.assertRaises(ValueError):
                converse(messages,'call',2,'nova fala')
            self.assertEqual(messages,[pending])
        messages=[pending.copy()]
        with patch('conversation.completion', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            converse(messages,'call',2,'nova fala')
        self.assertEqual(messages,[pending])

    def test_free_openrouter_request_has_no_retry_or_paid_fallback(self):
        env={'LLM_BASE_URL':'https://openrouter.ai/api/v1','LLM_MODEL':'google/gemma-4-31b-it:free','LLM_API_KEY':'test-key'}
        result=io.BytesIO(json.dumps({'choices':[{'message':{'content':'Tudo bem.'}}]}).encode())
        with patch.dict(os.environ,env), patch('conversation.urllib.request.urlopen', return_value=result) as send:
            self.assertEqual(completion([{'role':'user','content':'Oi'}]),'Tudo bem.')
            request=send.call_args.args[0]
            body=json.loads(request.data)
            self.assertEqual(body['provider']['allow_fallbacks'],False)
            self.assertEqual(body['reasoning']['enabled'],False)
            self.assertEqual(send.call_args.kwargs['timeout'],3)
            self.assertEqual(send.call_count,1)
        env['LLM_MODEL']='paid/model'
        with patch.dict(os.environ,env), patch('conversation.urllib.request.urlopen') as send, self.assertRaises(ValueError):
            completion([])
        send.assert_not_called()
