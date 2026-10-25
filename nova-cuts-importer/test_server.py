import os
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
import asyncio
import sys
from types import SimpleNamespace
os.environ['IMPORT_RESOLVER_KEY'] = 'test-secret-' * 4
os.environ['PUBLIC_ORIGIN'] = 'https://importer.example.com'
import server

class ResolverTests(unittest.TestCase):
    def test_only_youtube_ids(self):
        self.assertEqual(server.video_id('https://m.youtube.com/watch?v=mPZC9b3DWz4'), 'mPZC9b3DWz4')
        for url in ('https://127.0.0.1/a', 'https://evil.com/?v=mPZC9b3DWz4', 'https://youtube.com@evil.com/watch?v=mPZC9b3DWz4'):
            with self.assertRaises(ValueError): server.video_id(url)
    def test_signed_media_scope(self):
        payload = {'url':'https://rr1.googlevideo.com/videoplayback?a=1','expires':9999999999,'headers':{}}
        token = server.seal(payload)
        self.assertEqual(server.unseal(token), payload)
        with self.assertRaises(ValueError): server.unseal(token + 'x')
        with self.assertRaises(ValueError): server.unseal(server.seal({**payload,'expires':0}))
        with self.assertRaises(ValueError): server.unseal(server.seal({**payload,'url':'https://127.0.0.1/'}))
    @patch('server.validate_media')
    @patch('server.YoutubeDL')
    def test_video_audio_same_origin_relay(self, mock, validate):
        mock.return_value.__enter__.return_value.extract_info.return_value = {'title':'Fixture', 'formats':[
            {'url':'https://rr1.googlevideo.com/video','ext':'mp4','vcodec':'avc1','acodec':'none','height':720,'fps':30,'protocol':'https'},
            {'url':'https://rr1.googlevideo.com/video1080','ext':'mp4','vcodec':'avc1','acodec':'none','height':1080,'fps':30,'protocol':'https'},
            {'url':'https://rr1.googlevideo.com/audio','ext':'m4a','vcodec':'none','acodec':'mp4a','protocol':'https','abr':64},
            {'url':'https://rr1.googlevideo.com/audio128','ext':'m4a','vcodec':'none','acodec':'mp4a','protocol':'https','abr':128},
            {'url':'https://evil.com/video','ext':'mp4','vcodec':'avc1','height':720,'protocol':'https'}]}
        result = server.resolve_once('https://youtu.be/mPZC9b3DWz4')
        self.assertEqual(result['title'], 'Fixture')
        self.assertTrue(result['url'].startswith(server.ORIGIN + '/media?token='))
        self.assertTrue(result['audioUrl'].startswith(server.ORIGIN + '/media?token='))
        from urllib.parse import parse_qs, urlparse
        video = server.unseal(parse_qs(urlparse(result['url']).query)['token'][0])
        audio = server.unseal(parse_qs(urlparse(result['audioUrl']).query)['token'][0])
        self.assertEqual(video['url'], 'https://rr1.googlevideo.com/video1080')
        self.assertEqual(audio['url'], 'https://rr1.googlevideo.com/audio128')
        args = mock.call_args.args[0]
        self.assertIn('mweb', args['extractor_args']['youtube']['player_client'])
        self.assertEqual(args['extractor_args']['youtube']['player_client'][0], 'visionos')
        self.assertIn('youtubepot-bgutilhttp', args['extractor_args'])
        self.assertEqual(validate.call_count, 2)

    @patch('server.requests.get')
    def test_html_is_not_a_successful_import(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 206
        response.headers = {'Content-Type': 'text/html'}
        response.iter_content.return_value = iter([b'<html>Forbidden</html>'])
        with self.assertRaisesRegex(ValueError, 'invalid_media_response'):
            server.validate_media({'url':'https://rr1.googlevideo.com/video'})
        response.headers = {'Content-Type': 'video/mp4'}
        with self.assertRaisesRegex(ValueError, 'invalid_media_signature'):
            server.validate_media({'url':'https://rr1.googlevideo.com/video'})

    @patch.dict(os.environ, {'WPC_ENABLED': '1'})
    @patch('server.run_attempt')
    def test_wpc_recovers_failed_transfer(self, attempt):
        attempt.side_effect = [ValueError('invalid_media_response'), {'url':'https://importer.example.com/media?token=fixture','title':'Fixture'}]
        result = server.resolve('https://youtu.be/mPZC9b3DWz4')
        self.assertEqual(result['title'], 'Fixture')
        self.assertEqual([call.args[1] for call in attempt.call_args_list], ['bgutil', 'wpc'])

    @patch.dict(os.environ, {'WPC_ENABLED': '1'})
    @patch('server.run_attempt', side_effect=ValueError('youtube_login_required'))
    def test_persistent_login_requirement_remains_failure(self, attempt):
        with self.assertRaisesRegex(ValueError, 'youtube_login_required'):
            server.resolve('https://youtu.be/mPZC9b3DWz4')

    @patch('server.browser_guest_session', new_callable=AsyncMock)
    @patch('server.YoutubeDL')
    def test_guest_session_file_is_private_and_deleted_on_failure(self, ydl, guest):
        guest.return_value = '# Netscape HTTP Cookie File\n'
        event_loop = asyncio.new_event_loop()
        captured = {}
        def enter(options):
            path = options['cookiefile']
            captured['path'] = path
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            raise ValueError('fixture_failure')
        ydl.side_effect = enter
        try:
            with patch.dict(sys.modules, {'nodriver': SimpleNamespace(loop=lambda: event_loop)}):
                with self.assertRaisesRegex(ValueError, 'fixture_failure'):
                    server.resolve_once('https://youtu.be/mPZC9b3DWz4', 'wpc')
        finally:
            event_loop.close()
        self.assertFalse(os.path.exists(captured['path']))

if __name__ == '__main__': unittest.main()
