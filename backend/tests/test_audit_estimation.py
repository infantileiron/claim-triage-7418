"""可枚举小总体核对校正估计：用手算的总体真值验证 HT 估计。"""
import math
import pytest
from src.services import audit as svc

# 小总体：3 层，每层 4 笔；异常真值已知
POP=[
 {'claim_id':1,'code':'A1','stratum':'S1','score':0.05,'truth':0},
 {'claim_id':2,'code':'A2','stratum':'S1','score':0.10,'truth':1},
 {'claim_id':3,'code':'A3','stratum':'S1','score':0.15,'truth':0},
 {'claim_id':4,'code':'A4','stratum':'S1','score':0.19,'truth':0},
 {'claim_id':5,'code':'B1','stratum':'S2','score':0.25,'truth':1},
 {'claim_id':6,'code':'B2','stratum':'S2','score':0.35,'truth':1},
 {'claim_id':7,'code':'B3','stratum':'S2','score':0.45,'truth':0},
 {'claim_id':8,'code':'B4','stratum':'S2','score':0.55,'truth':0},
 {'claim_id':9,'code':'C1','stratum':'S3','score':0.65,'truth':1},
 {'claim_id':10,'code':'C2','stratum':'S3','score':0.75,'truth':1},
 {'claim_id':11,'code':'C3','stratum':'S3','score':0.85,'truth':0},
 {'claim_id':12,'code':'C4','stratum':'S3','score':0.95,'truth':1},
]
STRATA=[{'name':'S1','lo':0.0,'hi':0.2,'N':4,'n':2,'include_prob':0.5},
        {'name':'S2','lo':0.2,'hi':0.6,'N':4,'n':4,'include_prob':1.0},
        {'name':'S3','lo':0.6,'hi':1.0,'N':4,'n':1,'include_prob':0.25}]

class S:
 def __init__(self,i,stratum,score,decision):self.id=i;self.claim_id=i;self.stratum=stratum;self.score=score;self.decision=decision;self.note='x';self.claim_code='c%d'%i

def samples(chosen):
 return [S(i,s['stratum'],s['score'],'flagged' if s['truth'] else 'valid') for i,s in enumerate(POP,1) if i in chosen]

def test_census_all_strata_recovers_population_truth():
 chosen={i for i in range(1,13)}
 r=svc.compute_estimate(STRATA,samples(chosen),0.5)
 assert r['estimable'] and r['blockers']==[]
 m=r['matrix']
 # 阈值 .5：tp={9,10,12}=3, fp={8,11}=2, fn={2,5,6}=3, tn={1,3,4,7}=4
 assert (m['tp'],m['fp'],m['fn'],m['tn'])==(3,2,3,4)
 assert r['anomaly_rate']==pytest.approx(6/12)
 assert r['model_metrics']['precision']==pytest.approx(3/5)
 assert r['model_metrics']['recall']==pytest.approx(3/6)

def test_stratified_weighting_with_partial_sample():
 # S1 抽 {1,2}: 权重 2 -> tn+=2(1号), fn+=2(2号)
 # S2 全查: 5,6 fn；7 tn；8 fp (权重1)
 # S3 抽 {10}: 权重 4 -> 10 是 flagged 且 score>=.5 => tp +=4
 chosen={1,2,5,6,7,8,10}
 r=svc.compute_estimate(STRATA,samples(chosen),0.5)
 m=r['matrix']
 assert m['tp']==pytest.approx(4) and m['fn']==pytest.approx(4)
 assert m['fp']==pytest.approx(1) and m['tn']==pytest.approx(3)
 assert r['anomaly_rate']==pytest.approx(8/12)
 assert r['population_n']==12 and r['sampled_n']==7 and r['decided_n']==7

def test_pending_blocks_estimate_not_counted_as_normal():
 chosen={1,2,5,6,7,8,10}
 ss=samples(chosen)
 next(x for x in ss if x.claim_id==2).decision='pending'
 r=svc.compute_estimate(STRATA,ss,0.5)
 assert not r['estimable'] and r['matrix'] is None and r['anomaly_rate'] is None
 assert any(b['stratum']=='S1' and b['reason']=='incomplete' and b['pending_n']==1 for b in r['blockers'])
 assert '未结案' in r['warning']

def test_unsampled_stratum_blocks_estimate():
 chosen={5,6,7,8,10}
 r=svc.compute_estimate(STRATA,samples(chosen),0.5)
 assert not r['estimable']
 assert any(b['stratum']=='S1' and b['reason']=='unsampled' for b in r['blockers'])
 assert r['strata'][0]['estimable'] is False and r['strata'][1]['estimable'] is True

def test_standard_error_present_for_partial_sampling():
 chosen={1,3,5,6,7,8,9,11}
 r=svc.compute_estimate(STRATA,samples(chosen),0.5)
 # S2 全查方差 0, S1/S3 n=2 提供方差
 assert r['estimable'] and r['anomaly_rate_se'] is not None and r['anomaly_rate_se']>0
 assert math.isfinite(r['anomaly_rate_se'])

def test_strata_validation():
 with pytest.raises(ValueError):svc.normalize_strata([{'lo':0,'hi':0.5,'sample_n':1}],2)
 with pytest.raises(ValueError):svc.normalize_strata([{'lo':0,'hi':0.4,'sample_n':1},{'lo':0.5,'hi':1,'sample_n':1}],2)
 with pytest.raises(ValueError):svc.normalize_strata([{'lo':0,'hi':1,'sample_n':-1}],2)
 ok=svc.normalize_strata(None,4)
 assert len(ok)==5 and all(x['sample_n']==4 for x in ok)
 assert [x['name'] for x in ok]==['S1','S2','S3','S4','S5']

def test_build_frame_probability_and_determinism():
 pending=[{'id':i,'code':'P%d'%i,'score':s} for i,s in
          enumerate([0.05,0.1,0.15,0.19,0.25,0.3,0.45,0.5,0.65,0.8,0.9,0.99],1)]
 strata=svc.normalize_strata(None,2)
 pop,a,draws=svc.build_frame(pending,strata,3106,'sum')
 pop2,b,draws2=svc.build_frame(pending,strata,3106,'sum')
 assert draws==draws2
 assert sum(len(x['population_ids']) for x in a)==12
 s1=[x for x in a if x['name']=='S1'][0]
 assert s1['N']==4 and s1['n']==2 and s1['include_prob']==pytest.approx(0.5)
 for d in draws:
  cfg=[x for x in a if x['name']==d['stratum']][0]
  assert d['include_prob']==pytest.approx(cfg['n']/cfg['N'])
  assert d['basis']['population_n']==cfg['N'] and d['basis']['sample_n']==cfg['n']
  assert d['basis']['seed']==3106 and d['basis']['method']=='SRSWOR'

def test_build_frame_caps_sample_at_population():
 pending=[{'id':i,'code':'P%d'%i,'score':0.1} for i in range(1,4)]
 strata=svc.normalize_strata([{'lo':0,'hi':1,'sample_n':10}],0)
 pop,a,draws=svc.build_frame(pending,strata,1,'sum')
 assert a[0]['N']==3 and a[0]['n']==3 and a[0]['census'] and a[0]['cap_note']
 assert len(draws)==3 and all(d['include_prob']==1.0 for d in draws)
