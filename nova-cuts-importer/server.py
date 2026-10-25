"""Private YouTube resolver; signed, same-egress media relay for Nova Cuts."""
import base64
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import requests
from yt_dlp import YoutubeDL

KEY = os.environ.get('IMPORT_RESOLVER_KEY', '')
ORIGIN = (os.environ.get('PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL', '')).rstrip('/')
SLOTS = threading.BoundedSemaphore(1)
MEDIA_SLOTS = threading.BoundedSemaphore(6)
MAX_BYTES = 500 * 1024 * 1024


def video_id(value):
    u = urlparse(value)
    if u.scheme != 'https' or u.username or u.password or u.port:
        raise ValueError('Link do YouTube inválido.')
    host = (u.hostname or '').removeprefix('www.')
    if host == 'youtu.be':
        ident = u.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'm.youtube.com', 'music.youtube.com'):
        ident = parse_qs(u.query).get('v', [''])[0]
        if not ident:
            match = re.fullmatch(r'/(?:shorts|live|embed)/([\w-]{11})/?', u.path)
            ident = match[1] if match else ''
    else:
        ident = ''
    if not re.fullmatch(r'[\w-]{11}', ident):
        raise ValueError('Link do YouTube inválido.')
    return ident


def allowed_media(value):
    u = urlparse(value)
    return u.scheme == 'https' and not u.username and not u.password and not u.port and (u.hostname or '').endswith('.googlevideo.com')


def seal(payload):
    raw = json.dumps(payload, separators=(',', ':')).encode()
    data = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    sig = hmac.new(KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return data + '.' + sig


def unseal(token):
    if len(token) > 24000:
        raise ValueError('Token inválido.')
    data, sig = token.rsplit('.', 1)
    expected = hmac.new(KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError('Token inválido.')
    payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
    if payload['expires'] < time.time() or not allowed_media(payload['url']):
        raise ValueError('Mídia expirada.')
    return payload


class QuietLog:
    def debug(self, msg):
        match = re.search(r'([a-z_]+) player response playability status: ([A-Z_]+)', str(msg))
        if match:
            print(json.dumps({'event': 'youtube_playability', 'client': match[1], 'status': match[2]}), file=sys.stderr, flush=True)
    def warning(self, msg): pass
    def error(self, msg): pass


def resolve_once(value, provider="bgutil"):
    ident = video_id(value)
    logger = QuietLog()
    options = {
        'quiet': True, 'verbose': True, 'logger': logger, 'noplaylist': True,
        'skip_download': True, 'socket_timeout': 15, 'retries': 1,
        'extractor_retries': 1, 'cachedir': False,
        'js_runtimes': {'node': {}},
        'extractor_args': {
            'youtube': {'player_client': ['visionos', 'web_embedded', 'mweb'], 'player_skip': ['webpage', 'configs', 'initial_data']},
            'youtubepot-bgutilhttp': {'base_url': ['http://127.0.0.1:4416']},
        },
    }
    if provider == 'wpc':
        options['extractor_args'] = {
            'youtube': {'player_client': ['visionos', 'web_embedded', 'mweb'], 'player_skip': ['webpage', 'configs', 'initial_data']},
            'youtubepot-wpc': {'browser_path': [os.environ.get('WPC_BROWSER_PATH', '/usr/bin/chromium')]},
        }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info('https://www.youtube.com/watch?v=' + ident, download=False)
    formats = [f for f in info.get('formats', []) if allowed_media(f.get('url', '')) and f.get('protocol') in ('https', 'http') and not f.get('has_drm')]
    videos = [f for f in formats if f.get('ext') == 'mp4' and (f.get('vcodec') or '').startswith(('avc1', 'h264')) and 0 < (f.get('height') or 0) <= 1080 and (f.get('filesize') or 0) <= MAX_BYTES]
    videos.sort(key=lambda f: ((f.get('fps') or 30) <= 30, f.get('height') or 0, f.get('acodec') != 'none'), reverse=True)
    if not videos:
        raise ValueError('Nenhum formato MP4 compatível disponível.')
    video = videos[0]
    audio = None
    if video.get('acodec', 'none') == 'none':
        audios = [f for f in formats if f.get('vcodec') == 'none' and f.get('ext') in ('m4a', 'mp4') and (f.get('filesize') or 0) <= 64 * 1024 * 1024]
        audios.sort(key=lambda f: f.get('abr') or 0, reverse=True)
        if not audios:
            raise ValueError('Faixa de áudio indisponível.')
        audio = audios[0]
    validate_media(video)
    if audio:
        validate_media(audio)
    def relay(fmt):
        # Only known extractor headers are accepted; no caller-supplied headers.
        headers = {k: v for k, v in fmt.get('http_headers', {}).items() if k.lower() in ('user-agent', 'referer', 'origin')}
        return ORIGIN + '/media?token=' + seal({'url': fmt['url'], 'headers': headers, 'expires': int(time.time()) + 3600})
    return {'url': relay(video), 'audioUrl': relay(audio) if audio else None, 'title': info.get('title', 'Vídeo do YouTube')}


def validate_media(fmt):
    """A successful metadata lookup or HTTP 206 is not proof of usable media."""
    headers = {k: v for k, v in fmt.get('http_headers', {}).items()
               if k.lower() in ('user-agent', 'referer', 'origin')}
    headers['Range'] = 'bytes=0-65535'
    with requests.get(fmt['url'], headers=headers, stream=True,
                      timeout=(10, 15), allow_redirects=False) as response:
        if response.status_code not in (200, 206):
            raise ValueError('media_transfer_rejected')
        mime = response.headers.get('Content-Type', '').split(';')[0].lower()
        if mime not in ('video/mp4', 'audio/mp4'):
            raise ValueError('invalid_media_response')
        sample = next(response.iter_content(4096), b'')
        if len(sample) < 12 or sample[4:8] != b'ftyp':
            raise ValueError('invalid_media_signature')


def run_attempt(value, provider):
    interpreter = os.environ.get('WPC_PYTHON', '/opt/wpc/bin/python') if provider == 'wpc' else sys.executable
    process = subprocess.Popen(
        [interpreter, os.path.abspath(__file__), '--resolve-once', provider],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    try:
        raw, _ = process.communicate(json.dumps({'url': value}), timeout=75)
        if len(raw) > 100000:
            raise ValueError('resolver_invalid_output')
        data = json.loads(raw)
        if process.returncode or not data.get('url'):
            raise ValueError(data.get('error', 'resolver_failed'))
        return data
    except subprocess.TimeoutExpired:
        raise ValueError('resolver_timeout') from None
    finally:
        # Stop the complete worker/browser process group, including on timeout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def resolve(value):
    video_id(value)
    errors = []
    providers = ['bgutil']
    if os.environ.get('WPC_ENABLED', '0') == '1':
        providers.append('wpc')
    for provider in providers:
        try:
            result = run_attempt(value, provider)
            print(json.dumps({'event': 'import_source_verified', 'provider': provider}), file=sys.stderr, flush=True)
            return result
        except Exception as exc:
            message = str(exc).lower()
            login = any(t in message for t in ('not a bot', 'sign in', 'login_required'))
            errors.append('youtube_login_required' if login else 'resolver_failed')
            print(json.dumps({'event': 'import_source_failed', 'provider': provider,
                              'code': errors[-1]}), file=sys.stderr, flush=True)
    raise ValueError('youtube_login_required' if errors and all(x == 'youtube_login_required' for x in errors) else 'resolver_failed')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # Never log signed URLs, keys or upstream errors.
    def json(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        if self.path != '/resolve':
            return self.json(404, {'error': 'Rota desconhecida.'})
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + KEY):
            return self.json(401, {'error': 'Não autorizado.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 12000:
                return self.json(413, {'error': 'Solicitação inválida.'})
            value = json.loads(self.rfile.read(size)).get('url', '')
            video_id(value)
        except (ValueError, TypeError):
            return self.json(400, {'error': 'Link do YouTube inválido.'})
        if not SLOTS.acquire(blocking=False):
            return self.json(429, {'error': 'Importador ocupado. Tente em instantes.'})
        try:
            self.json(200, resolve(value))
        except Exception as exc:
            text = str(exc).lower()
            login = any(t in text for t in ('not a bot', 'sign in', 'login_required'))
            self.json(422 if login else 502, {'code': 'youtube_login_required' if login else 'resolver_failed', 'error': 'O YouTube exigiu login para confirmar que o servidor não é um robô.' if login else 'O importador não conseguiu obter uma mídia compatível.'})
        finally:
            SLOTS.release()
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/health':
            return self.json(200, {'ok': True, 'engine': 'yt-dlp', 'providers': ['bgutil', 'wpc'] if os.environ.get('WPC_ENABLED') == '1' else ['bgutil']})
        if u.path != '/media':
            return self.json(404, {'error': 'Rota desconhecida.'})
        try:
            payload = unseal(parse_qs(u.query).get('token', [''])[0])
        except Exception:
            return self.json(403, {'error': 'Mídia inválida ou expirada.'})
        byte_range = self.headers.get('Range', '')
        if byte_range and not re.fullmatch(r'bytes=\d+-\d*', byte_range):
            return self.json(416, {'error': 'Intervalo inválido.'})
        if not MEDIA_SLOTS.acquire(blocking=False):
            return self.json(429, {'error': 'Importador ocupado.'})
        try:
            headers = dict(payload['headers'])
            if byte_range: headers['Range'] = byte_range
            with requests.get(payload['url'], headers=headers, stream=True, timeout=(10, 60), allow_redirects=False) as response:
                if response.status_code not in (200, 206):
                    return self.json(502, {'error': 'O YouTube recusou a transferência.'})
                mime = response.headers.get('Content-Type', '').split(';')[0]
                if mime not in ('video/mp4', 'audio/mp4'):
                    return self.json(502, {'error': 'Resposta de mídia inválida.'})
                self.send_response(response.status_code)
                for name in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                    if name in response.headers: self.send_header(name, response.headers[name])
                self.send_header('Cache-Control', 'private, no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                self.end_headers()
                total = 0
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > MAX_BYTES: break
                    self.wfile.write(chunk)
        except (requests.RequestException, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            MEDIA_SLOTS.release()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--resolve-once':
        try:
            request = json.loads(sys.stdin.read(12000))
            result = resolve_once(request['url'], sys.argv[2])
            print(json.dumps(result), flush=True)
        except Exception as exc:
            message = str(exc).lower()
            code = 'youtube_login_required' if any(x in message for x in ('not a bot', 'sign in', 'login_required')) else 'resolver_failed'
            print(json.dumps({'error': code}), flush=True)
            sys.exit(1)
        sys.exit(0)
    if len(KEY) < 32 or not ORIGIN.startswith('https://'):
        raise SystemExit('Configure IMPORT_RESOLVER_KEY (32+ chars) e PUBLIC_ORIGIN (HTTPS).')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
