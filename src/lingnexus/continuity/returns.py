"""Archive creation and outbox publication, separately versioned and idempotent."""
import json

from lingnexus.common.errors import Conflict, DomainError
from lingnexus.common.types import new_id, utc_now

from . import sources
from .store import digest, encode
from .work import append_task_state

MAX_SOURCES=4096


class Returns:
    def __init__(self,owner):
        self.owner,self.app,self.database=owner,owner.app,owner.database

    def archive(self,bid):
        work=self.owner.work_domain
        row=work.row(bid)
        if row['archive_id']:
            return work.get(bid)
        if not row['finish_requested']:
            raise Conflict('尚未请求结束工作。')
        runs=work.safe(row)
        if not runs and row['outcome']!='cancel_requested':
            raise Conflict('工作尚无执行证据。')
        work.assert_fork(row)
        nodes,version=sources.path_metadata(self.app,row['conversation_id'])
        with self.database.transaction() as db:
            records=db.execute('SELECT * FROM work_event_records WHERE conversation_id=? ORDER BY rowid',
                               (row['conversation_id'],)).fetchall()
        if len(nodes)+len(records)>MAX_SOURCES:
            raise Conflict('工作来源数量超过本次安全归档上限。')
        scope=sources.scope(self.app,row['agent_id'])
        snapshots=[]
        for node in nodes:
            snapshots.append({'kind':'message','ref':{'id':node['id']},'conversation_id':row['conversation_id'],
                              'epoch':version,'grants':scope,'sha256':sources.hash_body(self.app,'message',{'id':node['id']}),
                              'byte_count':node['byte_count'],'occurred_at':sources.stamp(node['created_at'])})
            with self.database.transaction() as db:
                attachments=db.execute('SELECT attachment_id FROM conversation_attachments WHERE message_id=? ORDER BY ordinal',(node['id'],)).fetchall()
            for attachment in attachments:
                snapshots.append(sources.snapshot(self.app,row['agent_id'],'attachment',
                                 {'id':attachment[0],'message_id':node['id']}))
        for rec in records:
            grants=json.loads(rec['grants'])
            grants.pop('captured_from',None)
            if grants!=scope:
                raise Conflict('工作安全记录的原始授权不匹配。')
            snapshots.append({'kind':'record','ref':{'id':rec['id']},'conversation_id':row['conversation_id'],
                              'epoch':version,'grants':grants,'sha256':rec['sha256'],
                              'byte_count':rec['byte_count'],'occurred_at':sources.stamp(rec['created_at'])})
        if len(snapshots)>MAX_SOURCES:
            raise Conflict('工作来源过多。')
        now=utc_now()
        outcome='cancelled' if row['outcome']=='cancel_requested' else ('failed' if any(r['status']!='completed' for r in runs) else 'completed_unverified')
        final=''
        if nodes:
            final=sources.read_page(self.app,'message',{'id':nodes[-1]['id']},max_bytes=1024)['text']
        occurred_start=nodes[0]['created_at'] if nodes else row['created_at']
        occurred_end=nodes[-1]['created_at'] if nodes else now
        archive_id=new_id()
        archive_eid='work-archive:'+archive_id
        report=f"【工作回归】{occurred_start} — {occurred_end}\n目标：{row['objective']}\n执行状态：{outcome}；交付：无自动交付声明。\n运行最终陈述（未经独立事实背书）：{final}\n本次归档经历：{archive_eid}；分支：{bid}。需要证据时用 experience_records/experience_read。"
        with self.database.transaction() as db:
            current=work.row(bid)
            if current['archive_id']:
                return work.get(bid)
            if current['revision']!=row['revision'] or sources.epoch(db,row['conversation_id'])!=version:
                raise Conflict('归档期间工作已变化。')
            work.safe(current)
            work.assert_fork(current)
            binding=self.owner.main.binding(row['agent_id'])
            db.execute('INSERT INTO continuity_experiences(id,agent_id,kind,title,summary,revision,coverage,'
                       'valid,conversation_id,branch_id,occurred_start,occurred_end,created_at,updated_at) '
                       "VALUES(?,?,'work_branch',?,?,1,'retained',1,?,?,?,?,?,?)",
                       (archive_eid,row['agent_id'],row['objective'][:500],report,row['conversation_id'],bid,
                        occurred_start,occurred_end,now,now))
            for ordinal,s in enumerate(snapshots):
                sources.validate(self.app,row['agent_id'],s)
                db.execute('INSERT INTO continuity_experience_sources VALUES(?,?,?,?,?,?,?,?,?)',
                           (archive_eid,s['kind']+':'+s['ref']['id'],s['kind'],encode(s['ref']),ordinal,s['byte_count'],s['sha256'],'retained',encode(s)))
            members_digest=digest([n['id'] for n in nodes])
            result_revision=(current['result_revision'] or 0)+1
            return_id=new_id()
            db.execute('INSERT INTO continuity_branch_returns(id,branch_id,result_revision,archive_id,conversation_id,head_id,'
                       'members_digest,archive_version,target_conversation_id,target_binding_revision,anchor_id,report,status,created_at) '
                       "VALUES(?,?,?,?,?,?,?,1,?,?,?,?,'ready',?)",
                       (return_id,bid,result_revision,archive_id,row['conversation_id'],nodes[-1]['id'] if nodes else None,members_digest,
                        row['main_conversation_id'],binding['revision'],row['origin_message_id'],report,now))
            db.execute("UPDATE continuity_work_branches SET status='archived',outcome=?,archive_id=?,archive_version=1,"
                       "result_revision=?,return_state='pending',delivery_state='none',finished_at=?,return_report=? WHERE id=?",
                       (outcome,archive_id,result_revision,now,report,bid))
            db.execute("UPDATE continuity_work_episodes SET status='archived',reason=?,closed_at=? WHERE id=?",
                       (outcome,now,row['current_episode_id']))
            db.execute('INSERT INTO continuity_episode_archives(archive_id,branch_id,episode_id,experience_id,return_id,outcome,archive_version,head_id,members_digest,report,created_at)'
                       ' VALUES(?,?,?,?,?,?,1,?,?,?,?)',
                       (archive_id,bid,row['current_episode_id'],archive_eid,return_id,outcome,
                        nodes[-1]['id'] if nodes else None,members_digest,report,now))
            previous=db.execute('SELECT * FROM continuity_work_task_states WHERE branch_id=? ORDER BY revision DESC LIMIT 1',
                                (bid,)).fetchone()
            episode=db.execute('SELECT * FROM continuity_work_episodes WHERE id=?',
                               (current['current_episode_id'],)).fetchone()
            archived_progress=list(json.loads(previous['progress'])) if previous else []
            archived_progress.append('本轮工作已归档，历史原文与结果保留。')
            append_task_state(db,bid,(previous['revision']+1) if previous else 1,objective=current['objective'],
                              decisions=json.loads(previous['decisions']) if previous else [],
                              progress=archived_progress,
                              unfinished=json.loads(previous['unfinished']) if previous else [],
                              verified=json.loads(previous['verified']) if previous else [],
                              unknowns=json.loads(previous['unknowns']) if previous else [],
                              artifacts=json.loads(previous['artifacts']) if previous else [],
                              sources=json.loads(previous['sources']) if previous else [],
                              grants=json.loads(current['grants']),run_id=current['run_id'])
            db.execute('UPDATE continuity_work_branches SET task_state_revision=? WHERE id=?',(previous['revision']+1 if previous else 1,bid))
            if episode is not None:
                db.execute('UPDATE continuity_work_episodes SET task_state_revision=? WHERE id=?',
                           (previous['revision']+1 if previous else 1,episode['id']))
            work.bump(db,bid)
        return work.get(bid)

    def publish(self,*,limit=16):
        with self.database.transaction() as db:
            rows=db.execute("SELECT * FROM continuity_branch_returns WHERE status='ready' ORDER BY created_at,id LIMIT ?",(limit,)).fetchall()
        published=[]
        for candidate in rows:
            try:
                with self.database.transaction() as db:
                    item=db.execute('SELECT * FROM continuity_branch_returns WHERE id=?',(candidate['id'],)).fetchone()
                    if item['status']!='ready':
                        continue
                    row=self.owner.work_domain.row(item['branch_id'])
                    self.owner.assert_fork(row)
                    binding=self.owner.main.binding(row['agent_id'])
                    if binding['conversation_id']!=item['target_conversation_id'] or binding['revision']!=item['target_binding_revision']:
                        raise Conflict('主干绑定已变化。')
                    if db.execute("SELECT 1 FROM chat_runs WHERE conversation_id=? AND status='running'",(item['target_conversation_id'],)).fetchone():
                        continue
                    main=self.app.conversations.read(item['target_conversation_id'])
                    # All ancestry/permission mutations are fenced by assert_fork.
                    message=self.app.conversations.append_assistant(main['id'],item['report'],
                             request_id='continuity-return:'+item['id'],expected_head=main['head_id'])
                    eid=db.execute('SELECT e.id AS id,e.revision AS revision '
                                   'FROM continuity_episode_archives a JOIN continuity_experiences e '
                                   'ON e.id=a.experience_id WHERE a.archive_id=?',(item['archive_id'],)).fetchone()
                    # run_id is nullable for outbox reports: there is no fake chat run.
                    db.execute('INSERT OR IGNORE INTO continuity_answer_dependencies VALUES(?,?,?,?,?)',
                               ('return:'+item['id'],message['id'],eid['id'],eid['revision'],utc_now()))
                    db.execute("UPDATE continuity_branch_returns SET status='published',published_message_id=?,published_at=? WHERE id=?",
                               (message['id'],utc_now(),item['id']))
                    db.execute("UPDATE continuity_work_branches SET return_state='published' "
                               "WHERE id=? AND archive_id=? AND result_revision=?",
                               (row['id'],item['archive_id'],item['result_revision']))
                    published.append(message['id'])
            except DomainError:
                with self.database.transaction() as db:
                    db.execute("UPDATE continuity_branch_returns SET status='blocked',reason='source_or_binding_invalid' WHERE id=?",(candidate['id'],))
                    db.execute("UPDATE continuity_work_branches SET return_state='blocked' "
                               "WHERE id=? AND archive_id=? AND result_revision=?",
                               (candidate['branch_id'],candidate['archive_id'],candidate['result_revision']))
        return published
