"""批次生命周期：版本化结果、更正重算、幂等重试、结案并发。"""
import threading
import pytest
from src.models.entities import Training,Claim,AuditBatch,AuditSample,AuditResult
from src.services import audit as svc

def make_training(s,pending):
 s.add(Training(name='t',threshold=0.5,state='done',result={'pending':pending,'checksum':'ck'}))
 s.commit()
 return s.query(Training).order_by(Training.id.desc()).first()

PENDING=[{'id':i,'code':'P%02d'%i,'score':sc} for i,sc in
 zip(range(1,13),[0.05,0.1,0.15,0.19,0.25,0.3,0.45,0.5,0.65,0.8,0.9,0.99])]

@pytest.fixture()
def batch(db_session):
 t=make_training(db_session,PENDING)
 strata=svc.normalize_strata([{'lo':0,'hi':0.2,'sample_n':2},{'lo':0.2,'hi':0.6,'sample_n':4},{'lo':0.6,'hi':1,'sample_n':2}],0)
 b=svc.create_batch(db_session,t,0.5,3106,strata,'analyst@t')
 db_session.commit()
 return b.id

def samples(s,bid):return s.query(AuditSample).filter_by(batch_id=bid).order_by(AuditSample.id).all()

def test_batch_binds_training_pending_universe(db_session,batch):
 b=db_session.get(AuditBatch,batch)
 assert b.population_n==12 and b.sampled_n==8 and b.state=='open'
 assert b.training_checksum=='ck'
 cfg={x['name']:x for x in b.strata}
 assert cfg['S1']['N']==4 and cfg['S1']['n']==2 and cfg['S1']['include_prob']==0.5
 ss=samples(db_session,batch)
 assert all(x.decision=='pending' for x in ss)
 for x in ss:
  assert x.basis['include_prob']==pytest.approx(x.include_prob)
  assert x.basis['population_n']>=x.basis['sample_n']>0

def test_decision_creates_new_revision_and_keeps_history(db_session,batch):
 ss=samples(db_session,batch)
 dv=next(x for x in ss if x.stratum=='S1');fl=next(x for x in ss if x.stratum=='S3')
 _,r1,_=svc.set_decision(db_session,batch,dv.id,'valid','核验正常，无异常')
 assert r1.revision==2  # 修订 1 是创建时的全未结案快照
 _,r2,created=svc.set_decision(db_session,batch,fl.id,'flagged','发票金额与行程不符')
 assert created and r2.revision==3
 db_session.commit()
 rows=db_session.query(AuditResult).filter_by(batch_id=batch).order_by(AuditResult.revision).all()
 assert [r.revision for r in rows]==[1,2,3]
 assert rows[1].result['conclusions']!=rows[2].result['conclusions']
 # 旧结果的依据仍可回看：修订 2 时 fl 尚未结案，且不被当成正常
 old={c['sample_id']:c for c in rows[1].result['conclusions']}
 assert old[fl.id]['decision']=='pending'
 assert rows[1].result['pending_n']==rows[1].result['sampled_n']-1
 assert all('include_prob' in x and 'draw_order' in x for x in db_session.get(AuditBatch,batch).strata)


def test_identical_retry_is_idempotent(db_session,batch):
 ss=samples(db_session,batch);target=ss[0]
 _,r1,c1=svc.set_decision(db_session,batch,target.id,'valid','核验正常，票据齐全')
 _,r2,c2=svc.set_decision(db_session,batch,target.id,'valid','核验正常，票据齐全')
 assert c1 and not c2 and r1.id==r2.id and r2.revision==2
 # 反复重算不新增结果、不重复累计
 for _ in range(3):
  row,created=svc.recompute(db_session,batch)
  assert not created and row.id==r1.id
 count=db_session.query(AuditResult).filter_by(batch_id=batch).count()
 assert count==2  # 创建快照 + 本次结论，仅此两条

def test_pending_blocks_estimate_until_all_decided(db_session,batch):
 ss=samples(db_session,batch)
 for x in ss[:-1]:svc.set_decision(db_session,batch,x.id,'valid','核验正常，票据齐全')
 row,created=svc.recompute(db_session,batch)
 assert not row.result['estimable'] and row.result['matrix'] is None
 assert any(b['reason']=='incomplete' for b in row.result['blockers'])
 last=ss[-1]
 sample,row,created=svc.set_decision(db_session,batch,last.id,'flagged','发票金额与行程不符')
 assert row.result['estimable'] and row.result['matrix'] is not None
 assert created

def test_correction_triggers_recompute_and_preserves_old(db_session,batch):
 ss=samples(db_session,batch)
 for x in ss:svc.set_decision(db_session,batch,x.id,'valid','核验正常，票据齐全')
 final,_=svc.recompute(db_session,batch)
 old_rev=final.revision;old_rate=final.result['anomaly_rate']
 assert old_rate==0
 target=ss[0]
 _,corrected,created=svc.set_decision(db_session,batch,target.id,'flagged','复核后发现金额异常')
 assert created and corrected.revision==old_rev+1
 assert corrected.result['anomaly_rate']>old_rate
 db_session.commit()
 prior=db_session.query(AuditResult).filter_by(batch_id=batch,revision=old_rev).first()
 assert prior.result['anomaly_rate']==old_rate
 assert {c['sample_id']:c['decision'] for c in prior.result['conclusions']}[target.id]=='valid'

def test_close_publishes_matching_revision_and_locks(db_session,batch):
 ss=samples(db_session,batch)
 for x in ss:svc.set_decision(db_session,batch,x.id,'flagged','复核后发现金额异常')
 b,row,closed=svc.close_batch(db_session,batch)
 assert closed and b.state=='closed' and b.published_result_id==row.id
 assert b.closed_revision==row.revision
 # 重复结案幂等
 b2,row2,closed2=svc.close_batch(db_session,batch)
 assert not closed2 and row2.id==row.id
 with pytest.raises(PermissionError):svc.set_decision(db_session,batch,ss[0].id,'valid','核验正常，票据齐全')

def test_close_and_recompute_race():
 """并发结案与重算：发布结果必须对应当前结论；不产生半新半旧结果。"""
 import tempfile,os
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from src.config.database import Base
 path=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False).name
 eng=create_engine('sqlite+pysqlite:///'+path,connect_args={'check_same_thread':False,'timeout':30})
 Base.metadata.create_all(eng);S=sessionmaker(bind=eng,expire_on_commit=False)
 with S() as s:
  t=make_training(s,PENDING)
  st=svc.normalize_strata(None,2)
  bid=svc.create_batch(s,t,0.5,3106,st,'analyst@t').id;s.commit()
  ss=s.query(AuditSample).filter_by(batch_id=bid).all()
  for x in ss:svc.set_decision(s,bid,x.id,'valid','核验正常，票据齐全')
 errors=[];results=[]
 def closer():
  try:
   with S() as s:
    b,row,_=svc.close_batch(s,bid);results.append(('close',row.revision,row.id,row.conclusions_digest))
  except Exception as e:errors.append(e)
 def recompute():
  try:
   with S() as s:
    row,created=svc.recompute(s,bid);results.append(('recompute',row.revision,row.id,row.conclusions_digest))
  except Exception as e:errors.append(e)
 threads=[threading.Thread(target=closer),threading.Thread(target=recompute),
          threading.Thread(target=recompute)]
 for t in threads:t.start()
 for t in threads:t.join()
 assert not errors,errors
 with S() as s:
  b=s.get(AuditBatch,bid)
  pub=s.query(AuditResult).filter_by(id=b.published_result_id).first()
  assert b.state=='closed' and pub.revision==b.closed_revision
  # 所有已发布/回传结果都对应同一批结论
  digests={x[3] for x in results}|{pub.conclusions_digest}
  assert len(digests)==1
  assert pub.result['anomaly_rate']==0
  # 每次结论变更一条修订，重试/并发重算不新增；结案发布的就是全票结论那条
  assert s.query(AuditResult).filter_by(batch_id=bid).count()==len(ss)+1
  assert pub.result['pending_n']==0


def test_close_vs_correction_race_publishes_one_consistent_conclusion_set():
 """结案与结论更正并发：发布结果要么包含该更正，要么该更正被明确拒绝。

 绝不允许出现“已提交更正，但发布结果仍是旧结论”的混合状态。
 """
 import tempfile,threading
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from src.config.database import Base
 path=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False).name
 eng=create_engine('sqlite+pysqlite:///'+path,connect_args={'check_same_thread':False,'timeout':30})
 Base.metadata.create_all(eng);S=sessionmaker(bind=eng,expire_on_commit=False)
 with S() as s:
  t=make_training(s,PENDING)
  bid=svc.create_batch(s,t,0.5,3106,svc.normalize_strata(None,2),'analyst@t').id;s.commit()
  ss=s.query(AuditSample).filter_by(batch_id=bid).order_by(AuditSample.id).all()
  for x in ss[:-1]:svc.set_decision(s,bid,x.id,'valid','核验正常，票据齐全')
  target=ss[-1].id
 outcomes={};errors=[]
 barrier=threading.Barrier(2)
 def closer():
  try:
   barrier.wait()
   with S() as s:
    b,row,_=svc.close_batch(s,bid);outcomes['close']=(b.closed_revision,row.result['anomaly_rate'],row.conclusions_digest)
  except Exception as e:errors.append(('close',e))
 def corrector():
  try:
   barrier.wait()
   with S() as s:
    _,row,_=svc.set_decision(s,bid,target,'flagged','复核后发现金额异常')
    outcomes['correct']=(row.revision,row.result['anomaly_rate'],row.conclusions_digest)
  except PermissionError as e:outcomes['correct_rejected']=str(e)
  except Exception as e:errors.append(('correct',e))
 ts=[threading.Thread(target=closer),threading.Thread(target=corrector)]
 for t in ts:t.start()
 for t in ts:t.join()
 assert not errors,errors
 with S() as s:
  b=s.get(AuditBatch,bid);pub=s.query(AuditResult).filter_by(id=b.published_result_id).first()
  assert b.state=='closed'
  assert pub.revision==b.closed_revision
  current={c['sample_id']:c for c in pub.result['conclusions']}
  target_decision=current[target]['decision']
  if 'correct' in outcomes:
   # 更正先提交：发布结果必须包含它
   assert target_decision=='flagged'
   assert pub.result['anomaly_rate']==outcomes['correct'][1]
   assert pub.conclusions_digest==outcomes['correct'][2]
  else:
   # 结案先提交：更正被明确拒绝，发布结果保持正常
   assert 'correct_rejected' in outcomes
   assert target_decision=='valid'
   assert pub.result['anomaly_rate']==0
  # 同一批次不存在与发布结论不一致的“更新的”结果
  newer=s.query(AuditResult).filter(AuditResult.batch_id==bid,
                                    AuditResult.revision>pub.revision).all()
  assert newer==[]


def test_concurrent_create_same_config_yields_single_open_batch():
 import tempfile,threading
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from sqlalchemy.exc import IntegrityError
 from src.config.database import Base
 path=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False).name
 eng=create_engine('sqlite+pysqlite:///'+path,connect_args={'check_same_thread':False,'timeout':30})
 Base.metadata.create_all(eng);S=sessionmaker(bind=eng,expire_on_commit=False)
 with S() as s:
  t=make_training(s,PENDING)
 strata=svc.normalize_strata(None,2);made=[];conflicts=[]
 def worker():
  with S() as s:
   t=s.query(Training).order_by(Training.id).first()
   sig=svc.config_signature(0.5,3106,strata)
   with svc.batch_lock('create:%s:%s'%(t.id,sig)):
    if svc.find_reusable_batch(s,t.id,sig):conflicts.append('reused');return
    try:
     b=svc.create_batch(s,t,0.5,3106,strata,'a');s.commit();made.append(b.id)
    except IntegrityError:s.rollback();conflicts.append('retry')
 ts=[threading.Thread(target=worker) for _ in range(5)]
 for t in ts:t.start()
 for t in ts:t.join()
 with S() as s:
  opens=s.query(AuditBatch).filter_by(state='open').all()
 assert len(opens)==1 and len(made)==1 and len(conflicts)==4


def test_correction_back_and_forth_returns_latest_matching_result(db_session,batch):
 """A→B→A：改回后的当前结果必须是最新修订，不能停留在 B 的旧结果。"""
 from src.routes.api import audit_batch
 ss=samples(db_session,batch)
 a=ss[0]
 _,r1,_=svc.set_decision(db_session,batch,a.id,'valid','核验正常，票据齐全')
 _,r2,_=svc.set_decision(db_session,batch,a.id,'flagged','复核后发现金额异常')
 assert r2.revision==r1.revision+1
 # 改回原判断：结论集合与 r1 完全相同，但更正动作应追加新修订而非回退/复用 r1
 _,r3,created=svc.set_decision(db_session,batch,a.id,'valid','核验正常，票据齐全')
 assert created and r3.revision==r2.revision+1
 assert r3.conclusions_digest==r1.conclusions_digest and r3.id!=r1.id
 db_session.commit()
 view=audit_batch(batch,type('U',(),{'role':'analyst'})(),db_session)
 assert view['current']['revision']==r3.revision
 assert view['current_is_latest_conclusions'] is True
 digests=[h['result']['conclusions_digest'] for h in view['history']]
 # r1 与 r3 结论摘要相同，但更正动作各留一条不可变修订
 assert digests.count(r1.result['conclusions_digest'])==2
 assert view['history'][0]['revision']==r3.revision
 # 重试重算不会新增修订
 row,created=svc.recompute(db_session,batch)
 assert not created and row.revision==r3.revision

def test_untouched_samples_do_not_change_estimates(db_session,batch):
 ss=samples(db_session,batch)
 for x in ss:svc.set_decision(db_session,batch,x.id,'valid','核验正常，票据齐全')
 base,_=svc.recompute(db_session,batch)
 row,_=svc.recompute(db_session,batch)
 assert row.id==base.id
 assert row.result['anomaly_rate']==base.result['anomaly_rate']
 assert row.result['matrix']==base.result['matrix']

def test_empty_population_stratum_does_not_block_others(db_session):
 # 所有评分都 ≥0.4，S1 [0,0.2) 与 S2 [0.2,0.4) 总体为空
 sparse=[{'id':i,'code':'Q%02d'%i,'score':sc} for i,sc in
         zip(range(1,9),[0.45,0.48,0.52,0.55,0.62,0.7,0.85,0.95])]
 t=make_training(db_session,sparse)
 strata=svc.normalize_strata([
  {'lo':0,'hi':0.2,'sample_n':2},{'lo':0.2,'hi':0.4,'sample_n':2},
  {'lo':0.4,'hi':0.6,'sample_n':2},{'lo':0.6,'hi':0.8,'sample_n':2},{'lo':0.8,'hi':1,'sample_n':2}],0)
 bid=svc.create_batch(db_session,t,0.5,3106,strata,'a').id
 db_session.commit()
 ss=samples(db_session,bid)
 for x in ss:svc.set_decision(db_session,bid,x.id,'valid','核验正常，票据齐全')
 row,_=svc.recompute(db_session,bid)
 names={r['name']:r for r in row.result['strata']}
 empty=[r for r in names.values() if r['population_n']==0]
 assert empty, '本用例需要一个总体为空的分层'
 assert all(r['status']=='empty_population' for r in empty)
 assert row.result['estimable'] and row.result['matrix'] is not None
 assert not any(b['stratum'] in {r['name'] for r in empty} for b in row.result['blockers'])
