"""用可枚举小总体核对校正估计：设计权重、覆盖真值、空抽与未结案的处理。"""
import itertools,math,random
import pytest
from src.services import audit as svc

# 总体：2 层各 5 笔，异常真值已知。S1 低分段、S2 高分段。
POP={
 'S1':[(11,0.02,0),(12,0.06,1),(13,0.10,0),(14,0.14,0),(15,0.18,0)],
 'S2':[(21,0.55,1),(22,0.62,1),(23,0.70,0),(24,0.85,1),(25,0.95,1)],
}
THRESHOLD=0.5
TRUE_TP=4;TRUE_FP=1;TRUE_FN=1;TRUE_TN=4   # 阈值 .5：S2 的 0.55/0.62/0.85/0.95 判异常，其中 0.70 实为正常
TRUE_ANOMALY=5;TRUE_RATE=5/10

def strata(n1,n2):
 N1=len(POP['S1']);N2=len(POP['S2'])
 return [{'name':'S1','lo':0,'hi':0.5,'N':N1,'n':n1,'include_prob':n1/N1},
         {'name':'S2','lo':0.5,'hi':1,'N':N2,'n':n2,'include_prob':n2/N2}]

class Row:
 def __init__(self,i,s,score,truth,decision=None):
  self.id=i;self.claim_id=i;self.stratum=s;self.score=score
  self.decision=decision if decision is not None else ('flagged' if truth else 'valid')
  self.note='e';self.claim_code='C%d'%i

def make(spec_strata,skip=()):
 rows=[]
 for s,items in POP.items():
  for i,sc,t in items:
   if i in skip:continue
   rows.append(Row(i,s,sc,t))
 return rows

def test_full_census_recovers_exact_truth():
 r=svc.compute_estimate(strata(5,5),make(POP),THRESHOLD)
 assert r['estimable']
 m=r['matrix']
 assert (m['tp'],m['fp'],m['fn'],m['tn'])==(TRUE_TP,TRUE_FP,TRUE_FN,TRUE_TN)
 assert r['anomaly_rate']==pytest.approx(TRUE_RATE)
 assert r['anomaly_rate_se']==pytest.approx(0)  # 全查无抽样误差

@pytest.mark.parametrize('picked',[
 (1,2),(2,3),(1,3),(3,3),
])
def test_partial_draw_weighting_is_exact_insurance(picked):
 """任意抽取组合：加权矩阵都是无偏设计的确定性结果，逐项核对权重。"""
 n1,n2=picked
 cfg=strata(n1,n2)
 rows=[]
 for s,items in POP.items():
  take=items[:{'S1':n1,'S2':n2}[s]]
  for i,sc,t in take:rows.append(Row(i,s,sc,t))
 r=svc.compute_estimate(cfg,rows,THRESHOLD)
 assert r['estimable']
 w1=5/n1;w2=5/n2
 exp=[0.0]*4
 for s,items in POP.items():
  take=items[:{'S1':n1,'S2':n2}[s]]
  w=w1 if s=='S1' else w2
  for i,sc,t in take:
   p=sc>=THRESHOLD
   k=0 if (p and t) else 1 if p else 2 if t else 3
   exp[k]+=w
 m=r['matrix']
 assert [m['tp'],m['fp'],m['fn'],m['tn']]==pytest.approx(exp)
 assert r['anomaly_rate']==pytest.approx((exp[0]+exp[2])/10)

def test_unsampled_stratum_blocks_without_faking_zero():
 cfg=strata(0,5)
 rows=[Row(i,s,sc,t) for s,items in POP.items() for i,sc,t in (items if s=='S2' else [])]
 r=svc.compute_estimate(cfg,rows,THRESHOLD)
 assert not r['estimable'] and r['matrix'] is None and r['anomaly_rate'] is None
 assert r['blockers']==[{'stratum':'S1','reason':'unsampled','population_n':5,'sample_n':0,'pending_n':0}]
 assert 'S1' in r['warning']

def test_pending_in_any_stratum_blocks_and_is_not_normal():
 cfg=strata(5,5)
 rows=make(POP)
 rows[0].decision='pending'  # S1 一笔未结案（真值正常）
 r=svc.compute_estimate(cfg,rows,THRESHOLD)
 assert not r['estimable'] and r['matrix'] is None and r['anomaly_rate'] is None
 b=[x for x in r['blockers'] if x['stratum']=='S1'][0]
 assert b['reason']=='incomplete' and b['pending_n']==1
 assert r['pending_n']==1 and r['decided_n']==9

def test_correction_changes_estimate_consistently():
 cfg=strata(5,5);rows=make(POP)
 base=svc.compute_estimate(cfg,rows,THRESHOLD)
 rows[0].decision='flagged'  # 11 号由正常改为异常
 after=svc.compute_estimate(cfg,rows,THRESHOLD)
 assert after['anomaly_rate']==pytest.approx(base['anomaly_rate']+0.1)
 assert after['matrix']['tn']==base['matrix']['tn']-1
 assert after['matrix']['fn']==base['matrix']['fn']+1

def test_enumeration_over_all_draw_combinations_bounds_truth():
 """枚举若干层内抽样组合，HT 估计在设计上无偏/逐次可核对。"""
 cfg=[{'name':'S1','lo':0,'hi':0.5,'N':5,'n':2,'include_prob':0.4},
      {'name':'S2','lo':0.5,'hi':1,'N':5,'n':2,'include_prob':0.4}]
 combos=list(itertools.combinations(POP['S1'],2));combos2=list(itertools.combinations(POP['S2'],2))
 rates=[]
 for a in combos:
  for b in combos2:
   rows=[Row(i,'S1',sc,t) for i,sc,t in a]+[Row(i,'S2',sc,t) for i,sc,t in b]
   r=svc.compute_estimate(cfg,rows,THRESHOLD)
   assert r['estimable']
   rates.append(r['anomaly_rate'])
 # 全部组合的均值等于真值（分层 HT 的无偏性，对固定分层规模成立）
 assert sum(rates)/len(rates)==pytest.approx(TRUE_RATE)
 assert min(rates)<=TRUE_RATE<=max(rates)
