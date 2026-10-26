"""Public TikTok FLV -> local bounded HLS/DVR. No cookies or remote ffmpeg input."""
import json,math,re,secrets,shutil,subprocess,tempfile,threading,time
from pathlib import Path
from urllib.parse import urlparse
import requests
from kick import KickError,ROOT,select
WATCHES={};LOCK=threading.Lock()
HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://www.tiktok.com/'}
def parse_url(value):
 u=urlparse(value)
 if u.scheme!='https' or u.hostname not in ('tiktok.com','www.tiktok.com') or u.username or u.password or u.port:raise KickError('Use o link público tiktok.com/@perfil/live.')
 m=re.fullmatch(r'/@([A-Za-z0-9_.]{1,32})(?:/live)?/?',u.path)
 if not m:raise KickError('Use o link público tiktok.com/@perfil/live.')
 return m[1].lower()
def safe_media(value):
 u=urlparse(value);h=u.hostname or ''
 if u.scheme!='https' or u.username or u.password or u.port or not any(h.endswith('.'+s) for s in ('tiktokcdn.com','tiktokcdn-us.com','ttlivecdn.com','byteoversea.com')):raise KickError('Servidor de mídia do TikTok não permitido.')
 return value
def resolve(value):
 channel=parse_url(value)
 r=requests.get('https://www.tiktok.com/api-live/user/room',params={'aid':1988,'sourceType':54,'staleTime':600000,'uniqueId':channel},headers={**HEADERS,'Referer':'https://www.tiktok.com/@'+channel+'/live'},timeout=(8,20),allow_redirects=False)
 if r.status_code!=200:raise KickError('O TikTok não disponibilizou esta live ao servidor.')
 try:d=r.json();room=(d.get('data') or {}).get('liveRoom') or {}
 except ValueError:raise KickError('O TikTok não retornou os dados públicos desta live.')
 if d.get('statusCode')!=0:raise KickError('O TikTok recusou os dados da transmissão.')
 if room.get('status')!=2:raise KickError('Este perfil não está ao vivo publicamente no TikTok.')
 try:streams=json.loads(room['streamData']['pull_data']['stream_data']).get('data',{})
 except (KeyError,ValueError,TypeError):raise KickError('A live não disponibilizou vídeo compatível.')
 for quality in ('hd','sd','ld','origin'):
  main=(streams.get(quality) or {}).get('main') or {}
  if main.get('flv'):
   try:codec=json.loads(main.get('sdk_params') or '{}').get('VCodec','h264').lower()
   except ValueError:continue
   if codec not in ('h264','avc'):continue
   return channel,room.get('title') or channel,safe_media(main['flv'])
 raise KickError('A live não disponibilizou vídeo H.264 compatível.')
class Watch:
 def __init__(self,channel):
  self.channel=channel;self.title=channel;self.error=None;self.live=True;self.touched=time.monotonic();self.directory=Path(tempfile.mkdtemp(prefix='tiktok-dvr-'));self.lock=threading.Lock();self.process=None
  threading.Thread(target=self.run,daemon=True).start()
 def rows(self):
  path=self.directory/'live.m3u8'
  try:lines=path.read_text().splitlines()
  except FileNotFoundError:return []
  out=[];duration=None
  for line in lines:
   if line.startswith('#EXTINF:'):duration=float(line[8:].split(',')[0])
   elif re.fullmatch(r'seg\d{9}\.ts',line) and duration and duration>0:
    p=self.directory/line
    if p.is_file():out.append((p,duration))
    duration=None
  return out
 def status(self):
  self.touched=time.monotonic()
  with self.lock:rows=self.rows()
  return {'platform':'tiktok','channel':self.channel,'title':self.title,'bufferSeconds':round(sum(d for _,d in rows),1),'live':self.live,'error':self.error,'chatStatus':'unavailable','messages':[]}
 def run(self):
  try:
   _,self.title,url=resolve('https://www.tiktok.com/@'+self.channel+'/live')
   self.process=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','pipe,file','-i','pipe:0','-map','0:v:0','-map','0:a:0?','-c','copy','-f','segment','-segment_time','3','-segment_list','live.m3u8','-segment_list_size','45','-segment_list_flags','+live','-reset_timestamps','0','seg%09d.ts'],cwd=self.directory,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   with requests.get(url,headers=HEADERS,stream=True,timeout=(8,20),allow_redirects=False) as r:
    if r.status_code!=200:raise KickError('O TikTok recusou a transferência do vídeo desta live.')
    first=True
    for chunk in r.iter_content(32768):
     if time.monotonic()-self.touched>120:break
     if first:
      if not chunk.startswith(b'FLV'):raise KickError('O TikTok não entregou um fluxo de vídeo compatível.')
      first=False
     self.process.stdin.write(chunk);self.process.stdin.flush()
     with self.lock:
      rows=self.rows();names={p.name for p,_ in rows};newest=sorted(self.directory.glob('seg*.ts'))[-1:]
      for p in self.directory.glob('seg*.ts'):
       if p.name not in names and p not in newest:p.unlink(missing_ok=True)
      if sum(p.stat().st_size for p in self.directory.glob('seg*.ts'))>90*1024*1024:raise KickError('A live atingiu o limite de histórico temporário.')
   self.live=False
  except Exception as e:
   self.live=False;self.error=str(e) if isinstance(e,KickError) else 'A conexão de vídeo do TikTok foi interrompida. Reabra a live.'
  finally:
   if self.process:
    try:self.process.stdin.close();self.process.wait(timeout=5)
    except Exception:self.process.kill();self.process.wait()
   while time.monotonic()-self.touched<120:time.sleep(2)
   with LOCK:
    if WATCHES.get(self.channel) is self:WATCHES.pop(self.channel,None)
   with self.lock:shutil.rmtree(self.directory,ignore_errors=True)
def watch(value):
 channel=parse_url(value)
 with LOCK:
  current=WATCHES.get(channel)
  if not current:
   if len(WATCHES)>=1:raise KickError('O servidor já acompanha uma live do TikTok. Feche o painel anterior e aguarde 2 minutos.')
   current=Watch(channel);WATCHES[channel]=current
 return current.status()
def current(channel):
 if not re.fullmatch(r'[A-Za-z0-9_.]{1,32}',channel):raise KickError('Canal inválido.')
 with LOCK:w=WATCHES.get(channel)
 if not w:raise KickError('O histórico expirou. Reabra o painel da live.')
 return w
def preview(channel,seal,origin):
 w=current(channel)
 with w.lock:rows=w.rows()[-6:]
 if not rows:raise KickError('Aguardando vídeo da live.')
 seq=int(rows[0][0].stem[3:]);lines=['#EXTM3U','#EXT-X-VERSION:3','#EXT-X-TARGETDURATION:'+str(math.ceil(max(d for _,d in rows))),'#EXT-X-MEDIA-SEQUENCE:'+str(seq)]
 for path,duration in rows:
  token=seal({'tiktok_watch':channel,'segment':path.name,'expires':int(time.time())+180})
  lines.extend(['#EXTINF:'+str(duration)+',',origin+'/tiktok-segment?token='+token])
 if not w.live:lines.append('#EXT-X-ENDLIST')
 return '\n'.join(lines)+'\n'
def segment(channel,name):
 if not re.fullmatch(r'seg\d{9}\.ts',name):raise KickError('Segmento inválido.')
 w=current(channel)
 with w.lock:
  p=w.directory/name
  if not p.is_file():raise KickError('Segmento expirado.')
  return p.read_bytes()
def capture(value,duration):
 duration=float(duration)
 if not math.isfinite(duration) or not 15<=duration<=90:raise KickError('Escolha de 15 a 90 segundos.')
 w=current(parse_url(value));w.touched=time.monotonic()
 with w.lock:
  rows=w.rows();available=sum(d for _,d in rows)
  if available<15:raise KickError('Aguarde pelo menos 15 segundos de histórico.')
  chosen,offset=select(rows,max(0,available-duration),duration);raw=[p.read_bytes() for p,_ in chosen]
 ident=secrets.token_hex(16);target=ROOT/(ident+'.mp4')
 if sum(p.stat().st_size for p in ROOT.glob('*.mp4'))>300*1024*1024:raise KickError('O armazenamento temporário está ocupado.')
 try:
  with tempfile.TemporaryDirectory(prefix='tiktok-clip-') as tmp:
   path=Path(tmp)/'input.ts';path.write_bytes(b''.join(raw))
   subprocess.run(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(path),'-ss',str(offset),'-t',str(duration),'-map','0:v:0','-map','0:a:0?','-c','copy','-avoid_negative_ts','make_zero','-movflags','+faststart',str(target)],check=True,timeout=25,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  return {'id':ident,'title':w.title,'live':True,'size':target.stat().st_size,'start':0,'captureMode':'buffer'}
 except Exception:target.unlink(missing_ok=True);raise
