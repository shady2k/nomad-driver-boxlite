"""Correlated JSON RPC over the existing foreground BoxLite stdio, not host exec."""
import json
import threading
import uuid

class Channel:
    def __init__(self):
        self.proc=None;self.pending={};self.lock=threading.Lock();self.write_lock=threading.Lock()
        self.closed=False
    def attach(self,proc):self.proc=proc
    def call(self,path,method,body):
        if self.closed or self.proc is None or self.proc.poll() is not None:raise RuntimeError('Guest unavailable')
        rid=str(uuid.uuid4());event=threading.Event();slot={'event':event}
        with self.lock:
            if len(self.pending)>=4:raise RuntimeError('Request concurrency limit')
            self.pending[rid]=slot
        try:
            message={'rpc_id':rid,'path':path,'method':method,'body':json.loads(body or b'{}')}
            wire=(json.dumps(message)+'\n').encode()
            with self.write_lock:self.proc.stdin.write(wire);self.proc.stdin.flush()
            if not event.wait(125 if path=='/artifact-transfer' else 5):raise TimeoutError('RPC outcome uncertain; inspect request ID')
            if 'response' not in slot:raise RuntimeError('Guest closed')
            return slot['response']['status'],json.dumps(slot['response']['body']).encode()
        finally:
            with self.lock:self.pending.pop(rid,None)
    def receive(self,obj):
        with self.lock:
            slot=self.pending.get(obj.get('rpc_id'))
            if slot:
                slot['response']=obj;slot['event'].set();return True
        return False
    def close(self):
        with self.lock:
            self.closed=True
            for slot in self.pending.values():slot['event'].set()
