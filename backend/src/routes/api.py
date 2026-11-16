import os,json,csv,io
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Claim,Training,Review,Audit,AuditBatch,AuditSample,AuditResult
from src.schemas.business import Login,ClaimInput,TrainingInput,ReviewInput,AuditBatchInput,AuditDecisionInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services import audit as audit_svc
from src.controllers.business import ensure_training
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/claims')
def claims(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Claim).order_by(Claim.id).all()]
@router.post('/claims')
def create_claim(data:ClaimInput,u=Depends(editor),s=Depends(db)):
 r=Claim(**data.model_dump());s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'报销编号重复')
 audit(s,u,'录入报销 '+r.code);return dump(r)
@router.put('/claims/{i}')
def edit_claim(i:int,data:ClaimInput,u=Depends(editor),s=Depends(db)):
 r=s.get(Claim,i)
 if not r:raise HTTPException(404,'报销样本不存在')
 for k,v in data.model_dump().items():setattr(r,k,v)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'报销编号重复')
 audit(s,u,'修订报销 '+str(i));return dump(r)
@router.get('/trainings')
def trainings(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Training).order_by(Training.id.desc()).all()]
@router.post('/trainings')
def train(data:TrainingInput,u=Depends(editor),s=Depends(db)):
 rows=[dump(r) for r in s.query(Claim).order_by(Claim.id).all()];r=Training(**data.model_dump(),result={'snapshot':rows});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('claim:trainings',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，请稍后重新提交';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交训练 '+str(r.id));return dump(r)
@router.get('/trainings/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_training(s,i))
@router.get('/trainings/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_training(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功训练可导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="claim-triage-{i}.json"'})
@router.post('/trainings/{i}/reviews')
def generate(i:int,u=Depends(editor),s=Depends(db)):
 r=ensure_training(s,i)
 if r.state!='done':raise HTTPException(409,'训练完成后才能生成复核任务')
 created=0
 for p in r.result['pending']:
  if p['score']>=r.threshold and not s.query(Review).filter_by(training_id=i,claim_id=p['id']).first():s.add(Review(training_id=i,claim_id=p['id'],score=p['score']));created+=1
 s.commit();audit(s,u,'生成风险复核 '+str(i));return {'created':created}
@router.get('/reviews')
def reviews(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Review).order_by(Review.id.desc()).all()]
@router.put('/reviews/{i}')
def review(i:int,data:ReviewInput,u=Depends(editor),s=Depends(db)):
 r=s.get(Review,i)
 if not r:raise HTTPException(404,'复核任务不存在')
 if r.status=='approved':raise HTTPException(409,'已签核复核不可修改')
 r.decision=data.decision;r.note=data.note;s.commit();audit(s,u,'人工复核 '+str(i));return dump(r)
@router.post('/reviews/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=s.get(Review,i)
 if not r:raise HTTPException(404,'复核任务不存在')
 if r.decision=='pending' or r.status!='draft':raise HTTPException(409,'先填写人工判断，且只签核一次')
 r.status='approved';s.commit();audit(s,u,'签核复核 '+str(i));return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
def ensure_batch(s,i):
 row=s.get(AuditBatch,i)
 if not row:raise HTTPException(404,'抽检批次不存在')
 return row
def result_view(row):
 return {'id':row.id,'batch_id':row.batch_id,'revision':row.revision,'estimable':row.estimable,'result':row.result,'created_at':row.created_at}
@router.post('/trainings/{i}/audit-batches')
def create_audit_batch(i:int,data:AuditBatchInput,u=Depends(editor),s=Depends(db)):
 r=ensure_training(s,i)
 if r.state!='done':raise HTTPException(409,'训练完成后才能发起抽检')
 try:strata=audit_svc.normalize_strata([x.model_dump() for x in data.strata],data.default_sample_n)
 except ValueError as e:raise HTTPException(422,str(e))
 signature=audit_svc.config_signature(data.threshold,data.seed,strata)
 with audit_svc.batch_lock('create:%s:%s'%(i,signature)):
  reusable=audit_svc.find_reusable_batch(s,i,signature)
  if reusable:
   audit(s,u,'复用未结案抽检 %s'%reusable.id)
   return {'batch':audit_svc.serialize_batch(reusable),'reused':True}
  try:batch=audit_svc.create_batch(s,r,data.threshold,data.seed,strata,u.email)
  except ValueError as e:raise HTTPException(422,str(e))
  try:s.commit()
  except IntegrityError:
   s.rollback()
   batch=audit_svc.find_reusable_batch(s,i,signature)
   if not batch:raise HTTPException(409,'同一抽检配置正在被并发创建，请稍后重试')
   return {'batch':audit_svc.serialize_batch(batch),'reused':True}
 audit(s,u,'发起抽检 %s（训练 %s）'%(batch.id,i))
 return {'batch':audit_svc.serialize_batch(batch),'reused':False}
@router.get('/audit-batches')
def audit_batches(u=Depends(current),s=Depends(db),training_id:int=None):
 q=s.query(AuditBatch)
 if training_id is not None:q=q.filter_by(training_id=training_id)
 return [audit_svc.serialize_batch(r) for r in q.order_by(AuditBatch.id.desc()).all()]
@router.get('/audit-batches/{i}')
def audit_batch(i:int,u=Depends(current),s=Depends(db)):
 b=ensure_batch(s,i)
 samples=s.query(AuditSample).filter_by(batch_id=i).order_by(AuditSample.id).all()
 digest=audit_svc.conclusions_digest(samples)
 current_rev=s.query(AuditResult).filter_by(batch_id=i,conclusions_digest=digest).order_by(AuditResult.revision.desc()).first()
 if current_rev is None and samples:
  # 结论已变但尚未重算：返回最新修订，并提示需重算
  current_rev=s.query(AuditResult).filter_by(batch_id=i).order_by(AuditResult.revision.desc()).first()
 published=s.get(AuditResult,b.published_result_id) if b.published_result_id else None
 history=[result_view(x) for x in s.query(AuditResult).filter_by(batch_id=i).order_by(AuditResult.revision.desc()).all()]
 stale=bool(current_rev and current_rev.conclusions_digest!=digest)
 return {'batch':audit_svc.serialize_batch(b),'samples':[audit_svc.serialize_sample(x) for x in samples],
  'current':result_view(current_rev) if current_rev else None,
  'current_is_latest_conclusions':not stale,
  'published':result_view(published) if published else None,'history':history}
@router.put('/audit-batches/{i}/samples/{sid}')
def audit_decide(i:int,sid:int,data:AuditDecisionInput,u=Depends(editor),s=Depends(db)):
 ensure_batch(s,i)
 try:sample,row,created=audit_svc.set_decision(s,i,sid,data.decision,data.note)
 except LookupError as e:raise HTTPException(404,str(e))
 except PermissionError as e:raise HTTPException(409,str(e))
 audit(s,u,'抽检结论 %s/%s'%(i,sid))
 return {'sample':audit_svc.serialize_sample(sample),'result':result_view(row) if row else None,'revised':created}
@router.post('/audit-batches/{i}/recompute')
def audit_recompute(i:int,u=Depends(editor),s=Depends(db)):
 ensure_batch(s,i)
 try:row,created=audit_svc.recompute(s,i)
 except LookupError as e:raise HTTPException(404,str(e))
 audit(s,u,'重算抽检 %s'%i)
 return {'result':result_view(row),'created':created}
@router.post('/audit-batches/{i}/close')
def audit_close(i:int,u=Depends(admin),s=Depends(db)):
 ensure_batch(s,i)
 try:batch,row,closed=audit_svc.close_batch(s,i)
 except LookupError as e:raise HTTPException(404,str(e))
 audit(s,u,'结案抽检 %s'%i)
 return {'batch':audit_svc.serialize_batch(batch),'result':result_view(row),'closed_now':closed}
@router.get('/audit-batches/{i}/export')
def audit_export(i:int,scope:str='all',u=Depends(current),s=Depends(db)):
 b=ensure_batch(s,i)
 if scope=='published':
  row=s.get(AuditResult,b.published_result_id) if b.published_result_id else None
  if not row:raise HTTPException(409,'该批次尚未结案，没有已发布结果')
  return Response(json.dumps(result_view(row),ensure_ascii=False,indent=2),media_type='application/json',
   headers={'Content-Disposition':f'attachment; filename="audit-batch-{i}-published-r{row.revision}.json"'})
 if scope=='history':
  rows=s.query(AuditResult).filter_by(batch_id=i).order_by(AuditResult.revision).all()
  payload={'batch':audit_svc.serialize_batch(b),'results':[result_view(x) for x in rows]}
  return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',
   headers={'Content-Disposition':f'attachment; filename="audit-batch-{i}-history.json"'})
 samples=s.query(AuditSample).filter_by(batch_id=i).order_by(AuditSample.id).all()
 buf=io.StringIO();w=csv.writer(buf)
 w.writerow(['样本ID','报销编号','分层','模型概率','入选概率','抽样依据','结论','理由','更新时间'])
 for x in samples:
  w.writerow([x.id,x.claim_code,x.stratum,f'{x.score:.6f}',f'{x.include_prob:.6f}',
   json.dumps(x.basis,ensure_ascii=False,sort_keys=True),x.decision,x.note or '',x.updated_at or ''])
 return Response('﻿'+buf.getvalue(),media_type='text/csv; charset=utf-8',
  headers={'Content-Disposition':f'attachment; filename="audit-batch-{i}-samples.csv"'})
