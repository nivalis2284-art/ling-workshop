"""Lifespan-owned queue driver; model stop is separate from work completion."""
import asyncio

from lingnexus.common.errors import Conflict, DomainError
from lingnexus.common.types import utc_now
from lingnexus.runtime.types import ChatRequest

from .work import ACTIVE


class BranchCoordinator:
    def __init__(self,app,runtime=None):
        self.app,self.continuity,self.runtime=app,app.continuity,runtime
        self._task=None
        self._closed=False
        self._lock=asyncio.Lock()
        self.last_error=None

    def start(self):
        if self._task is None:
            self._task=asyncio.create_task(self._loop())
        return self

    async def aclose(self):
        self._closed=True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task,return_exceptions=True)
            self._task=None

    async def _loop(self):
        while not self._closed:
            try:
                await self.tick()
            except DomainError:
                self.last_error='continuity_conflict'
            except Exception:  # noqa: BLE001 - background boundary; status only
                self.last_error='coordinator_error'
            await asyncio.sleep(.2)

    async def tick(self):
        if self.runtime is None:
            return
        async with self._lock:
            with self.app.database.transaction() as db:
                active=[dict(r) for r in db.execute("SELECT * FROM continuity_work_branches WHERE status NOT IN ('archived','failed','cancelled') ORDER BY created_at LIMIT 128")]
            for row in active:
                run=self.runtime.get_run(row['run_id']) if row['run_id'] else None
                if row['outcome']=='cancel_requested':
                    if run is not None:
                        await self.runtime.cancel(run['id'])
                        run=self.runtime.get_run(run['id'])
                    try:
                        self.continuity.work_domain.safe(row)
                        if run is None:
                            with self.app.database.transaction() as db:
                                db.execute("UPDATE continuity_work_branches SET status='cancelled',outcome='cancelled',finished_at=? WHERE id=?",(utc_now(),row['id']))
                        else:
                            self.continuity.returns.archive(row['id'])
                    except DomainError:
                        self._status(row['id'],'unknown','cancel_cleanup_unresolved')
                    continue
                if run is not None and run['status']=='running':
                    self._deliver_guidance(row)
                    continue
                if row['finish_requested'] and run is not None:
                    try:
                        await asyncio.to_thread(self.continuity.returns.archive,row['id'])
                    except DomainError:
                        self._status(row['id'],'pending_review','finish_fenced')
                    continue
                if row['status'] in {'running','starting'} and run is not None:
                    if run['status']=='completed':
                        self._status(row['id'],'paused',None)
                    else:
                        self._status(row['id'],'interrupted' if run.get('recoverable') else 'pending_review',run.get('status'))
                elif row['status']=='starting' and run is None:
                    # Crash after durable claim but before submit. Deterministic
                    # request id below attaches a committed run instead of replay.
                    self._status(row['id'],'queued',None)
            with self.app.database.transaction() as db:
                queued=[dict(r) for r in db.execute("SELECT * FROM continuity_work_branches WHERE status='queued' ORDER BY created_at LIMIT 128")]
            for row in queued:
                with self.app.database.transaction() as db:
                    marks=','.join('?' for _ in ACTIVE)
                    if db.execute('SELECT 1 FROM continuity_work_branches WHERE agent_id=? AND status IN ('+marks+') AND id!=?',
                                  (row['agent_id'],*ACTIVE,row['id'])).fetchone():
                        continue
                    self.continuity.assert_fork(row)
                    db.execute("UPDATE continuity_work_branches SET status='starting' WHERE id=? AND status='queued'",(row['id'],))
                    guidance=[dict(r) for r in db.execute("SELECT * FROM continuity_work_guidance WHERE branch_id=? AND delivery='queued' ORDER BY created_at,id",(row['id'],))]
                message=row['objective'] if not row['run_id'] else '\n'.join(g['text'] for g in guidance)
                if not message:
                    self._status(row['id'],'paused',None)
                    continue
                identity=guidance[-1]['id'] if guidance else 'initial'
                try:
                    run=self.runtime.submit(ChatRequest(agent_id=row['agent_id'],conversation_id=row['conversation_id'],
                        message=message+'\n本工作经历入口：work-live:'+row['id']+'；完成工作时调用 work_branch_finish，普通回合结束只会暂停工作。',
                        request_id='work:'+row['id']+':'+identity,profile_id=row['profile_id']))
                except Conflict:
                    self._status(row['id'],'queued','runtime_busy')
                    continue
                except DomainError:
                    self._status(row['id'],'pending_review','admission_rejected')
                    continue
                with self.app.database.transaction() as db:
                    db.execute("UPDATE continuity_work_branches SET run_id=?,status='running',started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                               (run['id'],utc_now(),utc_now(),row['id']))
                    for guide in guidance:
                        db.execute("UPDATE continuity_work_guidance SET delivery='delivered',delivered_at=? WHERE id=?",(utc_now(),guide['id']))
            await asyncio.to_thread(self.continuity.returns.publish)

    def _deliver_guidance(self,row):
        with self.app.database.transaction() as db:
            guides=[dict(r) for r in db.execute("SELECT * FROM continuity_work_guidance WHERE branch_id=? AND delivery='queued' ORDER BY created_at,id",(row['id'],))]
        for guide in guides:
            self.runtime.guide(row['run_id'],guide['text'])
            with self.app.database.transaction() as db:
                db.execute("UPDATE continuity_work_guidance SET delivery='delivered',delivered_at=? WHERE id=?",(utc_now(),guide['id']))

    def _status(self,bid,status,error):
        with self.app.database.transaction() as db:
            db.execute('UPDATE continuity_work_branches SET status=?,error_code=?,updated_at=? WHERE id=?',(status,error,utc_now(),bid))
