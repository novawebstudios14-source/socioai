import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from urllib.parse import urlencode

from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient
from twilio.http.response import Response
from app import connect, create_app, start_call


class CallsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.account = "AC" + "1" * 32
        self.sid = "CA" + "2" * 32
        self.token = "session-test"
        self.phone = "+5594999999999"
        self.app = create_app({"TESTING": True, "DATABASE": self.tmp.name + "/test.sqlite3", "PUBLIC_URL": "https://example.com", "TWILIO_AUTH_TOKEN": "test-secret", "TWILIO_ACCOUNT_SID": self.account})
        self.client = self.app.test_client()
        with connect(self.app) as db:
            db.execute("INSERT INTO sessions(token,phone,messages) VALUES(?,?,?)", (self.token, self.phone, "[]"))

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, turn=0, speech=None, **extra):
        fields = {"AccountSid": self.account, "CallSid": self.sid, "To": self.phone}
        if speech is not None:
            fields["SpeechResult"] = speech
        fields.update(extra)
        path = f"/voice/{self.token}?turn={turn}"
        signature = RequestValidator("test-secret").compute_signature("https://example.com" + path, fields)
        return self.client.post(path, data=fields, headers={"X-Twilio-Signature": signature})

    def test_forged_request_denied(self):
        self.assertEqual(self.client.post("/voice/session-test").status_code, 403)

    def test_ai_check_requires_control_key(self):
        self.assertEqual(self.client.post("/check-ai").status_code, 403)
        env = {"CONTROL_API_KEY": "test-control", "LLM_BASE_URL": "https://example.com/v1", "LLM_MODEL": "test", "LLM_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch("app.answer", return_value="Conexão validada.") as model:
            response = self.client.post("/check-ai", headers={"Authorization": "Bearer test-control"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            model.assert_called_once()

    def test_ai_check_validates_actual_conversation_protocol(self):
        self.app.config["BRIDGE_ENABLED"] = True
        env = {"CONTROL_API_KEY": "test-control", "LLM_BASE_URL": "https://openrouter.ai/api/v1", "LLM_MODEL": "test:free", "LLM_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch("app.converse", return_value="Pode falar normalmente.") as model:
            response = self.client.post("/check-ai", headers={"Authorization": "Bearer test-control"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["model"], "test:free")
        model.assert_called_once()

    def test_signed_get_from_console(self):
        fields = {"AccountSid": self.account, "CallSid": self.sid, "To": self.phone}
        path = f"/voice/{self.token}?" + urlencode(fields)
        signature = RequestValidator("test-secret").compute_signature("https://example.com" + path, {})
        response = self.client.get(path, headers={"X-Twilio-Signature": signature})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(ET.fromstring(response.data).find("Gather"))

    def test_custom_console_url_creates_authorized_session(self):
        fields = {"AccountSid": self.account, "CallSid": self.sid, "To": self.phone}
        signature = RequestValidator("test-secret").compute_signature("https://example.com/voice", fields)
        with patch.dict(os.environ, {"ALLOWED_PHONES": self.phone}):
            response = self.client.post("/voice", data=fields, headers={"X-Twilio-Signature": signature})
        self.assertEqual(response.status_code, 200)
        self.assertIn("/voice/call-" + self.sid, response.get_data(as_text=True))

    def test_greeting_and_duplicate_llm_callback(self):
        first = self.send()
        self.assertEqual(first.status_code, 200)
        root = ET.fromstring(first.data)
        self.assertEqual(root.find("Gather").get("language"), "pt-BR")
        with patch("app.answer", return_value="Ouço você bem.") as model:
            response = self.send(1, "Você me ouve?")
            duplicate = self.send(1, "Você me ouve?")
            self.assertEqual(response.data, duplicate.data)
            self.assertEqual(model.call_count, 1)

    def test_wrong_account_destination_and_call_denied(self):
        self.assertEqual(self.send(To="+5511999999999").status_code, 403)
        self.assertEqual(self.send(AccountSid="AC" + "3" * 32).status_code, 403)
        self.send()
        self.assertEqual(self.send(1, "oi", CallSid="CA" + "4" * 32).status_code, 403)

    def test_hangup_and_xml_escaping(self):
        self.send()
        with patch("app.answer", return_value="Texto <seguro> & correto"):
            response = self.send(1, "oi")
            ET.fromstring(response.data)
            self.assertIn(b"&lt;seguro&gt;", response.data)
        end = self.send(2, "pode desligar")
        self.assertIsNotNone(ET.fromstring(end.data).find("Hangup"))
        self.assertIsNone(ET.fromstring(end.data).find("Gather"))

    def test_limit_and_out_of_order(self):
        self.assertEqual(self.send(3, "oi").status_code, 409)
        self.send()
        with patch("app.answer", return_value="Recebido."):
            for turn in range(1, 8):
                response = self.send(turn, "teste")
                self.assertEqual(response.status_code, 200)
        self.assertIsNone(ET.fromstring(response.data).find("Gather"))
        self.assertEqual(self.send(8, "teste").status_code, 400)

    def test_provider_failure_falls_back(self):
        self.send()
        with patch("app.answer", side_effect=TimeoutError):
            response = self.send(1, "teste")
        self.assertEqual(response.status_code, 200)
        self.assertIn("indisponível", response.get_data(as_text=True))

    def test_unauthorized_number_never_dials(self):
        with patch.dict(os.environ, {"ALLOWED_PHONES": self.phone}), patch("app.Client") as client:
            with self.assertRaises(ValueError):
                start_call(self.app, "+5511999999999")
            client.assert_not_called()

    def test_trial_creation_uses_only_to_and_url(self):
        env = {"ALLOWED_PHONES": self.phone, "TWILIO_ACCOUNT_SID": self.account, "TWILIO_AUTH_TOKEN": "test-secret", "TWILIO_TRIAL": "true"}
        with patch.dict(os.environ, env), patch("app.Client") as client:
            client.return_value.calls.create.return_value.sid = self.sid
            client.return_value.calls.create.return_value.status = "queued"
            start_call(self.app, self.phone)
            kwargs = client.return_value.calls.create.call_args.kwargs
            self.assertEqual(set(kwargs), {"to", "url", "from_"})
            self.assertIsNone(kwargs["from_"])
            self.assertEqual(kwargs["to"], self.phone)

    def test_control_route_requires_auth_and_idempotency(self):
        self.assertEqual(self.client.post("/calls", json={"to": self.phone}).status_code, 403)
        env = {"CONTROL_API_KEY": "test-control", "ALLOWED_PHONES": self.phone}
        headers = {"Authorization": "Bearer test-control", "Idempotency-Key": "test-call-0123456789"}
        with patch.dict(os.environ, env), patch("app.Client") as client:
            client.return_value.calls.create.return_value.sid = self.sid
            client.return_value.calls.create.return_value.status = "queued"
            response = self.client.post("/calls", json={"to": self.phone}, headers=headers)
            duplicate = self.client.post("/calls", json={"to": self.phone}, headers=headers)
            self.assertEqual(response.status_code, 201)
            self.assertTrue(duplicate.json["duplicate"])
            self.assertEqual(client.return_value.calls.create.call_count, 1)

    def test_bearer_requires_live_session_and_matches_call(self):
        path = "/voice/" + self.token
        self.assertEqual(self.client.post(path).status_code, 403)
        with connect(self.app) as db:
            db.execute("UPDATE sessions SET sid=?,expires_at=? WHERE token=?", (self.sid, int(time.time()) + 60, self.token))
        wrong = self.client.post(path, data={"CallSid": "CA" + "9" * 32})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.client.post(path, data={"CallSid": self.sid}).status_code, 200)
        with connect(self.app) as db:
            db.execute("UPDATE sessions SET expires_at=1 WHERE token=?", (self.token,))
        self.assertEqual(self.client.post(path).status_code, 403)

    def test_sdk_transport_omits_null_from(self):
        captured = {}
        class Transport(TwilioHttpClient):
            def request(self, *args, **kwargs):
                captured.update(kwargs["data"])
                return Response(201, '{"sid":"CA00000000000000000000000000000000","status":"queued"}')
        Client(self.account, "test-secret", http_client=Transport()).calls.create(to=self.phone, from_=None, url="https://example.com/voice/nonce")
        self.assertEqual(set(captured), {"To", "Url"})


if __name__ == "__main__":
    unittest.main()
