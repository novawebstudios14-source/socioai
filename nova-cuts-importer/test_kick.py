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
if __name__=='__main__':unittest.main()
