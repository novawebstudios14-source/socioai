"""VideoScale API adapter; activated only with server-side API credentials."""
import json, os, re, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
BASE='https://gate.apiscrape.net:16262/api'
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None

def call(path,auth,data=None):
 headers={'Authorization':auth,'Accept':'application/json','User-Agent':'NovaCutsImporter/1.0'}
 raw=None if data is None else json.dumps(data).encode()
 if raw is not None: headers['Content-Type']='application/json'
 with build_opener(NoRedirect()).open(Request(BASE+path,data=raw,headers=headers),timeout=25) as r:
  raw=r.read(2000001)
  if len(raw)>2000000: raise ValueError('videoscale_response_too_large')
  d=json.loads(raw)
  return d['data'] if isinstance(d,dict) and isinstance(d.get('data'),dict) else d

def select_formats(formats):
 size=lambda f:f.get('filesize') or f.get('filesize_approx') or 1
 videos=[f for f in formats if f.get('ext')=='mp4' and str(f.get('vcodec','')).startswith(('avc1','h264')) and 0<(f.get('height') or 0)<=1080 and 0<size(f)<=500*1024*1024]
 audios=[f for f in formats if f.get('vcodec')=='none' and f.get('ext') in ('m4a','mp4') and 0<size(f)<=64*1024*1024]
 if not videos or not audios: raise ValueError('videoscale_compatible_tracks_missing')
 def audio_rank(f):
  label=' '.join(str(f.get(k) or '') for k in ('format_note','language','audio_track','format_id')).lower()
  preference=f.get('language_preference')
  if 'original' in label or (isinstance(preference,(int,float)) and preference >= 10): rank=0
  elif 'dub' in label or 'dublad' in label or (isinstance(preference,(int,float)) and preference < 0): rank=2
  else: rank=1
  return rank,abs((f.get('abr') or 64)-64)
 return max(videos,key=lambda f:f.get('height') or 0),min(audios,key=audio_rank)

def validate_destination(value):
 u=urlparse(value)
 if u.scheme!='https' or u.hostname!='s3.fr-par.scw.cloud' or u.port or u.username or u.password or not u.path.startswith('/youtube-download/downloads/'):
  raise ValueError('videoscale_invalid_destination')
 return value

def download(url):
 auth=os.environ.get('VIDEOSCALE_AUTH','').strip()
 if not re.fullmatch(r'Basic [A-Za-z0-9+/]+={0,2}',auth): raise ValueError('videoscale_credentials_missing')
 deadline=time.monotonic()+125
 info=call('/formats?'+urlencode({'video_url':url}),auth)
 formats=info if isinstance(info,list) else info.get('formats',[])
 tracks=select_formats(formats)
 def track(fmt):
  created=call('/download',auth,{'url':url,'format_id':str(fmt['format_id'])})
  ident=created.get('task_id') or created.get('id')
  if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ident): raise ValueError('videoscale_invalid_task')
  while time.monotonic()<deadline:
   state=call('/status/'+ident,auth);status=str(state.get('status','')).lower()
   if status in ('error','failed','cancelled'): raise ValueError('videoscale_download_failed')
   if status in ('completed','complete','done','success','finished'):
    result=call('/download/'+ident,auth);target=result.get('download_url') or result.get('url')
    if not isinstance(target,str): raise ValueError('videoscale_download_url_missing')
    return validate_destination(target)
   time.sleep(2)
  raise ValueError('videoscale_download_timeout')
 with ThreadPoolExecutor(max_workers=2) as pool:
  jobs=[pool.submit(track,f) for f in tracks];video,audio=[j.result() for j in jobs]
 return {'url':video,'audioUrl':audio,'title':info.get('title') if isinstance(info,dict) else None}
