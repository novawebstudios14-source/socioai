"""Private YouTube resolver; signed, same-egress media relay for Nova Cuts."""
import base64
import asyncio
import tempfile
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
from urllib.parse import parse_qs, urlparse, urljoin
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
    return u.scheme == 'https' and not u.username and not u.password and not u.port and ((u.hostname or '').endswith('.googlevideo.com') or u.hostname in ('save.brianrianrehan.com', 'api.vidsnap.app') or (u.hostname == 's3.fr-par.scw.cloud' and u.path.startswith('/youtube-download/downloads/')))


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
    if payload['expires'] < time.time() or (not payload.get('kick_file') and not allowed_media(payload['url'])):
        raise ValueError('Mídia expirada.')
    return payload


class QuietLog:
    def debug(self, msg):
        match = re.search(r'([a-z_]+) player response playability status: ([A-Z_]+)', str(msg))
        if match:
            print(json.dumps({'event': 'youtube_playability', 'client': match[1], 'status': match[2]}), file=sys.stderr, flush=True)
    def warning(self, msg): pass
    def error(self, msg): pass


async def browser_guest_session(ident):
    import nodriver
    browser = await nodriver.start(
        headless=False, sandbox=False,
        browser_executable_path=os.environ.get('WPC_BROWSER_PATH', '/usr/bin/chromium'),
        browser_args=['--disable-dev-shm-usage'])
    try:
        page = await browser.get('https://www.youtube.com/watch?v=' + ident)
        await asyncio.sleep(5)
        await page.evaluate("document.querySelector('video')?.play().catch(() => {})")
        await asyncio.sleep(3)
        cookies = await browser.cookies.get_all()
        lines = ['# Netscape HTTP Cookie File']
        for cookie in cookies:
            domain = cookie.domain or ''
            if domain.lstrip('.') != 'youtube.com':
                continue
            lines.append('\t'.join([domain, 'TRUE' if domain.startswith('.') else 'FALSE',
                cookie.path or '/', 'TRUE' if cookie.secure else 'FALSE',
                str(max(0, int(cookie.expires or 0))), cookie.name, cookie.value]))
        if len(lines) == 1:
            raise ValueError('browser_session_unavailable')
        return '\n'.join(lines) + '\n'
    finally:
        browser.stop()


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
    cookie_path = None
    try:
        if provider == 'wpc':
            # This is a new anonymous session in our own server-side browser.
            # No user cookies or account credentials are collected.
            from nodriver import loop
            cookie_text = loop().run_until_complete(browser_guest_session(ident))
            fd, cookie_path = tempfile.mkstemp(prefix='nova-guest-', suffix='.txt')
            with os.fdopen(fd, 'w') as cookie_file:
                cookie_file.write(cookie_text)
            options['cookiefile'] = cookie_path
            options['extractor_args']['youtube'] = {'player_client': ['mweb', 'web_safari', 'tv']}
            print(json.dumps({'event': 'anonymous_browser_session_created'}), file=sys.stderr, flush=True)
        if provider == 'mediafetch':
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'mediafetch'))
            os.environ.setdefault('TOKEN_SECRET', KEY)
            os.environ.setdefault('APP_ENV', 'production')
            from app.services.extractor import _extract_sync
            info = _extract_sync('https://www.youtube.com/watch?v=' + ident)
        else:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info('https://www.youtube.com/watch?v=' + ident, download=False)
    finally:
        if cookie_path:
            try:
                os.unlink(cookie_path)
            except FileNotFoundError:
                pass
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
        if mime not in ('video/mp4', 'audio/mp4', 'audio/webm'):
            raise ValueError('invalid_media_response')
        sample = next(response.iter_content(4096), b'')
        valid = (mime == 'audio/webm' and sample[:4] == bytes.fromhex('1a45dfa3')) or (mime in ('video/mp4','audio/mp4') and len(sample) >= 12 and sample[4:8] == b'ftyp')
        if not valid:
            raise ValueError('invalid_media_signature')


def run_attempt(value, provider):
    interpreter = os.environ.get('WPC_PYTHON', '/opt/wpc/bin/python') if provider == 'wpc' else sys.executable
    if provider == 'mediafetch':
        interpreter = '/opt/mediafetch/bin/python'
    process = subprocess.Popen(
        [interpreter, os.path.abspath(__file__), '--resolve-once', provider],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        env={**os.environ, 'PATH': '/opt/mediafetch/bin:' + os.environ.get('PATH', '')} if provider == 'mediafetch' else None)
    try:
        raw, diagnostics = process.communicate(json.dumps({'url': value}), timeout=75)
        for line in diagnostics.splitlines()[-100:]:
            try:
                record = json.loads(line)
                if record.get('event') in ('youtube_playability', 'anonymous_browser_session_created', 'resolver_worker_failed'):
                    safe = {key: record[key] for key in ('event', 'client', 'status', 'exception', 'reason') if key in record}
                    print(json.dumps(safe), file=sys.stderr, flush=True)
            except (ValueError, TypeError, AttributeError):
                pass
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


PROVIDER_ORIGIN = 'https://save.brianrianrehan.com'
PROVIDER_HEADERS = {'User-Agent': 'NovaCutsImporter/1.0', 'Accept': 'application/json'}


def provider_url(value):
    target = urljoin(PROVIDER_ORIGIN + '/', str(value))
    parsed = urlparse(target)
    if parsed.scheme != 'https' or parsed.hostname != 'save.brianrianrehan.com' or parsed.username or parsed.password or parsed.port:
        raise ValueError('invalid_provider_destination')
    return target


def external_download(value):
    ident = video_id(value)
    response = requests.post(PROVIDER_ORIGIN + '/api/v1/download?wait=true', headers=PROVIDER_HEADERS,
        json={'url': 'https://www.youtube.com/watch?v=' + ident,
              'format': 'video', 'quality': '1080p', 'outputFormat': 'mp4'},
        timeout=(10, 85), allow_redirects=False)
    if response.status_code == 429:
        raise ValueError('provider_rate_limited')
    if response.status_code not in (200, 202):
        raise ValueError('provider_http_' + str(response.status_code))
    data = response.json()
    if isinstance(data.get('data'), dict):
        data = data['data']
    print(json.dumps({'event': 'external_download_response', 'status': response.status_code,
                      'fields': sorted(data.keys())[:20]}), file=sys.stderr, flush=True)
    deadline = time.monotonic() + 30
    while data.get('status') in ('pending', 'processing', 'downloading', 'converting') and time.monotonic() < deadline:
        status_url = data.get('statusUrl') or (PROVIDER_ORIGIN + '/api/v1/download/' + str(data.get('downloadId', data.get('id', ''))))
        time.sleep(3)
        response = requests.get(provider_url(status_url), headers=PROVIDER_HEADERS, timeout=(10, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('provider_status_failed')
        data = response.json()
        if isinstance(data.get('data'), dict):
            data = data['data']
    download = data.get('downloadUrl') or data.get('download_url')
    if not download:
        raise ValueError('provider_no_media')
    source = provider_url(download)
    validate_media({'url': source})
    proxy = ORIGIN + '/media?token=' + seal({'url': source, 'headers': {}, 'expires': int(time.time()) + 1800})
    return {'url': proxy, 'audioUrl': None, 'title': data.get('title') or data.get('fileName') or 'Vídeo do YouTube'}


def alldl_download(value):
    ident = video_id(value)
    response = requests.get('https://ahm7xmakki.com/api/alldl',
        params={'url': 'https://www.youtube.com/watch?v=' + ident},
        headers=PROVIDER_HEADERS, timeout=(10, 45), allow_redirects=False)
    if response.status_code == 429:
        raise ValueError('provider_rate_limited')
    if response.status_code != 200:
        raise ValueError('provider_http_' + str(response.status_code))
    data = response.json()
    info = data.get('mediaInfo') or {}
    if data.get('success') is not True or not info.get('videoUrl'):
        raise ValueError('provider_no_media')
    def relay(track):
        target = info.get(track)
        if not target:
            return None
        parsed = urlparse(target)
        if parsed.scheme != 'https' or parsed.hostname != 'api.vidsnap.app' or parsed.username or parsed.password or parsed.port:
            raise ValueError('invalid_provider_destination')
        headers = {'User-Agent': PROVIDER_HEADERS['User-Agent']}
        validate_media({'url': target, 'http_headers': headers})
        return ORIGIN + '/media?token=' + seal({'url': target, 'headers': headers, 'expires': int(time.time()) + 1800})
    video = relay('videoUrl')
    audio = relay('audioUrl')
    print(json.dumps({'event': 'external_media_verified', 'provider': 'alldl', 'separate_audio': bool(audio)}), file=sys.stderr, flush=True)
    return {'url': video, 'audioUrl': audio, 'title': info.get('title') or 'Vídeo do YouTube'}


def resolve(value, backend=None):
    video_id(value)
    if backend == 'videoscale' or (backend is None and os.environ.get('VIDEOSCALE_AUTH')):
        from videoscale import download
        result = download('https://www.youtube.com/watch?v=' + video_id(value))
        for field in ('url', 'audioUrl'):
            validate_media({'url': result[field]})
            result[field] = ORIGIN + '/media?token=' + seal({'url': result[field], 'headers': {}, 'expires': int(time.time()) + 1800})
        result['title'] = result.get('title') or 'Vídeo do YouTube'
        return result
    if (backend or os.environ.get('DOWNLOAD_BACKEND', 'local')) == 'mediafetch':
        return run_attempt(value, 'mediafetch')
    if (backend or os.environ.get('DOWNLOAD_BACKEND', 'local')) == 'alldl':
        return alldl_download(value)
    if (backend or os.environ.get('DOWNLOAD_BACKEND', 'local')) == 'webdownloader':
        return external_download(value)
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
        if self.path not in ('/resolve', '/kick'):
            return self.json(404, {'error': 'Rota desconhecida.'})
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + KEY):
            return self.json(401, {'error': 'Não autorizado.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 12000:
                return self.json(413, {'error': 'Solicitação inválida.'})
            body = json.loads(self.rfile.read(size))
            value = body.get('url', '')
            backend = body.get('backend')
            if backend not in (None, 'local', 'webdownloader', 'alldl', 'videoscale', 'mediafetch'):
                raise ValueError('invalid_backend')
            if self.path == '/kick':
                from kick import parse_url
                parse_url(value)
            else:
                video_id(value)
        except (ValueError, TypeError):
            return self.json(400, {'error': 'Link do YouTube inválido.'})
        if not SLOTS.acquire(blocking=False):
            return self.json(429, {'error': 'Importador ocupado. Tente em instantes.'})
        try:
            if self.path == '/kick':
                from kick import capture
                d = capture(value, body.get('start', 0), body.get('duration', 60))
                token = seal({'kick_file':d['id'],'expires':int(time.time())+3600})
                self.json(200, {**d,'url':ORIGIN+'/kick-media?token='+token})
            else:
                self.json(200, resolve(value, backend))
        except Exception as exc:
            if self.path == '/kick':
                from kick import KickError
                return self.json(422 if isinstance(exc, KickError) else 502, {'error':str(exc) if isinstance(exc, KickError) else 'Não foi possível preparar o trecho da Kick.'})
            text = str(exc).lower()
            login = any(t in text for t in ('not a bot', 'sign in', 'login_required'))
            self.json(422 if login else 502, {'code': 'youtube_login_required' if login else 'resolver_failed', 'error': 'O YouTube exigiu login para confirmar que o servidor não é um robô.' if login else 'O importador não conseguiu obter uma mídia compatível.'})
        finally:
            SLOTS.release()
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/health':
            return self.json(200, {'ok': True, 'engine': 'yt-dlp', 'providers': ['bgutil', 'wpc'] if os.environ.get('WPC_ENABLED') == '1' else ['bgutil']})
        if u.path == '/kick-media':
            try:
                from kick import local_path
                payload = unseal(parse_qs(u.query).get('token', [''])[0])
                path = local_path(payload.get('kick_file'))
                size = path.stat().st_size
                start, end = 0, size-1
                byte_range = self.headers.get('Range', '')
                if byte_range:
                    m = re.fullmatch(r'bytes=(\d+)-(\d*)', byte_range)
                    if not m: return self.json(416, {'error':'Intervalo inválido.'})
                    start, end = int(m[1]), min(size-1,int(m[2])) if m[2] else size-1
                    if start>end or start>=size: return self.json(416, {'error':'Intervalo inválido.'})
                self.send_response(206 if byte_range else 200)
                self.send_header('Content-Type','video/mp4')
                self.send_header('Content-Length',str(end-start+1))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Cache-Control','private, no-store')
                if byte_range: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                with path.open('rb') as f:
                    f.seek(start); remaining=end-start+1
                    while remaining:
                        chunk=f.read(min(65536,remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError): pass
            except Exception: return self.json(403, {'error':'Trecho inválido ou expirado.'})
            return
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
                if mime not in ('video/mp4', 'audio/mp4', 'audio/webm'):
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
            reason = next((label for text, label in [('no running event loop','event_loop_missing'), ('no current event loop','event_loop_missing'), ('connection refused','browser_connection_refused'), ('failed to connect','browser_connect_failed'), ('no such file','missing_file'), ('invalid_media','invalid_media'), ('browser_session_unavailable','guest_session_unavailable')] if text in message), 'other')
            print(json.dumps({'event':'resolver_worker_failed','exception':type(exc).__name__,'reason':reason}),file=sys.stderr,flush=True)
            code = 'youtube_login_required' if any(x in message for x in ('not a bot', 'sign in', 'login_required')) else 'resolver_failed'
            print(json.dumps({'error': code}), flush=True)
            sys.exit(1)
        sys.exit(0)
    if len(KEY) < 32 or not ORIGIN.startswith('https://'):
        raise SystemExit('Configure IMPORT_RESOLVER_KEY (32+ chars) e PUBLIC_ORIGIN (HTTPS).')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
