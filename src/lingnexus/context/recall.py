"""Bounded, diverse lexical recall with exact source revisions; no vector/graph store."""
import json
import re

from lingnexus.common.types import SourceRef
from lingnexus.memory.maintenance import MemoryMaintenance


def candidates(app,agent_id:str,query:str,scope:str="")->tuple[str,list[SourceRef],list[dict]]:
    if not query.strip():return "",[],[]
    query=query.split(chr(10),1)[0][:300]
    phrases=re.findall(r"[A-Za-z0-9_]{3,30}|[\u4e00-\u9fff]{2,24}",query)
    terms=list(dict.fromkeys(phrases+[t[i:i+2] for t in phrases if re.fullmatch(r"[\u4e00-\u9fff]+",t) for i in range(min(len(t)-1,16))]))[:24]
    found={}
    for term in terms:
        items=app.memory.find_episodes(agent_id,term,limit=5)
        items+=app.memory.knowledge.search(agent_id,term,limit=5)
        for item in items:
            identity=tuple(sorted((r["id"],r["revision"]) for r in item["sources"]))
            text=" ".join(item.get(k,"") for k in ("title","summary","statement"))
            score=sum(len(t)*len(t) for t in terms if t in text)
            previous=found.get(identity)
            if previous is None or score>previous[0]:found[identity]=(score,item)
    ranked=sorted(found.values(),key=lambda pair:(pair[0],pair[1].get("created_at","")),reverse=True)
    records,refs,markers=[],{},[]
    remaining=3200
    for _score,item in ranked:
        if len(records)>=4:break
        reference={key:item[key] for key in ("id","title","summary","statement","kind","sources") if key in item}
        originals=[]
        for value in item["sources"][:3]:
            ref=SourceRef(**value)
            record=app.history.read(ref)
            originals.append({"id":ref.id,"revision":ref.revision,"kind":record["kind"],"text":record["text"][:900]})
        reference["original_excerpt"]=originals
        size=len(json.dumps(reference,ensure_ascii=False).encode())
        if size>remaining:
            reference.pop("original_excerpt")
            size=len(json.dumps(reference,ensure_ascii=False).encode())
        if size>remaining:continue
        marker=MemoryMaintenance(app).snapshot_marker("knowledge" if "statement" in item else "episodes",item["id"])
        markers.append(marker)
        records.append(reference)
        remaining-=size
        for value in item["sources"]:refs[(value["id"],value["revision"])]=SourceRef(**value)
    if not records:return "",[],[]
    return "只读回忆参考（区分报告与事实，精确细节以原文为准）："+json.dumps(records,ensure_ascii=False),list(refs.values()),markers
