import time, os, csv, io, hashlib, json
from urllib.parse import quote
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from src.models import Dataset, Job, Report, Delivery
from src.config.settings import ARTIFACT_DIR
def _revoked(job):
    """结果是否仍可作为有效业务结论：任务自身或其数据批次任一撤回即失效。"""
    if job is None: return True
    if getattr(job, 'revoked', False): return True
    ds=job.dataset
    if ds is not None and getattr(ds, 'revoked', False): return True
    return False
def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    if _revoked(j):
        raise HTTPException(409,'该分析结果随数据批次撤回已失效，不能再生成巡检简报')
    r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True)
    db.add(r); db.flush()
    return r
def deliver(db, report):
    j=db.get(Job,report.job_id)
    # 撤回成功后任何在途/到期分发都不得发布有效结果
    if not j or not j.approved or _revoked(j):
        report.enabled=False
        return None
    out=io.StringIO()
    out.write('﻿')
    table=j.result.get('table',{})
    cols=table.get('columns',[])
    writer=csv.writer(out)
    def safe(v):
        if isinstance(v,(int,float)) and not isinstance(v,bool): return v
        text=str(v)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows',[]): writer.writerow([safe(row.get(c['key'],'')) for c in cols])
    # 落盘前再次确认：撤回与本次分发并发时，撤回成功的结果不得发布
    db.flush()
    if _revoked(j):
        report.enabled=False
        return None
    data=out.getvalue().encode('utf-8')
    delivery=Delivery(report_id=report.id, created_at=time.time(), checksum=hashlib.sha256(data).hexdigest(), filename='', destination=report.destination)
    db.add(delivery); db.flush()
    delivery.filename=f'report-{report.id}-delivery-{delivery.id}.csv'
    os.makedirs(ARTIFACT_DIR,exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR,delivery.filename),'wb') as f:f.write(data)
    with open(os.path.join(ARTIFACT_DIR,delivery.filename+'.json'),'w') as f: json.dump({'job':j.name,'params':j.params,'result':j.result,'checksum':delivery.checksum},f,ensure_ascii=False,allow_nan=False)
    report.next_run=time.time()+report.interval_seconds
    return delivery
def download_response(d, fmt):
    filename=d.filename+('.json' if fmt=='json' else '')
    p=os.path.join(ARTIFACT_DIR,filename)
    if not os.path.exists(p): raise HTTPException(404,'归档文件不存在，请重新安排分发')
    media='application/json; charset=utf-8' if fmt=='json' else 'text/csv; charset=utf-8'
    with open(p,'rb') as f: raw=f.read()
    headers={'X-Archived-Filename': quote(filename)}
    if d.invalidated:
        # 归档原文保留，下载时标明失效原因；原文件本身不被改写
        headers['X-Delivery-Invalidated']='true'
        headers['X-Invalidate-Reason']=quote(d.invalidate_reason)
        if fmt=='json':
            payload=json.loads(raw.decode('utf-8'))
            payload['_invalidated']=True
            payload['_invalidate_reason']=d.invalidate_reason
            payload['_invalidated_at']=d.invalidated_at
            body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
        else:
            banner=('【已失效归档】本简报依据的数据批次已撤回，以下内容仅作留痕，不得作为有效业务依据。失效原因：%s\n'%d.invalidate_reason).encode('utf-8')
            body=banner+raw
        return Response(content=body, media_type=media, headers=headers)
    return Response(content=raw, media_type=media, headers=headers)
