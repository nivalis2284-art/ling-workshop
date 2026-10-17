"""Authorized metadata directories and UTF-8 pages over authoritative stores."""
import base64
import json

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied

from . import sources
from .store import encode

MAX_DIRECTORY=20
MAX_PAGE=16384


class ExperienceDirectory:
    def __init__(self,app,continuity):
        self.app,self.database,self.continuity=app,app.database,continuity

    def list(self,agent_id,*,kind=None,cursor=None,limit=20):
        self.app.permissions.current(agent_id)
        if type(limit) is not int or not 1<=limit<=20 or kind not in {None,'work_branch','main_event','legacy_work_event','work_event'}:
            raise ValueError('经历目录参数无效。')
        offset=decode(cursor,['list',agent_id,kind])
        with self.database.transaction() as db:
            rows=db.execute('SELECT id,created_at FROM continuity_experiences WHERE agent_id=? '
                            'AND (? IS NULL OR kind=?) UNION ALL '
                            "SELECT 'legacy:'||id,created_at FROM work_events WHERE agent_id=? AND status='closed' "
                            "AND (? IS NULL OR ? IN ('legacy_work_event','work_event')) ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                            (agent_id,kind,kind,agent_id,kind,kind,limit+1,offset)).fetchall()
        return {'items':[self.get(row['id'],agent_id=agent_id) for row in rows[:limit]],
                'next_cursor':pack(['list',agent_id,kind,offset+limit]) if len(rows)>limit else None}

    def get(self,eid,*,agent_id=None):
        if eid.startswith('legacy:'):
            old=self.app.work_events.get(eid.removeprefix('legacy:'),agent_id=agent_id)
            return {'id':eid,'agent_id':old['agent_id'],'kind':'legacy_work_event','title':old['title'],
                    'summary':old.get('summary','') if old['valid'] else '', 'revision':old['revision'],
                    'coverage':old['coverage'],'valid':old['valid'],'reason':old.get('reason'),
                    'conversation_id':old['conversation_id'],'branch_id':None,
                    'occurred_start':old.get('created_at'),'occurred_end':old.get('closed_at'),
                    'source_count':old.get('source_count',0)}
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM continuity_experiences WHERE id=?',(eid,)).fetchone()
            if row is None or (agent_id is not None and row['agent_id']!=agent_id):
                raise NotFound('经历不存在。')
            item=dict(row)
            if item['branch_id']:
                item['branch_status']=self.continuity.work_domain.row(item['branch_id'])['status']
            if item['coverage']=='live':
                item['source_count']=db.execute('SELECT COUNT(*) FROM work_event_records WHERE conversation_id=?',
                                                (item['conversation_id'],)).fetchone()[0]
            else:
                item['source_count']=db.execute('SELECT COUNT(*) FROM continuity_experience_sources WHERE experience_id=?',(eid,)).fetchone()[0]
                archive=db.execute('SELECT episode_id FROM continuity_episode_archives WHERE experience_id=?',(eid,)).fetchone()
                if archive is not None:
                    item['episode_id']=archive['episode_id']
        item['valid'],item['reason']=self.continuity.validate_experience(item)
        if not item['valid']:
            item['summary']=''
        return item

    def records(self,eid,*,agent_id=None,conversation_id=None,cursor=None,limit=20):
        if type(limit) is not int or not 1<=limit<=20:
            raise ValueError('目录页大小无效。')
        item=self.get(eid,agent_id=agent_id)
        self.continuity.assert_experience_access(item,conversation_id=conversation_id)
        if eid.startswith('legacy:'):
            return self.app.work_events.directory(eid.removeprefix('legacy:'),agent_id=item['agent_id'],
                    conversation_id=item['conversation_id'],cursor=cursor,limit=limit)
        offset=decode(cursor,['directory',eid,item['revision'],conversation_id])
        with self.database.transaction() as db:
            if item['coverage']=='live':
                records=db.execute('SELECT id,kind,byte_count,sha256,grants FROM work_event_records WHERE conversation_id=? ORDER BY rowid LIMIT ? OFFSET ?',
                                   (item['conversation_id'],limit+1,offset)).fetchall()
                rows=[]
                for record in records:
                    grants=json.loads(record['grants'])
                    grants.pop('captured_from',None)
                    if grants!=sources.scope(self.app,item['agent_id']):
                        raise PermissionDenied('安全原文授权已改变。')
                    rows.append({'source_id':'record:'+record['id'],'kind':'record','byte_count':record['byte_count'],
                                 'sha256':record['sha256'],'coverage':'retained'})
            else:
                rows=[dict(r) for r in db.execute('SELECT source_id,kind,byte_count,sha256,coverage FROM continuity_experience_sources '
                                                 'WHERE experience_id=? ORDER BY ordinal LIMIT ? OFFSET ?',(eid,limit+1,offset))]
        return {'items':[dict(r,id=r['source_id'],location=r['source_id']) for r in rows[:limit]],
                'next_cursor':pack(['directory',eid,item['revision'],conversation_id,offset+limit]) if len(rows)>limit else None,
                'revision':item['revision'],'coverage':item['coverage']}

    def read(self,eid,sid,*,agent_id=None,conversation_id=None,cursor=None,max_bytes=8192):
        if type(max_bytes) is not int or not 4<=max_bytes<=MAX_PAGE:
            raise ValueError('正文单页须在4到16384字节之间。')
        with self.database.transaction() as db:
            item=self.get(eid,agent_id=agent_id)
            self.continuity.assert_experience_access(item,conversation_id=conversation_id)
            if eid.startswith('legacy:'):
                page=self.app.work_events.read_record(eid.removeprefix('legacy:'),sid,agent_id=item['agent_id'],
                        conversation_id=item['conversation_id'],cursor=cursor,max_bytes=max_bytes)
                return dict(page,page_bytes=len(page['text'].encode()))
            offset=decode(cursor,['read',eid,item['revision'],conversation_id,sid])
            if item['coverage']=='live':
                row=db.execute('SELECT * FROM work_event_records WHERE id=? AND conversation_id=?',
                               (sid.removeprefix('record:'),item['conversation_id'])).fetchone()
                if row is None or not sid.startswith('record:'):
                    raise NotFound('安全来源不属于当前工作。')
                grants=json.loads(row['grants'])
                grants.pop('captured_from',None)
                if grants!=sources.scope(self.app,item['agent_id']):
                    raise PermissionDenied('安全原文授权已改变。')
                kind,ref,sha,coverage='record',{'id':row['id']},row['sha256'],'retained'
            else:
                row=db.execute('SELECT * FROM continuity_experience_sources WHERE experience_id=? AND source_id=?',(eid,sid)).fetchone()
                if row is None:
                    raise NotFound('来源不在经历目录。')
                token=json.loads(row['grants'])
                sources.validate(self.app,item['agent_id'],token)
                kind,ref,sha,coverage=row['kind'],json.loads(row['ref']),row['sha256'],row['coverage']
            page=sources.read_page(self.app,kind,ref,offset=offset,max_bytes=max_bytes)
        return {'experience_id':eid,'source_id':sid,'text':page['text'],'page_bytes':len(page['text'].encode()),
                'next_cursor':pack(['read',eid,item['revision'],conversation_id,sid,page['next_offset']]) if page['next_offset'] is not None else None,
                'sha256':sha,'revision':item['revision'],'coverage':coverage}


def pack(value):
    return base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip('=')


def decode(cursor,prefix):
    if not cursor:
        return 0
    try:
        value=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
        if not isinstance(value,list) or value[:-1]!=prefix or type(value[-1]) is not int or value[-1]<0:
            raise ValueError
        return value[-1]
    except (ValueError,TypeError,UnicodeError) as error:
        raise Conflict('分页游标已失效。') from error
