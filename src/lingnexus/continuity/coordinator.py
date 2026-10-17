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
                if run is None or run['status']!='running':
                    # A process restart erased the in-memory guide queue. A
                    # queued successor with no run is the same failure mode.
                    # Hand unconsumed guidance back before any recover/submit so
                    # it cannot disappear as "delivered but never applied".
                    with self.app.database.transaction() as db:
                        db.execute("UPDATE continuity_work_guidance SET delivery='queued',delivered_at=NULL "
                                   "WHERE branch_id=? AND consumed_at IS NULL AND delivery='delivered'",
                                   (row['id'],))
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
                    self._queue_guidance(row)
                    continue
                if row['status']=='paused' and run is None:
                    # A paused work branch has no live run. Keep it dormant so a
                    # later wake/guide creates an explicit successor episode.
                    continue
                if row['status']=='interrupted' and run is not None:
                    # A startup/dirty shutdown may leave a complete checkpoint.
                    # Restore that exact run once; a failed guard stays fenced.
                    try:
                        self.runtime.recover(run['id'])
                        self._status(row['id'],'running',None)
                    except DomainError:
                        self._status(row['id'],'pending_review','recovery_refused')
                    continue
                if row['status']=='pending_review' and run is not None:
                    continue
                if row['finish_requested'] and run is not None:
                    try:
                        if run['status']=='running':
                            continue
                        await asyncio.to_thread(self.continuity.returns.archive,row['id'])
                    except DomainError:
                        self._status(row['id'],'pending_review','finish_fenced')
                    continue
                if row['status'] in {'running','starting'} and run is not None:
                    if run['status']=='completed':
                        self.continuity.work_domain.settle_episode(row,'paused','round_completed')
                    else:
                        self.continuity.work_domain.settle_episode(
                            row,'interrupted' if run.get('recoverable') else 'pending_review',
                            run.get('recovery_reason') or run.get('status'))
                elif row['status']=='starting' and run is None:
                    # Crash after durable claim but before submit. Deterministic
                    # request id below attaches a committed run instead of replay.
                    # The claim did not end the episode. Closing it here makes
                    # every later submit fail its active-episode guard.
                    self._claim_status(row,'queued','claim_without_run')
            with self.app.database.transaction() as db:
                queued=[dict(r) for r in db.execute("SELECT * FROM continuity_work_branches WHERE status='queued' ORDER BY created_at LIMIT 128")]
            for row in queued:
                try:
                    with self.app.database.transaction() as db:
                        marks=','.join('?' for _ in ACTIVE)
                        if db.execute('SELECT 1 FROM continuity_work_branches WHERE agent_id=? AND status IN ('+marks+') AND id!=?',
                                      (row['agent_id'],*ACTIVE,row['id'])).fetchone():
                            continue
                        self.continuity.assert_fork(row)
                        db.execute("UPDATE continuity_work_branches SET status='starting' WHERE id=? AND status='queued'",(row['id'],))
                        guidance=[dict(r) for r in db.execute("SELECT * FROM continuity_work_guidance WHERE branch_id=? "
                                                              "AND consumed_at IS NULL AND delivery='queued' ORDER BY created_at,id LIMIT 64",(row['id'],))]
                    message=self._wake_message(row,guidance)
                except DomainError:
                    # One revoked fork must not starve unrelated agents or the
                    # durable return outbox on every background tick.
                    self._claim_status(row,'pending_review','source_unavailable')
                    continue
                if not message:
                    self._claim_status(row,'paused',None)
                    continue
                # One request id per awake episode. A wake always opens a new
                # episode, so an old completed run can never satisfy this submit.
                identity=(row['current_episode_id'] or 'episode')+':'+(
                    guidance[-1]['id'] if guidance else 'initial')
                try:
                    run=self.runtime.submit(ChatRequest(agent_id=row['agent_id'],conversation_id=row['conversation_id'],
                        message=message+'\n本工作经历入口：work-live:'+row['id']+'；完成工作时调用 work_branch_finish，普通回合结束只会暂停工作。需要延续判断时先调用 work_branch_state 记录任务状态。',
                        request_id='work:'+row['id']+':'+identity,profile_id=row['profile_id'],
                        work_episode_id=row['current_episode_id'],
                        work_guidance_ids=[g['id'] for g in guidance]))
                except Conflict:
                    self._claim_status(row,'queued','runtime_busy')
                    continue
                except DomainError:
                    self._claim_status(row,'pending_review','admission_rejected')
                    continue
                with self.app.database.transaction() as db:
                    for guide in guidance:
                        db.execute("UPDATE continuity_work_guidance SET delivery='delivered',delivered_at=COALESCE(delivered_at,?) WHERE id=?",(utc_now(),guide['id']))
            await asyncio.to_thread(self.continuity.returns.publish)

    def _wake_message(self,row,guidance):
        """Build a bounded continuation packet from durable task state.

        The work conversation itself supplies the original transcript. This
        packet adds the explicit state the owner can inspect: unchanged goals,
        decisions, unfinished items and only guidance not yet consumed.
        """
        if not row['run_id'] and not guidance and not self.continuity.archives(row['id']):
            return row['objective']
        state=self.continuity.task_state(row['id']) or {}
        decisions=state.get('decisions') or []
        unfinished=state.get('unfinished') or []
        verified=state.get('verified') or []
        unknown=state.get('unknowns') or []
        lines=[
            '【继续同一子 Agent】',
            '原目标：'+row['objective'],
        ]
        if decisions:
            lines.append('关键决策：')
            lines.extend('- '+item for item in decisions[-16:])
        if unfinished:
            lines.append('未完成项：')
            lines.extend('- '+item for item in unfinished[-16:])
        if verified:
            lines.append('已验证事实：')
            lines.extend('- '+item for item in verified[-16:])
        if unknown:
            lines.append('未知或阻塞：')
            lines.extend('- '+item for item in unknown[-16:])
        archives=self.continuity.archives(row['id'])
        if archives:
            latest=archives[-1]
            lines.append('上次归档：'+latest['archive_id']+'；经历：'+latest['experience_id'])
        if guidance:
            lines.append('新的所有者指导：')
            lines.extend('- '+item['text'] for item in guidance)
        lines.append('历史原文与证据仍在同一工作交流及经历目录中；不要仅凭这段交接重新执行已完成的副作用。')
        return '\n'.join(lines)

    def _queue_guidance(self,row):
        """Hand queued text to the live loop; consumed_at is set by the runtime."""
        with self.app.database.transaction() as db:
            guides=[dict(r) for r in db.execute("SELECT * FROM continuity_work_guidance WHERE branch_id=? AND episode_id=? "
                                               "AND consumed_at IS NULL AND delivery='queued' ORDER BY created_at,id LIMIT 64",
                                               (row['id'],row['current_episode_id']))]
        for guide in guides:
            self.runtime.guide(row['run_id'],guide['text'])
            with self.app.database.transaction() as db:
                db.execute("UPDATE continuity_work_guidance SET delivery='delivered',delivered_at=COALESCE(delivered_at,?) "
                           "WHERE id=? AND consumed_at IS NULL",(utc_now(),guide['id']))

    def _status(self,bid,status,error):
        with self.app.database.transaction() as db:
            db.execute('UPDATE continuity_work_branches SET status=?,error_code=?,updated_at=? WHERE id=?',(status,error,utc_now(),bid))

    def _claim_status(self,row,status,error):
        """Release only this still-unsubmitted claim, never a newer owner run."""
        with self.app.database.transaction() as db:
            db.execute("UPDATE continuity_work_branches SET status=?,error_code=?,updated_at=?,revision=revision+1 "
                       "WHERE id=? AND current_episode_id IS ? AND run_id IS NULL AND status IN ('queued','starting')",
                       (status,error,utc_now(),row['id'],row['current_episode_id']))
