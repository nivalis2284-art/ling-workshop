"""Public continuity facade for runtime, API, background and maintenance."""
import json

from lingnexus.common.errors import (
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    RevisionConflict,
)
from lingnexus.common.types import utc_now

from . import sources
from .dependencies import Dependencies
from .experience import ExperienceDirectory
from .main import MainConversation
from .projection import project
from .reflection import ReflectionBridge
from .returns import Returns
from .work import WorkDomain


class Continuity:
    def __init__(self,app):
        self.app,self.database=app,app.database
        self.main=MainConversation(app)
        self.work_domain=WorkDomain(self)
        self.experiences=ExperienceDirectory(app,self)
        self.dependencies=Dependencies(self)
        self.returns=Returns(self)
        self.reflection_bridge=ReflectionBridge(self)

    def read_main_conversation(self,agent_id):
        return self.main.get(agent_id)

    def bind_main(self,agent_id,conversation_id,expected_revision):
        return self.main.bind(agent_id,conversation_id,expected_revision)

    def branch_for_conversation(self,conversation_id):
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_work_branches WHERE conversation_id=?',(conversation_id,)).fetchone()
        return dict(row) if row else None

    def is_work_conversation(self,conversation_id):
        return self.branch_for_conversation(conversation_id) is not None

    def branch(self,agent_id,**kwargs):
        return self.work_domain.create(agent_id,**kwargs)

    def work(self,branch_id):
        return self.work_domain.get(branch_id)

    def list_work(self,agent_id):
        return self.work_domain.list(agent_id)

    def guide(self,branch_id,text,request_id):
        return self.work_domain.guide(branch_id,text,request_id)

    def cancel(self,branch_id,expected_revision):
        return self.work_domain.cancel(branch_id,expected_revision)

    def request_finish(self,branch_id,expected_revision,*,run_id=None):
        return self.work_domain.finish(branch_id,expected_revision,run_id=run_id)

    def wake(self,branch_id,expected_revision):
        return self.work_domain.wake(branch_id,expected_revision)

    def task_state(self,branch_id):
        return self.work_domain.task_state(branch_id)

    def task_states(self,branch_id,**kwargs):
        return self.work_domain.task_states(branch_id,**kwargs)

    def episodes(self,branch_id,**kwargs):
        return self.work_domain.episodes(branch_id,**kwargs)

    def archives(self,branch_id,**kwargs):
        return self.work_domain.archives(branch_id,**kwargs)

    def guidance(self,branch_id,**kwargs):
        return self.work_domain.guidance(branch_id,**kwargs)

    def assert_fork(self,branch):
        return self.work_domain.assert_fork(branch)

    def project_messages(self,agent_id,conversation_id,messages):
        return project(self,agent_id,conversation_id,messages)

    def capture_run_dependencies(self,run_id,message_ids):
        return self.dependencies.capture(run_id,message_ids)

    def assert_run_sources(self,run_id):
        return self.dependencies.assert_current(run_id)

    def inherit_answer_dependencies(self,run_id,assistant_message_id):
        return self.dependencies.inherit(run_id,assistant_message_id)

    def message_dependencies(self,message_id):
        return self.dependencies.message(message_id)

    def list_experiences(self,agent_id,**kwargs):
        return self.experiences.list(agent_id,**kwargs)

    def experience(self,experience_id,*,agent_id=None):
        return self.experiences.get(experience_id,agent_id=agent_id)

    def experience_records(self,experience_id,**kwargs):
        return self.experiences.records(experience_id,**kwargs)

    def read_experience_record(self,experience_id,source_id,**kwargs):
        return self.experiences.read(experience_id,source_id,**kwargs)

    def stage_reflection_batch(self,agent_id,**kwargs):
        return self.reflection_bridge.stage(agent_id,**kwargs)

    def publish_reflection_batch(self,draft_id,**kwargs):
        return self.reflection_bridge.publish(draft_id,**kwargs)

    def assert_experience_access(self,item,*,conversation_id=None):
        if not item['valid']:
            raise Conflict('经历已撤销或来源不可用。')
        if conversation_id is None:
            return  # owner API: identity is authenticated outside this service
        conv=self.app.conversations.for_chat(item['agent_id'],conversation_id)
        if item.get('branch_id'):
            branch=self.work_domain.row(item['branch_id'])
            allowed={branch['conversation_id']}
            if item['coverage']!='live':
                allowed.add(branch['main_conversation_id'])
            if conv['id'] in allowed:
                return
        elif conv['id']==item.get('conversation_id'):
            return
        branch=self.branch_for_conversation(conversation_id)
        if branch and item['kind']=='main_event' and branch['main_conversation_id']==item.get('conversation_id'):
            self.assert_fork(branch)
            return
        raise PermissionDenied('该交流没有显式来源关系。')

    def validate_experience(self,item):
        if not item.get('valid'):
            return False,item.get('reason') or 'revoked'
        try:
            if item.get('branch_id'):
                self.assert_fork(self.work_domain.row(item['branch_id']))
            with self.database.transaction() as db:
                rows=db.execute('SELECT grants FROM continuity_experience_sources WHERE experience_id=?',(item['id'],)).fetchall()
                tokens={row[0] for row in rows}
                for token in tokens:
                    if token and token!='{}':
                        sources.validate(self.app,item['agent_id'],json.loads(token))
        except PermissionDenied:
            return False,'permissions_changed'
        except DomainError:
            return False,'sources_unavailable'
        return True,None

    def revoke_experience(self,experience_id,*,expected_revision,agent_id,reason='owner_revoked'):
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_experiences WHERE id=?',(experience_id,)).fetchone()
            if row is None or row['agent_id']!=agent_id:
                raise NotFound('经历不存在。')
            if type(expected_revision) is not int or row['revision']!=expected_revision:
                raise RevisionConflict('经历版本已变化。')
            if row['coverage']=='live':
                raise Conflict('进行中经历请先取消或安全结束工作。')
            db.execute('UPDATE continuity_experiences SET valid=0,reason=?,revision=revision+1,updated_at=? WHERE id=?',
                       (reason,utc_now(),experience_id))
            db.execute("UPDATE continuity_experience_sources SET coverage='revoked' WHERE experience_id=?",(experience_id,))
        return self.experiences.get(experience_id,agent_id=agent_id)
