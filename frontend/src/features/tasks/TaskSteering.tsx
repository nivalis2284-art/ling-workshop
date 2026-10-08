import {useState} from "react";
import type {LingApi} from "../../api/client";
import {ErrorNotice} from "../../shared/ui";

export function TaskSteering({api,id,onUpdated}:{api:LingApi;id:string;onUpdated:()=>Promise<void>}){
 const [text,setText]=useState("");const [busy,setBusy]=useState(false);const [error,setError]=useState("");
 async function send(){if(!text.trim()||busy)return;setBusy(true);setError("");try{await api.call("/tasks/"+encodeURIComponent(id)+"/steer",{method:"POST",body:JSON.stringify({text:text.trim()})});setText("");await onUpdated();}catch{setError("任务状态已变化或暂时无法接受指导，请刷新后核对。");}finally{setBusy(false);}}
 return <section><h3>补充要求或纠正方向</h3><p className="metadata">当前步骤会先暂停并保存真实结果，再接收你的指导；已经发生的操作不会被撤销。</p><form onSubmit={e=>{e.preventDefault();void send();}}><label>新的指导<textarea aria-label="新的指导" rows={2} value={text} maxLength={16000} onChange={e=>setText(e.target.value)} disabled={busy}/></label><button className="button secondary" disabled={busy||!text.trim()}>提交指导</button></form>{error&&<ErrorNotice message={error}/>}</section>;
}
