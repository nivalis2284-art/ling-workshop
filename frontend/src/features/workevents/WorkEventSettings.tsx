import { useCallback, useState } from "react";
import { errorMessage } from "../../api/client";
import type { LingApi } from "../../api/client";
import type { Agent, SavedModel } from "../../api/types";
import { ErrorNotice, Loading } from "../../shared/ui";
import { useResource } from "../../shared/useResource";
import { workEventSettingsApi } from "./api";
import type { WorkEventSettings } from "./api";

/**
 * 工作事件概况的可选整理模型（P4 预留接口）。默认关闭；只有所有者在这里明确
 * 选择连接后，才会用所选模型生成概况，绝不默认使用主用模型。
 */
export function WorkEventSummarySettings({
  api,
  agent,
}: {
  api: LingApi;
  agent: Agent;
}) {
  const load = useCallback(
    async (signal: AbortSignal) => {
      const [settings, models] = await Promise.all([
        workEventSettingsApi(api, agent.id).read(signal),
        api.models(signal),
      ]);
      return { settings, models };
    },
    [api, agent.id],
  );
  const resource = useResource(load);
  return (
    <section className="panel section-panel stack" aria-label="工作事件概况">
      <div className="section-heading">
        <h3>工作事件概况模型（可选）</h3>
      </div>
      {resource.status === "loading" && <Loading label="正在读取概况设置…" />}
      {resource.status === "error" && (
        // 这是可选能力：读不到就安静地当作未配置，不用错误横幅打扰设置页。
        <p className="metadata" role="status">
          概况整理服务当前不可用（{resource.error}）。你可以照常使用工作事件与
          原文回读；需要时再点下面的按钮重试。
          <br />
          <button
            type="button"
            className="text-button"
            onClick={() => void resource.refresh()}
          >
            重新读取概况设置
          </button>
        </p>
      )}
      {resource.status === "ready" && (
        <Editor
          api={api}
          agentId={agent.id}
          initial={resource.data.settings}
          models={resource.data.models}
          refresh={resource.refresh}
        />
      )}
    </section>
  );
}

function Editor({
  api,
  agentId,
  initial,
  models,
  refresh,
}: {
  api: LingApi;
  agentId: string;
  initial: WorkEventSettings;
  models: SavedModel[];
  refresh: () => Promise<void>;
}) {
  const [enabled, setEnabled] = useState(initial.enabled);
  const [profileId, setProfileId] = useState(initial.profile_id ?? "");
  const [maxCalls, setMaxCalls] = useState(String(initial.max_calls));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requested = Number(maxCalls);
  const validCalls =
    Number.isInteger(requested) && requested >= 1 && requested <= 8;
  // 启用必须绑定一个明确选择的连接：没有选择就不允许保存为启用。
  const ready = !enabled || !!profileId;

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (busy) return;
        setBusy(true);
        setError("");
        setNotice("");
        void workEventSettingsApi(api, agentId)
          .save({
            enabled,
            profile_id: profileId || null,
            max_calls: validCalls ? requested : 4,
          })
          .then((value) => {
            setEnabled(value.enabled);
            setProfileId(value.profile_id ?? "");
            setMaxCalls(String(value.max_calls));
            setNotice(
              value.enabled
                ? "已启用。结束工作后可请求用所选模型生成概况；生成结果属于未核验文本。"
                : "已关闭。关闭后不会调用任何模型生成概况，原有概况文本保留。",
            );
            return refresh();
          })
          .catch((cause) => setError(errorMessage(cause)))
          .finally(() => setBusy(false));
      }}
    >
      <p className="muted">
        默认关闭。启用后，只有你在对话里明确请求时才会调用这里选择的模型来生成这段
        工作的短概况；费用按该连接的服务商价格结算，不由主用模型代为计费。
      </p>
      <label>
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy}
          onChange={(event) => {
            setEnabled(event.target.checked);
            setNotice("");
          }}
        />
        允许用所选模型生成工作事件概况
      </label>
      <label htmlFor="work-event-profile">用于生成概况的连接</label>
      <select
        id="work-event-profile"
        value={profileId}
        disabled={busy}
        onChange={(event) => {
          setProfileId(event.target.value);
          setNotice("");
        }}
      >
        <option value="">未选择（不会调用模型）</option>
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.name}
            {model.credentials_available ? "" : "（尚未提供密钥）"}
          </option>
        ))}
      </select>
      <label htmlFor="work-event-max-calls">单次整理最多调用模型次数</label>
      <input
        id="work-event-max-calls"
        type="number"
        min={1}
        max={8}
        step={1}
        value={maxCalls}
        disabled={busy}
        onChange={(event) => {
          setMaxCalls(event.target.value);
          setNotice("");
        }}
      />
      <p className="field-help">
        允许 1–8 次，默认 4
        次。生成失败不会覆盖你已经更正的概况，也不会改变任何执行 状态。
      </p>
      {!ready && (
        <p className="metadata">
          想启用时请先选择具体的连接；主用模型不会被自动使用。
        </p>
      )}
      <div className="button-row">
        <button
          className="button secondary"
          disabled={busy || !ready || !validCalls}
        >
          {busy ? "正在保存…" : "保存概况设置"}
        </button>
      </div>
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
    </form>
  );
}
