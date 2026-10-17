"""Model tools delegate to the same domain entrypoints used by the owner API."""
from lingnexus.common.errors import PermissionDenied


def call(scope,name,args):
    app=scope.app
    c=app.continuity
    with app.database.transaction() as db:
        run=db.execute('SELECT * FROM chat_runs WHERE id=? AND agent_id=?',(scope.run_id,scope.agent_id)).fetchone()
        if run is None or run['status']!='running':
            raise PermissionDenied('工具调用必须绑定活动运行。')
        cid=run['conversation_id']
        if name=='work_branch_start':
            binding=c.main.binding(scope.agent_id)
            if not binding or binding['conversation_id']!=cid or c.is_work_conversation(cid):
                raise PermissionDenied('只有绑定主干能发起独立工作。')
            value=c.branch(scope.agent_id,**args,origin_message_id=run['user_message_id'])
        else:
            branch=c.work_domain.row(args['branch_id']) if args.get('branch_id') else c.branch_for_conversation(cid)
            if (branch is None or branch['agent_id']!=scope.agent_id or
                    cid not in {branch['conversation_id'],branch['main_conversation_id']}):
                raise PermissionDenied('调用交流没有该工作的显式关系。')
            if name=='work_branch_status':
                value=c.work(branch['id'])
            elif name=='work_branch_guide':
                value=c.guide(branch['id'],args['text'],args['request_id'])
            elif name=='work_branch_finish':
                if cid!=branch['conversation_id']:
                    raise PermissionDenied('finish 工具只能由工作本身记录意图。')
                value=c.request_finish(branch['id'],branch['revision'],run_id=scope.run_id)
            else:
                raise ValueError('未知工作工具。')
    return {'status':'completed','result':value},None
