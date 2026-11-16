"""分层随机抽检与入选概率校正估计。

设计要点：
- 抽样框固定为某一训练版本冻结的“待标注全集”（training.result['pending']）。
- 按模型概率分层做分层简单随机无放回抽样（SRSWOR），每层抽取量可配置。
- 每个入选样本保留入选概率 pi_h = n_h / N_h 与抽样依据（层、N_h、n_h、seed、抽签序号）。
- 人工结论只取 valid/flagged；pending 永远不当作正常。
- 每次人工结论变更都追加一个不可变结果修订（A→B→A 也追加），当前结果 = 最新修订；
  同一结论的重试/重复重算按结论摘要复用，结果按 (batch_id, revision) 唯一，不会重复累计。
- 结案在行锁内基于当前结论计算结果并只发布该结果；结案后结论不可再改（409），
  因此并发重算不会改变已发布结果，发布结果必然对应当前这一批结论。
"""
import hashlib,json,math,random,threading
from datetime import datetime,timezone
from sqlalchemy.exc import IntegrityError
from src.models.entities import AuditBatch,AuditResult,AuditSample

DEFAULT_BANDS=[(0.0,0.2),(0.2,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.0)]
DEFAULT_SAMPLE_N=3
DECISION_TO_LABEL={'valid':0,'flagged':1}
METHOD='SRSWOR'

_LOCK_REGISTRY={}
_REGISTRY_GUARD=threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def batch_lock(key):
    """按批次（或创建配置）串行化；跨进程由数据库行锁兜底。"""
    with _REGISTRY_GUARD:
        return _LOCK_REGISTRY.setdefault(str(key),threading.RLock())


def _seed_int(*parts):
    return int(hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:16],16)


def _sha(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def normalize_strata(strata,default_n):
    """校验并规范化分层配置：区间连续、覆盖 [0,1]、层内抽取量非负。"""
    if not strata:
        spec=[{'lo':lo,'hi':hi,'sample_n':default_n} for lo,hi in DEFAULT_BANDS]
    else:
        spec=[]
        for item in strata:
            lo=float(item['lo']);hi=float(item['hi']);n=int(item['sample_n'])
            if not (0<=lo<=1 and 0<hi<=1 and lo<hi):
                raise ValueError('分层区间必须满足 0 ≤ lo < hi ≤ 1')
            if n<0:
                raise ValueError('分层抽取数量不能为负数')
            spec.append({'lo':lo,'hi':hi,'sample_n':n})
        spec.sort(key=lambda x:x['lo'])
        if abs(spec[0]['lo'])>1e-9 or abs(spec[-1]['hi']-1)>1e-9:
            raise ValueError('分层区间必须覆盖 0 到 1')
        for a,b in zip(spec,spec[1:]):
            if abs(a['hi']-b['lo'])>1e-9:
                raise ValueError('分层区间必须连续且不重叠')
    return [{'name':f'S{i+1}','lo':x['lo'],'hi':x['hi'],'sample_n':x['sample_n']} for i,x in enumerate(spec)]


def _band_index(score,strata):
    for i,band in enumerate(strata):
        last=i==len(strata)-1
        if band['lo']<=score<(band['hi'] if not last else band['hi']+1e-12):
            return i
    raise ValueError(f'评分 {score} 不在任何分层区间内')


def build_frame(pending,strata,seed,training_checksum):
    """按分层抽签，返回 (population, strata_cfg, draws)。抽样结果完全由 seed 决定。"""
    population=[]
    buckets=[[] for _ in strata]
    for row in pending:
        score=float(row['score'])
        idx=_band_index(score,strata)
        buckets[idx].append({'claim_id':int(row['id']),'code':row['code'],'score':score,'stratum':strata[idx]['name']})
    strata_cfg=[];draws=[]
    for idx,band in enumerate(strata):
        units=sorted(buckets[idx],key=lambda x:x['claim_id'])
        n_pop=len(units);n_take=min(band['sample_n'],n_pop)
        include_prob=(n_take/n_pop) if n_pop else 0.0
        rng=random.Random(_seed_int(seed,training_checksum,band['name']))
        picked=rng.sample(units,n_take) if n_take else []
        strata_cfg.append({
            'name':band['name'],'lo':band['lo'],'hi':band['hi'],'N':n_pop,'n':n_take,
            'sample_n_requested':band['sample_n'],'include_prob':include_prob,
            'method':METHOD,'seed':seed,'draw_order':[u['claim_id'] for u in picked],
            'population_ids':[u['claim_id'] for u in units],
            'census':bool(n_pop and n_take==n_pop),
            'cap_note':('请求抽取 %d 笔，超过层内总体 %d 笔，按全查处理'%(band['sample_n'],n_pop)) if band['sample_n']>n_pop else '',
        })
        population.extend(units)
        for draw_index,u in enumerate(picked):
            draws.append({
                'claim_id':u['claim_id'],'claim_code':u['code'],'stratum':band['name'],
                'score':u['score'],'include_prob':include_prob,
                'basis':{
                    'method':METHOD,'stratum':band['name'],'range':[band['lo'],band['hi']],
                    'population_n':n_pop,'sample_n':n_take,'include_prob':include_prob,
                    'selection_weight':(n_pop/n_take) if n_take else None,
                    'seed':seed,'draw_index':draw_index,
                    'note':'分层简单随机无放回；入选概率 = n_h / N_h',
                },
            })
    return population,strata_cfg,draws


def config_signature(threshold,seed,strata):
    def take_n(x):
        return int(x.get('sample_n',x.get('sample_n_requested',0)))
    return _sha({'threshold':round(float(threshold),6),'seed':int(seed),
                 'strata':[[round(x['lo'],6),round(x['hi'],6),take_n(x)] for x in strata]})


def open_key(training_id,signature):
    return '%s:%s'%(training_id,signature)


def find_reusable_batch(s,training_id,signature):
    for batch in s.query(AuditBatch).filter_by(training_id=training_id,state='open').order_by(AuditBatch.id.desc()).all():
        if _batch_signature(batch)==signature:
            return batch
    return None


def _batch_signature(batch):
    strata=[{'lo':x['lo'],'hi':x['hi'],'sample_n':x['sample_n_requested']} for x in (batch.strata or [])]
    return config_signature(batch.threshold,batch.seed,strata)


def create_batch(s,training,threshold,seed,strata_spec,actor):
    pending=(training.result or {}).get('pending') or []
    if not pending:
        raise ValueError('该训练版本没有待标注样本，无法绑定抽检总体')
    training_checksum=(training.result or {}).get('checksum','')
    population,strata_cfg,draws=build_frame(pending,strata_spec,seed,training_checksum)
    batch=AuditBatch(
        training_id=training.id,threshold=threshold,seed=seed,
        strata=strata_cfg,population=population,population_n=len(population),sampled_n=len(draws),
        state='open',revision=0,checksum=_sha({'population':population,'strata':strata_cfg}),
        training_checksum=training_checksum,created_by=actor,created_at=now(),
        open_key=open_key(training.id,config_signature(threshold,seed,strata_cfg)),
    )
    s.add(batch);s.flush()
    created=[]
    for item in draws:
        sample=AuditSample(
            batch_id=batch.id,claim_id=item['claim_id'],claim_code=item['claim_code'],
            stratum=item['stratum'],score=item['score'],include_prob=item['include_prob'],
            basis=item['basis'],decision='pending',note='',created_at=now(),updated_at=now(),
        )
        s.add(sample);created.append(sample)
    s.flush()
    # 建立修订 1：全部未结案，立即能看到“无法估计”的明确提示
    ensure_result(s,batch,created,revision=1)
    s.flush()
    return batch


def conclusions_digest(samples):
    rows=sorted(((x.id,x.claim_id,x.decision,x.note or '') for x in samples))
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()


def compute_estimate(strata_cfg,samples,threshold):
    """按入选概率（Horvitz-Thompson）校正总体混淆矩阵与异常率。

    pending 或空抽分层会阻止总体估计，并逐个分层给出原因；不会把未结案当成正常。
    """
    by_stratum={}
    for sample in samples:
        by_stratum.setdefault(sample.stratum,[]).append(sample)
    tp=fp=fn=tn=0.0
    raw={'tp':0,'fp':0,'fn':0,'tn':0}
    stratum_rows=[];blockers=[];total_n=0;se_ok=True;variance=0.0;decided_total=0
    for cfg in strata_cfg:
        name=cfg['name'];N=int(cfg['N']);rows=by_stratum.get(name,[]);n=len(rows)
        decided=[r for r in rows if r.decision in DECISION_TO_LABEL]
        pending=[r for r in rows if r.decision not in DECISION_TO_LABEL]
        status='complete'
        if N==0:status='empty_population'
        elif n==0:status='unsampled'
        elif pending:status='incomplete'
        if status in ('unsampled','incomplete'):
            blockers.append({'stratum':name,'reason':status,'population_n':N,'sample_n':n,'pending_n':len(pending)})
        total_n+=N
        weight=(N/n) if n else 0.0
        anomaly=0;normal=0
        for r in decided:
            actual=DECISION_TO_LABEL[r.decision]
            predicted=r.score>=threshold
            if predicted and actual==1:tp+=weight;raw['tp']+=1
            elif predicted and actual==0:fp+=weight;raw['fp']+=1
            elif (not predicted) and actual==1:fn+=weight;raw['fn']+=1
            else:tn+=weight;raw['tn']+=1
            if actual==1:anomaly+=1
            else:normal+=1
        decided_total+=len(decided)
        complete=status=='complete'
        if N==0:
            # 总体为空的分层不贡献权重，也不妨碍总体估计
            estimable_stratum=True
        elif complete:
            estimable_stratum=True
            p=anomaly/n if n else 0
            if n<N:
                if n>=2:
                    s2=p*(1-p)*n/(n-1)
                    variance+=N*N*(1-n/N)*s2/n
                else:
                    se_ok=False
        else:
            estimable_stratum=False
        stratum_rows.append({
            'name':name,'lo':cfg['lo'],'hi':cfg['hi'],'population_n':N,'sample_n':n,
            'decided_n':len(decided),'pending_n':len(pending),'include_prob':cfg['include_prob'],
            'method':METHOD,'status':status,
            'observed_anomaly':anomaly,'observed_normal':normal,
            'estimated_anomaly':(anomaly*weight) if estimable_stratum and N>0 else None,
            'estimated_normal':(normal*weight) if estimable_stratum and N>0 else None,
            'estimable':estimable_stratum,
        })
    estimable=bool(total_n>0 and not blockers)
    result={
        'threshold':threshold,'population_n':total_n,
        'sampled_n':len(samples),'decided_n':decided_total,'pending_n':len(samples)-decided_total,
        'estimable':estimable,'blockers':blockers,'strata':stratum_rows,
        'observed_matrix':raw,
    }
    if estimable:
        anomaly=tp+fn;normal=fp+tn
        result['matrix']={'tp':tp,'fp':fp,'fn':fn,'tn':tn}
        result['anomaly_rate']=anomaly/total_n
        result['normal_rate']=normal/total_n
        result['anomaly_count']=anomaly
        if se_ok:
            se=math.sqrt(variance);result['anomaly_rate_se']=se/total_n;result['anomaly_count_se']=se
        else:
            result['anomaly_rate_se']=None;result['anomaly_count_se']=None
        precision=tp/(tp+fp) if tp+fp else None
        recall=tp/(tp+fn) if tp+fn else None
        result['model_metrics']={
            'precision':precision,'recall':recall,
            'f1':(2*precision*recall/(precision+recall)) if precision is not None and recall is not None and (precision+recall) else None,
        }
        note=('按分层入选概率加权校正；待标注总体共 %d 笔。'
              '估计值只代表当前训练版本冻结的待标注全集，未结案样本不计入。'%total_n)
        if not se_ok:note+=' 部分分层仅抽到 1 笔且未全查，无法给出标准误（点估计仍有效）。'
        else:note+=' 标准误仅反映抽样设计，不含人工判断误差。'
        result['warning']=note
    else:
        result['matrix']=None;result['anomaly_rate']=None;result['normal_rate']=None
        result['anomaly_count']=None;result['anomaly_rate_se']=None;result['anomaly_count_se']=None
        result['model_metrics']={'precision':None,'recall':None,'f1':None}
        reasons=[]
        if not total_n:reasons.append('总体为空')
        for blocker in blockers:
            if blocker['reason']=='unsampled':reasons.append('%s 层未抽到样本（%d 笔）'%(blocker['stratum'],blocker['population_n']))
            elif blocker['reason']=='incomplete':reasons.append('%s 层仍有 %d 笔未结案'%(blocker['stratum'],blocker['pending_n']))
        result['warning']='无法估计总体：'+'；'.join(reasons)+'。未结案样本不被视为正常。'
    return result


def _snapshot(batch,samples):
    return [{'sample_id':x.id,'claim_id':x.claim_id,'code':x.claim_code,'stratum':x.stratum,
             'score':x.score,'include_prob':x.include_prob,'decision':x.decision,'note':x.note or ''}
            for x in sorted(samples,key=lambda r:r.id)]


def _next_revision(s,batch_id):
    highest=s.query(AuditResult.revision).filter_by(batch_id=batch_id).order_by(AuditResult.revision.desc()).first()
    return (highest[0]+1) if highest else 1


def ensure_result(s,batch,samples,revision=None,reuse_matching=True):
    """按当前结论集合生成（或复用）结果修订。

    - 每个不同结论集合对应一条不可变结果，保留其完整抽样依据；
    - reuse_matching=True（重算/重试）时，同一结论复用最近一条结果，不重复累计；
    - reuse_matching=False（人工结论更正）时，即使改回旧结论也追加新修订，
      使更正动作本身可追溯，且“当前结果”始终是最新修订。
    """
    digest=conclusions_digest(samples)
    if reuse_matching:
        existing=(s.query(AuditResult).filter_by(batch_id=batch.id,conclusions_digest=digest)
                  .order_by(AuditResult.revision.desc()).first())
        if existing:
            batch.revision=max(batch.revision,existing.revision)
            return existing,False
    rev=revision if revision is not None else _next_revision(s,batch.id)
    payload={**compute_estimate(batch.strata,samples,batch.threshold),'batch_id':batch.id,'revision':rev,
             'conclusions_digest':digest,'generated_at':now(),
             'sampling':{'strata':batch.strata,'seed':batch.seed,'method':METHOD},
             'conclusions':_snapshot(batch,samples)}
    row=AuditResult(batch_id=batch.id,revision=rev,estimable=payload['estimable'],
                    result=payload,conclusions_digest=digest,created_at=now())
    try:
        with s.begin_nested():
            s.add(row);s.flush()
    except IntegrityError:
        # 并发重算竞争：另一线程已写入该修订，读取并复用
        row=s.query(AuditResult).filter_by(batch_id=batch.id,revision=rev).first()
        if row is None:raise
        batch.revision=max(batch.revision,row.revision)
        return row,False
    batch.revision=max(batch.revision,rev)
    return row,True


def recompute(s,batch_id):
    with batch_lock(batch_id):
        batch=s.query(AuditBatch).filter_by(id=batch_id).with_for_update().first()
        if not batch:raise LookupError('抽检批次不存在')
        samples=s.query(AuditSample).filter_by(batch_id=batch_id).order_by(AuditSample.id).all()
        row,created=ensure_result(s,batch,samples)
        s.commit()
    return row,created


def set_decision(s,batch_id,sample_id,decision,note):
    with batch_lock(batch_id):
        batch=s.query(AuditBatch).filter_by(id=batch_id).with_for_update().first()
        if not batch:raise LookupError('抽检批次不存在')
        sample=s.get(AuditSample,sample_id)
        if not sample or sample.batch_id!=batch_id:raise LookupError('抽检样本不存在')
        if batch.state!='open':
            raise PermissionError('抽检批次已结案，结论不可再改；如需更正请发起新批次')
        if sample.decision==decision and (sample.note or '')==note:
            samples=s.query(AuditSample).filter_by(batch_id=batch_id).order_by(AuditSample.id).all()
            row=s.query(AuditResult).filter_by(batch_id=batch_id,conclusions_digest=conclusions_digest(samples)).first()
            return sample,row,False
        sample.decision=decision;sample.note=note;sample.updated_at=now()
        batch.last_error=''
        samples=s.query(AuditSample).filter_by(batch_id=batch_id).order_by(AuditSample.id).all()
        row,created=ensure_result(s,batch,samples,reuse_matching=False)
        s.commit()
    return sample,row,created


def close_batch(s,batch_id):
    with batch_lock(batch_id):
        batch=s.query(AuditBatch).filter_by(id=batch_id).with_for_update().first()
        if not batch:raise LookupError('抽检批次不存在')
        if batch.state=='closed':
            row=s.get(AuditResult,batch.published_result_id)
            return batch,row,False
        samples=s.query(AuditSample).filter_by(batch_id=batch_id).order_by(AuditSample.id).all()
        row,created=ensure_result(s,batch,samples)
        batch.state='closed';batch.closed_revision=batch.revision;batch.published_result_id=row.id
        batch.open_key=None
        s.commit()
    return batch,row,True


def serialize_batch(batch):
    return {
        'id':batch.id,'training_id':batch.training_id,'threshold':batch.threshold,'seed':batch.seed,
        'state':batch.state,'revision':batch.revision,'closed_revision':batch.closed_revision,
        'published_result_id':batch.published_result_id,'population_n':batch.population_n,
        'sampled_n':batch.sampled_n,'checksum':batch.checksum,'training_checksum':batch.training_checksum,
        'last_error':batch.last_error or '','created_by':batch.created_by,'created_at':batch.created_at,
        'strata':[{k:v for k,v in x.items() if k!='population_ids'} for x in (batch.strata or [])],
    }


def serialize_sample(sample):
    return {'id':sample.id,'batch_id':sample.batch_id,'claim_id':sample.claim_id,'code':sample.claim_code,
            'stratum':sample.stratum,'score':sample.score,'include_prob':sample.include_prob,
            'basis':sample.basis,'decision':sample.decision,'note':sample.note or '',
            'updated_at':sample.updated_at}
