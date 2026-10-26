import unittest,tempfile,threading
from pathlib import Path
from unittest.mock import patch
import kick
class BufferTests(unittest.TestCase):
 def fixture(self,tmp):
  watcher=kick.Watch.__new__(kick.Watch);watcher.lock=threading.Lock();watcher.rows=[]
  for index in range(9):
   p=Path(tmp)/str(index);p.write_bytes(str(index).encode());watcher.rows.append((p,10))
  return watcher
 def test_lookback_uses_latest_existing_video(self):
  with tempfile.TemporaryDirectory() as tmp:
   watcher=self.fixture(tmp);rows,offset,available=watcher.snapshot(30)
   self.assertEqual([x[0] for x in rows],[b'6',b'7',b'8']);self.assertEqual(offset,0);self.assertEqual(available,90)
 def test_warmup_is_not_an_empty_clip(self):
  watcher=kick.Watch.__new__(kick.Watch);watcher.lock=threading.Lock();watcher.rows=[]
  with self.assertRaisesRegex(kick.KickError,'histórico'):watcher.snapshot(30)
 def test_buffer_mode_never_downloads_or_waits_for_future_video(self):
  with patch('kick.buffered',return_value=('Fixture',[(b'video',20)],0,20)),patch('kick.metadata') as metadata,patch('kick.get') as get,patch('kick.time.sleep') as sleep,patch('kick.subprocess.run') as run:
   def output(args,**kwargs):Path(args[-1]).write_bytes(b'x'*2000)
   run.side_effect=output
   d=kick.capture('https://kick.com/example',0,30,'buffer')
   try:
    self.assertEqual(d['captureMode'],'buffer');metadata.assert_not_called();get.assert_not_called();sleep.assert_not_called()
   finally:kick.local_path(d['id']).unlink()
if __name__=='__main__':unittest.main()
