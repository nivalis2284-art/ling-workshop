"""Independent work conversations and durable archive/return transitions."""
import json

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied, RevisionConflict
from lingnexus.common.types import new_id, utc_now

from . import sources
from .store import digest, encode

ACTIVE = ('starting','running','awaiting_approval','paused','finishing','unknown','pending_review','interrupted')
TERMINAL = {'archived','cancelled','failed'}
DORMANT = {'dormant','archived'}
HISTORY_LIMIT=100
HISTORY_KINDS={'task_state','episode','archive','guidance'}


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
        return self.public_branch(self.row(branch_id))

    def episode_for_run(self,branch_id,run_id):
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_work_episodes WHERE branch_id=? AND run_id=?',
                           (branch_id,run_id)).fetchone()
        return dict(row) if row else None

    def public_branch(self,row):
        value=public(row)
        with self.database.transaction() as db:
            archived=db.execute('SELECT archive_id,episode_id,experience_id,outcome,archive_version,created_at '
                                'FROM continuity_episode_archives WHERE branch_id=? ORDER BY created_at DESC,archive_id DESC LIMIT 1',
                                (row['id'],)).fetchone()
        value['latest_archive']=dict(archived) if archived else None
        return value

    def list(self,agent_id):
        self.app.permissions.current(agent_id)
        with self.database.transaction() as db:
            rows=db.execute(
                'SELECT b.*,a.archive_id AS latest_archive_id,a.episode_id AS latest_archive_episode_id, '
                'a.experience_id AS latest_archive_experience_id,a.outcome AS latest_archive_outcome, '
                'a.archive_version AS latest_archive_version,a.created_at AS latest_archive_created_at '
                'FROM continuity_work_branches b LEFT JOIN continuity_episode_archives a ON a.archive_id=('
                ' SELECT candidate.archive_id FROM continuity_episode_archives candidate '
                ' WHERE candidate.branch_id=b.id '
                ' ORDER BY candidate.created_at DESC,candidate.archive_id DESC LIMIT 1) '
                'WHERE b.agent_id=? ORDER BY b.created_at,b.id',(agent_id,)).fetchall()
        return [public_list_branch(row) for row in rows]

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
                return self.public_branch(old)
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
            episode_id='episode:'+new_id()
            state_revision=1
            db.execute('INSERT INTO continuity_work_episodes(id,branch_id,ordinal,status,opened_at,task_state_revision,source_epoch)'
                       " VALUES(?,?,1,'active',?,1,?)",(episode_id,bid,now,version))
            db.execute('UPDATE continuity_work_branches SET current_episode_id=?,task_state_revision=? WHERE id=?',(episode_id,state_revision,bid))
            append_task_state(db,bid,state_revision,objective=objective,progress=['工作分支已建立独立执行上下文。'],grants=grants,run_id=None)
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
                raise Conflict('工作已经休眠；请先唤醒同一个工作分支。')
            if row['status'] in {'unknown','pending_review','awaiting_approval','finishing'}:
                raise Conflict('工作仍有未知结果、待核交付或待审批操作，不能继续投递指导。')
            if row['status']=='interrupted':
                raise Conflict('原运行仍需恢复或人工核对，请先处理中断状态。')
            if row['status']=='paused':
                # Starting a successor is the same lifecycle decision as wake:
                # it must not walk past an unresolved checkpoint or unknown
                # effect. Live guidance on a running loop remains append-only.
                self.safe(row)
                self.assert_fork(row)
                episode_id=self._open_successor(db,row)
                row=dict(row) | {'current_episode_id':episode_id,'run_id':None}
                db.execute("UPDATE continuity_work_branches SET status='queued',run_id=NULL,current_episode_id=?,"
                           "finished_at=NULL,error_code=NULL,finish_requested=0,updated_at=? WHERE id=?",
                           (episode_id,utc_now(),bid))
            else:
                episode_id=row['current_episode_id']
            db.execute('INSERT INTO continuity_work_guidance(id,branch_id,request_id,fingerprint,text,created_at) VALUES(?,?,?,?,?,?)',
                       (new_id(),bid,request_id,digest(text),text,utc_now()))
            db.execute('UPDATE continuity_work_guidance SET episode_id=? WHERE branch_id=? AND request_id=?',
                       (episode_id,bid,request_id))
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

    def task_state(self, branch_id):
        with self.database.transaction() as db:
            self.row(branch_id)
            row=db.execute('SELECT * FROM continuity_work_task_states WHERE branch_id=? ORDER BY revision DESC LIMIT 1',
                           (branch_id,)).fetchone()
        return public_task_state(row) if row else None

    def guidance(self, branch_id, *, limit=None, before=None):
        """Owner-visible guidance delivery state for this branch."""
        with self.database.transaction() as db:
            self.row(branch_id)
            version=parse_history_cursor(before,'guidance')
            if before is None:
                rows=db.execute('SELECT id,episode_id,request_id,text,delivery,created_at,delivered_at,consumed_at '
                                'FROM continuity_work_guidance WHERE branch_id=? ORDER BY created_at,id',
                                (branch_id,)).fetchall() if limit is None else db.execute(
                    'SELECT * FROM (SELECT id,episode_id,request_id,text,delivery,created_at,delivered_at,consumed_at '
                    'FROM continuity_work_guidance WHERE branch_id=? ORDER BY created_at DESC,id DESC LIMIT ?) '
                    'ORDER BY created_at,id',(branch_id,_page_size(limit))).fetchall()
            else:
                assert version is not None
                rows=db.execute('SELECT * FROM (SELECT id,episode_id,request_id,text,delivery,created_at,delivered_at,consumed_at '
                                'FROM continuity_work_guidance WHERE branch_id=? AND '
                                '(created_at<? OR (created_at=? AND id<?)) ORDER BY created_at DESC,id DESC LIMIT ?) '
                                'ORDER BY created_at,id',(branch_id,version[0],version[0],version[1],_page_size(limit))).fetchall()
        return [dict(row) for row in rows]

    def task_states(self, branch_id, *, limit=None, before=None):
        with self.database.transaction() as db:
            self.row(branch_id)
            version=parse_history_cursor(before,'task_state')
            if before is None:
                rows=db.execute('SELECT * FROM continuity_work_task_states WHERE branch_id=? ORDER BY revision',
                                (branch_id,)).fetchall() if limit is None else db.execute(
                    'SELECT * FROM (SELECT * FROM continuity_work_task_states WHERE branch_id=? '
                    'ORDER BY revision DESC LIMIT ?) ORDER BY revision',
                    (branch_id,_page_size(limit))).fetchall()
            else:
                assert version is not None
                rows=db.execute('SELECT * FROM (SELECT * FROM continuity_work_task_states WHERE branch_id=? '
                                'AND revision<? ORDER BY revision DESC LIMIT ?) ORDER BY revision',
                                (branch_id,version,_page_size(limit))).fetchall()
        return [public_task_state(row) for row in rows]

    def episodes(self, branch_id, *, limit=None, before=None):
        with self.database.transaction() as db:
            self.row(branch_id)
            version=parse_history_cursor(before,'episode')
            if before is None:
                rows=db.execute('SELECT * FROM continuity_work_episodes WHERE branch_id=? ORDER BY ordinal',
                                (branch_id,)).fetchall() if limit is None else db.execute(
                    'SELECT * FROM (SELECT * FROM continuity_work_episodes WHERE branch_id=? '
                    'ORDER BY ordinal DESC LIMIT ?) ORDER BY ordinal',
                    (branch_id,_page_size(limit))).fetchall()
            else:
                assert version is not None
                rows=db.execute('SELECT * FROM (SELECT * FROM continuity_work_episodes WHERE branch_id=? '
                                'AND ordinal<? ORDER BY ordinal DESC LIMIT ?) ORDER BY ordinal',
                                (branch_id,version,_page_size(limit))).fetchall()
        return [dict(row) for row in rows]

    def archives(self, branch_id, *, limit=None, before=None):
        with self.database.transaction() as db:
            self.row(branch_id)
            version=parse_history_cursor(before,'archive')
            if before is None:
                rows=db.execute('SELECT archive_id,branch_id,episode_id,experience_id,outcome,archive_version,'
                                'head_id,members_digest,report,created_at FROM continuity_episode_archives '
                                'WHERE branch_id=? ORDER BY created_at,archive_id',(branch_id,)).fetchall() if limit is None else db.execute(
                    'SELECT * FROM (SELECT archive_id,branch_id,episode_id,experience_id,outcome,archive_version,'
                    'head_id,members_digest,report,created_at FROM continuity_episode_archives '
                    'WHERE branch_id=? ORDER BY created_at DESC,archive_id DESC LIMIT ?) '
                    'ORDER BY created_at,archive_id',(branch_id,_page_size(limit))).fetchall()
            else:
                assert version is not None
                rows=db.execute('SELECT * FROM (SELECT archive_id,branch_id,episode_id,experience_id,outcome,archive_version,'
                                'head_id,members_digest,report,created_at FROM continuity_episode_archives '
                                'WHERE branch_id=? AND (created_at<? OR (created_at=? AND archive_id<?)) '
                                'ORDER BY created_at DESC,archive_id DESC LIMIT ?) ORDER BY created_at,archive_id',
                                (branch_id,version[0],version[0],version[1],_page_size(limit))).fetchall()
        return [dict(row) for row in rows]

    def record_task_state(self, branch_id, run_id, *, decisions=None, progress=None,
                          unfinished=None, verified=None, unknowns=None, artifacts=None, sources=None):
        """Append an evidence-linked understanding for the current run.

        The model supplies only work facts; identity, grant snapshot and episode
        are read from the active branch and run. Empty strings are rejected so a
        task-state version cannot claim a decision without content.
        """
        fields={'decisions':decisions,'progress':progress,'unfinished':unfinished,
                'verified':verified,'unknowns':unknowns,'artifacts':artifacts,'sources':sources}
        cleaned={}
        for name,items in fields.items():
            if items is None:
                continue
            if not isinstance(items,list) or len(items)>64 or any(not isinstance(item,str) or not item.strip() or len(item.encode())>2048 for item in items):
                raise ValueError('任务状态条目无效。')
            cleaned[name]=[item.strip() for item in items]
        with self.database.transaction() as db:
            row=self.row(branch_id)
            episode=db.execute('SELECT * FROM continuity_work_episodes WHERE branch_id=? AND run_id=?',
                               (branch_id,run_id)).fetchone()
            if episode is None or episode['status']!='active':
                raise PermissionDenied('任务状态只能由活动工作阶段记录。')
            run=db.execute('SELECT * FROM chat_runs WHERE id=?',(run_id,)).fetchone()
            if run is None or run['status'] not in {'running','completed'} or run['conversation_id']!=row['conversation_id']:
                raise PermissionDenied('任务状态必须绑定当前活动运行。')
            previous=db.execute('SELECT * FROM continuity_work_task_states WHERE branch_id=? ORDER BY revision DESC LIMIT 1',
                                (branch_id,)).fetchone()
            revision=(previous['revision']+1) if previous else 1
            decisions=list(json.loads(previous['decisions'])) if previous else []
            progress=list(json.loads(previous['progress'])) if previous else []
            unfinished=list(json.loads(previous['unfinished'])) if previous else []
            verified=list(json.loads(previous['verified'])) if previous else []
            unknowns=list(json.loads(previous['unknowns'])) if previous else []
            artifacts=list(json.loads(previous['artifacts'])) if previous else []
            sources=list(json.loads(previous['sources'])) if previous else []
            updates={'decisions':decisions,'progress':progress,'unfinished':unfinished,
                     'verified':verified,'unknowns':unknowns,'artifacts':artifacts,'sources':sources}
            for name,items in cleaned.items():
                updates[name]=items
            append_task_state(db,branch_id,revision,objective=row['objective'],grants=json.loads(row['grants']),
                              run_id=run_id,**updates)
            db.execute('UPDATE continuity_work_branches SET task_state_revision=? WHERE id=?',(revision,branch_id))
            db.execute('UPDATE continuity_work_episodes SET task_state_revision=? WHERE id=?',
                       (revision,episode['id']))
            self.bump(db,branch_id)
        return self.task_state(branch_id)

    def settle_episode(self,row,status,reason=None,*,run_id=None):
        """Record a real run boundary without calling it task completion.

        A normal model finish ends this awake episode but leaves the branch
        paused and resumable. Only an explicit finish request can archive it.
        """
        if row['status'] in TERMINAL:
            return self.get(row['id'])
        now=utc_now()
        with self.database.transaction() as db:
            current=db.execute('SELECT * FROM continuity_work_branches WHERE id=?',(row['id'],)).fetchone()
            if current is None or current['status'] in TERMINAL:
                return self.get(row['id'])
            if status in {'queued','paused'}:
                db.execute('UPDATE continuity_work_episodes SET status=\'paused\',reason=?,closed_at=? WHERE id=? AND status=\'active\'',
                           (reason,now,current['current_episode_id']))
            else:
                db.execute('UPDATE continuity_work_episodes SET status=?,reason=?,closed_at=? WHERE id=? AND status=\'active\'',
                           (status,reason,now,current['current_episode_id']))
            pending=db.execute("SELECT 1 FROM continuity_work_guidance WHERE branch_id=? AND consumed_at IS NULL "
                               "AND delivery IN ('queued','delivered') LIMIT 1",(row['id'],)).fetchone()
            if (reason=='round_completed' and not current['finish_requested'] and pending is not None):
                db.execute("UPDATE continuity_work_guidance SET delivery='queued',delivered_at=NULL "
                           "WHERE branch_id=? AND consumed_at IS NULL AND delivery='delivered'",(row['id'],))
                episode_id=self._open_successor(db,dict(current))
                db.execute("UPDATE continuity_work_branches SET status='queued',run_id=NULL,current_episode_id=?,"
                           "error_code=NULL,updated_at=? WHERE id=?",(episode_id,now,row['id']))
            else:
                db.execute('UPDATE continuity_work_branches SET status=?,error_code=?,updated_at=? WHERE id=?',
                           (status,reason,now,row['id']))
            self.bump(db,row['id'])
        return self.get(row['id'])

    def wake(self,bid,expected_revision):
        """Turn dormant work back into queued successor work, preserving history."""
        with self.database.transaction() as db:
            row=self.row(bid)
            self.revision(row,expected_revision)
            if row['status'] not in DORMANT and row['status'] not in {'paused','interrupted'}:
                raise Conflict('工作仍在执行、等待核对或已经取消，不能直接唤醒。')
            self.safe(row)
            episode_id=self._open_successor(db,row)
            db.execute("UPDATE continuity_work_branches SET status='queued',run_id=NULL,"
                       "finished_at=NULL,error_code=NULL,finish_requested=0,outcome=NULL,"
                       "current_episode_id=?,updated_at=? WHERE id=?",(episode_id,utc_now(),bid))
            self.bump(db,bid)
        return self.get(bid)

    def _open_successor(self,db,row):
        latest=db.execute('SELECT * FROM continuity_work_episodes WHERE branch_id=? ORDER BY ordinal DESC LIMIT 1',
                          (row['id'],)).fetchone()
        state_revision=row['task_state_revision'] or 1
        if latest is not None and latest['status']=='active' and latest['run_id'] is None:
            episode_id=latest['id']
            db.execute('UPDATE continuity_work_episodes SET task_state_revision=? WHERE id=?',
                       (state_revision,episode_id))
        else:
            episode_id='episode:'+new_id()
            db.execute('INSERT INTO continuity_work_episodes(id,branch_id,ordinal,predecessor_id,status,opened_at,task_state_revision,source_epoch)'
                       ' VALUES(?,?,?,?,\'active\',?,?,?)',
                       (episode_id,row['id'],(latest['ordinal']+1) if latest else 1,
                        latest['id'] if latest else None,utc_now(),state_revision,
                        _source_epoch(db,row['conversation_id'])))
        # Guidance is addressed to an episode because the runtime can consume
        # only rows bound to the run's current episode. Once an episode is no
        # longer awake, carry exactly its undelivered history into the successor.
        # Consumed rows and unknown deliveries stay attached to their evidence.
        db.execute("UPDATE continuity_work_guidance SET episode_id=?,delivery='queued',delivered_at=NULL "
                   "WHERE branch_id=? AND consumed_at IS NULL AND delivery IN ('queued','delivered')",
                   (episode_id,row['id']))
        return episode_id

    @staticmethod
    def bump(db,bid):
        db.execute('UPDATE continuity_work_branches SET revision=revision+1,updated_at=? WHERE id=?',(utc_now(),bid))


def append_task_state(db,branch_id,revision,*,objective,decisions=None,progress=None,unfinished=None,
                      verified=None,unknowns=None,artifacts=None,sources=None,grants,run_id):
    value={
        'branch_id':branch_id,'revision':revision,'objective':objective,
        'decisions':decisions or [],'progress':progress or [],'unfinished':unfinished or [],
        'verified':verified or [],'unknowns':unknowns or [],'artifacts':artifacts or [],
        'sources':sources or [],'grants':grants,'run_id':run_id,
    }
    db.execute('INSERT INTO continuity_work_task_states(branch_id,revision,objective,decisions,progress,'
               'unfinished,verified,unknowns,artifacts,sources,grants,digest,run_id,created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (branch_id,revision,objective,encode(value['decisions']),encode(value['progress']),
                encode(value['unfinished']),encode(value['verified']),encode(value['unknowns']),
                encode(value['artifacts']),encode(value['sources']),encode(grants),digest(value),run_id,utc_now()))


def public_task_state(row):
    value=dict(row)
    for field in ('decisions','progress','unfinished','verified','unknowns','artifacts','sources','grants'):
        value[field]=json.loads(value[field]) if value.get(field) else ({} if field=='grants' else [])
    return value


def public_list_branch(row):
    """Build a public branch from the joined list query without an N+1 read."""
    archive = None
    if row['latest_archive_id'] is not None:
        archive = {
            'archive_id': row['latest_archive_id'],
            'episode_id': row['latest_archive_episode_id'],
            'experience_id': row['latest_archive_experience_id'],
            'outcome': row['latest_archive_outcome'],
            'archive_version': row['latest_archive_version'],
            'created_at': row['latest_archive_created_at'],
        }
    value=public({key:item for key,item in dict(row).items() if not key.startswith('latest_archive_')})
    value['latest_archive']=archive
    return value


def _page_size(limit):
    if limit is None:
        return HISTORY_LIMIT
    if type(limit) is not int or limit<1 or limit>HISTORY_LIMIT:
        raise ValueError('历史读取条数无效。')
    return limit


def parse_history_cursor(value,kind):
    """Decode an opaque keyset position; malformed cursors never fall back."""
    if value is None:
        return None
    if kind not in HISTORY_KINDS:
        raise ValueError('历史游标类型无效。')
    try:
        encoded=json.loads(value)
        if not isinstance(encoded,dict) or encoded.get('kind')!=kind:
            raise ValueError
        if kind in {'task_state','episode'}:
            revision=encoded['revision']
            if type(revision) is not int or revision<1:
                raise ValueError
            return revision
        created_at=encoded['created_at']
        identity=encoded['id']
        if (not isinstance(created_at,str) or not created_at or len(created_at)>64
                or not isinstance(identity,str) or not identity or len(identity)>200):
            raise ValueError
        return created_at,identity
    except (KeyError,TypeError,ValueError,json.JSONDecodeError):
        raise ValueError('历史游标无效。') from None


def _source_epoch(db,conversation_id):
    row=db.execute('SELECT epoch FROM continuity_source_versions WHERE conversation_id=?',(conversation_id,)).fetchone()
    return row[0] if row else 0


def public(row):
    value={k:v for k,v in dict(row).items() if k not in {'members','background','grants','return_report'}}
    return value
