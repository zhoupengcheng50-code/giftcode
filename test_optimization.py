import tempfile
import threading
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import auth_manager as auth
import web_server as web
from auto_gift_code import GiftCodeAutomator
from unified_parser import TIERS, parse_tiers

class Tests(unittest.TestCase):
    def test_initial_admin_is_unique_and_authenticates(self):
        with tempfile.TemporaryDirectory() as d, patch.object(auth, 'BASE_DIR', Path(d)):
            h = auth._admin_hash()
            password = (Path(d)/'首次登录密码.txt').read_text(encoding='utf-8').splitlines()[1].split(': ',1)[1]
            self.assertTrue(auth.verify_password(password,h))
            self.assertEqual(h,auth._admin_hash())
            self.assertNotEqual(h, auth.ADMIN_HASH)
    def test_parser_and_job_validation(self):
        rewards = parse_tiers([(tier,'金币×2') for tier in TIERS])
        job={'activity_name':'测试20261126','batch_date':'20261126','expire_time':(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),'count':2,'rewards':rewards,'username':'u','password':'p'}
        self.assertEqual(web.validate_job(job)['count'],2)
        for field,value in [('count',5001),('expire_time','2000-01-01 00:00:00')]:
            bad={**job,field:value}
            with self.assertRaises(ValueError): web.validate_job(bad)
        with self.assertRaises(ValueError): parse_tiers([(tier,'金币×2') for tier in TIERS[:-1]])
    def test_cancel_does_not_release_running_job(self):
        event=threading.Event()
        with web.LOCK:
            web.STATUS['running']=True
            web.CANCEL_EVENT=event
            web.TASK_OWNER='u'
        request=SimpleNamespace(state=SimpleNamespace(user={'sub':'u'}))
        self.assertEqual(web.stop_task(request)['status'],'stopping')
        self.assertTrue(event.is_set())
        self.assertTrue(web.STATUS['running'])
        web.STATUS['running']=False
        obj=GiftCodeAutomator({'credentials':('u','p'),'directory':Path('.'),'expire_time':'2030-01-01 00:00:00','count':1,'cancel_event':event},Path('.'),lambda *a:None,lambda *a:None)
        with self.assertRaises(RuntimeError): obj.post_view_service({})
    def test_tls_verification_enabled(self):
        obj=GiftCodeAutomator({'credentials':('u','p'),'directory':Path('.'),'expire_time':'2030-01-01 00:00:00','count':1},Path('.'),lambda *a:None,lambda *a:None)
        with patch.dict('os.environ',{},clear=True):
            obj._init_session()
            self.assertIs(obj.session.verify,True)
    def test_cloud_accounts_use_database_without_file_fallback(self):
        import storage
        records = {}
        with patch.object(storage, 'CLOUD', True), patch.object(storage, 'get', side_effect=lambda k: records.get(k)), patch.object(storage, 'put', side_effect=lambda k,v: records.update({k:v})):
            auth._save_users({'operator': {'password_hash': 'hash'}})
            self.assertEqual(auth._load_users()['operator']['password_hash'], 'hash')
        with patch.object(storage, 'CLOUD', True), patch.object(storage, 'get', side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):
                auth._load_users()

    def test_cloud_job_file_persistence_and_path_boundary(self):
        import storage
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            f = root / 'jobs' / 'abc' / 'progress.json'
            f.parent.mkdir(parents=True)
            f.write_bytes(b'checkpoint')
            with patch.object(storage, 'CLOUD', True), patch.object(storage, 'ROOT', root), patch.object(storage, 'put') as put:
                storage.save_job_file(f)
                put.assert_called_once_with('jobs/abc/progress.json', b'checkpoint')
                with self.assertRaises(ValueError):
                    storage.save_job_file(root / 'outside')

    def test_export_rejects_duplicates_and_expiry(self):
        from export_merger import validate_records
        row={'code':'a','created_time':'2026-01-01 00:00:00','expired_time':'2030-01-01 00:00:00'}
        with self.assertRaises(ValueError): validate_records('100',[row,row],2,'2030-01-01 00:00:00')
        with self.assertRaises(ValueError): validate_records('100',[row],1,'2031-01-01 00:00:00')

if __name__=='__main__': unittest.main()
