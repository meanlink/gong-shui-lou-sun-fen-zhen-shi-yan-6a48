from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Dataset
from src.schemas.dataset import DatasetInput, WithdrawInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import datasets
from src.services.audit import record
from src.utils.serialization import dataset_dict, job_dict, report_dict, delivery_dict
router=APIRouter(prefix='/datasets')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [dataset_dict(d) for d in db.scalars(select(Dataset).order_by(Dataset.id.desc()))]
@router.post('',status_code=201)
def create(data:DatasetInput,db=Depends(get_db),u=Depends(can_write)):
    d=datasets.create(db,data); record(db,u.email,'dataset.create',{'id':d.id,'rows':len(d.records)})
    db.commit(); return dataset_dict(d)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Dataset,id)
    if not d:raise HTTPException(404,'数据批次不存在')
    return {**dataset_dict(d),'records':d.records}
@router.post('/{id}/withdraw')
def withdraw(id:int,data:WithdrawInput,db=Depends(get_db),u=Depends(admin_only)):
    result=datasets.withdraw(db,id,data.reason,u.email)
    db.commit()
    datasets.remove_queued_jobs(result['stopped_job_ids'])
    return {**dataset_dict(result['dataset']),
            'withdraw_scope': {'jobs': result['jobs'], 'jobs_stopped': result['jobs_stopped'],
                               'reports_paused': result['reports_paused'],
                               'reports_already_paused': result['reports_already_paused'],
                               'deliveries_invalidated': result['deliveries_invalidated']}}
@router.get('/{id}/withdraw-scope')
def withdraw_scope(id:int,db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Dataset,id)
    if not d:raise HTTPException(404,'数据批次不存在')
    s=datasets.scope(db,id)
    return {**dataset_dict(d),
            'jobs':[job_dict(j) for j in s['jobs']],
            'reports':[report_dict(r) for r in s['reports']],
            'deliveries':[delivery_dict(x) for x in s['deliveries']]}
