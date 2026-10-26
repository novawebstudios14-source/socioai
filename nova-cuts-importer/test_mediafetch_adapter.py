import unittest,json,sys
from unittest.mock import patch,MagicMock
from types import SimpleNamespace
import test_server
import server
class MediaFetchAdapterTests(unittest.TestCase):
 @patch('server.validate_media')
 def test_original_extractor_returns_signed_video_and_audio(self, validate):
  info={'title':'Fixture','formats':[{'url':'https://rr1.googlevideo.com/video','ext':'mp4','vcodec':'avc1','acodec':'none','height':720,'protocol':'https'},{'url':'https://rr1.googlevideo.com/audio','ext':'m4a','vcodec':'none','acodec':'mp4a','protocol':'https'}]}
  extract=MagicMock(return_value=info)
  with patch.dict(sys.modules,{'app.services.extractor':SimpleNamespace(_extract_sync=extract)}):
   result=server.resolve_once('https://youtu.be/4948LkibJpw','mediafetch')
  extract.assert_called_once_with('https://www.youtube.com/watch?v=4948LkibJpw')
  self.assertTrue(result['url'].startswith(server.ORIGIN+'/media?token='))
  self.assertTrue(result['audioUrl'].startswith(server.ORIGIN+'/media?token='))
  self.assertEqual(validate.call_count,2)
 @patch('server.subprocess.Popen')
 @patch('server.os.killpg')
 def test_child_has_isolated_interpreter_and_deno_path(self,kill,popen):
  p=popen.return_value;p.communicate.return_value=(json.dumps({'url':'https://example.com/media'}),'');p.returncode=0
  server.run_attempt('https://youtu.be/4948LkibJpw','mediafetch')
  self.assertEqual(popen.call_args.args[0][0],'/opt/mediafetch/bin/python')
  self.assertTrue(popen.call_args.kwargs['env']['PATH'].startswith('/opt/mediafetch/bin:'))
if __name__=='__main__':unittest.main()
