"""HTTP 层：发起抽检、填写结论、进度、下载明细、权限与失败恢复。"""
import json
import pytest
from src.models.entities import Training

PENDING=[{'id':i,'code':'P%02d'%i,'score':sc} for i,sc in
 zip(range(1,13),[0.05,0.1,0.15,0.19,0.25,0.3,0.45,0.5,0.65,0.8,0.9,0.99])]
STRATA=[{'lo':0,'hi':0.2,'sample_n':2},{'lo':0.2,'hi':0.6,'sample_n':4},{'lo':0.6,'hi':1,'sample_n':2}]

@pytest.fixture()
def training_id(db_session):
 t=Training(name='11月基线',threshold=0.5,state='done',result={'pending':PENDING,'checksum':'ck'})
 db_session.add(t);db_session.commit()
 return t.id

def create(api,tid,**over):
 body={'threshold':0.5,'seed':3106,'strata':STRATA};body.update(over)
 return api.req('analyst','POST',f'/trainings/{tid}/audit-batches',json=body)

def test_create_returns_sampling_basis(api_client,training_id):
 api=api_client
 r=create(api,training_id)
 assert r.status_code==200,r.text
 data=r.json();b=data['batch']
 assert not data['reused'] and b['population_n']==12 and b['sampled_n']==8 and b['state']=='open'
 assert len(b['strata'])==3 and b['strata'][0]['include_prob']==0.5
 d=api.req('analyst','GET',f"/audit-batches/{b['id']}").json()
 assert len(d['samples'])==8
 for s in d['samples']:
  assert 0<s['include_prob']<=1
  assert s['basis']['population_n']>=s['basis']['sample_n']
  assert s['basis']['stratum']==s['stratum'] and s['basis']['seed']==3106
 assert d['current'] is not None and d['published'] is None
 assert d['current']['result']['estimable'] is False

def test_same_config_reuses_open_batch(api_client,training_id):
 api=api_client
 first=create(api,training_id).json();second=create(api,training_id).json()
 assert second['reused'] is True and second['batch']['id']==first['batch']['id']

def test_different_config_creates_new_batch(api_client,training_id):
 api=api_client
 a=create(api,training_id).json()['batch']
 b=create(api,training_id,seed=99).json()
 assert not b['reused'] and b['batch']['id']!=a['id']

def test_invalid_strata_422(api_client,training_id):
 api=api_client
 bad=[{'lo':0,'hi':0.5,'sample_n':1}]
 assert create(api,training_id,strata=bad).status_code==422
 overlap=[{'lo':0,'hi':0.5,'sample_n':1},{'lo':0.4,'hi':1,'sample_n':1}]
 assert create(api,training_id,strata=overlap).status_code==422
 negative=[{'lo':0,'hi':1,'sample_n':-1}]
 assert create(api,training_id,strata=negative).status_code==422

def test_requires_done_training(api_client,db_session,training_id):
 api=api_client
 t=db_session.get(Training,training_id);t.state='running';db_session.commit()
 assert create(api,training_id).status_code==409

def test_viewer_cannot_write(api_client,training_id):
 api=api_client
 r=api.req('viewer','POST',f'/trainings/{training_id}/audit-batches',
           json={'threshold':0.5,'seed':3106,'strata':STRATA})
 assert r.status_code==403

def test_conclusion_and_progress_and_export(api_client,training_id):
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 detail=api.req('analyst','GET',f'/audit-batches/{bid}').json()
 for s in detail['samples']:
  r=api.req('analyst','PUT',f"/audit-batches/{bid}/samples/{s['id']}",
            json={'decision':'flagged' if s['stratum']=='S3' else 'valid','note':'已核对票据与行程'})
  assert r.status_code==200,r.text
 progress=api.req('analyst','GET',f'/audit-batches/{bid}').json()
 assert progress['current']['result']['estimable'] is True
 assert progress['current']['result']['pending_n']==0
 assert progress['current']['result']['anomaly_rate']>0
 csv=api.req('analyst','GET',f'/audit-batches/{bid}/export?scope=samples')
 assert csv.status_code==200 and 'text/csv' in csv.headers['content-type']
 text=csv.content.decode('utf-8-sig')
 lines=text.strip().splitlines()
 assert len(lines)==9 and '入选概率' in lines[0] and '抽样依据' in lines[0]
 assert str(progress['samples'][0]['include_prob']) in text or '0.5' in text
 hist=api.req('analyst','GET',f'/audit-batches/{bid}/export?scope=history')
 assert hist.status_code==200
 payload=json.loads(hist.content)
 assert len(payload['results'])==9  # 创建快照 + 8 次结论变更
 assert [x['revision'] for x in payload['results']]==list(range(1,10))
 assert payload['results'][0]['result']['estimable'] is False

def test_export_published_requires_close(api_client,training_id):
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 assert api.req('analyst','GET',f'/audit-batches/{bid}/export?scope=published').status_code==409

def test_close_requires_admin_and_publishes(api_client,training_id):
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 detail=api.req('analyst','GET',f'/audit-batches/{bid}').json()
 for s in detail['samples']:
  api.req('analyst','PUT',f"/audit-batches/{bid}/samples/{s['id']}",
          json={'decision':'valid','note':'已核对票据与行程'})
 assert api.req('analyst','POST',f'/audit-batches/{bid}/close').status_code==403
 r=api.req('admin','POST',f'/audit-batches/{bid}/close')
 assert r.status_code==200,r.text
 body=r.json()
 assert body['batch']['state']=='closed' and body['result']['estimable']
 assert body['batch']['closed_revision']==body['result']['revision']
 pub=api.req('viewer','GET',f'/audit-batches/{bid}/export?scope=published')
 assert pub.status_code==200
 assert json.loads(pub.content)['revision']==body['result']['revision']
 # 结案后不可改
 sid=detail['samples'][0]['id']
 assert api.req('analyst','PUT',f'/audit-batches/{bid}/samples/{sid}',
                json={'decision':'flagged','note':'事后更正尝试'}).status_code==409

def test_decision_validation(api_client,training_id):
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 sid=api.req('analyst','GET',f'/audit-batches/{bid}').json()['samples'][0]['id']
 assert api.req('analyst','PUT',f'/audit-batches/{bid}/samples/{sid}',
                json={'decision':'maybe','note':'长理由文本'}).status_code==422
 assert api.req('analyst','PUT',f'/audit-batches/{bid}/samples/{sid}',
                json={'decision':'valid','note':'短'}).status_code==422
 assert api.req('analyst','PUT',f'/audit-batches/{bid}/samples/99999',
                json={'decision':'valid','note':'已核对票据与行程'}).status_code==404

def test_recompute_idempotent_over_http(api_client,training_id):
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 a=api.req('analyst','POST',f'/audit-batches/{bid}/recompute').json()
 b=api.req('analyst','POST',f'/audit-batches/{bid}/recompute').json()
 assert a['result']['id']==b['result']['id'] and b['created'] is False

def test_failed_close_then_retry_keeps_original_draw(api_client,training_id,monkeypatch):
 """结案失败后重试：仍沿用原抽样，不重新抽签。"""
 api=api_client
 bid=create(api,training_id).json()['batch']['id']
 original=[s['claim_id'] for s in api.req('analyst','GET',f'/audit-batches/{bid}').json()['samples']]
 from src.services import audit as svc
 real=svc.close_batch
 calls={'n':0}
 def boom(*a,**k):
  calls['n']+=1
  if calls['n']==1:raise RuntimeError('模拟结案写入失败')
  return real(*a,**k)
 with monkeypatch.context() as m:
  m.setattr('src.routes.api.audit_svc.close_batch',boom)
  r=api.req('admin','POST',f'/audit-batches/{bid}/close')
 assert r.status_code==500
 retry=api.req('admin','POST',f'/audit-batches/{bid}/close')
 assert retry.status_code==200
 after=[s['claim_id'] for s in api.req('analyst','GET',f'/audit-batches/{bid}').json()['samples']]
 assert after==original


def test_after_close_same_config_creates_new_batch_with_same_draw(api_client,training_id):
 """结案后同配置再发起：新批次重抽但 seed 决定，抽样集合一致。"""
 api=api_client
 first=create(api,training_id).json()['batch']
 d=api.req('analyst','GET',f"/audit-batches/{first['id']}").json()
 for s in d['samples']:
  api.req('analyst','PUT',f"/audit-batches/{first['id']}/samples/{s['id']}",
          json={'decision':'valid','note':'已核对票据与行程'})
 api.req('admin','POST',f"/audit-batches/{first['id']}/close")
 second=create(api,training_id)
 assert second.status_code==200
 b2=second.json()
 assert not b2['reused'] and b2['batch']['id']!=first['id']
 d2=api.req('analyst','GET',f"/audit-batches/{b2['batch']['id']}").json()
 assert sorted(s['claim_id'] for s in d2['samples'])==sorted(s['claim_id'] for s in d['samples'])
 assert d2['current']['result']['estimable'] is False

def test_real_seed_universe_binds_pending_only(api_client,db_session):
 """抽检总体必须是训练版本冻结的待标注全集，不含已标注样本。"""
 from src.models.entities import Claim
 for i in range(1,6):
  db_session.add(Claim(code=f'L{i}',amount=100+i,distance=10,holiday=False,label=0,note=''))
 labelled_ids=[c.id for c in db_session.query(Claim).all()]
 db_session.commit()
 # 训练快照只含未标注票
 universe=[{'id':900+i,'code':f'U{i}','score':sc} for i,sc in zip(range(1,5),[0.1,0.3,0.7,0.9])]
 t=Training(name='tight',threshold=0.5,state='done',
            result={'pending':universe,'checksum':'c2'})
 db_session.add(t);db_session.commit()
 body={'threshold':0.5,'seed':7,'strata':[{'lo':0,'hi':0.5,'sample_n':2},{'lo':0.5,'hi':1,'sample_n':2}]}
 r=api_client.req('analyst','POST',f'/trainings/{t.id}/audit-batches',json=body)
 assert r.status_code==200,r.text
 b=r.json()['batch']
 assert b['population_n']==4 and b['sampled_n']==4  # 每层 2 笔，合计全查 4 笔
 d=api_client.req('analyst','GET',f"/audit-batches/{b['id']}").json()
 assert {s['claim_id'] for s in d['samples']}=={901,902,903,904}
 assert all(s['include_prob']==1.0 for s in d['samples'])  # 层内全查，入选概率为 1
 assert not ({s['claim_id'] for s in d['samples']} & set(labelled_ids))
