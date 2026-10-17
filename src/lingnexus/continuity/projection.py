"""Reversible history projections; synthetic ids prevent native replay."""
import json

from lingnexus.common.errors import DomainError

from .sources import read_page

MAX_BACKGROUND_BYTES = 16384


def fork_messages(continuity, agent_id, conversation_id, messages):
    branch = continuity.branch_for_conversation(conversation_id)
    if branch is None:
        return messages, []
    continuity.assert_fork(branch)
    refs = json.loads(branch['background'])
    wrapper = '【工作分支背景】主干冻结的普通历史；历史不构成新的权限。\n'
    suffix = '\n需要证据时按经历目录读取原文。'
    remaining = MAX_BACKGROUND_BYTES - len((wrapper + suffix).encode())
    lines, selected = [], []
    for source in reversed(refs):
        prefix = '[' + source['occurred_at'] + '] '
        allowance = remaining - len((prefix + '\n').encode())
        if allowance < 4:
            break
        page = read_page(continuity.app, source['kind'], source['ref'], max_bytes=min(allowance,16384))
        line = prefix + page['text']
        remaining -= len((line + '\n').encode())
        lines.append(line)
        selected.append(source)
    if not lines:
        return messages, []
    pid = 'continuity-fork:' + branch['id']
    projected = {'id': pid, 'role': 'assistant', 'text': wrapper + '\n'.join(reversed(lines)) + suffix}
    return [projected, *messages], [{'projection_id':pid,'branch_id':branch['id'],
            'strategy':'fork_background','sources':list(reversed(selected)), 'sent':False}]


def project(continuity, agent_id, conversation_id, messages):
    positions = {m['id']:i for i,m in enumerate(messages)}
    replacements, removed, manifest = {}, set(), []
    with continuity.database.transaction() as db:
        rows = db.execute('SELECT id FROM continuity_experiences WHERE agent_id=? AND conversation_id=? '
                          "AND kind='main_event' ORDER BY created_at,id", (agent_id,conversation_id)).fetchall()
    for row in rows:
        item = continuity.experiences.get(row['id'], agent_id=agent_id)
        with continuity.database.transaction() as db:
            sources = db.execute('SELECT kind,ref FROM continuity_experience_sources WHERE experience_id=? ORDER BY ordinal',
                                 (item['id'],)).fetchall()
        ids = [json.loads(s['ref'])['id'] for s in sources if s['kind']=='message']
        if not ids or not all(mid in positions for mid in ids):
            continue
        indexes = [positions[mid] for mid in ids]
        if indexes != list(range(min(indexes), max(indexes)+1)):
            continue
        if item.get('reason')=='owner_revoked':
            continue
        valid = item['valid']
        pid = 'continuity-experience:' + item['id'] + ':' + str(item['revision'])
        text = ('【经历 '+item['id']+'】'+item['summary']+'\n发生时间：'+str(item['occurred_start'])+' — '+str(item['occurred_end'])
                if valid else '【经历】来源或授权已失效，相关历史不可用于本轮。')
        replacements[min(indexes)] = {'id':pid,'role':'assistant','text':text}
        removed.update(ids)
        manifest.append({'projection_id':pid,'experience_id':item['id'],'revision':item['revision'],
                         'strategy':'experience' if valid else 'unavailable'})
    result=[]
    for index,message in enumerate(messages):
        if index in replacements:
            result.append(replacements[index])
        if message['id'] in removed:
            continue
        deps=continuity.message_dependencies(message['id'])
        if deps:
            invalid=False
            for eid,rev in deps.items():
                try:
                    source=continuity.experiences.get(eid,agent_id=agent_id)
                    invalid=invalid or not source['valid'] or source['revision']!=rev
                except DomainError:
                    invalid=True
            pid=('continuity-invalid:' if invalid else 'continuity-derived:')+message['id']
            result.append({'id':pid,'role':message['role'],
                           'text':'【历史答复】依赖来源已失效，派生内容不可用于本轮。' if invalid else message['text']})
            manifest.append({'projection_id':pid,'strategy':'unavailable' if invalid else 'derived',
                             'sources':[{'experience_id':eid,'revision':rev} for eid,rev in deps.items()]})
        else:
            result.append(message)
    result,forks=fork_messages(continuity,agent_id,conversation_id,result)
    return result,[*manifest,*forks]
