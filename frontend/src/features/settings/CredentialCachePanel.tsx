import { useState } from "react";
import { errorMessage, type LingApi } from "../../api/client";
import { ConfirmDialog } from "../../shared/ConfirmDialog";
import { ErrorNotice } from "../../shared/ui";

export function CredentialCachePanel({
  api,
  onCleared,
}: {
  api: LingApi;
  onCleared: () => void | Promise<void>;
}) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function clear() {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.call("/models/credentials/cache", { method: "DELETE" });
      setConfirm(false);
      setNotice(
        "密钥缓存已清除，模型连接和其他资料保留。下次调用前请重新填写密钥。",
      );
      await onCleared();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-label="模型密钥缓存">
      <p className="field-help">
        各连接的密钥分别保存在本地加密缓存，重启后自动读取。缓存仅当前 Windows
        用户可解密，不包含在备份或导出中。清除后，下次调用需要重新填写。
      </p>
      <button
        className="button secondary"
        type="button"
        disabled={busy}
        onClick={() => {
          setError("");
          setNotice("");
          setConfirm(true);
        }}
      >
        清除模型密钥缓存
      </button>
      {notice && (
        <p role="status" className="notice success">
          {notice}
        </p>
      )}
      {error && !confirm && <ErrorNotice message={error} />}
      {confirm && (
        <ConfirmDialog
          title="清除模型密钥缓存"
          confirmLabel="确认清除密钥缓存"
          busy={busy}
          onCancel={() => setConfirm(false)}
          onConfirm={() => void clear()}
        >
          <p>
            将清除所有模型连接的本地密钥缓存和当前运行中的密钥。已发出的请求可能继续完成；后续调用需要重新填写。模型连接、对话、记忆和其他资料会保留。
          </p>
          {error && <ErrorNotice message={error} />}
        </ConfirmDialog>
      )}
    </section>
  );
}
