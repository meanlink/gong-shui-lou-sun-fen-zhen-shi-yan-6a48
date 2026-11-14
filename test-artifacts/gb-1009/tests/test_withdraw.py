"""批次撤回（仪表失准）全链路集成测试：SQLite + FastAPI TestClient，无需 Redis/Postgres。"""
import os, sys, tempfile, pathlib
TEST_DIR = pathlib.Path(tempfile.mkdtemp(prefix='wt-withdraw-'))
os.environ['DATABASE_URL'] = 'sqlite:///' + str(TEST_DIR / 'test.db')
os.environ['ARTIFACT_DIR'] = str(TEST_DIR / 'artifacts')
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'backend'))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.main import app
from src.config.database import Session
from src.models import User, Dataset, Job, Report, Delivery, Audit
from src.services.business import seed_records
from src.utils.security import hash_password
from src.services import jobs as jobs_service
from src.worker import execute, tick


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr(jobs_service, 'enqueue', lambda job_id: None)
    monkeypatch.setattr('src.services.datasets.remove_queued_jobs', lambda ids: None)


def _login(client, role):
    r = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200, r.text
    t = r.json()['access_token']
    client.headers['Authorization'] = 'Bearer ' + t
    return t


def _auth(role):
    return {'Authorization': 'Bearer ' + _tokens[role]}


_tokens = {}


@pytest.fixture(scope='module', autouse=True)
def _seed(client):
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=f'{role}@demo.local', name=role, role=role,
                        password=hash_password('Demo123!')))
        db.commit()
    for role in ('admin', 'analyst', 'viewer'):
        _tokens[role] = _login(client, role)
    client.headers['Authorization'] = 'Bearer ' + _tokens['admin']


def _make_dataset(client, name, rows=None):
    rows = rows or seed_records()
    r = client.post('/datasets', json={'name': name, 'source': 'meter-a.json', 'records': rows})
    assert r.status_code == 201, r.text
    return r.json()


def _make_job(client, ds_id, name, token='analyst'):
    r = client.post('/jobs', headers=_auth(token),
                    json={'name': name, 'dataset_id': ds_id, 'params': {}})
    assert r.status_code == 201, r.text
    return r.json()


def test_01_withdraw_requires_admin(client):
    ds = _make_dataset(client, '批次A-权限测试')
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('analyst'),
                    json={'reason': '仪表失准'})
    assert r.status_code == 403
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('viewer'),
                    json={'reason': '仪表失准'})
    assert r.status_code == 403
    # 原因必填且有长度要求
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'), json={'reason': 'x'})
    assert r.status_code == 422


def test_02_full_withdraw_cascade_and_guards(client):
    ds = _make_dataset(client, '批次B-撤回全链路')
    # 三个试验：已完成已批准、排队中、另一个排队中
    j_done = _make_job(client, ds['id'], 'B-已完成试验')
    execute(j_done['id'])
    r = client.post(f"/jobs/{j_done['id']}/approve", headers=_auth('admin'))
    assert r.status_code == 200
    j_q1 = _make_job(client, ds['id'], 'B-排队试验1')
    j_q2 = _make_job(client, ds['id'], 'B-排队试验2')
    # 基于已批准结果建一个启用计划并投递一次
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'B-简报', 'job_id': j_done['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0, 'destination': '业务复核组'})
    assert r.status_code == 201, r.text
    report_id = r.json()['id']
    with Session() as db:
        rep = db.get(Report, report_id)
        from src.services.reports import deliver
        d = deliver(db, rep)
        db.commit()
        delivery_id = d.id
        original_csv = pathlib.Path(os.environ['ARTIFACT_DIR']) / d.filename
        original_json = pathlib.Path(str(original_csv) + '.json')
        csv_bytes_before = original_csv.read_bytes()
        assert original_json.exists()

    # 负责人带原因撤回
    reason = '夜间流量计与声学仪表失准，读数不可信'
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                    json={'reason': reason})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['revoked'] is True and body['revoke_reason'] == reason
    assert body['revoked_by'] == 'admin@demo.local' and body['revoked_at']
    scope = body['withdraw_scope']
    assert scope['jobs'] == 3 and scope['jobs_stopped'] == 2
    assert scope['reports_paused'] == 1 and scope['deliveries_invalidated'] == 1

    with Session() as db:
        # 关联试验全部失效；旧结果保留
        for jid in (j_done['id'], j_q1['id'], j_q2['id']):
            j = db.get(Job, jid)
            assert j.revoked is True and j.revoke_reason == reason
        assert db.get(Job, j_done['id']).result  # 旧结果未清空
        assert db.get(Job, j_q1['id']).status == 'revoked'
        # 计划已停用且标记自动暂停
        rep = db.get(Report, report_id)
        assert rep.enabled is False and rep.auto_paused is True
        # 归档投递失效但文件原文保留
        dl = db.get(Delivery, delivery_id)
        assert dl.invalidated is True and dl.invalidate_reason == reason
        assert original_csv.read_bytes() == csv_bytes_before  # 原文未被改写

    # 拒绝再批准
    r = client.post(f"/jobs/{j_done['id']}/approve", headers=_auth('admin'))
    assert r.status_code == 409 and '撤回' in r.json()['detail']
    # 拒绝再建试验
    r = client.post('/jobs', headers=_auth('analyst'),
                    json={'name': 'B-撤回后新试验', 'dataset_id': ds['id'], 'params': {}})
    assert r.status_code == 409 and '撤回' in r.json()['detail']
    # 拒绝再生成简报（建计划）
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'B-撤回后简报', 'job_id': j_done['id'],
                          'interval_seconds': 86400, 'delay_seconds': 0})
    assert r.status_code == 409
    # 暂停的计划不能自行恢复
    r = client.post(f'/reports/{report_id}/toggle', headers=_auth('admin'))
    assert r.status_code == 409 and '不能重新启用' in r.json()['detail']
    with Session() as db:
        assert db.get(Report, report_id).enabled is False
    # 重复撤回拒绝
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                    json={'reason': reason})
    assert r.status_code == 409


def test_03_worker_does_not_publish_revoked_queued_job(client):
    ds = _make_dataset(client, '批次C-排队撤回')
    j = _make_job(client, ds['id'], 'C-排队中撤回')
    r = client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                    json={'reason': '压力表校准漂移'})
    assert r.status_code == 200
    execute(j['id'])  # worker 即使拿到该任务也不得产出有效结果
    with Session() as db:
        job = db.get(Job, j['id'])
        assert job.status == 'revoked' and not job.result


def test_04_revoke_during_running_computation(client, monkeypatch):
    ds = _make_dataset(client, '批次D-计算中撤回')
    j = _make_job(client, ds['id'], 'D-计算中撤回')
    import src.worker as worker
    real_analyze = worker.analyze

    def analyze_then_revoke(records, params):
        out = real_analyze(records, params)
        # 模拟计算进行期间负责人完成撤回
        with Session() as wdb:
            d = wdb.get(Dataset, ds['id'])
            d.revoked = True; d.revoke_reason = '运行中撤回'; d.revoked_by = 'admin@demo.local'
            import time as _t
            d.revoked_at = _t.time()
            wdb.commit()
        return out

    monkeypatch.setattr(worker, 'analyze', analyze_then_revoke)
    execute(j['id'])
    monkeypatch.setattr(worker, 'analyze', real_analyze)
    with Session() as db:
        job = db.get(Job, j['id'])
        assert job.status == 'revoked' and job.revoked is True
        assert not job.result  # 撤回成功后不发布有效结果


def test_05_worker_delivery_suppressed_after_withdraw(client):
    ds = _make_dataset(client, '批次E-分发撤回')
    j = _make_job(client, ds['id'], 'E-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'E-简报', 'job_id': j['id'], 'interval_seconds': 60,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    with Session() as db:
        before = db.scalar(select(Delivery).where(Delivery.report_id == rep_id))
        assert before is None
    # 撤回后 tick：不得产生新投递
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': '仪表失准撤回'})
    tick()
    with Session() as db:
        assert db.scalar(select(Delivery).where(Delivery.report_id == rep_id)) is None
        assert db.get(Report, rep_id).enabled is False


def test_06_download_marks_invalidation_reason(client):
    ds = _make_dataset(client, '批次F-下载标注')
    j = _make_job(client, ds['id'], 'F-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'F-简报', 'job_id': j['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    with Session() as db:
        from src.services.reports import deliver
        d = deliver(db, db.get(Report, rep_id)); db.commit()
        dl_id = d.id
    reason = '声学仪表失准'
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'), json={'reason': reason})
    r = client.get(f'/deliveries/{dl_id}/download?format=csv')
    assert r.status_code == 200
    assert r.headers['x-delivery-invalidated'] == 'true'
    assert reason in r.content.decode('utf-8').splitlines()[0]
    r = client.get(f'/deliveries/{dl_id}/download?format=json')
    assert r.status_code == 200
    payload = r.json()
    assert payload['_invalidated'] is True and payload['_invalidate_reason'] == reason
    assert payload['checksum']  # 原始归档内容仍在
    # 列表接口带失效状态
    r = client.get('/deliveries')
    item = next(x for x in r.json() if x['id'] == dl_id)
    assert item['invalidated'] is True and item['invalidate_reason'] == reason


def test_07_scope_traceability(client):
    ds = _make_dataset(client, '批次G-追溯页')
    j = _make_job(client, ds['id'], 'G-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'G-简报', 'job_id': j['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    with Session() as db:
        from src.services.reports import deliver
        deliver(db, db.get(Report, rep_id)); db.commit()
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': 'G 批次仪表失准'})
    r = client.get(f"/datasets/{ds['id']}/withdraw-scope")
    assert r.status_code == 200
    body = r.json()
    assert len(body['jobs']) == 1 and body['jobs'][0]['revoked'] is True
    assert len(body['reports']) == 1 and body['reports'][0]['enabled'] is False
    assert len(body['deliveries']) == 1 and body['deliveries'][0]['invalidated'] is True
    # viewer 也可查看追溯范围（只读）
    r = client.get(f"/datasets/{ds['id']}/withdraw-scope", headers=_auth('viewer'))
    assert r.status_code == 200


def test_08_other_batch_unaffected_and_correction_flow(client):
    good = _make_dataset(client, '批次H-正常批次')
    bad = _make_dataset(client, '批次H-问题批次')
    j_bad = _make_job(client, bad['id'], 'H-问题试验')
    execute(j_bad['id'])
    client.post(f"/datasets/{bad['id']}/withdraw", headers=_auth('admin'),
                json={'reason': '问题批次仪表失准'})
    # 正常批次仍可建试验、完成、批准、建计划
    j_good = _make_job(client, good['id'], 'H-正常试验')
    execute(j_good['id'])
    r = client.post(f"/jobs/{j_good['id']}/approve", headers=_auth('admin'))
    assert r.status_code == 200
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'H-正常简报', 'job_id': j_good['id'],
                          'interval_seconds': 86400, 'delay_seconds': 0})
    assert r.status_code == 201
    # 更正数据另建批次可重新评估
    fixed = _make_dataset(client, '批次H-更正后批次')
    j_fix = _make_job(client, fixed['id'], 'H-更正试验')
    execute(j_fix['id'])
    assert client.get(f"/jobs/{j_fix['id']}").json()['status'] == 'completed'


def test_09_audit_trail_written(client):
    with Session() as db:
        entries = db.scalars(select(Audit).where(Audit.action == 'dataset.revoked')).all()
        assert entries, '撤回必须写审计'
        for e in entries:
            assert e.detail.get('reason') and 'jobs' in e.detail
    # 竞态兜底：撤回后若计划仍处于启用态（如撤回与分发并发），deliver 必须压制且停用
    ds = _make_dataset(client, '批次K-分发竞态')
    j = _make_job(client, ds['id'], 'K-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'K-简报', 'job_id': j['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': 'K 批次仪表失准'})
    with Session() as db:
        rep = db.get(Report, rep_id)
        rep.enabled = True  # 模拟撤回提交与 worker 扫描交错的瞬间状态
        rep.next_run = 0
        db.commit()
    tick()  # worker 到期扫描：必须压制分发、停用计划并留痕
    with Session() as db:
        assert db.get(Report, rep_id).enabled is False
        assert db.scalar(select(Delivery).where(Delivery.report_id == rep_id)) is None
        suppressed = db.scalars(select(Audit).where(Audit.action == 'report.suppressed')).all()
        assert suppressed


def test_10_overview_counts(client):
    r = client.get('/overview')
    assert r.json()['revoked_datasets'] >= 1
    assert r.json()['invalidated_deliveries'] >= 1


def test_11_manual_paused_plan_stays_paused_and_recoverable_when_valid(client):
    ds = _make_dataset(client, '批次I-手动暂停')
    j = _make_job(client, ds['id'], 'I-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'I-简报', 'job_id': j['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    client.post(f'/reports/{rep_id}/toggle', headers=_auth('admin'))  # 手动暂停
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': 'I 批次撤回'})
    with Session() as db:
        rep = db.get(Report, rep_id)
        assert rep.enabled is False and rep.auto_paused is False  # 原本就是手动暂停
    assert client.post(f'/reports/{rep_id}/toggle', headers=_auth('admin')).status_code == 409


def test_12_running_job_recovery_does_not_resurrect_revoked(client):
    ds = _make_dataset(client, '批次J-崩溃恢复')
    j = _make_job(client, ds['id'], 'J-运行中任务')
    with Session() as db:
        job = db.get(Job, j['id'])
        job.status = 'running'
        db.commit()
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': 'J 批次撤回'})
    # 模拟 worker 启动时的恢复循环
    from src.worker import main  # noqa
    with Session() as db:
        running = db.scalars(select(Job).where(Job.status == 'running')).all()
        for x in running:
            if x.revoked:
                x.status = 'revoked'
        db.commit()
    with Session() as db:
        assert db.get(Job, j['id']).status == 'revoked'


def test_13_post_commit_self_heal_invalidates_racing_delivery(client):
    ds = _make_dataset(client, '批次L-分发并发自愈')
    j = _make_job(client, ds['id'], 'L-试验')
    execute(j['id'])
    client.post(f"/jobs/{j['id']}/approve", headers=_auth('admin'))
    r = client.post('/reports', headers=_auth('admin'),
                    json={'name': 'L-简报', 'job_id': j['id'], 'interval_seconds': 86400,
                          'delay_seconds': 0})
    rep_id = r.json()['id']
    with Session() as db:
        from src.services.reports import deliver
        d = deliver(db, db.get(Report, rep_id))
        db.commit()
        dl_id = d.id
    # 分发已落库后撤回恰好提交（窄竞态），worker 自愈必须把该投递改判失效
    client.post(f"/datasets/{ds['id']}/withdraw", headers=_auth('admin'),
                json={'reason': 'L 批次仪表失准'})
    # 撤回已提交（dataset.revoked=True），但该投递在撤回后才插入、撤回未触达它（真实交错）
    with Session() as db:
        rep = db.get(Report, rep_id)
        rep.enabled = True; rep.auto_paused = False  # 计划仍处于启用扫描态
        dl = db.get(Delivery, dl_id)
        dl.invalidated = False; dl.invalidate_reason = ''  # 撤回未见到该投递
        db.commit()
    from src.worker import _heal
    _heal([(rep_id, dl_id)])
    with Session() as db:
        dl = db.get(Delivery, dl_id)
        assert dl.invalidated is True and dl.invalidate_reason == 'L 批次仪表失准'
        assert db.get(Report, rep_id).enabled is False
        healed = db.scalars(select(Audit).where(Audit.action == 'delivery.self_healed')).all()
        assert healed
