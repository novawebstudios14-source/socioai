import json, unittest
from unittest.mock import patch
import kick
class KickTests(unittest.TestCase):
 def test_url_scope(self):
  self.assertEqual(kick.parse_url('https://kick.com/coringa'),('coringa',None))
  for url in ('https://kick.com@evil.com/a','https://127.0.0.1/a','https://kick.com:444/a','https://kick.com/search'):
   with self.assertRaises(kick.KickError):kick.parse_url(url)
 def test_nested_media_scope(self):
  for url in ('https://127.0.0.1/file.ts','http://stream.kick.com/a','https://live-video.net.evil.com/a'):
   with self.assertRaises(kick.KickError):kick.safe_url(url)
 @patch('kick.get')
 def test_playlist_nested_url_rejected(self,get):
  get.return_value=b'#EXTM3U\n#EXTINF:2,\nhttps://127.0.0.1/secret\n'
  with self.assertRaises(kick.KickError):kick.playlist('https://stream.kick.com/master.m3u8')
 def test_interval_uses_overlapping_segments(self):
  rows=[('a',6),('b',6),('c',6),('d',6)]
  self.assertEqual(kick.select(rows,8,9),([('b',6),('c',6)],2))
 @patch('kick.get')
 def test_offline_not_download_success(self,get):
  get.return_value=json.dumps({'livestream':None,'playback_url':'https://stream.kick.com/old.m3u8'}).encode()
  with self.assertRaisesRegex(kick.KickError,'offline'):kick.metadata('https://kick.com/coringa')
 def test_invalid_duration_does_not_connect(self):
  with patch('kick.metadata') as meta:
   for duration in (float('nan'),float('inf'),-1,100000):
    with self.assertRaises(kick.KickError):kick.capture('https://kick.com/coringa',0,duration)
   meta.assert_not_called()
class RecentCaptureTests(unittest.TestCase):
 @patch('kick.time.sleep')
 @patch('kick.metadata',return_value=('https://stream.kick.com/live.m3u8','Fixture',True))
 @patch('kick.playlist',return_value=('https://stream.kick.com/live.m3u8',[('https://stream.kick.com/a.ts',10),('https://stream.kick.com/b.ts',10),('https://stream.kick.com/c.ts',10)],False))
 @patch('kick.get',return_value=b'fragment')
 @patch('kick.subprocess.run')
 def test_recent_uses_existing_buffer_without_waiting(self,run,get,playlist,metadata,sleep):
  def output(args,**kwargs):
   from pathlib import Path
   Path(args[-1]).write_bytes(b'x'*2000)
  run.side_effect=output
  result=kick.capture('https://kick.com/example',0,60,'recent')
  try:
   self.assertEqual(result['captureMode'],'recent');sleep.assert_not_called();playlist.assert_called_once();self.assertEqual(get.call_count,3)
  finally:kick.local_path(result['id']).unlink()
 def test_invalid_mode_does_not_connect(self):
  with patch('kick.metadata') as metadata:
   with self.assertRaises(kick.KickError):kick.capture('https://kick.com/example',0,60,'invalid')
   metadata.assert_not_called()
if __name__=='__main__':unittest.main()
