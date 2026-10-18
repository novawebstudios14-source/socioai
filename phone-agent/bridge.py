"""Single-owner MCP command bridge. Durable encrypted state, OAuth and webhook outbox."""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet
from flask import Response, abort, jsonify, redirect, render_template_string, request
from itsdangerous import URLSafeTimedSerializer, BadSignature

EVENT = "voice.command.confirmed"
SCOPE = "voice.commands"
OWNER = "phone-owner-v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stamp(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Unavailable(Exception):
    pass


class Store:
    """One encrypted state row, transactionally locked; appropriate for one owner."""
    def __init__(self, url, key, sqlite_path=None):
        self.url, self.sqlite_path = url, sqlite_path
        self.cipher = Fernet(key) if key else None
        self.initialized = False
        self.init_lock = threading.Lock()

    @property
    def ready(self):
        return bool(self.cipher and (self.url or self.sqlite_path))

    def mutate(self, fn):
        if not self.ready:
            raise Unavailable("Configure BRIDGE_DATABASE_URL e BRIDGE_ENCRYPTION_KEY")
        if self.url:
            import psycopg
            db = psycopg.connect(self.url, connect_timeout=3)
        else:
            db = sqlite3.connect(self.sqlite_path, timeout=5)
        try:
            if not self.initialized:
                with self.init_lock:
                    db.execute("CREATE TABLE IF NOT EXISTS phone_bridge_state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                    empty = self.cipher.encrypt(canonical({}).encode()).decode()
                    if self.url:
                        db.execute("INSERT INTO phone_bridge_state VALUES (1,%s) ON CONFLICT(id) DO NOTHING", (empty,))
                    else:
                        db.execute("INSERT OR IGNORE INTO phone_bridge_state VALUES (1,?)", (empty,))
                    db.commit()
                    self.initialized = True
            if not self.url:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM phone_bridge_state WHERE id=1" + (" FOR UPDATE" if self.url else "")).fetchone()
            state = json.loads(self.cipher.decrypt(row[0].encode()))
            result = fn(state)
            encrypted = self.cipher.encrypt(canonical(state).encode()).decode()
            db.execute("UPDATE phone_bridge_state SET value=" + ("%s" if self.url else "?") + " WHERE id=1", (encrypted,))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def public_post(url, body, headers):
    """No proxy/redirect; pin a validated public IP while verifying original TLS host."""
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.port not in (None, 443):
        raise ValueError("invalid_callback")
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    ips = [item[4][0] for item in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("non_public_callback")
    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((ips[0], 443), timeout=8)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise
    conn = PinnedHTTPS(p.hostname, timeout=8, context=ssl.create_default_context())
    try:
        conn.request("POST", (p.path or "/") + ("?" + p.query if p.query else ""), body=body, headers=headers)
        response = conn.getresponse()
        data = response.read(16385)
        if len(data) > 16384:
            raise ValueError("oversized_response")
        return response.status, data
    finally:
        conn.close()


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("invalid_secret")
    key = base64.b64decode(secret[6:], validate=True)
    if not 24 <= len(key) <= 64:
        raise ValueError("invalid_secret")
    return key


def signed_headers(sub, ident, body):
    ts = str(int(time.time()))
    value = ident.encode() + b"." + ts.encode() + b"." + body
    keys = [sub["secret"]]
    if sub.get("old_secret") and sub.get("rotate_until", 0) > time.time():
        keys.append(sub["old_secret"])
    signatures = ["v1," + base64.b64encode(hmac.new(signing_key(key), value, hashlib.sha256).digest()).decode() for key in keys]
    return {"Content-Type": "application/json", "webhook-id": ident, "webhook-timestamp": ts,
            "webhook-signature": " ".join(signatures), "X-MCP-Subscription-Id": sub["id"]}


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


EVENT_DEF = {"name": EVENT, "description": "Comando falado pelo titular, relido e explicitamente confirmado na ligação.",
             "delivery": ["webhook"], "inputSchema": schema(),
             "payloadSchema": schema({"command_id": {"type": "string"}, "text": {"type": "string"}}, ["command_id", "text"])}


def tool(name, desc, props=None, required=None, read=True):
    return {"name": name, "description": desc, "inputSchema": schema(props, required),
            "annotations": {"readOnlyHint": read, "destructiveHint": False, "openWorldHint": False, "idempotentHint": True},
            "securitySchemes": [{"type": "oauth2", "scopes": [SCOPE]}]}


TOOLS = [
    tool("list_voice_commands", "Listar comandos confirmados ainda pendentes ou em execução."),
    tool("get_voice_command", "Ler o comando completo e seu estado antes de executar.", {"command_id": {"type": "string"}}, ["command_id"]),
    tool("claim_voice_command", "Reservar comando antes de executar. Se acquired=false, não repita a execução.",
         {"command_id": {"type": "string"}, "run_id": {"type": "string", "minLength": 16, "maxLength": 100}}, ["command_id", "run_id"], False),
    tool("complete_voice_command", "Registrar resultado de um comando reservado. Não executa ações externas por si só.",
         {"command_id": {"type": "string"}, "run_id": {"type": "string"}, "status": {"enum": ["completed", "failed"]},
          "result": {"type": "string", "maxLength": 3000}}, ["command_id", "run_id", "status", "result"], False),
]


class Bridge:
    def __init__(self, app):
        self.app = app
        self.store = Store(app.config.get("BRIDGE_DATABASE_URL") or os.getenv("BRIDGE_DATABASE_URL"),
                           app.config.get("BRIDGE_ENCRYPTION_KEY") or os.getenv("BRIDGE_ENCRYPTION_KEY"),
                           app.config.get("BRIDGE_SQLITE_TEST_PATH") if app.testing else None)
        self.post = app.config.get("BRIDGE_POST", public_post)
        self.wake = threading.Event()
        self.root = app.config["PUBLIC_URL"]
        self.resource = self.root + "/mcp"
        self.install_routes()
        if not app.testing and self.store.ready:
            threading.Thread(target=self.worker, daemon=True, name="voice-event-outbox").start()

    def accept(self, command_id, sid, text):
        """Called only after explicit phone confirmation. Stable ID deduplicates retries."""
        def save(state):
            commands = state.setdefault("commands", {})
            if command_id in commands:
                return commands[command_id]
            if len(commands) >= 1000:
                terminal = [k for k, v in commands.items() if v["status"] in ("completed", "failed")]
                if not terminal:
                    raise Unavailable("Fila cheia")
                del commands[terminal[0]]
            now = time.time()
            cmd = {"id": command_id, "text": text, "call_sid": sid, "status": "pending", "created_at": stamp(now)}
            commands[command_id] = cmd
            linked = False
            for sub in state.setdefault("subscriptions", {}).values():
                if sub["expires"] > now and sub["owner"] == OWNER:
                    linked = True
                    event = {"eventId": "evt_" + command_id, "name": EVENT, "timestamp": stamp(now),
                             "data": {"command_id": command_id, "text": text}, "cursor": None}
                    state.setdefault("outbox", {})[command_id + ":" + sub["id"]] = {
                        "subscription": sub["id"], "body": canonical(event), "event_id": event["eventId"], "attempts": 0, "due": now}
            cmd["chat_linked_at_confirmation"] = linked
            return dict(cmd)
        cmd = self.store.mutate(save)
        self.wake.set()
        return cmd

    def deliver_once(self):
        now = time.time()
        def reserve(state):
            outbox = state.setdefault("outbox", {})
            for key, item in list(outbox.items()):
                sub = state.setdefault("subscriptions", {}).get(item["subscription"])
                if not sub or sub["expires"] <= now or sub["owner"] != OWNER:
                    del outbox[key]
                    continue
                if item["due"] <= now and item["attempts"] < 8:
                    item["attempts"] += 1
                    item["due"] = now + min(1800, 5 * 2 ** item["attempts"]) + secrets.randbelow(5)
                    return key, dict(item), dict(sub)
            return None
        work = self.store.mutate(reserve)
        if not work:
            return False
        key, item, sub = work
        status = 0
        try:
            raw = item["body"].encode()
            status, _ = self.post(sub["url"], raw, signed_headers(sub, item["event_id"], raw))
        except Exception:
            pass  # Never log callback URLs, tokens or voice text.
        def finish(state):
            current = state.get("outbox", {}).get(key)
            if current and (200 <= status < 300 or status in (410, 413) or (400 <= status < 500 and status not in (408, 429))):
                del state["outbox"][key]
            if status == 410:
                state.get("subscriptions", {}).pop(sub["id"], None)
        self.store.mutate(finish)
        return True

    def worker(self):
        while True:
            try:
                for _ in range(20):
                    if not self.deliver_once():
                        break
            except Exception:
                self.app.logger.warning("bridge_delivery_temporarily_unavailable")
            self.wake.wait(5)
            self.wake.clear()

    def subscribe(self, p):
        if p.get("name") != EVENT or p.get("arguments", {}) != {} or p.get("cursor") is not None:
            raise ValueError("invalid_subscription")
        delivery = p.get("delivery", {})
        if delivery.get("mode") != "webhook":
            raise ValueError("unsupported_delivery")
        url, secret = delivery.get("url", ""), delivery.get("secret", "")
        signing_key(secret)
        ident = "sub_" + digest(canonical([OWNER, url, EVENT, {}]))[:40]
        sub = {"id": ident, "owner": OWNER, "url": url, "secret": secret}
        challenge = secrets.token_urlsafe(32)
        body = canonical({"type": "verification", "challenge": challenge}).encode()
        try:
            status, response = self.post(url, body, signed_headers(sub, "verify_" + secrets.token_hex(16), body))
            if not 200 <= status < 300 or not secrets.compare_digest(json.loads(response).get("challenge", ""), challenge):
                raise ValueError("challenge_failed")
        except Exception as error:
            raise CallbackError("timeout" if isinstance(error, TimeoutError) else "challenge_failed") from None
        ttl = p.get("ttlMs", 86400000)
        if ttl is None:
            ttl = 86400000
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise ValueError("invalid_ttl")
        sub["expires"] = time.time() + min(ttl / 1000, 7 * 86400)
        def save(state):
            subs = state.setdefault("subscriptions", {})
            if ident not in subs and len(subs) >= 5:
                raise ValueError("subscription_limit")
            old = subs.get(ident)
            if old and old["secret"] != secret:
                sub.update(old_secret=old["secret"], rotate_until=time.time() + 300)
            subs[ident] = sub
        self.store.mutate(save)
        return {"id": ident, "refreshBefore": stamp(sub["expires"]), "cursor": None, "truncated": False}

    def invoke(self, name, args):
        definition = next((t for t in TOOLS if t["name"] == name), None)
        if not definition:
            raise ValueError("unknown_tool")
        import jsonschema
        try:
            jsonschema.validate(args, definition["inputSchema"])
        except jsonschema.ValidationError:
            raise ValueError("invalid_arguments") from None
        def run(state):
            cmds = state.setdefault("commands", {})
            if name == "list_voice_commands":
                return {"commands": sorted((c for c in cmds.values() if c["status"] in ("pending", "running")), key=lambda c: (c["created_at"], c["id"]))[-100:]}
            cmd = cmds.get(args["command_id"])
            if not cmd:
                raise ValueError("command_not_found")
            if name == "get_voice_command":
                return dict(cmd)
            if name == "claim_voice_command":
                if cmd["status"] == "pending":
                    cmd.update(status="running", run_id=args["run_id"])
                    return {"acquired": True, "command": dict(cmd)}
                return {"acquired": cmd["status"] == "running" and cmd.get("run_id") == args["run_id"], "command": dict(cmd)}
            if cmd.get("run_id") != args["run_id"] or cmd["status"] not in ("running", args["status"]):
                raise ValueError("claim_required_or_conflict")
            if cmd["status"] == "running":
                cmd.update(status=args["status"], result=args["result"], completed_at=stamp())
            elif cmd.get("result") != args["result"]:
                raise ValueError("result_conflict")
            return dict(cmd)
        result = self.store.mutate(run)
        return {"content": [{"type": "text", "text": canonical(result)}], "structuredContent": result, "isError": False}

    def authorized(self):
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or len(value) > 1000:
            return False
        hashed = digest(value[7:])
        def check(state):
            token = state.get("access", {}).get(hashed, {})
            return token.get("expires", 0) > time.time() and token.get("owner") == OWNER and token.get("resource") == self.resource and token.get("scope") == SCOPE
        return self.store.mutate(check)

    def install_routes(self):
        app = self.app
        @app.after_request
        def private_headers(response):
            if request.path.startswith(("/mcp", "/oauth", "/.well-known", "/control/")):
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
            return response

        @app.errorhandler(Unavailable)
        def unavailable(_error):
            return {"error": "bridge_storage_not_configured_or_unavailable"}, 503

        @app.route("/control/voice-commands", methods=["GET", "POST"])
        def direct_commands():
            key = os.getenv("CONTROL_API_KEY", "")
            if not key or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
                abort(403)
            if request.method == "GET":
                return self.invoke("list_voice_commands", {})["structuredContent"]
            body = request.get_json(silent=True)
            actions = {"get": "get_voice_command", "claim": "claim_voice_command", "complete": "complete_voice_command"}
            if not isinstance(body, dict) or body.get("action") not in actions:
                return {"error": "invalid_action"}, 400
            args = {k: v for k, v in body.items() if k != "action"}
            try:
                return self.invoke(actions[body["action"]], args)["structuredContent"]
            except ValueError:
                return {"error": "invalid_command_or_state"}, 409

        @app.get("/.well-known/oauth-protected-resource")
        @app.get("/.well-known/oauth-protected-resource/mcp")
        def resource_metadata():
            return {"resource": self.resource, "authorization_servers": [self.root], "scopes_supported": [SCOPE]}

        @app.get("/.well-known/oauth-authorization-server")
        def oauth_metadata():
            return {"issuer": self.root, "authorization_endpoint": self.root + "/oauth/authorize", "token_endpoint": self.root + "/oauth/token",
                    "registration_endpoint": self.root + "/oauth/register", "revocation_endpoint": self.root + "/oauth/revoke", "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": [SCOPE],
                    "authorization_response_iss_parameter_supported": True}

        @app.post("/oauth/register")
        def register():
            data = request.get_json(silent=True) or {}
            redirects = data.get("redirect_uris", [])
            if not isinstance(redirects, list) or not 1 <= len(redirects) <= 5 or any(not valid_redirect(u) for u in redirects) or data.get("token_endpoint_auth_method", "none") != "none":
                return {"error": "invalid_client_metadata"}, 400
            ident = "client_" + secrets.token_urlsafe(24)
            def save(state):
                clients = state.setdefault("clients", {})
                # Bounded public registration, oldest unused records expire after a day.
                now = time.time()
                for key in list(clients):
                    if clients[key]["expires"] < now:
                        del clients[key]
                if len(clients) >= 100:
                    abort(429)
                clients[ident] = {"redirects": redirects, "expires": now + 86400}
            self.store.mutate(save)
            return {"client_id": ident, "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}, 201

        @app.route("/oauth/authorize", methods=["GET", "POST"])
        def authorize():
            form_key = os.getenv("BRIDGE_FORM_KEY") or app.config.get("BRIDGE_FORM_KEY")
            if not form_key:
                raise Unavailable("OAuth signing key missing")
            serializer = URLSafeTimedSerializer(form_key, salt="voice-oauth-consent")
            if request.method == "GET":
                p = request.args.to_dict()
                def validate(state):
                    client = state.get("clients", {}).get(p.get("client_id"), {})
                    return client and p.get("redirect_uri") in client["redirects"]
                if not self.store.mutate(validate) or p.get("response_type") != "code" or p.get("resource") != self.resource or p.get("scope") != SCOPE or p.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", p.get("code_challenge", "")):
                    return {"error": "invalid_request"}, 400
                csrf = secrets.token_urlsafe(24)
                ticket = serializer.dumps({"params": p, "csrf": csrf})
                response = Response(render_template_string(CONSENT, ticket=ticket))
                response.set_cookie("voice_csrf", csrf, secure=True, httponly=True, samesite="Lax", max_age=300)
                return response
            try:
                ticket = serializer.loads(request.form.get("ticket", ""), max_age=300)
                if not secrets.compare_digest(ticket["csrf"], request.cookies.get("voice_csrf", "")):
                    abort(403)
            except BadSignature:
                abort(403)
            p = ticket["params"]
            hashed = os.getenv("BRIDGE_PAIRING_HASH") or app.config.get("BRIDGE_PAIRING_HASH", "")
            def grant(state):
                now = time.time()
                tries = state.setdefault("login_attempts", [])
                tries[:] = [n for n in tries if n > now - 3600]
                if len(tries) >= 20:
                    return None
                tries.append(now)
                if not hashed or not secrets.compare_digest(digest(request.form.get("pairing_code", "").strip()), hashed):
                    return None
                code = secrets.token_urlsafe(32)
                state.setdefault("codes", {})[digest(code)] = {"params": p, "expires": now + 120}
                return code
            code = self.store.mutate(grant)
            if not code:
                return "Código inválido ou limite de tentativas atingido.", 403
            return redirect(p["redirect_uri"] + "?" + urlencode({"code": code, "state": p.get("state", ""), "iss": self.root}))

        @app.post("/oauth/token")
        def token():
            p = request.form
            if p.get("resource") != self.resource:
                return {"error": "invalid_target"}, 400
            def exchange(state):
                now = time.time()
                kind = p.get("grant_type")
                if kind == "authorization_code":
                    table = state.setdefault("codes", {})
                    key = digest(p.get("code", ""))
                    grant = table.get(key)
                    if not grant or grant["expires"] < now:
                        return None
                    a = grant["params"]
                    verifier = p.get("code_verifier", "")
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                    if not 43 <= len(verifier) <= 128 or not secrets.compare_digest(challenge, a["code_challenge"]) or p.get("client_id") != a["client_id"] or p.get("redirect_uri") != a["redirect_uri"]:
                        return None
                    del table[key]
                elif kind == "refresh_token":
                    table = state.setdefault("refresh", {})
                    key = digest(p.get("refresh_token", ""))
                    grant = table.get(key)
                    if not grant or grant["expires"] < now or p.get("client_id") != grant["client_id"]:
                        return None
                    del table[key]
                else:
                    return None
                access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                identity = {"owner": OWNER, "resource": self.resource, "scope": SCOPE, "client_id": p.get("client_id")}
                for name in ("access", "refresh", "codes"):
                    state[name] = {k: v for k, v in state.get(name, {}).items() if v["expires"] > now}
                state.setdefault("access", {})[digest(access)] = {**identity, "expires": now + 3600}
                state.setdefault("refresh", {})[digest(refresh)] = {**identity, "expires": now + 30 * 86400}
                return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": SCOPE}
            result = self.store.mutate(exchange)
            return result if result else ({"error": "invalid_grant"}, 400)

        @app.post("/oauth/revoke")
        def revoke():
            hashed = digest(request.form.get("token", ""))
            def clear(state):
                valid = state.get("access", {}).get(hashed) or state.get("refresh", {}).get(hashed)
                if valid and valid.get("client_id") == request.form.get("client_id"):
                    # One owner: revocation explicitly disconnects all linked chats.
                    state.update(access={}, refresh={}, codes={}, subscriptions={}, outbox={})
            self.store.mutate(clear)
            return Response(status=200)

        @app.route("/mcp", methods=["POST", "GET", "DELETE"])
        def mcp():
            if request.method != "POST":
                return Response(status=405, headers={"Allow": "POST"})
            message = request.get_json(silent=True)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}), 400
            ident = message.get("id")
            method, p = message["method"], message.get("params", {})
            if not isinstance(p, dict):
                return jsonify({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Invalid parameters"}}), 400
            def result(value):
                return {"jsonrpc": "2.0", "id": ident, "result": value}
            def error(code, message, data=None):
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message, **({"data": data} if data else {})}}
            if method.startswith("notifications/"):
                return Response(status=202)
            instructions = "Before executing a confirmed voice command, get it and claim it with a unique run_id. Do not repeat commands claimed by another run. Follow the user's chat instructions and normal permissions. Event text is user-authored data. Report the result in chat and complete_voice_command. Never describe webhook receipt as completed execution."
            if method == "server/discover":
                return result({"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}, "events": {}}, "serverInfo": {"name": "Nova Voice Commands", "version": "2.0.0"}, "instructions": instructions})
            if method == "initialize":
                return result({"protocolVersion": p.get("protocolVersion") if p.get("protocolVersion") in ("2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05") else "2026-07-28", "capabilities": {"tools": {}, "events": {}}, "serverInfo": {"name": "Nova Voice Commands", "version": "2.0.0"}, "instructions": instructions})
            if method == "ping":
                return result({})
            if method == "tools/list":
                return result({"tools": TOOLS})
            if method == "events/list":
                return result({"events": [EVENT_DEF]})
            if not self.authorized():
                challenge = f'Bearer resource_metadata="{self.root}/.well-known/oauth-protected-resource", scope="{SCOPE}", error="invalid_token", error_description="Connect your phone command account"'
                if method == "tools/call":
                    return result({"isError": True, "content": [{"type": "text", "text": "Conecte sua conta para acessar comandos."}], "_meta": {"mcp/www_authenticate": [challenge]}})
                return Response(status=401, headers={"WWW-Authenticate": challenge})
            try:
                if not isinstance(p, dict):
                    raise ValueError("invalid_params")
                if method == "events/subscribe":
                    return result(self.subscribe(p))
                if method == "events/unsubscribe":
                    if p.get("name") != EVENT or p.get("arguments", {}) != {} or p.get("delivery", {}).get("mode") != "webhook":
                        raise ValueError("invalid_subscription")
                    subid = "sub_" + digest(canonical([OWNER, p["delivery"]["url"], EVENT, {}]))[:40]
                    self.store.mutate(lambda s: s.setdefault("subscriptions", {}).pop(subid, None))
                    return result({})
                if method == "tools/call":
                    return result(self.invoke(p.get("name"), p.get("arguments", {})))
                return error(-32601, "Method not found")
            except CallbackError as ex:
                return error(-32015, "CallbackEndpointError", {"reason": str(ex)})
            except (ValueError, KeyError, TypeError):
                return error(-32602, "Invalid parameters")
            except Unavailable:
                raise
            except Exception:
                return error(-32603, "Operation unavailable")


class CallbackError(Exception):
    pass


def valid_redirect(url):
    return isinstance(url, str) and (url == "https://chatgpt.com/connector_platform_oauth_redirect" or bool(re.fullmatch(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+", url)))


def normalized(text):
    folded = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\W_]+", " ", folded).strip()


STOP_PHRASES = {"desligar", "pode desligar", "tchau", "encerrar", "parar", "pare", "pode encerrar", "pode desligar a ligacao", "ate mais"}


def is_hangup_request(text):
    return normalized(text) in STOP_PHRASES


def voice_command(bridge, messages, sid, turn, speech):
    """Natural request intake; routine replies do not wait for a second model."""
    clean = normalized(speech)
    pending = next((m for m in reversed(messages) if m.get("role") == "voice_pending"), None)
    confirmations = {"sim", "sim pode", "sim pode enviar", "sim pode mandar", "pode", "pode enviar", "pode mandar", "pode salvar", "pode fazer", "pode executar", "manda", "envia", "isso", "isso mesmo", "certo", "confirmado", "confirmo", "confirmar", "confirmar comando", "confirmo o comando", "ok", "okay", "beleza", "positivo", "sim por favor", "pode sim", "pode enviar por favor"}
    cancellations = {"nao", "nao envia", "nao envie", "nao pode", "cancela", "cancelar", "cancelar comando", "cancelar pedido", "deixa pra la", "esquece"}
    if is_hangup_request(speech):
        return None
    if pending:
        if clean in confirmations:
            try:
                cmd = bridge.accept(pending["id"], sid, pending["content"])
            except Exception:
                return "Não consegui salvar. Posso tentar de novo?"
            messages.remove(pending)
            if cmd["chat_linked_at_confirmation"]:
                return "Salvei e deixei na fila do chat."
            return "Salvei. No chat, diga: execute meu último pedido."
        if clean in cancellations:
            messages.remove(pending)
            return "Cancelei. Pode falar outro pedido."
        if re.match(r"^(sim|talvez|acho|pode ser|nao sei)\b", clean):
            return "Você quer enviar esse pedido para o chat?"
        if clean in {"repete", "repita", "o que voce entendeu", "qual foi o pedido"}:
            return pending["content"] + ". Posso enviar?"
        # A correction replaces an unsent request; an addition extends it.
        messages.remove(pending)
        if re.match(r"^(tambem|e tambem|inclua|inclui|adicione|adiciona)\b", clean):
            speech = pending["content"] + ". " + speech
        else:
            speech = re.sub(r"^(?:não[, ]+|na verdade[, ]+|corrigindo[, ]+)", "", speech, flags=re.I)
    elif clean in {"oi", "ola", "alo", "bom dia", "boa tarde", "boa noite", "oi tudo bem", "tudo bem", "voce me ouve", "ta me ouvindo", "esta me ouvindo"}:
        return "Estou ouvindo. Pode falar."
    elif clean in {"obrigado", "obrigada", "valeu", "beleza"}:
        return "Pode falar se precisar de mais alguma coisa."
    elif clean in confirmations or clean in cancellations:
        return "Pode me dizer o que você precisa."
    collecting = next((m for m in reversed(messages) if m.get("role") == "voice_collect"), None)
    if collecting:
        messages.remove(collecting)
    # Old explicit prefixes remain accepted, but are never required.
    content = re.sub(r"^(?:comando para o chat|comando pro chat|executar no chat|enviar para o chat)\s*[:,.-]?\s*", "", speech, flags=re.I).strip()[:1500]
    if not content:
        return "O que você precisa?"
    command_id = "cmd_" + digest(sid + ":" + str(turn))[:32]
    messages.append({"role": "voice_pending", "id": command_id, "content": content})
    return "Entendi. Posso enviar esse pedido para o chat?"


CONSENT = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar comandos por telefone</title><style>body{font:18px system-ui;max-width:500px;margin:60px auto;padding:24px}input,button{font:inherit;padding:14px;margin:12px 0;width:100%;box-sizing:border-box}button{background:#175bd8;color:white;border:0;border-radius:10px}</style><h1>Conectar seu telefone ao Work</h1><p>Permitir que o ChatGPT leia seus comandos confirmados e registre os resultados.</p><form method="post"><input type="hidden" name="ticket" value="{{ticket}}"><label>Código de conexão<input name="pairing_code" type="password" required autocomplete="off"></label><button>Autorizar conexão</button></form></html>"""
