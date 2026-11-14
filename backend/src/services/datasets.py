import time
from fastapi import HTTPException
from sqlalchemy import select
from src.models import Dataset, Job, Report, Delivery
from src.services.business import metadata
from src.services.audit import record
from src.utils.serialization import checksum
import math

def create(db, data):
    name=data.name.strip()
    if len(name)<2: raise HTTPException(422, '批次名称去除空格后至少需要两个字符')
    if db.scalar(select(Dataset).where(Dataset.name==name)):
        raise HTTPException(409, '该数据批次名已存在，请修改名称')
    for index,row in enumerate(data.records):
        for c in metadata()['columns']:
            value=row.get(c['key'])
            if value is None or value=='': raise HTTPException(422, f'第 {index+1} 行缺少字段 {c["label"]}')
            if c['type']=='number':
                try:
                    if isinstance(value,bool) or not math.isfinite(float(value)): raise ValueError()
                    row[c['key']]=float(value)
                except (TypeError, ValueError, OverflowError): raise HTTPException(422, f'第 {index+1} 行 {c["label"]} 必须为有限数值')
    d=Dataset(name=name, source=data.source, records=data.records, checksum=checksum(data.records), created_at=time.time())
    db.add(d); db.flush()
    return d

def withdraw(db, dataset_id, reason, actor):
    d=db.get(Dataset, dataset_id)
    if not d: raise HTTPException(404, '数据批次不存在')
    if d.revoked: raise HTTPException(409, '该批次已撤回，请勿重复操作；更正数据请另建批次重新评估')
    reason=reason.strip()
    if len(reason)<2: raise HTTPException(422, '撤回原因至少需要两个字符')
    now=time.time()
    # 批次本体：原始记录与指纹保留，仅标记撤回状态与原因
    d.revoked=True; d.revoke_reason=reason; d.revoked_by=actor; d.revoked_at=now
    # 关联试验立即失效：保留旧结果，排队/计算中的任务改判撤回且不再产出有效结果
    jobs=list(db.scalars(select(Job).where(Job.dataset_id==d.id)))
    stopped=0
    for j in jobs:
        if j.revoked: continue
        j.revoked=True; j.revoke_reason=reason; j.revoked_at=now
        if j.status in ('queued','running'):
            j.status='revoked'; j.finished_at=now; stopped+=1
    # 关联分发计划立即停用（暂停的计划不会自行恢复，启用中的标记为撤回自动暂停）
    job_ids=[j.id for j in jobs]
    reports=list(db.scalars(select(Report).where(Report.job_id.in_(job_ids)))) if job_ids else []
    paused=0; already_paused=0
    for r in reports:
        if r.enabled:
            r.enabled=False; r.auto_paused=True; paused+=1
        else:
            already_paused+=1
    # 已归档投递标记失效，归档原文与指纹保留
    report_ids=[r.id for r in reports]
    deliveries=list(db.scalars(select(Delivery).where(Delivery.report_id.in_(report_ids)))) if report_ids else []
    invalidated=0
    for dl in deliveries:
        if dl.invalidated: continue
        dl.invalidated=True; dl.invalidate_reason=reason; dl.invalidated_at=now; invalidated+=1
    record(db, actor, 'dataset.revoked', {'id': d.id, 'reason': reason, 'jobs': len(jobs), 'jobs_stopped': stopped, 'reports_paused': paused, 'reports_already_paused': already_paused, 'deliveries_invalidated': invalidated})
    db.flush()
    return {'dataset': d, 'jobs': len(jobs), 'jobs_stopped': stopped, 'stopped_job_ids': [j.id for j in jobs if j.status == 'revoked'], 'reports_paused': paused, 'reports_already_paused': already_paused, 'deliveries_invalidated': invalidated}

def remove_queued_jobs(job_ids):
    """尽力从 Redis 队列移除已撤回的排队任务；SQL 状态本身是持久兜底。"""
    if not job_ids: return
    try:
        from redis import Redis
        from src.config.settings import REDIS_URL
        r=Redis.from_url(REDIS_URL, socket_timeout=2)
        for jid in job_ids:
            r.lrem('analysis:queue', 0, str(jid))
        r.close()
    except Exception:
        pass

def scope(db, dataset_id):
    d=db.get(Dataset, dataset_id)
    if not d: raise HTTPException(404, '数据批次不存在')
    jobs=list(db.scalars(select(Job).where(Job.dataset_id==d.id).order_by(Job.id)))
    job_ids=[j.id for j in jobs]
    reports=list(db.scalars(select(Report).where(Report.job_id.in_(job_ids)).order_by(Report.id))) if job_ids else []
    report_ids=[r.id for r in reports]
    deliveries=list(db.scalars(select(Delivery).where(Delivery.report_id.in_(report_ids)).order_by(Delivery.id))) if report_ids else []
    return {'jobs': jobs, 'reports': reports, 'deliveries': deliveries}
