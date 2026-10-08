import {useState} from "react";
import type {LingApi} from "../../api/client";
import {ConfirmDialog} from "../../shared/ConfirmDialog";
import {ErrorNotice} from "../../shared/ui";

const choices=[{key:"identity.description",label:"自我说明",kind:"identity"},{key:"identity.name",label:"当前名字",kind:"identity"},{key:"personality.expression",label:"表达倾向",kind:"major_personality"},{key:"values.understanding",label:"价值理解",kind:"value"}];

export function IdentityEditor({api,agentId,revision,refresh}:{api:LingApi;agentId:string;revision:number;refresh:()=>Promise<void>}){
 const [key,setKey]=useState("identity.description");const [text,setText]=useState("");const [confirm,setConfirm]=useState(false);const [busy,setBusy]=useState(false);const [error,setError]=useState("");const [done,setDone]=useState("");
 async function apply(){
  if(busy)return;setBusy(true);setError("");setDone("");
  try{
   const item=choices.find(c=>c.key===key)!;
   const source=await api.call<{id:string;revision:number}>("/sources",{method:"POST",body:JSON.stringify({agent_id:agentId,content:"所有者设置或纠正"+item.label+"："+text,request_id:crypto.randomUUID()})});
   const candidate=await api.call<{id:string}>("/proposals",{method:"POST",body:JSON.stringify({agent_id:agentId,kind:item.kind,key,text,sources:[source],base_revision:revision,request_id:crypto.randomUUID()})});
   await api.call("/proposals/"+encodeURIComponent(candidate.id)+"/decision",{method:"POST",body:JSON.stringify({decision:"approve",expected_revision:revision})});
   setDone("这项认识已采用，变化依据和旧版本保留。");setText("");await refresh();
  }catch{setError("状态可能已经变化，未确认的设置会留在候选中。请刷新后核对。");await refresh();}
  finally{setBusy(false);setConfirm(false);}
 }
 return <section className="panel section-panel"><h2>设定起点或纠正认识</h2><p className="muted">这里设置的是当前有效状态。以后仍可以通过经历继续发展，也可以查看和回退。</p>
 {error&&<ErrorNotice message={error}/>} {done&&<p role="status">{done}</p>}
 <form onSubmit={e=>{e.preventDefault();if(text.trim())setConfirm(true);}}>
 <label>调整哪项认识<select aria-label="调整哪项认识" value={key} disabled={busy} onChange={e=>setKey(e.target.value)}>{choices.map(c=><option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
 <label>当前希望采用的内容<textarea aria-label="当前希望采用的内容" rows={3} maxLength={6000} value={text} disabled={busy} onChange={e=>setText(e.target.value)}/></label>
 <button className="button secondary" disabled={busy||!text.trim()}>审阅这项设置</button></form>
 {confirm&&<ConfirmDialog title="采用新的自我认识" confirmLabel="确认采用" busy={busy} onCancel={()=>setConfirm(false)} onConfirm={()=>void apply()}><p>将作为当前有效状态保存，保留你这次设置的依据和旧版本。</p><p className="preserve-text">{text}</p></ConfirmDialog>}
 </section>;
}
