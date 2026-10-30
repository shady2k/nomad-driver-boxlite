"""Durable bounded replay and condition-based subscriptions; no status polling."""
import json,os,threading,time,uuid
from pathlib import Path
class Journal:
 def __init__(self,path,max_events=2048,max_bytes=4194304):
  self.path=Path(path);self.cv=threading.Condition();self.limit=max_events;self.bytes=max_bytes;self.events=[];self.seq=0;self.snapshot={};self.epoch=str(uuid.uuid4());self.closed=False
  if self.path.exists():
   for line in self.path.read_text().splitlines():
    e=json.loads(line);self.events.append(e);self.seq=e['sequence'];self.epoch=e['epoch'];self.snapshot.update(e.get('snapshot',{}))
 def append(self,event):
  with self.cv:
   self.seq+=1;e={**event,'sequence':self.seq,'epoch':self.epoch,'time':time.time()}
   if event.get('event')=='guest_session_ready':e['snapshot']={k:event[k] for k in ['session_id','acp_session_id','state','requests'] if k in event}
   self.snapshot.update(e.get('snapshot',{}))
   if event.get('event')=='request_running':self.snapshot['state']='busy'
   if event.get('event')=='request_terminal':self.snapshot['state']='ready'
   e['snapshot']=dict(self.snapshot);self.events.append(e)
   rotate=len(self.events)>self.limit or sum(len(json.dumps(v)) for v in self.events)>self.bytes
   if rotate:
    while len(self.events)>max(1,self.limit//2) or sum(len(json.dumps(v)) for v in self.events)>self.bytes//2:
     self.events.pop(0)
   self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
   target=self.path.with_suffix('.tmp') if rotate else self.path
   flags=os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW|(os.O_TRUNC if rotate else os.O_APPEND)
   fd=os.open(target,flags,0o600)
   with os.fdopen(fd,'w') as f:
    for row in (self.events if rotate else [e]):f.write(json.dumps(row)+'\n')
    f.flush();os.fsync(f.fileno())
   if rotate:os.replace(target,self.path)
   self.cv.notify_all();return e
 def stream(self,after,deadline):
  with self.cv:
   initial={'event':'snapshot','sequence':self.seq,'epoch':self.epoch,'snapshot':dict(self.snapshot),'replay_from':self.events[0]['sequence'] if self.events else self.seq}
  yield initial
  while True:
   with self.cv:
    gap=None
    if self.events and after<self.events[0]['sequence']-1:
     gap={'event':'replay_gap','sequence':self.events[0]['sequence']-1,'epoch':self.epoch,'snapshot':dict(self.snapshot)}
     after=self.events[0]['sequence']-1
    rows=[e for e in self.events if e['sequence']>after]
    if not rows and gap is None:
     remaining=deadline-time.monotonic()
     if remaining<=0:
      expired={'event':'wait_deadline','sequence':self.seq,'epoch':self.epoch}
     elif self.closed:return
     else:self.cv.wait(remaining);continue
    else:expired=None
   if gap:yield gap
   if expired:yield expired;return
   for e in rows:after=e['sequence'];yield e
 def close(self):
  with self.cv:self.closed=True;self.cv.notify_all()
