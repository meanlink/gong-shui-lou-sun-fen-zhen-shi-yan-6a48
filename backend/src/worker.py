import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, Delivery
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def _heal(created):
    """提交后自愈：撤回与分发并发交错时，把已落库却不应有效的投递改判失效。"""
    for report_id, delivery_id in created:
        with Session() as db:
            r=db.get(Report,report_id); j=db.get(Job,r.job_id) if r else None
            ds=db.get(Dataset,j.dataset_id) if j else None
            if (j is not None and j.revoked) or (ds is not None and ds.revoked):
                d=db.get(Delivery,delivery_id)
                if d is not None and not d.invalidated:
                    reason=(j.revoke_reason if j else '') or (ds.revoke_reason if ds else '') or '数据批次撤回'
                    d.invalidated=True; d.invalidate_reason=reason; d.invalidated_at=time.time()
                    if r is not None:
                        r.enabled=False; r.auto_paused=True
                    record(db,'worker','delivery.self_healed',{'id':delivery_id,'reason':reason})
                    db.commit()
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        # 已被撤回的排队任务不允许再启动计算
        if j.revoked:
            j.status='revoked'; j.finished_at=time.time()
            record(db,'worker','job.revoked_skipped',{'id':j.id,'reason':j.revoke_reason})
            db.commit(); return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            # 计算进行期间批次/任务被撤回：结果不得作为有效结论发布
            db.refresh(j)
            if d is not None: db.refresh(d)
            if j.revoked or (d is not None and d.revoked):
                reason=j.revoke_reason or (d.revoke_reason if d else '')
                j.revoked=True; j.revoke_reason=reason; j.status='revoked'; j.finished_at=time.time()
                record(db,'worker','job.revoked_during_run',{'id':j.id,'reason':reason})
            else:
                j.result=result;j.status='completed';j.finished_at=time.time()
                record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    created=[]
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:
                record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
                created.append((r.id,d.id))
            else:
                record(db,'worker','report.suppressed',{'id':r.id})
        db.commit()
    _heal(created)
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            # 撤回成功的任务不得被崩溃恢复逻辑重新排队
            if j.revoked:
                j.status='revoked';record(db,'worker','job.revoked_skipped',{'id':j.id,'reason':j.revoke_reason})
            else:
                j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
