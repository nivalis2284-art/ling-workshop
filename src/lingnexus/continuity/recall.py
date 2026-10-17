"""Experience recall reuses EventRecall's measured native-envelope fitter."""
from lingnexus.common.errors import Conflict, DomainError
from lingnexus.common.types import new_id, utc_now
from lingnexus.work_events.recall import (
    MIN_PAGE_BYTES,
    RUN_BODY_LIMIT,
    RUN_PAGE_LIMIT,
    EventRecall,
    _budget_view,
    _completed,
    _envelope,
    _map_error,
    _refuse,
)


class _Adapter:
    def __init__(self,recall):
        self.recall=recall

    def get(self,eid,**kwargs):
        item=self.recall.app.continuity.experiences.get(eid,agent_id=kwargs.get('agent_id'))
        self.recall.app.continuity.assert_experience_access(item,conversation_id=kwargs.get('conversation_id'))
        return item

    def directory(self,eid,**kwargs):
        return dict(self.recall.app.continuity.experience_records(eid,**kwargs),event_id=eid)

    def read_record(self,eid,sid,**kwargs):
        return dict(self.recall.app.continuity.read_experience_record(eid,sid,**kwargs),event_id=eid,location=sid)


class ExperienceRecall(EventRecall):
    def directory(self,args):
        return super().directory(dict(args,event_id=args['experience_id']))

    def read(self,args):
        return super().read(dict(args,event_id=args['experience_id']))

    def _admit(self,kind,event_id,source_id,cursor,page_size):
        scope=self._scope()
        if scope is None:
            return {'refusal':_refuse('permission_denied','run_not_active','当前运行不可读取经历。')}
        adapter=_Adapter(self)
        try:
            event=adapter.get(event_id,agent_id=self.agent_id,conversation_id=scope['conversation_id'])
            if not event['valid']:
                raise Conflict('来源已失效。')
        except DomainError as error:
            return {'refusal':_map_error(error)}
        budget=self._budget()
        if budget is None:
            return {'refusal':_refuse('budget_exhausted','no_checkpoint','没有实时预算，拒绝正文。')}
        state={'event':event,'events':adapter,'conversation_id':scope['conversation_id'],'budget':budget}
        repeat=self._repeat(state,kind=kind,source_id=source_id,cursor=cursor,page_size=page_size)
        if budget['remaining']<MIN_PAGE_BYTES or (repeat is None and (
                budget['pages_used']>=RUN_PAGE_LIMIT or (kind=='record' and budget['body_used']>=RUN_BODY_LIMIT))):
            return {'refusal':_refuse('budget_exhausted','run_or_window_limit','已达到回读预算。',**budget)}
        return dict(state,repeat=repeat)

    def _budget(self):
        budget=super()._budget()
        if budget is None:
            return None
        with self.app.database.transaction() as db:
            rows=db.execute('SELECT * FROM continuity_experience_reads WHERE run_id=?',(self.run_id,)).fetchall()
        budget['remaining']-=sum(r['envelope_bytes'] for r in rows if r['model_step']>=budget['model_step'])
        budget['pages_used']+=len(rows)
        budget['body_used']+=sum(r['byte_count'] for r in rows if r['kind']=='record')
        return budget

    def _repeat(self,state,*,kind,source_id,cursor,page_size):
        with self.app.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_experience_reads WHERE run_id=? AND kind=? AND experience_id=? '
                           'AND experience_revision=? AND source_id=? AND cursor=? AND page_size=?',
                           (self.run_id,kind,state['event']['id'],state['event']['revision'],source_id,cursor,page_size)).fetchone()
        return dict(row) if row else None

    def _ledger(self,state,*,kind,source_id,cursor,next_cursor,sha256,byte_count,page_size,envelope_bytes):
        current=_Adapter(self).get(state['event']['id'],agent_id=self.agent_id,conversation_id=state['conversation_id'])
        if current['revision']!=state['event']['revision'] or not current['valid']:
            raise Conflict('读取期间经历版本改变。')
        with self.app.database.transaction() as db:
            db.execute('INSERT INTO continuity_experience_reads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (new_id(),self.run_id,self.agent_id,state['conversation_id'],current['id'],current['revision'],kind,
                        source_id,cursor,next_cursor,sha256,byte_count,byte_count,page_size,1,state['budget']['model_step'],envelope_bytes,utc_now()))
            self.app.continuity.dependencies._experience(db,self.run_id,current['id'],current['revision'])

    def _reference(self,state,row,*,kind):
        result={'experience_id':state['event']['id'],'revision':state['event']['revision'],'already_read':True,
                'source_id':row['source_id'],'next_cursor':row['next_cursor'],'sha256':row['sha256'],
                'text':'','text_omitted':'identical_page_already_returned',
                'budget':_budget_view(self._budget(),kind=kind)}
        if _envelope(_completed(result))>state['budget']['remaining']:
            return _refuse('budget_exhausted','window_exhausted','读取凭条也无法放入本轮。')
        with self.app.database.transaction() as db:
            db.execute('UPDATE continuity_experience_reads SET hits=hits+1 WHERE id=?',(row['id'],))
        return _completed(result)
