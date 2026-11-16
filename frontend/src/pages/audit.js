import {state} from '../stores/session.js';
import {esc} from '../utils/html.js';
import {badge} from '../components/common/status.js';
import {table} from '../components/common/table.js';
import {field,select} from '../components/common/fields.js';

const pc=v=>v===null||v===undefined?'—':(100*v).toFixed(1)+'%';
const px=v=>v===null||v===undefined?'—':v.toFixed(2);
const DECISION_TEXT={pending:'未结案',valid:'核验正常',flagged:'需进一步核实'};
const STATUS_TEXT={open:'进行中',closed:'已结案'};
const BLOCK_TEXT={unsampled:'空抽（该层未抽到样本）',incomplete:'仍有未结案样本',empty_population:'该层总体为空'};

function estimatorPanel(view){
 if(!view)return '<p class="notice">尚未生成估计。</p>';
 const r=view.result;
 if(!r.estimable){
  const list=(r.blockers||[]).map(b=>`<li>${esc(b.stratum)} · ${esc(BLOCK_TEXT[b.reason]||b.reason)} · 总体 ${b.population_n} 笔 · 已抽 ${b.sample_n} 笔${b.reason==='incomplete'?` · 未结案 ${b.pending_n} 笔`:''}</li>`).join('');
  return `<p class="notice">无法估计总体：${esc(r.warning)}</p><ul class="blockers">${list||'<li>总体为空</li>'}</ul>`;
 }
 return `<div class="metrics"><div><strong>${pc(r.anomaly_rate)}</strong>校正异常率${r.anomaly_rate_se!=null?`<small>± ${pc(r.anomaly_rate_se)}</small>`:''}</div><div><strong>${px(r.anomaly_count)}</strong>估计异常笔数 / 总体 ${r.population_n}</div><div><strong>${pc(r.model_metrics.precision)}</strong>校正 Precision</div><div><strong>${pc(r.model_metrics.recall)}</strong>校正 Recall</div></div>
 ${table(['校正混淆矩阵','预测正常','预测异常'],[['实际正常',px(r.matrix.tn),px(r.matrix.fp)],['实际异常',px(r.matrix.fn),px(r.matrix.tp)]])}
 <p class="notice">${esc(r.warning)}</p>`;
}

export function render(){
 const batches=state.auditBatches||[],detail=state.auditDetail;
 const doneTrainings=state.trainings.filter(t=>t.state==='done');
 const selectedBatch=detail?detail.batch:null;
 return `<div class="split"><section class="panel">
 <h2>分层随机抽检 · 校正总体估计</h2>
 <p class="notice">只看高分票据会漏估低分异常。这里先按模型概率分层，在训练版本冻结的<b>待标注全集</b>内分层随机抽取，每票保存入选概率与抽样依据；人工结论回来后按入选概率（Horvitz–Thompson）校正混淆矩阵与异常率。未结案或空抽的分层会明确提示无法估计，不会把未结案当正常。</p>
 ${selectedBatch?`<div class="result-head"><h2>批次 #${selectedBatch.id} · 训练 #${selectedBatch.training_id} ${badge(STATUS_TEXT[selectedBatch.state]||selectedBatch.state)}</h2><button id="audit-refresh">刷新进度</button></div>
  <p class="eyebrow">总体 ${selectedBatch.population_n} 笔 · 抽样 ${selectedBatch.sampled_n} 笔 · 阈值 ${selectedBatch.threshold} · seed ${selectedBatch.seed} · 修订 r${selectedBatch.revision}${selectedBatch.published_result_id?` · 已发布 r${selectedBatch.closed_revision}`:''}</p>
  <h3>当前进度</h3>
  ${table(['分层','概率区间','总体 N','抽取 n','入选概率','已结案','未结案','状态'],[...(detail.current?detail.current.result.strata:[]).map(s=>[s.name,`${s.lo}–${s.hi}`,s.population_n,s.sample_n,pc(s.include_prob),s.decided_n,s.pending_n,s.estimable?'可估计':'不可估计']),['合计',`${selectedBatch.population_n} 笔总体`,selectedBatch.population_n,selectedBatch.sampled_n,'—',detail.samples.filter(s=>s.decision!=='pending').length,detail.samples.filter(s=>s.decision==='pending').length,'']])}
  <h3>按入选概率校正的总体估计 · r${detail.current?.revision??'—'}</h3>
  ${detail.current_is_latest_conclusions===false?'<p class="notice warn">结论已变更但尚未重算；下方结果对应旧结论，请点击“手动重算”或重新保存结论。</p>':''}
  ${estimatorPanel(detail.current)}
  ${selectedBatch.published_result_id&&detail.published?`<h3>已发布（结案）结果 · r${detail.published.revision}</h3>${estimatorPanel(detail.published)}`:'<p class="notice">批次尚未结案，还没有已发布结果。结案后发布的结果与该时刻结论一一对应。</p>'}
  <h3>抽检样本与抽样依据</h3>
  ${table(['样本','报销编号','分层','模型概率','入选概率','抽样依据','人工结论'],[...detail.samples].sort((a,b)=>a.stratum.localeCompare(b.stratum)||a.id-b.id).map(s=>[`#${s.id} 样本#${s.claim_id}`,s.code,s.stratum,pc(s.score),pc(s.include_prob),`N=${s.basis.population_n} / n=${s.basis.sample_n} · seed ${s.basis.seed} · 第 ${s.basis.draw_index+1} 签`,DECISION_TEXT[s.decision]||s.decision]))}
  <p><button id="audit-export-csv">下载明细 CSV</button> <button id="audit-export-history">下载历史与依据 JSON</button>${selectedBatch.published_result_id?' <button id="audit-export-published">下载已发布结果</button>':''}</p>
  <h3>结果修订历史（旧结果与依据可回看）</h3>
  ${table(['修订','可估计','抽样数','已结案','未结案','异常率','生成时间',''],detail.history.map(h=>[`r${h.revision}`,h.result.estimable?'是':'否',h.result.sampled_n,h.result.decided_n,h.result.pending_n,h.result.estimable?pc(h.result.anomaly_rate):'无法估计',h.created_at,`<button class="link" data-audit-result="${h.id}">查看</button>`]))}
  ${state.auditResultView?`<h3>修订 r${state.auditResultView.revision} 的完整依据</h3><pre class="json">${esc(JSON.stringify(state.auditResultView.result,null,2))}</pre>`:''}`
 :'<p class="notice">从下方选择一个训练版本发起抽检；同一配置的未结案批次会被复用，失败重试沿用原抽样。</p>'}
 <h3>抽检批次</h3>
 ${batches.map(b=>`<button class="study" data-audit-batch="${b.id}">#${b.id} 训练#${b.training_id} · ${esc(STATUS_TEXT[b.state]||b.state)} · 总体${b.population_n}/抽${b.sampled_n} · 修订 r${b.revision}</button>`).join('')||'<p>暂无抽检批次。</p>'}
 </section><aside class="side">
 <h2>发起抽检</h2>
 ${doneTrainings.length?`<form id="audit-start">${select('训练版本（绑定其待标注全集）','training_id',doneTrainings.map(t=>[t.id,'#'+t.id+' '+t.name]))}${field('判定阈值','threshold','number','required min=0 max=1 step=0.05 value=0.5')}${field('抽样 seed','seed','number','required min=0 step=1 value=3106')}
 <p class="eyebrow">分层区间与各层抽取数量</p>
 ${[[0,0.2],[0.2,0.4],[0.4,0.6],[0.6,0.8],[0.8,1]].map(([lo,hi],i)=>`<div class="stratum-input"><span>S${i+1} · ${lo}–${hi}</span><input name="n_${i}" type="number" min="0" step="1" value="3" aria-label="S${i+1} 抽取数量"></div>`).join('')}
 <p>默认 5 层各抽 3 笔；低分段也要抽，才能看到被高分排序漏掉的异常。留空视为 0。</p>
 <button class="primary">发起分层抽检</button></form>`
 :'<p class="notice">还没有训练完成的版本。先在分类评估中完成一次训练，抽检会绑定该版本的待标注全集。</p>'}
 <h2>填写人工结论</h2>
 ${detail?(selectedBatch.state==='closed'
  ?`<p class="notice">批次已结案，结论已锁定。已发布结果 r${selectedBatch.closed_revision} 可回看与下载；如需补充结论请发起新批次。</p>`
  :`<form id="audit-decide">${select('抽检样本（可更正已填结论）','sample_id',detail.samples.map(s=>[s.id,`#${s.id} ${s.code} · ${s.stratum} · 当前：${DECISION_TEXT[s.decision]||s.decision}`]))}${select('人工判断','decision',[['valid','核验正常'],['flagged','需进一步核实']])}<label>理由与证据<textarea name="note" minlength="6" required rows="4"></textarea></label><button class="primary">保存并重算</button></form>
 <p><button id="audit-recompute">手动重算</button>${state.user.role==='admin'?' <button id="audit-close">结案并发布</button>':''}</p>
 <p class="eyebrow">结论更正会自动重算；重试不重复累计。结案会把当前结论的结果发布为对外版本。</p>`)
 :'<p>先发起或选择抽检批次。</p>'}
 </aside></div>`;
}
