import {useCallback,useState} from "react";
import type {LingApi} from "../../api/client";
import type {ConversationMessage} from "../../api/types";
import {useResource} from "../../shared/useResource";
import {ErrorNotice} from "../../shared/ui";

type Segment={id:string;title:string;start_message_id:string;revision:number;origin:string};

export function SegmentPanel({api,conversationId,messages,disabled}:{api:LingApi;conversationId:string;messages:ConversationMessage[];disabled:boolean}){
  const load=useCallback((signal:AbortSignal)=>api.call<Segment[]>("/conversations/"+encodeURIComponent(conversationId)+"/segments",{signal}),[api,conversationId]);
  const resource=useResource(load);
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(false);
  const [title,setTitle]=useState("");
  const [message,setMessage]=useState("");
  const [editing,setEditing]=useState<Segment|null>(null);
  async function save(){
    if(!title.trim()||busy||disabled)return;
    setBusy(true);setError("");
    try{
      if(editing)await api.call("/segments/"+encodeURIComponent(editing.id),{method:"PATCH",body:JSON.stringify({title:title.trim(),expected_revision:editing.revision})});
      else if(message)await api.call("/conversations/"+encodeURIComponent(conversationId)+"/segments",{method:"POST",body:JSON.stringify({message_id:message,title:title.trim()})});
      else return;
      setTitle("");setMessage("");setEditing(null);await resource.refresh();
    }catch{setError("交流段已变化或暂时无法保存，请刷新后重试。");}
    finally{setBusy(false);}
  }
  return <details className="panel section-panel"><summary>交流主题与分段</summary>
    <p className="metadata">系统根据时间与整理后的主题组织交流；调整分段不会重置灵或改写原始消息。</p>
    {error&&<ErrorNotice message={error}/>}
    {resource.status==="ready"&&resource.data.map(item=><div className="section-heading" key={item.id}><span>{item.title} · {item.origin==="owner"?"已调整":"自动整理"}</span><button className="text-button" disabled={disabled||busy} onClick={()=>{setEditing(item);setTitle(item.title);}}>修改主题</button></div>)}
    <button className="text-button" onClick={()=>void resource.refresh()} disabled={busy}>刷新主题</button>
    <form onSubmit={e=>{e.preventDefault();void save();}}>
      {!editing&&<label>从哪条消息开始新段<select aria-label="分段起点" value={message} disabled={disabled||busy} onChange={e=>setMessage(e.target.value)}><option value="">选择消息</option>{messages.map(m=><option key={m.id} value={m.id}>{m.role==="user"?"你":"灵"}：{m.text.slice(0,35)}</option>)}</select></label>}
      <label>主题名称<input aria-label="主题名称" value={title} disabled={disabled||busy} maxLength={200} onChange={e=>setTitle(e.target.value)}/></label>
      <button className="button secondary" disabled={disabled||busy||!title.trim()||(!editing&&!message)}>{editing?"保存主题":"从此处分段"}</button>
      {editing&&<button type="button" className="text-button" onClick={()=>{setEditing(null);setTitle("");}}>取消修改</button>}
    </form>
  </details>;
}
