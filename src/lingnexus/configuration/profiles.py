"""Non-secret profiles and endpoint-bound credentials with an optional user cache."""

import json
from threading import RLock

from lingnexus.common.errors import NotFound, PermissionDenied
from lingnexus.common.types import new_id
from lingnexus.configuration.credential_cache import CredentialCache
from lingnexus.configuration.settings import read_json, write_json
from lingnexus.models import ModelProfile, RuntimeCredentials


class Profiles:
    def __init__(self, database, *, cache: CredentialCache | None = None):
        self.database = database
        self.cache = cache
        self._keys: dict[str, tuple[str, str]] = cache.load() if cache else {}
        self._lock = RLock()

    def save(
        self,
        name: str,
        profile: ModelProfile,
        *,
        api_key: str | None = None,
        profile_id: str | None = None,
    ) -> dict:
        if not name.strip():
            raise ValueError("连接名称不能为空")
        with self.database.transaction() as db, self._lock:
            if (
                profile_id
                and not db.execute(
                    "SELECT 1 FROM model_profiles WHERE id=?", (profile_id,)
                ).fetchone()
            ):
                raise NotFound("模型连接不存在")
            pid = profile_id or new_id()
            db.execute(
                "INSERT INTO model_profiles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,profile=excluded.profile",
                (pid, name, profile.model_dump_json()),
            )
            # Bind each runtime key to its configured endpoint, never to a reusable ID alone.
            if api_key is not None or (
                pid in self._keys and self._keys[pid][0] != profile.base_url
            ):

                def publish_key() -> None:
                    with self._lock:
                        next_keys = self._keys.copy()
                        if api_key:
                            next_keys[pid] = (profile.base_url, api_key)
                        else:
                            next_keys.pop(pid, None)
                        if self.cache:
                            self.cache.save(next_keys)
                        self._keys = next_keys

                self.database.after_commit(publish_key)
        return self.get(pid)

    def get(self, profile_id: str) -> dict:
        with self.database.transaction() as db:
            row = db.execute("SELECT * FROM model_profiles WHERE id=?", (profile_id,)).fetchone()
            if row is None:
                raise NotFound("模型连接不存在")
            result = {"id": row["id"], "name": row["name"], "profile": json.loads(row["profile"])}
        with self._lock:
            saved = self._keys.get(profile_id)
            result["credentials_available"] = (
                saved is not None and saved[0] == result["profile"]["base_url"]
            )
        return result

    def list(self) -> list[dict]:
        with self.database.transaction() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM model_profiles ORDER BY rowid")]
        return [self.get(pid) for pid in ids]

    def credentials(self, profile_id: str) -> RuntimeCredentials:
        return self.resolve(profile_id)[1]

    def resolve(self, profile_id: str) -> tuple[ModelProfile, RuntimeCredentials]:
        """Take a matching configuration/credential snapshot for a network call."""
        with self.database.transaction(), self._lock:
            item = self.get(profile_id)
            saved = self._keys.get(profile_id)
            if saved is None or saved[0] != item["profile"]["base_url"]:
                raise PermissionDenied("本次运行尚未配置该模型的凭据")
            return ModelProfile.model_validate(item["profile"]), RuntimeCredentials(
                api_key=saved[1]
            )

    def select(self, primary: str, fallback: str | None) -> dict:
        with self.database.transaction() as db:
            # Query without credentials lock; never invert database/credential lock order.
            for pid in (primary, fallback):
                if (
                    pid
                    and not db.execute("SELECT 1 FROM model_profiles WHERE id=?", (pid,)).fetchone()
                ):
                    raise NotFound("所选模型连接不存在")
            value = {"primary": primary, "fallback": fallback}
            write_json(self.database, "model_selection", value)
            return value

    def selection(self) -> dict:
        return read_json(
            self.database, "model_selection", {"primary": None, "fallback": None}
        )

    def clear_credentials(self) -> None:
        with self._lock:
            self._keys.clear()

    def cache_status(self) -> dict:
        with self.database.transaction(), self._lock:
            return {
                "enabled": self.cache is not None,
                "cached_profiles": sum(item["credentials_available"] for item in self.list()),
                "error": self.cache.error if self.cache else None,
            }

    def clear_cached_credentials(self) -> dict:
        with self.database.transaction(), self._lock:
            if self.cache:
                self.cache.clear()
            self._keys.clear()
            return self.cache_status()
