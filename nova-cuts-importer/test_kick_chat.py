import unittest,time,threading
from types import SimpleNamespace
from unittest.mock import patch
import kick_chat
class ChatStatsTests(unittest.TestCase):
 def fixture(self):
  w=SimpleNamespace(lock=threading.Lock());kick_chat.initialize(w);w.chat_started=1;return w
 def test_duplicates_and_distinct_people(self):
  w=self.fixture()
  with patch('kick_chat.time.time',return_value=100):
   for index in range(12):kick_chat.add_message(w,{'id':str(index),'sender':{'id':str(index%4),'username':'viewer'},'content':'hello'})
   kick_chat.add_message(w,{'id':'1','sender':{'id':'1'},'content':'duplicate'})
   d=kick_chat.snapshot(w)
   self.assertEqual(d['chatStats']['count'],12);self.assertEqual(d['chatStats']['unique'],4);self.assertFalse(d['chatStats']['warming']);self.assertEqual(len(d['messages']),12)
 def test_old_events_leave_baseline(self):
  w=self.fixture()
  with patch('kick_chat.time.time',return_value=100):kick_chat.add_message(w,{'id':'old','sender':{'id':'a'}})
  with patch('kick_chat.time.time',return_value=240):
   self.assertEqual(kick_chat.snapshot(w)['chatStats']['count'],0);self.assertEqual(len(w.chat_ids),0)
if __name__=='__main__':unittest.main()
