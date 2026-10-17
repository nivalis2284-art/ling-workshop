"""Durable dependency union for actual sent history, independent of checkpoints."""
import json

from lingnexus.common.errors import Conflict, NotFound
from lingnexus.common.types import utc_now

from . import sources
from .store import encode


class Dependencies:
    def __init__(self, owner):
        self.owner, self.app, self.database = owner, owner.app, owner.database

    def message(self, message_id):
        with self.database.transaction() as db:
            rows=db.execute('SELECT experience_id,revision FROM continuity_answer_dependencies '
                            'WHERE assistant_message_id=?',(message_id,)).fetchall()
        return {r['experience_id']:r['revision'] for r in rows}

    def capture(self, run_id, message_ids):
        with self.database.transaction() as db:
            run=db.execute('SELECT * FROM chat_runs WHERE id=?',(run_id,)).fetchone()
            if run is None:
                raise NotFound('运行不存在。')
            for mid in message_ids:
                if mid.startswith('continuity-invalid:'):
                    continue
                if mid.startswith('continuity-derived:'):
                    mid=mid.removeprefix('continuity-derived:')
                if mid.startswith('continuity-experience:'):
                    eid=mid.removeprefix('continuity-experience:').rsplit(':',1)[0]
                    item=self.owner.experiences.get(eid,agent_id=run['agent_id'])
                    self._experience(db,run_id,item['id'],item['revision'])
                    continue
                if mid.startswith('continuity-fork:'):
                    branch=self.owner.branch_for_conversation(run['conversation_id'])
                    if not branch or branch['id']!=mid.removeprefix('continuity-fork:'):
                        raise Conflict('fork 来源不属于本次运行。')
                    self.owner.assert_fork(branch)
                    # Persist the union conservatively; all refs are <=16KiB of
                    # background metadata and cannot grant another conversation.
                    for source in json.loads(branch['background']):
                        self._source(db,run_id,source)
                    continue
                if mid.startswith(('event:', 'receipt:', 'invalidated:')):
                    continue
                row=db.execute('SELECT conversation_id FROM conversation_messages WHERE id=?',(mid,)).fetchone()
                if row is None or row['conversation_id']!=run['conversation_id']:
                    raise Conflict('运行来源不在调用交流。')
                for eid,rev in self.message(mid).items():
                    self._experience(db,run_id,eid,rev)
            return {'run_id':run_id,'count':len(message_ids)}

    def _source(self,db,run_id,source):
        db.execute('INSERT OR IGNORE INTO continuity_run_sources VALUES(?,?,?,?,?,?)',
                   (run_id,'frozen',encode(source),source['epoch'],source['sha256'],utc_now()))

    def _experience(self,db,run_id,eid,rev):
        db.execute('INSERT OR IGNORE INTO continuity_run_sources VALUES(?,?,?,?,?,?)',
                   (run_id,'experience',eid,rev,None,utc_now()))

    def assert_current(self, run_id):
        with self.database.transaction() as db:
            run=db.execute('SELECT * FROM chat_runs WHERE id=?',(run_id,)).fetchone()
            if run is None:
                raise NotFound('运行不存在。')
            rows=db.execute('SELECT * FROM continuity_run_sources WHERE run_id=?',(run_id,)).fetchall()
            for row in rows:
                if row['source_kind']=='frozen':
                    sources.validate(self.app,run['agent_id'],json.loads(row['ref']))
                elif row['source_kind']=='experience':
                    item=self.owner.experiences.get(row['ref'],agent_id=run['agent_id'])
                    self.owner.assert_experience_access(item,conversation_id=run['conversation_id'])
                    if not item['valid'] or item['revision']!=row['revision']:
                        raise Conflict('经历来源已撤回或改变。')

    def inherit(self, run_id, assistant_message_id):
        if assistant_message_id is None:
            return
        self.assert_current(run_id)
        with self.database.transaction() as db:
            rows=db.execute("SELECT ref,revision FROM continuity_run_sources WHERE run_id=? AND source_kind='experience'",
                            (run_id,)).fetchall()
            reads=db.execute('SELECT experience_id AS ref,experience_revision AS revision '
                             'FROM continuity_experience_reads WHERE run_id=?',(run_id,)).fetchall()
            for row in [*rows,*reads]:
                db.execute('INSERT OR IGNORE INTO continuity_answer_dependencies VALUES(?,?,?,?,?)',
                           (run_id,assistant_message_id,row['ref'],row['revision'],utc_now()))
