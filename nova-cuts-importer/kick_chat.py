"""Public guest chat using Kick's current Centrifugo protocol (kicksdk-py)."""
import collections,json,time,uuid
from urllib.parse import urlparse
import kicksdk
from websockets.sync.client import connect

def run(watch):
    from kick import get
    rt=kicksdk.realtime
    def request(req):
        raw=get(req.url,api=True,body=json.loads(req.body) if req.body else None,max_bytes=300000)
        return 200,raw.decode()
    def now():return int(time.monotonic()*1000)
    backoff=2
    while time.monotonic()-watch.touched<120:
        try:
            client=str(uuid.uuid4())
            servers=rt.parse_connection(*request(rt.chat_connection_request(watch.channel_id,client)))
            server=next(x for x in servers if x.get('provider')=='centrifugo')
            u=urlparse(server['url'])
            if u.scheme!='wss' or not ((u.hostname or '').endswith('.kick.com')) or u.username or u.password or u.port:raise ValueError('Unexpected chat server')
            token=rt.parse_token(*request(rt.token_request(client)))
            channels=rt.chat_channels(watch.chatroom,watch.channel_id);state=rt.Session(channels)
            with connect(server['url'],origin=rt.REQUIRED_ORIGIN,user_agent_header='Mozilla/5.0',ping_interval=None,max_size=300000,open_timeout=15) as socket:
                def actions(items):
                    for item in items:
                        kind=item['action']
                        if kind=='send':socket.send(item['text'])
                        elif kind=='close':raise RuntimeError('Chat connection closed')
                        elif kind=='refresh_token':actions(state.refresh(rt.parse_token(*request(rt.token_request(client))),now()))
                        elif kind=='subscribed':
                            watch.chat_status='connected'
                            if not watch.chat_started:watch.chat_started=time.time()
                        elif kind=='event' and item['name'] in ('App\\Events\\ChatMessageEvent','App\\Events\\ChatMessageSentEvent'):
                            data=item['data'];data=json.loads(data) if isinstance(data,str) else data
                            watch.add_message(data)
                actions(state.open(token,now()));backoff=2
                while time.monotonic()-watch.touched<120:
                    try:
                        raw=socket.recv(timeout=2)
                        actions(state.receive(raw.decode() if isinstance(raw,bytes) else raw,now()))
                    except TimeoutError:actions(state.tick(now()))
        except Exception as error:
            watch.chat_error=type(error).__name__
            watch.chat_status='reconnecting';time.sleep(backoff);backoff=min(30,backoff*2)
    watch.chat_status='disconnected'

def add_message(watch,data):
    if not isinstance(data,dict):return
    ident=str(data.get('id') or data.get('message_id') or '')
    if not ident:return
    user=str((data.get('sender') or {}).get('id') or data.get('user_id') or '')
    with watch.lock:
        if ident in watch.chat_ids:return
        watch.chat_ids.add(ident);watch.chat_events.append((time.time(),user,ident))
        watch.chat_messages.append({'id':ident,'sender':{'id':user,'username':str((data.get('sender') or {}).get('username') or 'Chat')[:40]},'content':str(data.get('content') or '')[:400]})
        while len(watch.chat_events)>15000:
            _,_,old=watch.chat_events.popleft();watch.chat_ids.discard(old)

def snapshot(watch):
    now=time.time()
    while watch.chat_events and watch.chat_events[0][0]<now-120:
        _,_,old=watch.chat_events.popleft();watch.chat_ids.discard(old)
    current=[x for x in watch.chat_events if x[0]>now-10]
    previous=[x for x in watch.chat_events if now-70<x[0]<=now-10]
    baseline=len(previous)/6
    return {'chatStatus':watch.chat_status,'messages':list(watch.chat_messages),'chatStats':{'count':len(current),'baseline':baseline,'ratio':len(current)/max(1,baseline),'unique':len(set(x[1] for x in current)),'warming':not watch.chat_started or now-watch.chat_started<70}}

def initialize(watch):
    watch.chat_status='connecting';watch.chat_started=0;watch.chat_events=collections.deque();watch.chat_ids=set();watch.chat_messages=collections.deque(maxlen=40)
