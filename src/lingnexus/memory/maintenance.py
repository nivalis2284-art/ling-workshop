"""Owner edits of derived memory; original source history is never rewritten here."""

import json

from lingnexus.common.errors import Conflict, NotFound, RevisionConflict, SourceInvalid
from lingnexus.common.types import SourceRef, new_id, utc_now


class MemoryMaintenance:
    def __init__(self, app):
        self.database, self.history = app.database, app.history

    @staticmethod
    def _tables(kind):
        if kind not in {"episodes", "knowledge"}:
            raise ValueError("记忆类型无效")
        singular = "episode" if kind == "episodes" else "knowledge"
        return kind, singular + "_sources", singular + "_id"

    def _snapshot(self, db, kind, item_id) -> dict:
        table, sources, key = self._tables(kind)
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("记忆条目不存在，可能已被删除")
        refs = [
            {"id": r[0], "revision": r[1]}
            for r in db.execute(
                f"SELECT source_id,source_revision FROM {sources} WHERE {key}=? "
                "ORDER BY source_id,source_revision",
                (item_id,),
            )
        ]
        return dict(row) | {"sources": refs}

    def _validate(self, row):
        if row["status"] == "invalidated":
            raise SourceInvalid("这条记忆的依据已失效，需要重新整理，不能直接更正或恢复")
        self.history.validate_refs(
            row["agent_id"], [SourceRef(**ref) for ref in row["sources"]], row["scope"]
        )

    def _present(self, db, kind, row):
        try:
            self._validate(row)
            valid, reason = True, None
        except (SourceInvalid, NotFound):
            valid, reason = False, "来源已失效或不再属于当前情境，需要重新整理。"
        corrections = [
            dict(r) | {"prior": json.loads(r["prior"]), "current": json.loads(r["current"])}
            for r in db.execute(
                "SELECT revision,actor,created_at,prior,current FROM memory_edits "
                "WHERE target_kind=? AND target_id=? AND action='correct' ORDER BY revision",
                (kind, row["id"]),
            )
        ]
        return row | {
            "source_valid": valid,
            "source_notice": reason,
            "provenance": "owner_assertion" if corrections else "derived",
            "corrections": corrections,
            "memory_ref": {"kind": kind, "id": row["id"], "revision": row["revision"]},
        }

    def read(self, kind, item_id):
        with self.database.transaction() as db:
            return self._present(db, kind, self._snapshot(db, kind, item_id))

    def snapshot_marker(self, kind: str, item_id: str) -> dict:
        """Capture under the same outer transaction as the recalled text."""
        with self.database.transaction() as db:
            row = self._snapshot(db, kind, item_id)
            self._validate(row)
            if row["status"] != "active":
                raise SourceInvalid("记忆已归档，不能用于当前交流")
            return {"kind": kind, "id": item_id, "revision": row["revision"]}

    def validate_markers(self, agent_id: str, markers: list[dict]) -> None:
        """Revalidate manifest memory_refs before accepting a pending model result."""
        with self.database.transaction() as db:
            for marker in markers:
                try:
                    if (
                        set(marker) != {"kind", "id", "revision"}
                        or type(marker["revision"]) is not int
                        or marker["revision"] < 1
                    ):
                        raise ValueError("记忆版本标记无效")
                    row = self._snapshot(db, marker["kind"], marker["id"])
                    if (
                        row["agent_id"] != agent_id
                        or row["scope"] not in {"real", "external"}
                        or row["revision"] != marker["revision"]
                        or row["status"] != "active"
                    ):
                        raise SourceInvalid("交流引用的记忆已更改，请重新生成回复")
                    self._validate(row)
                except (NotFound, ValueError, TypeError, KeyError) as error:
                    raise SourceInvalid("交流引用的记忆已更改或不可用，请重新生成回复") from error

    def items(
        self, agent_id, *, kind="episodes", scope="real", status="all", query="", limit=20, offset=0
    ):
        table, _, _ = self._tables(kind)
        if (
            scope not in {"real", "external"}
            or status not in {"all", "active", "archived", "invalidated"}
            or not 1 <= limit <= 100
            or offset < 0
        ):
            raise ValueError("记忆筛选条件无效")
        with self.database.transaction() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise NotFound("个体不存在")
            sql, params = f"SELECT id FROM {table} WHERE agent_id=? AND scope=?", [agent_id, scope]
            if status != "all":
                sql += " AND status=?"
                params.append(status)
            if query:
                literal = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                fields = ("title", "summary") if kind == "episodes" else ("statement",)
                sql += " AND (" + " OR ".join(f"{f} LIKE ? ESCAPE '!'" for f in fields) + ")"
                params.extend([f"%{literal}%"] * len(fields))
            sql += " ORDER BY created_at DESC,id LIMIT ? OFFSET ?"
            rows = db.execute(sql, [*params, limit, offset]).fetchall()
            return [self._present(db, kind, self._snapshot(db, kind, r[0])) for r in rows]

    @staticmethod
    def _check_revision(row, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("请提供有效的记忆版本")
        if row["revision"] != expected_revision:
            raise RevisionConflict("记忆已在其他地方更新，请重新读取后再决定")

    @staticmethod
    def _check_running(db, row):
        # Today's manifest has recall_sources, not memory revisions. Ref overlap is
        # conservative; future manifests can name the derived ID directly. The
        # transaction also serializes edits against creation/completion of a run.
        def contains(value):
            if isinstance(value, dict):
                return any(contains(v) for v in value.values())
            if isinstance(value, list):
                return any(contains(v) for v in value)
            return value == row["id"]

        refs = {(r["id"], r["revision"]) for r in row["sources"]}
        for run in db.execute(
            "SELECT context_manifest FROM chat_runs WHERE agent_id=? AND status='running'",
            (row["agent_id"],),
        ):
            manifest = json.loads(run[0] or "{}")
            recalled = {
                (r.get("id"), r.get("revision")) for r in manifest.get("recall_sources", [])
            }
            if contains(manifest) or refs & recalled:
                raise Conflict("这条记忆正在用于交流，请等本次回复结束后再维护")

    @staticmethod
    def _audit(db, kind, prior, current, action):
        # Snapshots include original refs and revision. Runtime integration may
        # compare these records with the manifest captured before a model call.
        db.execute(
            "INSERT INTO memory_edits "
            "(id,agent_id,target_kind,target_id,revision,action,actor,prior,current,created_at) "
            "VALUES (?,?,?,?,?,?,'owner',?,?,?)",
            (
                new_id(),
                prior["agent_id"],
                kind,
                prior["id"],
                current["revision"],
                action,
                json.dumps(prior, ensure_ascii=False),
                json.dumps(current, ensure_ascii=False),
                utc_now(),
            ),
        )

    def correct(self, kind, item_id, *, expected_revision, **changes):
        table, _, _ = self._tables(kind)
        fields = {"title", "summary"} if kind == "episodes" else {"statement"}
        if (
            not changes
            or not set(changes) <= fields
            or any(not isinstance(v, str) or not v.strip() for v in changes.values())
        ):
            raise ValueError("更正内容不能为空，且只能更正标题、摘要或知识主张")
        with self.database.transaction() as db:
            prior = self._snapshot(db, kind, item_id)
            self._check_revision(prior, expected_revision)
            self._validate(prior)
            self._check_running(db, prior)
            current = prior | changes | {"revision": prior["revision"] + 1}
            assignments = ",".join(f"{field}=?" for field in changes)
            db.execute(
                f"UPDATE {table} SET {assignments},revision=? WHERE id=?",
                [*changes.values(), current["revision"], item_id],
            )
            if kind == "episodes":
                db.execute("DELETE FROM episode_search WHERE episode_id=?", (item_id,))
                db.execute(
                    "INSERT INTO episode_search VALUES (?,?,?)",
                    (item_id, current["title"], current["summary"]),
                )
            self._audit(db, kind, prior, current, "correct")
            return self._present(db, kind, current)

    def set_state(self, kind, item_id, *, expected_revision, action):
        table, sources, key = self._tables(kind)
        if action not in {"archive", "restore", "delete"}:
            raise ValueError("记忆操作无效")
        with self.database.transaction() as db:
            prior = self._snapshot(db, kind, item_id)
            self._check_revision(prior, expected_revision)
            if action == "restore":
                self._validate(prior)
                if prior["status"] != "archived":
                    raise Conflict("只能恢复已归档的记忆")
            if action == "archive" and prior["status"] != "active":
                raise Conflict("只能归档使用中的记忆；失效条目可以删除")
            self._check_running(db, prior)
            status = {"archive": "archived", "restore": "active", "delete": "deleted"}[action]
            current = prior | {"status": status, "revision": prior["revision"] + 1}
            self._audit(db, kind, prior, current, action)
            if action == "delete":
                db.execute(f"DELETE FROM {sources} WHERE {key}=?", (item_id,))
                if kind == "episodes":
                    db.execute("DELETE FROM episode_search WHERE episode_id=?", (item_id,))
                db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
                return {"id": item_id, "status": status, "revision": current["revision"]}
            db.execute(
                f"UPDATE {table} SET status=?,revision=? WHERE id=?",
                (status, current["revision"], item_id),
            )
            return self._present(db, kind, current)
