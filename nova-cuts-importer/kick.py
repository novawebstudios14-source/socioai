"""Kick public HLS → bounded, local MP4. Every upstream URL is allowlisted."""
import json, math, os, re, secrets, subprocess, tempfile, time, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests

ROOT = Path(tempfile.gettempdir()) / 'novacuts-kick'
ROOT.mkdir(mode=0o700, exist_ok=True)
LIMIT = 100 * 1024 * 1024
SESSIONS = threading.local()
def session():
    if not hasattr(SESSIONS,"client"): SESSIONS.client=requests.Session()
    return SESSIONS.client

class KickError(ValueError): pass

def parse_url(value):
    u = urlparse(value)
    if u.scheme != 'https' or u.hostname not in ('kick.com', 'www.kick.com') or u.username or u.password or u.port:
        raise KickError('Cole um link público de canal ou gravação da Kick.')
    m = re.fullmatch(r'/([\w-]+)(?:/videos/([a-fA-F0-9-]{36}))?/?', u.path)
    old = re.fullmatch(r'/video/([a-fA-F0-9-]{36})/?', u.path)
    if old: return None, old[1]
    if not m or m[1] in ('video', 'videos', 'categories', 'search', 'auth'):
        raise KickError('Use kick.com/canal ou o link de uma gravação da Kick.')
    return m[1], m[2]

def safe_url(value, api=False):
    u = urlparse(value); h = u.hostname or ''
    allowed = h in ('kick.com','web.kick.com') if api else (h.endswith('.live-video.net') or h.endswith('.playback.live-video.net') or h == 'stream.kick.com')
    if u.scheme != 'https' or u.username or u.password or u.port or not allowed:
        raise KickError('A Kick retornou um servidor de mídia não permitido.')
    return value

def get(value, api=False, body=None, max_bytes=2*1024*1024):
    safe_url(value, api)
    with session().request('POST' if body is not None else 'GET', value, json=body,
            headers={'User-Agent':'Mozilla/5.0','Origin':'https://kick.com','Referer':'https://kick.com/'},
            timeout=(8,20), allow_redirects=False, stream=True) as r:
        if not r.ok: raise KickError('A Kick recusou o acesso à transmissão (%s). Use uma live ou gravação pública disponível.' % r.status_code)
        out=bytearray()
        for chunk in r.iter_content(65536):
            out.extend(chunk)
            if len(out)>max_bytes: raise KickError('O trecho ultrapassou o limite de transferência.')
        return bytes(out)

def metadata(value):
    channel, ident=parse_url(value)
    if ident:
        try:
            legacy=json.loads(get('https://kick.com/api/v1/video/'+ident,True))
            if not legacy.get('is_private') and legacy.get('status')=='public' and legacy.get('source'):
                return safe_url(legacy['source']),(legacy.get('livestream') or {}).get('session_title') or 'Gravação Kick',False
        except KickError: pass
        # Both currently published VOD IDs and older public recordings remain in use.
        if channel:
            try:
                videos=json.loads(get('https://kick.com/api/v2/channels/'+channel+'/videos',True))
                for video in videos if isinstance(videos,list) else []:
                    meta=video.get('video') or {}
                    if meta.get('uuid')==ident and not meta.get('is_private') and meta.get('status')=='public' and video.get('source'):
                        return safe_url(video['source']),video.get('session_title') or 'Gravação Kick',False
            except KickError: pass
        d=json.loads(get('https://web.kick.com/api/v1/stream/'+ident+'/playback',True,{
            'video_player':{'player':{}},'video_session':{},'user_session':{'non_personalised_ads':True}}))
        url=(d.get('playback_url') or {}).get('vod')
        if not url: raise KickError('Esta gravação não está disponível publicamente na Kick.')
        return safe_url(url), 'Gravação Kick · '+(channel or ident), False
    d=json.loads(get('https://kick.com/api/v2/channels/'+channel,True))
    if not d.get('livestream'): raise KickError('Este canal está offline. Cole o link de uma gravação na aba Vídeos da Kick.')
    return safe_url(d['playback_url']), d['livestream'].get('session_title') or channel, True

def playlist(url, depth=0):
    if depth>3: raise KickError('Playlist inválida.')
    raw=get(url).decode('utf-8-sig'); lines=[x.strip() for x in raw.splitlines() if x.strip()]
    if not raw.startswith('#EXTM3U'): raise KickError('A Kick não entregou uma playlist válida.')
    variants=[]
    for i,line in enumerate(lines[:-1]):
        if line.startswith('#EXT-X-STREAM-INF:'):
            height=re.search(r'RESOLUTION=\d+x(\d+)',line); bandwidth=re.search(r'BANDWIDTH=(\d+)',line)
            variants.append((int(height[1]) if height else 0,int(bandwidth[1]) if bandwidth else 0,safe_url(urljoin(url,lines[i+1]))))
    if variants:
        choices=[x for x in variants if 0<x[0]<=720] or variants
        return playlist(max(choices,key=lambda x:(x[0],x[1]))[2],depth+1)
    if any(x.startswith(('#EXT-X-KEY:', '#EXT-X-MAP:', '#EXT-X-BYTERANGE:')) for x in lines):
        raise KickError('O formato desta transmissão ainda não é compatível com o importador.')
    rows=[]; duration=None
    for line in lines:
        if line.startswith('#EXTINF:'): duration=float(line[8:].split(',')[0])
        elif not line.startswith('#') and duration is not None:
            if not math.isfinite(duration) or duration<=0 or duration>30: raise KickError('Segmento inválido.')
            rows.append((safe_url(urljoin(url,line)),duration));duration=None
    return url,rows,'#EXT-X-ENDLIST' in lines

def select(rows,start,duration):
    out=[];cursor=0;offset=0
    for url,seconds in rows:
        if cursor+seconds>start and cursor<start+duration:
            if not out: offset=start-cursor
            out.append((url,seconds))
        cursor+=seconds
    return out,max(0,offset)

def local_path(ident):
    if not re.fullmatch(r'[0-9a-f]{32}',str(ident)): raise KickError('Arquivo inválido.')
    p=ROOT/(ident+'.mp4')
    if not p.is_file() or p.stat().st_mtime<time.time()-3600: raise KickError('O trecho expirou. Importe novamente.')
    return p

def capture(value,start=0,duration=60,mode="recent"):
    if mode not in ("recent","forward"): raise KickError("Modo de captura inválido.")
    start=float(start);duration=float(duration)
    if not math.isfinite(start) or not math.isfinite(duration) or start<0 or not 15<=duration<=300: raise KickError('Escolha de 15 a 300 segundos e um início válido.')
    for p in ROOT.glob('*.mp4'):
        if p.stat().st_mtime<time.time()-3600: p.unlink(missing_ok=True)
    if sum(p.stat().st_size for p in ROOT.glob('*.mp4'))>300*1024*1024: raise KickError('Armazenamento temporário ocupado. Tente mais tarde.')
    url,title,live=metadata(value)
    if live and duration>90: raise KickError('Na live ao vivo, capture até 90 segundos por vez.')
    media,rows,ended=playlist(url)
    if not rows: raise KickError('A transmissão ainda não entregou segmentos de vídeo.')
    if live and mode == 'recent':
        available=sum(seconds for _,seconds in rows)
        selected,offset=select(rows,max(0,available-duration),duration)
    elif live:
        # Capture from the current live edge, then follow newly published segments.
        selected=[rows[-1]];seen={row[0] for row in rows};deadline=time.monotonic()+duration+25
        while sum(x[1] for x in selected)<duration and not ended:
            if time.monotonic()>deadline: raise KickError('A live parou de entregar vídeo. Tente novamente.')
            time.sleep(2)
            _,new,ended=playlist(media)
            for row in new:
                if row[0] not in seen: selected.append(row);seen.add(row[0])
        offset=0
    else:
        selected,offset=select(rows,start,duration)
        if not selected: raise KickError('O início escolhido está além do fim desta gravação.')
    ident=secrets.token_hex(16);target=ROOT/(ident+'.mp4')
    try:
        with tempfile.TemporaryDirectory(prefix='kick-') as tmp:
            src=Path(tmp)/'input.ts';total=0
            with src.open('wb') as f:
                # Bound parallel downloads to three fragments at a time, preserving order.
                with ThreadPoolExecutor(max_workers=3) as pool:
                    for index in range(0,len(selected),3):
                        batch=selected[index:index+3]
                        for chunk in pool.map(lambda row:get(row[0],max_bytes=20*1024*1024),batch):
                            total+=len(chunk)
                            if total>LIMIT: raise KickError('O trecho ultrapassou o limite de transferência.')
                            f.write(chunk)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(src),'-ss',str(offset),'-t',str(duration),'-map','0:v:0','-map','0:a:0?','-c','copy','-avoid_negative_ts','make_zero','-movflags','+faststart',str(target)],check=True,timeout=25,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if target.stat().st_size<1000: raise KickError('A captura retornou um vídeo vazio.')
        return {'id':ident,'title':title,'live':live,'size':target.stat().st_size,'start':0 if live else start,'captureMode':mode if live else 'vod'}
    except Exception:
        target.unlink(missing_ok=True);raise
