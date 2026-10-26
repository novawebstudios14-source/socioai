import unittest
from unittest.mock import patch
import videoscale as v
class Tests(unittest.TestCase):
 def test_rejects_foreign_or_private_destinations(self):
  for u in ['https://127.0.0.1/a','https://s3.fr-par.scw.cloud.evil.org/youtube-download/downloads/a','https://s3.fr-par.scw.cloud/private/a','https://user:pass@s3.fr-par.scw.cloud/youtube-download/downloads/a']:
   with self.assertRaises(ValueError): v.validate_destination(u)
 def test_missing_credentials_make_no_requests(self):
  with patch.dict('os.environ',{},clear=True),patch.object(v,'call') as call:
   with self.assertRaisesRegex(ValueError,'credentials_missing'):v.download('https://youtu.be/4948LkibJpw')
   call.assert_not_called()
 def test_selects_h264_and_bounded_audio(self):
  fs=[{'ext':'mp4','vcodec':'avc1','height':720,'format_id':'136'},{'ext':'mp4','vcodec':'av01','height':2160,'format_id':'401'},{'ext':'m4a','vcodec':'none','abr':48,'format_id':'139'},{'ext':'m4a','vcodec':'none','abr':64,'filesize':70000000,'format_id':'bad'}]
  video,audio=v.select_formats(fs);self.assertEqual(video['format_id'],'136');self.assertEqual(audio['format_id'],'139')
 def test_failed_tasks_are_not_success(self):
  fs=[{'ext':'mp4','vcodec':'avc1','height':360,'format_id':'18'},{'ext':'m4a','vcodec':'none','format_id':'139'}]
  def call(path,*args):
   if path.startswith('/formats?'):return {'formats':fs}
   if path=='/download':return {'task_id':'fixture'}
   return {'status':'failed'}
  with patch.dict('os.environ',{'VIDEOSCALE_AUTH':'Basic dGVzdDp0ZXN0'}),patch.object(v,'call',side_effect=call):
   with self.assertRaisesRegex(ValueError,'download_failed'):v.download('https://youtu.be/4948LkibJpw')
if __name__=='__main__':unittest.main()
