"""Independent work conversations and durable archive/return transitions."""
import json

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied, RevisionConflict
from lingnexus.common.types import new_id, utc_now

from . import sources
from .store import digest, encode

ACTIVE = ('starting','running','awaiting_approval','paused','finishing','unknown','pending_review','interrupted')
TERMINAL = {'archived','cancelled','failed'}


class WorkDomain:
    def __init__(self, owner):
        self.owner,self.app,self.database=owner,owner.app,owner.database

    def row(self,branch_id):
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_work_branches WHERE id=?',(branch_id,)).fetchone()
        if row is None:
            raise NotFound('工作分支不存在。')
        return dict(row)

    def get(self,branch_id):
        return public(self.row(branch_id))

    def list(self,agent_id):
        self.app.permissions.current(agent_id)
        with self.database.transaction() as db:
            rows=db.execute('SELECT * FROM continuity_work_branches WHERE agent_id=? ORDER BY created_at,id',
                            (agent_id,)).fetchall()
        return [public(row) for row in rows]

    def create(self,agent_id,*,objective,request_id,profile_id=None,origin_message_id=None,main_conversation_id=None):
        if not objective.strip() or not request_id.strip() or len(objective)>8000 or len(request_id)>256:
            raise ValueError('工作目标或请求编号无效。')
        fingerprint=digest([objective,profile_id,origin_message_id,main_conversation_id])
        with self.database.transaction() as db:
            old=db.execute('SELECT * FROM continuity_work_branches WHERE agent_id=? AND request_id=?',
                           (agent_id,request_id)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:
                    raise Conflict('工作请求编号冲突。')
                return public(old)
        binding=self.owner.main.binding(agent_id)
        if binding is None or (main_conversation_id is not None and main_conversation_id!=binding['conversation_id']):
            raise Conflict('请先明确选择产品主干。')
        if profile_id:
            self.app.profiles.get(profile_id)
        cid=binding['conversation_id']
        main=self.app.conversations.for_chat(agent_id,cid)
        anchor=origin_message_id or main['head_id']
        nodes,version=sources.path_metadata(self.app,cid,anchor=anchor)
        # Freeze only the latest necessary ordinary text, never replay main native.
        background=[]
        remaining=15000
        for node in reversed(nodes):
            if remaining<4:
                break
            with self.database.transaction() as db:
                asset_revision=sources.asset_version(db,node['id'])
            page=sources.read_page(self.app,'message',{'id':node['id']},max_bytes=min(remaining,16384))
            source={'kind':'message','ref':{'id':node['id']},'conversation_id':cid,'epoch':version,
                    'grants':sources.scope(self.app,agent_id),'sha256':digest(page['text']),
                    'byte_count':node['byte_count'],'prefix_bytes':page['page_bytes'],
                    'occurred_at':sources.stamp(node['created_at'])}
            with self.database.transaction() as db:
                source['message_id']=node['id']
                source['asset_revision']=asset_revision
            background.append(source)
            remaining-=page['page_bytes']+len(source['occurred_at'].encode())+8
        background.reverse()
        with self.database.transaction() as db:
            if sources.epoch(db,cid)!=version or self.owner.main.binding(agent_id)!=binding:
                raise Conflict('创建工作时来源或主干已变化。')
            conversation=self.app.conversations.create(agent_id,'工作 · '+objective[:100])
            bid,now=new_id(),utc_now()
            members=[{'id':n['id'],'parent_id':n['parent_id'],'role':n['role']} for n in nodes]
            grants={'scope':sources.scope(self.app,agent_id),'main_epoch':version,'binding_revision':binding['revision']}
            db.execute('INSERT INTO continuity_work_branches(id,agent_id,conversation_id,main_conversation_id,'
                       'origin_message_id,objective,profile_id,status,revision,source_anchor_id,members,members_digest,'
                       'background,background_digest,grants,request_id,fingerprint,created_at,updated_at) '
                       "VALUES(?,?,?,?,?,?,?,'queued',1,?,?,?,?,?,?,?,?,?,?)",
                       (bid,agent_id,conversation['id'],cid,anchor,objective,profile_id,anchor,encode(members),digest(members),
                        encode(background),digest(background),encode(grants),request_id,fingerprint,now,now))
            db.execute('INSERT INTO continuity_experiences(id,agent_id,kind,title,summary,revision,coverage,conversation_id,branch_id,created_at,updated_at) '
                       "VALUES(?,?,'work_branch',?,'进行中工作：请按目录读取安全记录。',1,'live',?,?,?,?)",
                       ('work-live:'+bid,agent_id,objective[:500],conversation['id'],bid,now,now))
        return self.get(bid)

    def assert_fork(self,row):
        grants=json.loads(row['grants'])
        with self.database.transaction() as db:
            binding=self.owner.main.binding(row['agent_id'])
            if (binding is None or binding['conversation_id']!=row['main_conversation_id']
                    or binding['revision']!=grants['binding_revision']):
                raise Conflict('工作来源的主干绑定已变化。')
            if sources.epoch(db,row['main_conversation_id'])!=grants['main_epoch']:
                raise Conflict('工作背景的选路或成员已失效。')
            if sources.scope(self.app,row['agent_id'])!=grants['scope']:
                raise PermissionDenied('工作原始授权已撤回。')
            for source in json.loads(row['background']):
                sources.validate(self.app,row['agent_id'],source)

    @staticmethod
    def revision(row,expected):
        if type(expected) is not int or row['revision']!=expected:
            raise RevisionConflict('工作版本已变化，请刷新。')

    def guide(self,bid,text,request_id):
        if not text.strip() or not request_id.strip() or len(text.encode())>16384:
            raise ValueError('指导内容无效。')
        with self.database.transaction() as db:
            row=self.row(bid)
            old=db.execute('SELECT * FROM continuity_work_guidance WHERE branch_id=? AND request_id=?',
                           (bid,request_id)).fetchone()
            if old:
                if old['fingerprint']!=digest(text):
                    raise Conflict('指导请求编号冲突。')
                return self.get(bid)
            if row['status'] in TERMINAL:
                raise Conflict('工作已经结束。')
            db.execute('INSERT INTO continuity_work_guidance(id,branch_id,request_id,fingerprint,text,created_at) VALUES(?,?,?,?,?,?)',
                       (new_id(),bid,request_id,digest(text),text,utc_now()))
            if row['status'] == 'paused':
                # One continuation per guide, submitted by the coordinator with a
                # deterministic request id. Existing native recovery is preserved.
                db.execute("UPDATE continuity_work_branches SET status='queued' WHERE id=?",(bid,))
            self.bump(db,bid)
        return self.get(bid)

    def finish(self,bid,expected_revision,*,run_id=None):
        with self.database.transaction() as db:
            row=self.row(bid)
            self.revision(row,expected_revision)
            if run_id is not None and row['run_id']!=run_id:
                raise PermissionDenied('完成意图必须属于本次工作运行。')
            if row['status'] in TERMINAL:
                return self.get(bid)
            if run_id is None:
                self.safe(row)  # owner finish while active/unknown must refuse
            db.execute('UPDATE continuity_work_branches SET finish_requested=1 WHERE id=?',(bid,))
            self.bump(db,bid)
        return self.get(bid)

    def cancel(self,bid,expected_revision):
        with self.database.transaction() as db:
            row=self.row(bid)
            self.revision(row,expected_revision)
            if row['status'] in TERMINAL:
                return self.get(bid)
            # Coordinator drains the real run first. Unknown never unlocks work.
            db.execute("UPDATE continuity_work_branches SET outcome='cancel_requested',finish_requested=1 WHERE id=?",(bid,))
            self.bump(db,bid)
        return self.get(bid)

    def safe(self,row):
        with self.database.transaction() as db:
            runs=db.execute('SELECT id,status FROM chat_runs WHERE conversation_id=?',(row['conversation_id'],)).fetchall()
            for run in runs:
                if run['status'] in {'running','interrupted'}:
                    raise Conflict('工作仍活动或待恢复。')
                if db.execute("SELECT 1 FROM chat_tool_effects WHERE run_id=? AND (status!='completed' OR json_extract(result,'$[1]')='unknown')",(run['id'],)).fetchone():
                    raise Conflict('工具副作用未知。')
                cp=db.execute('SELECT state FROM chat_run_checkpoints WHERE run_id=?',(run['id'],)).fetchone()
                if cp:
                    state=json.loads(cp[0])
                    if state.get('evidence',{}).get('unknown_failures') or state.get('pending'):
                        raise Conflict('执行结果或工具交换尚未解决。')
                if any(s['status'] in {'running','cancelling','unknown'} for s in self.app.sandbox.run_sessions(row['agent_id'],run['id'],'chat')):
                    raise Conflict('执行会话尚未安全结束。')
            # Explicit linked tasks preserve existing application/recovery fences.
            tasks=db.execute('SELECT t.* FROM tasks t JOIN work_event_task_links l ON l.task_id=t.id '
                             'WHERE l.conversation_id=?',(row['conversation_id'],)).fetchall()
            for task in tasks:
                if task['status'] not in {'completed','needs_review','cancelled'}:
                    raise Conflict('关联任务仍活动。')
                if task['workspace'] and not db.execute("SELECT 1 FROM task_reviews WHERE task_id=? AND status='applied'",(task['id'],)).fetchone():
                    raise Conflict('关联任务尚未交付。')
                if db.execute("SELECT 1 FROM task_steps WHERE task_id=? AND status IN ('executing','unknown')",(task['id'],)).fetchone():
                    raise Conflict('关联任务副作用未知。')
                if db.execute("SELECT 1 FROM task_reviews WHERE task_id=? AND status!='applied' AND json_extract(result,'$.replacement_review_id') IS NULL",(task['id'],)).fetchone():
                    raise Conflict('关联任务有待核对交付。')
        return runs

    @staticmethod
    def bump(db,bid):
        db.execute('UPDATE continuity_work_branches SET revision=revision+1,updated_at=? WHERE id=?',(utc_now(),bid))


def public(row):
    return {k:v for k,v in dict(row).items() if k not in {'members','background','grants','return_report'}}
