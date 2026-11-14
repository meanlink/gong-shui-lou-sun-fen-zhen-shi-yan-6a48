import {datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,notify,date} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {parseCSV} from '../utils/csv.js';
function revokedBadge(d){
 return d.revoked?'<span class="revoked-badge">已撤回</span>':'';
}
export async function dataPage(){
 workspace.datasets=await datasets.list();
 const chosenId=workspace.selectedDataset&&workspace.datasets.some(d=>d.id===workspace.selectedDataset)?workspace.selectedDataset:workspace.datasets[0]?.id;
 let selected=chosenId?await datasets.get(chosenId):null,m=workspace.meta;
 let scopeHtml='';
 if(selected?.revoked){
  const scope=await datasets.scope(selected.id);
  const stopped=scope.jobs.filter(j=>j.status==='revoked').length;
  scopeHtml='<section class="panel revoke-panel"><div class="section-title"><h2>撤回范围与处理状态</h2><span>可追溯 · 仅本批次</span></div>'
   +'<div class="revoke-meta"><p><strong>撤回原因：</strong>'+esc(selected.revoke_reason)+'</p><p><strong>负责人：</strong>'+esc(selected.revoked_by)+' · <strong>撤回时间：</strong>'+date(selected.revoked_at)+'</p></div>'
   +'<div class="revoke-stats">'+[['关联试验',scope.jobs.length+' 项，全部失效'],['中止计算/排队',stopped+' 项'],['停用简报计划',scope.reports.length+' 项（暂停计划不自行恢复）'],['失效归档',scope.deliveries.filter(x=>x.invalidated).length+' 份（原文保留）']].map(([k,v])=>'<div><span>'+esc(k)+'</span><strong>'+esc(v)+'</strong></div>').join('')+'</div>'
   +'<div class="scope-block"><h3>关联试验</h3>'+(scope.jobs.length?'<ul class="scope-list">'+scope.jobs.map(j=>'<li class="'+(j.revoked?'is-revoked':'')+'"><strong>'+esc(j.name)+'</strong><span>'+esc(j.status)+(j.revoked?' · 已失效：'+esc(j.revoke_reason):'')+'</span></li>').join('')+'</ul>':'<p class="empty">无关联试验。</p>')+'</div>'
   +'<div class="scope-block"><h3>分发计划</h3>'+(scope.reports.length?'<ul class="scope-list">'+scope.reports.map(r=>'<li class="is-revoked"><strong>'+esc(r.name)+'</strong><span>'+(r.enabled?'启用中':'已停用'+(r.auto_paused?'（撤回自动暂停）':'（手动暂停，保持暂停）'))+' → '+esc(r.destination)+'</span></li>').join('')+'</ul>':'<p class="empty">无关联分发计划。</p>')+'</div>'
   +'<div class="scope-block"><h3>归档投递</h3>'+(scope.deliveries.length?'<ul class="scope-list">'+scope.deliveries.map(x=>'<li class="'+(x.invalidated?'is-revoked':'')+'"><strong>'+esc(x.filename)+'</strong><span>'+(x.invalidated?'已失效：'+esc(x.invalidate_reason):'有效')+' · '+date(x.created_at)+'</span></li>').join('')+'</ul>':'<p class="empty">无归档投递。</p>')+'</div>'
   +'<p class="hint">旧结果与归档原文均保留留痕；更正数据请另建批次重新评估，本批次不可恢复。</p></section>';
 }
 return heading('02 / DATA INTAKE','数据接入与批次追溯','保留原始业务字段、来源和内容指纹。分析任务始终绑定一个不可变数据批次；仪表失准的批次可由负责人带原因撤回。')+'<div class="dataset-grid"><section class="panel"><div class="section-title"><h2>业务批次</h2><span>'+workspace.datasets.length+' 批</span></div><div class="batch-list">'+workspace.datasets.map(d=>'<button class="batch-item'+(d.revoked?' is-revoked':'')+'" data-dataset="'+d.id+'"><strong>'+esc(d.name)+' '+revokedBadge(d)+'</strong><span>'+d.rows+' 条 · '+esc(d.source)+(d.revoked?' · 已撤回：'+esc(d.revoke_reason):'')+'</span><small>SHA256 '+esc(d.checksum.slice(0,18))+'…</small>'+(d.revoked?'<small>撤回人 '+esc(d.revoked_by)+' · '+date(d.revoked_at)+'</small>':'')+'</button>').join('')+'</div><div class="section-title"><h2>接入新数据</h2></div><form id="dataset-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：11 月复测批次"></label><label>上传 CSV 或 JSON<input type="file" id="file-input" accept=".json,.csv"></label><label>记录 JSON<textarea name="records" rows="6" placeholder="粘贴 JSON 数组，或先选择上方文件" required></textarea></label><p class="hint">字段：'+m.columns.map(c=>esc(c.key)+' / '+esc(c.label)).join('，')+'。5–5,000 条记录。撤回批次的更正数据应作为新批次接入。</p><button type="submit" class="primary" '+(session.user.role==='viewer'?'disabled':'')+'>校验并接入批次</button></form></section><section class="panel"><div class="section-title"><h2 id="preview-name">'+esc(selected?.name||'尚无批次')+'</h2><span>'+(selected?.revoked?'已撤回 · 预览前 30 条（原文保留）':'预览前 30 条')+'</span></div>'+(selected?.revoked?'<div class="warning revoke-banner"><strong>该批次已撤回。</strong>失效原因：'+esc(selected.revoke_reason)+'。关联试验立即失效，不能再批准、建试验或生成简报；更正数据请另建批次重新评估。</div>':'')+'<div id="data-preview">'+table(m.columns,selected?.records.slice(0,30)||[])+'</div>'+(selected&&!selected.revoked&&session.user.role==='admin'?'<div class="withdraw-zone"><h3>批次撤回（负责人）</h3><p class="hint">撤回仅影响本批次：关联试验立即失效、在途计算与分发不得发布有效结果、暂停计划不自行恢复；旧结果与归档原文保留，下载时标明失效原因。</p><form id="withdraw-form"><label>撤回原因（必填，将随审计与归档留痕）<input name="reason" required minlength="2" maxlength="300" placeholder="例如：夜间流量计失准，整批读数不可信"></label><button type="submit" class="danger">带原因撤回此批次</button></form></div>':'')+scopeHtml+'</section></div>';
}
export function bindData(refresh){
 let source='manual.json';
 document.querySelector('#file-input').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;source=file.name;const text=await file.text(),records=file.name.endsWith('.csv')?parseCSV(text):JSON.parse(text);document.querySelector('[name=records]').value=JSON.stringify(records,null,2);}catch(error){notify(error.message,true);}});
 document.querySelectorAll('[data-dataset]').forEach(button=>button.onclick=()=>{workspace.selectedDataset=Number(button.dataset.dataset);refresh();});
 const df=document.querySelector('#dataset-form');
 if(df)submit(df,async f=>{await datasets.create({name:f.get('name'),source,records:JSON.parse(f.get('records'))});notify('数据已接入，可前往工作台执行分析');workspace.selectedDataset=null;await refresh();});
 const wf=document.querySelector('#withdraw-form');
 if(wf)submit(wf,async f=>{const reason=(f.get('reason')||'').trim();if(reason.length<2){notify('撤回原因至少需要两个字符',true);return;}const targetId=workspace.selectedDataset||workspace.datasets[0]?.id;if(targetId==null){notify('未找到要撤回的批次',true);return;}if(!window.confirm('确认撤回该批次？关联试验将立即失效，且本操作不可恢复；更正数据需另建批次。'))return;const r=await datasets.withdraw(targetId,reason);workspace.selectedDataset=targetId;notify('已撤回：'+r.withdraw_scope.jobs+' 项试验失效、'+r.withdraw_scope.reports_paused+' 个计划停用、'+r.withdraw_scope.deliveries_invalidated+' 份归档标记失效');await refresh();});
}
