import {api,post} from './api/client.js';
import {state} from './stores/session.js';
import {routes,page} from './router/index.js';
import {values,feedback} from './hooks/form.js';
import {esc} from './utils/html.js';
import * as claims from './pages/claims.js';
import * as training from './pages/training.js';
import * as evaluation from './pages/evaluation.js';
import * as reviews from './pages/reviews.js';
import * as audit from './pages/audit.js';
const root=document.querySelector('#app');
const pages=[claims,training,evaluation,reviews,audit];

async function load(){
 [state.claims,state.trainings,state.reviews,state.auditBatches]=await Promise.all(['/claims','/trainings','/reviews','/audit-batches'].map(x=>api(x)));
 state.selected=state.trainings.find(t=>t.id===state.selected?.id)||state.trainings[0]||null;
 let id=state.auditDetail?.batch?.id||state.auditBatches[0]?.id;
 if(id)await loadAudit(id);else state.auditDetail=null;
}
async function loadAudit(id){
 state.auditDetail=await api('/audit-batches/'+id);
 if(state.auditResultView?.batch_id!==id)state.auditResultView=state.auditDetail.published||state.auditDetail.current;
}
async function refreshAudit(){
 let id=state.auditDetail?.batch?.id||state.auditBatches[0]?.id;
 if(id)await loadAudit(id);
}

function login(){root.innerHTML=`<div class="login"><section class="cover"><div class="eyebrow">HEXU / EXPENSE REVIEW 006</div><h1>让复核精力，<br>先到有疑点的票据。</h1><p>禾序咨询 · 财务审阅卷宗<br>从历史人工标签学习，保留每一次模型版本。<br>评分解释 / 阈值比较 / 独立复核判断</p></section><section><div class="eyebrow">CLAIM TRIAGE</div><h2>登录财务卷宗</h2><form id="login"><label>工作邮箱<input name="email" type="email" value="analyst@demo.local" required></label><label>密码<input name="password" type="password" value="Demo123!" required></label><button class="primary">进入报销复核</button></form><p id="error" class="error"></p><p>演示：admin / analyst / viewer @demo.local<br>密码：Demo123!</p></section></div>`;document.querySelector('form').onsubmit=e=>{e.preventDefault();run(async()=>{let d=await post('/auth/login',values(e.target));localStorage.setItem('token',d.token);state.user=d.user;await load();render()})}}
function render(){let n=Math.min(routes.length-1,Math.max(0,page()));root.innerHTML=`<header><div class=brand>禾序 / 财务审阅卷宗 <span class=eyebrow>CLAIM TRIAGE</span></div><div>${esc(state.user.email)} · ${esc(state.user.role)} <button id=logout>退出</button></div></header><div class=layout><nav>${routes.map((r,i)=>`<button data-nav=${i} class="${i===n?'active':''}">0${i+1} ${r}</button>`).join('')}<p class=eyebrow>AUDIT FILES<br>2026 / HEXU</p></nav><main><div class=intro><div><div class=eyebrow>OPERATIONS RESEARCH / 0${n+1}</div><h1>${routes[n]}</h1><p>历史标签形成训练版本，留出指标帮助选择复核阈值。模型筛选候选，人工保留判断，分层抽检校正总体估计。</p></div><span class=tag>300 笔种子样本</span></div><p id=error class=error></p>${pages[n].render()}</main></div>`;bind();if(state.user.role==='viewer')document.querySelectorAll('form button, #generate, #audit-decide button, #audit-recompute, #audit-close').forEach(b=>{b.disabled=true;b.textContent='观察员只读'})}
async function run(fn){try{await fn()}catch(e){feedback(e.message)}}
function download(name,body,type){let u=URL.createObjectURL(new Blob([body],{type}));let a=document.createElement('a');a.href=u;a.download=name;a.click();URL.revokeObjectURL(u)}
async function downloadAudit(scope,name){
 let bid=state.auditDetail.batch.id;
 let r=await fetch('/api/audit-batches/'+bid+'/export?scope='+scope,{headers:{Authorization:'Bearer '+localStorage.getItem('token')}});
 if(!r.ok){let e=await r.json().catch(()=>({detail:r.statusText}));throw Error(typeof e.detail==='string'?e.detail:'下载失败')}
 let text=await r.text();
 if(scope!=='samples')text=JSON.stringify(JSON.parse(text),null,2);
 download(name.replace('{id}',bid),text,scope==='samples'?'text/csv':'application/json');
}
function bind(){document.querySelectorAll('[data-nav]').forEach(b=>b.onclick=()=>location.hash=b.dataset.nav);document.querySelector('#logout').onclick=()=>{localStorage.removeItem('token');state.user=null;login()};
for(let [id,path] of [['claim','/claims'],['training','/trainings'],['review','/reviews']]){let f=document.querySelector('#'+id);if(f)f.onsubmit=e=>{e.preventDefault();run(async()=>{let d=values(f);for(let k of ['amount','distance','threshold','label'])if(k in d)d[k]=d[k]===''?null:Number(d[k]);if('holiday'in d)d.holiday=d.holiday==='true';let record=d.record_id;delete d.record_id;let created=record?await api(path+'/'+record,{method:'PUT',body:JSON.stringify(d)}):await post(path,d);if(id==='training'){state.selected=created;location.hash='2'}await load();render()})}}
document.querySelectorAll('[data-training]').forEach(b=>b.onclick=()=>run(async()=>{state.selected=await api('/trainings/'+b.dataset.training);location.hash='2';render()}));document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=()=>run(async()=>{await post('/reviews/'+b.dataset.approve+'/approve',{});await load();render()}));let refresh=document.querySelector('#refresh');if(refresh)refresh.onclick=()=>run(async()=>{await load();render()});let gen=document.querySelector('#generate');if(gen)gen.onclick=()=>run(async()=>{let r=await post('/trainings/'+state.selected.id+'/reviews',{});await load();location.hash='3';render();feedback('已新增 '+r.created+' 个复核任务')});let dl=document.querySelector('#download');if(dl)dl.onclick=()=>run(async()=>{let d=await api('/trainings/'+state.selected.id+'/export');download('claim-triage-'+d.id+'.json',JSON.stringify(d,null,2),'application/json')});
bindAudit();}
function bindAudit(){
 document.querySelectorAll('[data-audit-batch]').forEach(b=>b.onclick=()=>run(async()=>{await loadAudit(Number(b.dataset.auditBatch));state.auditResultView=null;render()}));
 document.querySelectorAll('[data-audit-result]').forEach(b=>b.onclick=()=>run(async()=>{state.auditResultView=state.auditDetail.history.find(h=>String(h.id)===b.dataset.auditResult);render()}));
 let start=document.querySelector('#audit-start');
 if(start)start.onsubmit=e=>{e.preventDefault();run(async()=>{let d=values(start);let strata=[0,1,2,3,4].map(i=>({lo:[0,0.2,0.4,0.6,0.8][i],hi:[0.2,0.4,0.6,0.8,1][i],sample_n:Number(d['n_'+i]||0)}));let r=await post('/trainings/'+d.training_id+'/audit-batches',{threshold:Number(d.threshold),seed:Number(d.seed),strata});state.auditDetail=await api('/audit-batches/'+r.batch.id);state.auditResultView=null;await load();render();feedback(r.reused?'已复用同配置的未结案批次 #'+r.batch.id:'已发起抽检 #'+r.batch.id+'，共抽 '+r.batch.sampled_n+' 笔')})};
 let decide=document.querySelector('#audit-decide');
 if(decide)decide.onsubmit=e=>{e.preventDefault();run(async()=>{let d=values(decide);let bid=state.auditDetail.batch.id;await api('/audit-batches/'+bid+'/samples/'+d.sample_id,{method:'PUT',body:JSON.stringify({decision:d.decision,note:d.note})});await loadAudit(bid);render();feedback('结论已保存并按入选概率重算')})};
 let recompute=document.querySelector('#audit-recompute');
 if(recompute)recompute.onclick=()=>run(async()=>{let bid=state.auditDetail.batch.id;let r=await post('/audit-batches/'+bid+'/recompute',{});await loadAudit(bid);render();feedback(r.created?'已生成新修订 r'+r.result.revision:'结论未变，沿用修订 r'+r.result.revision)});
 let close=document.querySelector('#audit-close');
 if(close)close.onclick=()=>run(async()=>{let bid=state.auditDetail.batch.id;let r=await post('/audit-batches/'+bid+'/close',{});await loadAudit(bid);render();feedback('已结案并发布 r'+r.result.revision)});
 let csv=document.querySelector('#audit-export-csv');
 if(csv)csv.onclick=()=>run(()=>downloadAudit('samples','audit-batch-{id}-samples.csv'));
 let hist=document.querySelector('#audit-export-history');
 if(hist)hist.onclick=()=>run(()=>downloadAudit('history','audit-batch-{id}-history.json'));
 let pub=document.querySelector('#audit-export-published');
 if(pub)pub.onclick=()=>run(()=>downloadAudit('published','audit-batch-{id}-published.json'));
 let ar=document.querySelector('#audit-refresh');
 if(ar)ar.onclick=()=>run(async()=>{await load();render()});
}
window.onhashchange=()=>{if(state.user)render()};if(localStorage.getItem('token'))run(async()=>{try{state.user=await api('/me');await load();render()}catch(e){localStorage.removeItem('token');login();feedback(e.message)}});else login();
