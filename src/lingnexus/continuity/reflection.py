"""Frozen reflection staging and bounded, atomic publication for the C runner."""
import json

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict
from lingnexus.common.types import new_id, utc_now

from . import sources
from .store import digest, encode

MAX_BATCH_BYTES=131072
MAX_EVENTS=6


class ReflectionBridge:
    def __init__(self,owner):
        self.owner,self.app,self.database=owner,owner.app,owner.database

    def stage(self,agent_id,*,conversation_id,units,batch_key,policy_version='1'):
        if not units or not batch_key or len(units)>4096:
            raise ValueError('冻结批次无效。')
        binding=self.owner.main.binding(agent_id)
        if not binding or binding['conversation_id']!=conversation_id:
            raise Conflict('反思只能来自已绑定主干。')
        metadata,version=sources.path_metadata(self.app,conversation_id)
        positions={n['id']:i for i,n in enumerate(metadata)}
        frozen,total=[],0
        for unit in units:
            # Units may group complete turns, but every authoritative component
            # must have an independent frozen ref/hash/grants/time.
            components=unit.get('sources') or [unit]
            snapshots=[]
            for component in components:
                if not component.get('sha256') or not component.get('grants'):
                    raise Conflict('来源必须携带冻结 hash 与原始授权。')
                snapshot=sources.snapshot(self.app,agent_id,component['kind'],component['ref'],
                        expected_sha=component['sha256'],max_bytes=MAX_BATCH_BYTES-total,
                        allowed_ids=positions,original_grants=component['grants'])
                if snapshot['epoch']!=version or sources.stamp(component['occurred_at'])!=snapshot['occurred_at']:
                    raise Conflict('来源选路或发生时间已变化。')
                total+=snapshot['byte_count']
                if total>MAX_BATCH_BYTES:
                    raise Conflict('冻结批次超过128KiB。')
                snapshots.append(snapshot)
            frozen.append(dict(unit,sources=snapshots))
        selected=[]
        for unit in frozen:
            selected.extend(s['ref']['id'] for s in unit['sources'] if s['kind']=='message')
        indexes=[positions[mid] for mid in selected]
        if indexes!=sorted(set(indexes)):
            raise Conflict('冻结消息来源重复或顺序不合法。')
        # Complete turns are indivisible. C may skip an oversize island, but must
        # label the gap; events cannot span across it at publish time.
        chosen=set(selected)
        for i,node in enumerate(metadata):
            if node['id'] not in chosen:
                continue
            turn=i
            while turn>0 and metadata[turn]['role']!='user':
                turn-=1
            end=turn+1
            while end<len(metadata) and metadata[end]['role']!='user':
                end+=1
            if any(n['id'] not in chosen for n in metadata[turn:end]):
                raise Conflict('反思不能拆断完整对话回合。')
        value={'units':frozen,'binding':binding,'epoch':version,'positions':{mid:positions[mid] for mid in selected}}
        with self.database.transaction() as db:
            if sources.epoch(db,conversation_id)!=version:
                raise Conflict('冻结过程中来源改变。')
            old=db.execute('SELECT * FROM continuity_reflection_drafts WHERE agent_id=? AND batch_key=?',(agent_id,batch_key)).fetchone()
            if old:
                if old['units_digest']!=digest(value):
                    raise Conflict('批次编号已用于其他来源。')
                return dict(old)
            did,now=new_id(),utc_now()
            db.execute('INSERT INTO continuity_reflection_drafts(id,agent_id,batch_key,policy_version,conversation_id,units,units_digest,status,created_at) '
                       "VALUES(?,?,?,?,?,?,?,'staged',?)",(did,agent_id,batch_key,policy_version,conversation_id,encode(value),digest(value),now))
        return {'id':did,'revision':1,'status':'staged','units':frozen}

    def publish(self,draft_id,*,expected_revision,events):
        if not events or len(events)>MAX_EVENTS:
            raise Conflict('事件数量超限。')
        with self.database.transaction() as db:
            draft=db.execute('SELECT * FROM continuity_reflection_drafts WHERE id=?',(draft_id,)).fetchone()
            if draft is None:
                raise NotFound('反思冻结批次不存在。')
            if expected_revision!=draft['revision']:
                raise RevisionConflict('批次版本改变。')
            if draft['status']=='published':
                return [self.owner.experiences.get(r[0]) for r in db.execute('SELECT id FROM continuity_experiences WHERE batch_id=? ORDER BY created_at,id',(draft_id,))]
            if draft['status']!='staged':
                raise Conflict('批次已废弃。')
            value=json.loads(draft['units'])
            if self.owner.main.binding(draft['agent_id'])!=value['binding'] or sources.epoch(db,draft['conversation_id'])!=value['epoch']:
                raise Conflict('批次的选路或绑定已失效。')
            if db.execute("SELECT 1 FROM chat_runs WHERE conversation_id=? AND status='running'",(draft['conversation_id'],)).fetchone():
                raise Conflict('主干活动回合尚未结束，请延后发布。')
            units={u['source_id']:u for u in value['units']}
            order=list(units)
            seen=[]
            eids=[]
            for event in events:
                ids=event.get('source_ids') or [s['source_id'] for s in event.get('sources',[])]
                if not ids or any(s not in units for s in ids):
                    raise Conflict('事件引用快照外来源。')
                seen.extend(ids)
                if ids!=order[order.index(ids[0]):order.index(ids[0])+len(ids)]:
                    raise Conflict('事件来源必须连续有序。')
                components=[s for uid in ids for s in units[uid]['sources']]
                indexes=[value['positions'][s['ref']['id']] for s in components if s['kind']=='message']
                if indexes and indexes!=list(range(min(indexes),max(indexes)+1)):
                    raise Conflict('事件不能跨越排除或延期的来源岛。')
                for s in components:
                    sources.validate(self.app,draft['agent_id'],s)
                summary=event.get('summary')
                if not isinstance(summary,str) or not summary.strip() or len(summary.encode())>8192:
                    raise Conflict('摘要为空或过长。')
                times=[s['occurred_at'] for s in components]
                start,end=min(times),max(times)
                # Model timestamps are never authoritative, even when plausible.
                for key,truth in [('occurred_start',start),('occurred_end',end)]:
                    if event.get(key) is not None and sources.stamp(event[key])!=truth:
                        raise Conflict('模型事件时间与原记录不一致。')
                for s in components:
                    if db.execute("SELECT 1 FROM continuity_experience_sources s JOIN continuity_experiences e ON e.id=s.experience_id "
                                  "WHERE e.agent_id=? AND s.kind=? AND s.ref=? AND (e.valid=1 OR e.reason='owner_revoked')",
                                  (draft['agent_id'],s['kind'],encode(s['ref']))).fetchone():
                        raise Conflict('来源已覆盖或处于撤销冷却。')
                eid,now=new_id(),utc_now()
                db.execute('INSERT INTO continuity_experiences(id,agent_id,kind,title,summary,revision,occurred_start,occurred_end,coverage,conversation_id,batch_id,policy_version,created_at,updated_at) '
                           "VALUES(?,?,'main_event',?,?,1,?,?,'exact',?,?,?,?,?)",
                           (eid,draft['agent_id'],event.get('title') or '主干事件',summary,start,end,draft['conversation_id'],draft_id,draft['policy_version'],now,now))
                for ordinal,s in enumerate(components):
                    db.execute('INSERT INTO continuity_experience_sources VALUES(?,?,?,?,?,?,?,?,?)',
                               (eid,s['kind']+':'+s['ref']['id'],s['kind'],encode(s['ref']),ordinal,s['byte_count'],s['sha256'],'retained',encode(s)))
                eids.append(eid)
            if seen!=order:
                raise Conflict('批次必须精确覆盖一次且顺序一致。')
            db.execute("UPDATE continuity_reflection_drafts SET status='published',published_at=? WHERE id=?",(utc_now(),draft_id))
        return [self.owner.experiences.get(eid) for eid in eids]
