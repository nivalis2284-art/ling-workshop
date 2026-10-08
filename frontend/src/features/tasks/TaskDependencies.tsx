import {useState} from "react";
import type {LingApi} from "../../api/client";
import {ConfirmDialog} from "../../shared/ConfirmDialog";
import {ErrorNotice} from "../../shared/ui";
import {useResource} from "../../shared/useResource";
import {useCallback} from "react";

type Plan={id:string;status:string;intent_hash:string;plan:{manager:string;packages:string[];destination:string;network_mode:string};result:unknown;error_code?:string};
export function TaskDependencies({api,taskId,status,onUpdated,candidate}:{api:LingApi;taskId:string;status:string;onUpdated:()=>Promise<void>;candidate?:{manager:"pip"|"npm";packages:string[]}}){
 const path="/tasks/"+encodeURIComponent(taskId)+"/dependencies";
 const load=useCallback((signal:AbortSignal)=>api.call<Plan[]>(path,{signal}),[api,path]);const resource=useResource(load);
 const [manager,setManager]=useState<string>(candidate?.manager??"pip");const [packages,setPackages]=useState(candidate?.packages.join("\n")??"");const [busy,setBusy]=useState(false);const [error,setError]=useState("");const [confirm,setConfirm]=useState<Plan|null>(null);
 const stopped=["paused","blocked"].includes(status);
 async function prepare(){if(busy)return;setBusy(true);setError("");try{await api.call(path+"/plan",{method:"POST",body:JSON.stringify({manager,packages:packages.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean),request_id:crypto.randomUUID()})});await resource.refresh();}catch{setError("安装计划未建立，请确认任务已暂停、版本已明确，并检查项目授权。");}finally{setBusy(false);}}
 async function decide(plan:Plan,decision:"approve"|"reject"){setBusy(true);setError("");try{await api.call(path+"/"+encodeURIComponent(plan.id)+"/decision",{method:"POST",body:JSON.stringify({decision,intent_hash:plan.intent_hash})});await resource.refresh();await onUpdated();}catch{setError("项目或安装计划已变化，请重新核对；结果不明时不要重复执行。");}finally{setBusy(false);setConfirm(null);}}
 return <section className="panel section-panel"><h3>任务副本的依赖</h3><p className="metadata">按任务提出的包与版本建立安装计划。只有确认后才临时联网；完成后继续在隔离环境运行。</p>
 {error&&<ErrorNotice message={error}/>}<form onSubmit={e=>{e.preventDefault();void prepare();}}>
 <label>安装类型<select aria-label="安装类型" value={manager} onChange={e=>setManager(e.target.value)} disabled={busy||!stopped}><option value="pip">Python 包</option><option value="npm">Node.js 包</option></select></label>
 <label>包与确切版本<textarea aria-label="包与确切版本" value={packages} onChange={e=>setPackages(e.target.value)} placeholder={manager==="pip"?"例如 colorama==0.4.6":"例如 typescript@5.9.3"} disabled={busy||!stopped}/></label>
 <button className="button secondary" disabled={busy||!stopped||!packages.trim()}>建立安装计划</button></form>
 {!stopped&&<p className="field-help">需要安装时，先暂停任务。</p>}
 {candidate&&<p className="field-help">已填写任务提出的包与版本，请先核对再确认。</p>}
 {resource.status==="ready"&&resource.data.map(plan=><article key={plan.id}><h4>{plan.plan.manager==="pip"?"Python":"Node.js"} 安装计划</h4><p>{plan.plan.packages.join("、")}</p><p>状态：{{pending:"等待确认",running:"安装中",success:"安装完成",failed:"安装失败",unknown:"结果不明，需核查"}[plan.status]??plan.status}</p>{plan.status==="pending"&&<div className="button-row"><button className="button secondary" disabled={busy} onClick={()=>void decide(plan,"reject")}>拒绝安装</button><button className="button primary" disabled={busy||!stopped} onClick={()=>setConfirm(plan)}>审阅并安装</button></div>}{plan.status==="success"&&<p>可恢复任务继续验证。</p>}</article>)}
 <button className="text-button" disabled={busy} onClick={()=>void resource.refresh()}>刷新安装记录</button>
 {confirm&&<ConfirmDialog title="允许这次隔离安装" confirmLabel="确认联网安装" busy={busy} onCancel={()=>setConfirm(null)} onConfirm={()=>void decide(confirm,"approve")}><p>将临时联网安装以下包，只写入这个任务的项目副本，不修改原项目或授予其他权限。</p><p>{confirm.plan.packages.join("、")}</p><p>安装脚本和源码构建钩子不执行。结果不明时将停止等待核查。</p></ConfirmDialog>}
 </section>;
}
