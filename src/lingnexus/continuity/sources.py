"""Bounded authoritative source capture and constant-size prefix validation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from lingnexus.common.errors import Conflict, NotFound, PermissionDenied

from .store import digest


def scope(app, agent_id):
    browser = getattr(app, 'browser', None)
    return {'permissions': app.permissions.current(agent_id),
            'browser': browser.current_origins(agent_id) if browser is not None else None}


def epoch(db, conversation_id):
    row = db.execute('SELECT epoch FROM continuity_source_versions WHERE conversation_id=?',
                     (conversation_id,)).fetchone()
    if row is None:
        raise NotFound('来源交流不存在。')
    return row[0]


def stamp(value):
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError
        return moment.astimezone(UTC).isoformat()
    except (ValueError, TypeError):
        raise Conflict('来源时间无效。') from None


def asset_version(db, message_id):
    row = db.execute('SELECT revision FROM continuity_message_assets WHERE message_id=?', (message_id,)).fetchone()
    if row is None:
        raise Conflict('来源消息已删除。')
    return row[0]


def path_metadata(app, conversation_id, *, anchor=None):
    """Read no bodies. Release the database lock every 128 ancestors."""
    with app.database.transaction() as db:
        row = db.execute('SELECT head_id FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None:
            raise NotFound('来源交流不存在。')
        selected_head = row[0]
        version = epoch(db, conversation_id)
    head, nodes, seen = selected_head, [], set()
    while head:
        with app.database.transaction() as db:
            if epoch(db, conversation_id) != version:
                raise Conflict('捕获来源期间选路已变化。')
            for _ in range(128):
                if not head:
                    break
                if head in seen:
                    raise Conflict('来源路径存在循环。')
                seen.add(head)
                row = db.execute(
                    'SELECT id,parent_id,role,created_at,length(CAST(text AS BLOB)) AS byte_count '
                    'FROM conversation_messages WHERE id=? AND conversation_id=?',
                    (head, conversation_id)).fetchone()
                if row is None:
                    raise Conflict('来源路径已删除。')
                nodes.append(dict(row))
                head = row['parent_id']
    nodes.reverse()
    if anchor is not None:
        index = next((i for i, n in enumerate(nodes) if n['id'] == anchor), None)
        if index is None:
            raise Conflict('来源锚点不在允许的当前路径。')
        nodes = nodes[:index + 1]
    return nodes, version


def ref_dict(value):
    value = json.loads(value) if isinstance(value, str) else dict(value)
    if 'message_id' in value and 'id' not in value:
        value['id'] = value.pop('message_id')
    return value


def snapshot(app, agent_id, kind, ref, *, expected_sha=None, max_bytes=131072,
             allowed_ids=None, original_grants=None):
    ref = ref_dict(ref)
    if kind not in {'message', 'record', 'attachment'} or not isinstance(ref.get('id'), str):
        raise Conflict('来源引用不受支持。')
    with app.database.transaction() as db:
        if kind == 'message':
            row = db.execute('SELECT id,conversation_id,created_at,role,length(CAST(text AS BLOB)) AS byte_count '
                             'FROM conversation_messages WHERE id=?', (ref['id'],)).fetchone()
        elif kind == 'record':
            row = db.execute('SELECT id,conversation_id,created_at,byte_count,grants FROM work_event_records WHERE id=?',
                             (ref['id'],)).fetchone()
        else:
            row = db.execute('SELECT a.id,l.conversation_id,a.created_at,length(CAST(a.extraction AS BLOB)) AS byte_count '
                             'FROM attachments a JOIN conversation_attachments l ON l.attachment_id=a.id '
                             'WHERE a.id=? AND l.message_id=?', (ref['id'], ref.get('message_id'))).fetchone()
        if row is None:
            raise NotFound('来源不存在。')
        conv = app.conversations.for_chat(agent_id, row['conversation_id'])
        token = epoch(db, conv['id'])
        current_scope = scope(app, agent_id)
        if kind == 'record':
            original_grants = json.loads(row['grants'])
            original_grants.pop('captured_from', None)
        if original_grants is not None and original_grants != current_scope:
            raise PermissionDenied('来源原始授权已失效。')
        if row['byte_count'] > max_bytes:
            raise Conflict('来源超过本次捕获预算。')
        metadata = dict(row)
        asset_mid = (ref['id'] if kind == 'message' else ref.get('message_id')) if kind != 'record' else None
        asset_rev = asset_version(db, asset_mid) if asset_mid is not None else None
    if allowed_ids is not None and kind == 'message' and ref['id'] not in allowed_ids:
        raise PermissionDenied('来源不在冻结的允许路径。')
    parts, offset = [], 0
    while True:
        page = read_page(app, kind, ref, offset=offset, max_bytes=min(16384, max_bytes-offset))
        parts.append(page['text'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
        if offset >= max_bytes:
            raise Conflict('来源超过本次捕获预算。')
    text = ''.join(parts)
    sha = digest(text)
    if expected_sha is not None and sha != expected_sha:
        raise Conflict('冻结来源正文已变化。')
    result = {'kind': kind, 'ref': ref, 'conversation_id': conv['id'], 'epoch': token,
              'grants': original_grants or current_scope, 'sha256': sha,
              'byte_count': len(text.encode('utf-8')), 'occurred_at': stamp(row['created_at']),
              'role': metadata.get('role')}
    if kind in {'message', 'attachment'}:
        result['message_id'] = asset_mid
        result['asset_revision'] = asset_rev
    validate(app, agent_id, result)
    return result


def validate(app, agent_id, source):
    """No history/body scan; token maintained on every source mutation."""
    with app.database.transaction() as db:
        app.conversations.for_chat(agent_id, source['conversation_id'])
        if epoch(db, source['conversation_id']) != source['epoch']:
            raise Conflict('来源路径或成员已失效。')
        if scope(app, agent_id) != source['grants']:
            raise PermissionDenied('来源授权已改变。')
        if source.get('message_id') is not None and asset_version(db, source['message_id']) != source['asset_revision']:
            raise Conflict('来源消息的附件版本已变化。')


def read_page(app, kind, ref, *, offset=0, max_bytes=8192):
    if kind == 'record':
        return app.work_events.records.read(ref['id'], offset=offset, max_bytes=max(4, max_bytes))
    table, column = ('conversation_messages', 'text') if kind == 'message' else ('attachments', 'extraction')
    with app.database.transaction() as db:
        row = db.execute(f'SELECT rowid,length(CAST({column} AS BLOB)) AS size FROM {table} WHERE id=?',
                         (ref['id'],)).fetchone()
        if row is None:
            raise NotFound('原文已删除。')
        if offset > row['size']:
            raise Conflict('正文游标超过范围。')
        with db.blobopen(table, column, row['rowid'], readonly=True) as body:
            body.seek(offset)
            data = body.read(max_bytes)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        if error.reason == 'unexpected end of data' and error.end == len(data):
            text = data[:error.start].decode('utf-8')
        else:
            raise Conflict('正文游标不是 UTF-8 边界。') from error
    used = len(text.encode('utf-8'))
    return {'text': text, 'page_bytes': used, 'next_offset': offset + used if offset + used < row['size'] else None}


def hash_body(app, kind, ref):
    """Offline capture helper for large records, with bounded read buffers."""
    hasher, offset = hashlib.sha256(), 0
    while True:
        page = read_page(app, kind, ref, offset=offset, max_bytes=16384)
        hasher.update(page['text'].encode('utf-8'))
        if page['next_offset'] is None:
            return hasher.hexdigest()
        offset = page['next_offset']
