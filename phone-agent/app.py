"""Agente de telefone por turnos, compatível com TwiML Gather/Say."""
import argparse
import json
import os
import re
import secrets
import time
import sqlite3
import urllib.request
from pathlib import Path

from flask import Flask, Response, abort, request
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Gather, VoiceResponse


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def connect(app):
    db = sqlite3.connect(app.config["DATABASE"], timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, phone TEXT, sid TEXT, messages TEXT, next_turn INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS replies (token TEXT, turn INTEGER, xml TEXT, PRIMARY KEY(token,turn))")
    columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
    if "expires_at" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN expires_at INTEGER DEFAULT 0")
    return db


def answer(messages):
    base = os.getenv("LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_MODEL", "")
    if not base or not model:
        return "O áudio chegou. Este é o modo de teste de telefonia; a inteligência artificial ainda não foi conectada."
    system = {
        "role": "system",
        "content": "Você é um assistente de IA em uma ligação de teste autorizada. Fale português brasileiro. Responda em até duas frases curtas. Não afirme ser humano nem ter acesso ao ChatGPT ou executar ações externas. Não peça dados sensíveis. Não faça ligações nem prometa retornos."
    }
    headers = {"Content-Type": "application/json", "User-Agent": "NovaPhoneAgent/1.0"}
    if os.getenv("LLM_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["LLM_API_KEY"]
    body = {"model": model, "messages": [system] + messages[-12:], "max_tokens": 100}
    if model.startswith("openai/gpt-oss-"):
        body.pop("max_tokens")
        body.update(max_completion_tokens=512, reasoning_effort="low")
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    # O teste da Twilio exige resposta do webhook em até 5 segundos.
    with urllib.request.urlopen(req, timeout=3) as result:
        text = json.load(result)["choices"][0]["message"]["content"]
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Resposta de IA vazia")
    return text.strip()[:700]


def create_app(config=None):
    app = Flask(__name__)
    app.logger.setLevel("INFO")
    app.config.update(
        MAX_CONTENT_LENGTH=16384,
        DATABASE=os.getenv("DATABASE_PATH", "calls.sqlite3"),
        PUBLIC_URL=(os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/"),
        TWILIO_ACCOUNT_SID=os.getenv("TWILIO_ACCOUNT_SID", ""),
        TWILIO_AUTH_TOKEN=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )
    if config:
        app.config.update(config)
    with connect(app):
        pass

    @app.get("/health")
    def health():
        return {"status": "ok", "ai_configured": bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))}

    @app.post("/check-ai")
    def check_ai():
        key = os.getenv("CONTROL_API_KEY", "")
        if not key or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
            abort(403)
        if not os.getenv("LLM_BASE_URL") or not os.getenv("LLM_MODEL"):
            return {"ok": False, "error": "Modelo não configurado"}, 503
        start = time.monotonic()
        try:
            text = answer([{"role": "user", "content": "Confirme brevemente em português que consegue conversar por telefone."}])
            return {"ok": True, "response": text, "seconds": round(time.monotonic() - start, 2)}
        except Exception as error:
            return {"ok": False, "kind": type(error).__name__}, 503

    @app.post("/calls")
    def call_api():
        key = os.getenv("CONTROL_API_KEY", "")
        if not key or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
            abort(403)
        body = request.get_json(silent=True) or {}
        phone = body.get("to", "")
        allowed = {p.strip() for p in os.getenv("ALLOWED_PHONES", "").split(",") if p.strip()}
        if phone not in allowed or not re.fullmatch(r"\+55\d{10,11}", phone):
            abort(403)
        idem = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", idem):
            abort(400)
        import hmac
        import hashlib
        token = hmac.new(key.encode(), idem.encode(), hashlib.sha256).hexdigest()
        with connect(app) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT sid FROM sessions WHERE token=?", (token,)).fetchone()
            if existing:
                return {"call_sid": existing["sid"], "duplicate": True}, 200 if existing["sid"] else 409
            active = db.execute("SELECT 1 FROM sessions WHERE phone=? AND expires_at>?", (phone, int(time.time()))).fetchone()
            if active:
                abort(429)
            db.execute("INSERT INTO sessions(token,phone,messages,expires_at) VALUES(?,?,?,?)", (token, phone, "[]", int(time.time()) + 180))
        try:
            client = Client(app.config["TWILIO_ACCOUNT_SID"], app.config["TWILIO_AUTH_TOKEN"])
            call = client.calls.create(to=phone, from_=None, url=app.config["PUBLIC_URL"] + "/voice/" + token)
            with connect(app) as db:
                db.execute("UPDATE sessions SET sid=? WHERE token=?", (call.sid, token))
            return {"call_sid": call.sid, "status": call.status}, 201
        except Exception as error:
            # Não refazer automaticamente: pode haver uma chamada criada após timeout.
            kind = type(error).__name__
            code = getattr(error, "code", None)
            app.logger.warning("call_creation_failed kind=%s provider_code=%s", kind, code)
            return {"error": "Não foi possível confirmar a criação. Consulte a Twilio antes de repetir.", "kind": kind, "provider_code": code}, 502

    @app.route("/voice", defaults={"token": None}, methods=["GET", "POST"])
    @app.route("/voice/<token>", methods=["GET", "POST"])
    def voice(token):
        auth = app.config["TWILIO_AUTH_TOKEN"]
        if not auth or not app.config["PUBLIC_URL"]:
            abort(503)
        url = app.config["PUBLIC_URL"] + request.path
        if request.query_string:
            url += "?" + request.query_string.decode("ascii")
        fields = (request.get_json(silent=True) or {}) if request.is_json else (request.form if request.method == "POST" else request.args)
        signed = RequestValidator(auth).validate(url, request.form, request.headers.get("X-Twilio-Signature", ""))
        bearer_session = None
        if not signed and token:
            with connect(app) as db:
                bearer_session = db.execute("SELECT * FROM sessions WHERE token=? AND expires_at>?", (token, int(time.time()))).fetchone()
            if not bearer_session or not bearer_session["sid"]:
                abort(403)
            # Token individual de 256 bits, gerado apenas pela rota de controle autenticada.
            # Só serve para esta chamada e expira; não habilita a rota pública /voice.
            if fields.get("CallSid") and fields["CallSid"] != bearer_session["sid"]:
                abort(403)
            if fields.get("To") and fields["To"] != bearer_session["phone"]:
                abort(403)
            if fields.get("AccountSid") and fields["AccountSid"] != app.config["TWILIO_ACCOUNT_SID"]:
                abort(403)
        elif not signed:
            app.logger.warning("webhook_denied signature_present=%s content_type=%s", bool(request.headers.get("X-Twilio-Signature")), request.mimetype)
            abort(403)
        if not bearer_session and fields.get("AccountSid") != app.config["TWILIO_ACCOUNT_SID"]:
            app.logger.warning("webhook_denied account_mismatch present=%s", bool(fields.get("AccountSid")))
            abort(403)
        try:
            turn = int(request.args.get("turn", "0"))
        except ValueError:
            abort(400)
        if not 0 <= turn <= 7:
            abort(400)
        sid = bearer_session["sid"] if bearer_session else fields.get("CallSid", "")
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid):
            abort(400)
        console_start = token is None
        if console_start:
            allowed = {p.strip() for p in os.getenv("ALLOWED_PHONES", "").split(",") if p.strip()}
            if fields.get("To") not in allowed:
                abort(403)
            token = "call-" + sid
        with connect(app) as db:
            # Serializa os callbacks e mantém retries da Twilio idempotentes.
            db.execute("BEGIN IMMEDIATE")
            if console_start:
                db.execute("INSERT OR IGNORE INTO sessions (token,phone,sid,messages) VALUES (?,?,?,?)", (token, fields.get("To"), sid, "[]"))
            session = db.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            if not session or (not bearer_session and fields.get("To") != session["phone"]):
                abort(403)
            if session["sid"] and session["sid"] != sid:
                abort(403)
            cached = db.execute("SELECT xml FROM replies WHERE token=? AND turn=?", (token, turn)).fetchone()
            if cached:
                return Response(cached["xml"], mimetype="text/xml")
            if turn != session["next_turn"]:
                abort(409)
            messages = json.loads(session["messages"])
            speech = fields.get("SpeechResult", "").strip()[:1500]
            ai_success = False
            stop = bool(re.search(r"\b(tchau|encerrar|desligar|pare|parar)\b", speech, re.I))
            if turn == 0:
                text = "Olá. Sou um assistente de inteligência artificial em uma ligação de teste. Você consegue me ouvir bem? Para encerrar, diga desligar."
            elif stop:
                text = "Teste encerrado. Até mais."
            elif speech:
                messages.append({"role": "user", "content": speech})
                try:
                    text = answer(messages)
                    ai_success = bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))
                except Exception:
                    app.logger.warning("O provedor de IA não respondeu dentro do teste; sem registrar credenciais ou fala.")
                    text = "Recebi sua fala, mas a inteligência artificial está indisponível agora."
                messages.append({"role": "assistant", "content": text})
            else:
                text = "Não ouvi sua resposta. Pode repetir?"
            xml = VoiceResponse()
            if stop or turn == 7:
                xml.say(text, language="pt-BR")
                if not stop:
                    xml.say("O limite deste teste foi atingido. Até mais.", language="pt-BR")
                xml.hangup()
            else:
                gather = Gather(input="speech", language="pt-BR", timeout=5, speech_timeout="auto", action=f"{app.config['PUBLIC_URL']}/voice/{token}?turn={turn + 1}", method="POST", action_on_empty_result=True)
                gather.say(text, language="pt-BR")
                xml.append(gather)
                xml.hangup()
            content = str(xml)
            db.execute("UPDATE sessions SET sid=?, messages=?, next_turn=? WHERE token=?", (sid, json.dumps(messages, ensure_ascii=False), turn + 1, token))
            db.execute("INSERT INTO replies VALUES (?,?,?)", (token, turn, content))
            app.logger.info("voice_turn processed turn=%s speech_present=%s ai_success=%s", turn, bool(speech), ai_success)
        return Response(content, mimetype="text/xml")

    return app


def start_call(app, phone):
    allowed = {p.strip() for p in os.getenv("ALLOWED_PHONES", "").split(",") if p.strip()}
    if phone not in allowed or not re.fullmatch(r"\+55\d{10,11}", phone):
        raise ValueError("Número brasileiro não autorizado em ALLOWED_PHONES.")
    trial = os.getenv("TWILIO_TRIAL", "true").lower() == "true"
    required = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]
    if not trial:
        required.append("TWILIO_FROM_NUMBER")
    if any(not os.getenv(name) for name in required):
        raise ValueError("Preencha as credenciais Twilio; contas pagas também precisam do número de origem.")
    if not app.config["PUBLIC_URL"].startswith("https://"):
        raise ValueError("PUBLIC_URL precisa usar HTTPS.")
    token = secrets.token_urlsafe(32)
    with connect(app) as db:
        db.execute("INSERT INTO sessions (token,phone,messages) VALUES (?,?,?)", (token, phone, "[]"))
    # Sem retries automáticos: evita ligações duplicadas após falhas de rede.
    client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    options = {"to": phone, "from_": None, "url": app.config["PUBLIC_URL"] + "/voice/" + token}
    if not trial:
        options.update(from_=os.environ["TWILIO_FROM_NUMBER"], method="POST", time_limit=120, timeout=20, record=False)
    # Trial aceita To + Url. From, Twiml e parâmetros extras podem ser recusados.
    call = client.calls.create(**options)
    print(json.dumps({"call_sid": call.sid, "status": call.status}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "call"])
    parser.add_argument("--to")
    args = parser.parse_args()
    application = create_app()
    if args.command == "serve":
        application.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")), threaded=False)
    else:
        if not args.to:
            parser.error("call exige --to +55...")
        try:
            start_call(application, args.to)
        except Exception as error:
            # Mensagem sem dump de configurações nem tokens.
            from twilio.base.exceptions import TwilioRestException
            if isinstance(error, TwilioRestException):
                print(json.dumps({"error": "Twilio recusou a chamada", "code": error.code, "status": error.status}))
            elif isinstance(error, ValueError):
                print(str(error))
            else:
                print("Falha de comunicação. Confira o histórico da Twilio antes de repetir a chamada.")
            raise SystemExit(1)
