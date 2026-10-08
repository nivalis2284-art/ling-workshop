import { useState } from "react";
import type { LingApi } from "../../api/client";
import { errorMessage } from "../../api/client";
import type { ModelProfile, SavedModel } from "../../api/types";
import { ErrorNotice } from "../../shared/ui";
import { ModelLimits } from "./ModelLimits";

const blank: ModelProfile = {
  api_family: "openai_chat_completions",
  model: "",
  base_url: "",
  capabilities: {
    streaming: true,
    tools: true,
    thinking: false,
    effort_levels: [],
  },
};

export function ModelEditor({
  api,
  existing,
  saved,
}: {
  api: LingApi;
  existing?: SavedModel;
  saved: () => Promise<void>;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [profile, setProfile] = useState<ModelProfile>(
    existing?.profile ?? blank,
  );
  const [key, setKey] = useState("");
  const [cloning, setCloning] = useState(false);
  const [effort, setEffort] = useState(
    profile.capabilities.effort_levels.join(", "),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const effortLevels = [
    ...new Set(
      effort
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
  function clonePreset() {
    if (!existing || busy) return;
    setCloning(true);
    setName(`${name.trim()} 副本`.slice(0, 100));
    // Keys belong to a separate runtime submission, never a cloned profile.
    setKey("");
    setError("");
  }
  async function submit() {
    if (busy) return;
    setError("");
    try {
      const url = new URL(profile.base_url.trim());
      if (
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        url.search ||
        url.hash ||
        /[?#\s]/.test(profile.base_url)
      )
        throw new Error();
    } catch {
      setError(
        "服务地址需要是完整的 HTTP 或 HTTPS 地址，不包含密码、查询参数或片段。",
      );
      return;
    }
    if (cloning && !key.trim()) {
      setError("新预设需要单独提供本次运行的密钥，原连接的密钥不会复制。");
      return;
    }
    if (
      profile.default_effort != null &&
      !effortLevels.includes(profile.default_effort)
    ) {
      setError("默认思考强度必须在支持的档位中，或改为服务商默认。");
      return;
    }
    for (const field of ["temperature", "top_p"] as const) {
      const value = profile[field];
      const upper =
        field === "temperature" && profile.api_family !== "anthropic_messages"
          ? 2
          : 1;
      if (
        value != null &&
        (!profile.capabilities[field] ||
          !Number.isFinite(value) ||
          value < 0 ||
          value > upper)
      ) {
        setError("生成参数必须由服务支持，并填写允许范围内的数字。");
        return;
      }
    }
    if (
      profile.cache_system_prompt &&
      (profile.api_family !== "anthropic_messages" ||
        !profile.capabilities.prompt_caching)
    ) {
      setError("系统前缀缓存需要声明 Anthropic 渠道支持。");
      return;
    }
    const prices = {
      input_price_per_million:
        profile.input_price_per_million == null
          ? null
          : Number(profile.input_price_per_million),
      output_price_per_million:
        profile.output_price_per_million == null
          ? null
          : Number(profile.output_price_per_million),
    };
    if (
      Object.values(prices).some(
        (value) => value != null && (!Number.isFinite(value) || value < 0),
      )
    ) {
      setError("价格需要是非负数字；未知请留空。");
      return;
    }
    setBusy(true);
    try {
      await api.saveModel({
        name: name.trim(),
        profile: {
          ...profile,
          ...prices,
          model: profile.model.trim(),
          base_url: profile.base_url.trim(),
          capabilities: {
            ...profile.capabilities,
            effort_levels: effortLevels,
          },
        },
        ...(existing && !cloning ? { profile_id: existing.id } : {}),
        ...(key ? { api_key: key } : {}),
      });
      setKey("");
      await saved();
    } catch (error) {
      setError(
        key
          ? errorMessage(error).split(key).join("[密钥已隐藏]")
          : errorMessage(error),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="model-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h3>
        {cloning ? "另存模型预设" : existing ? "编辑模型连接" : "添加模型连接"}
      </h3>
      <fieldset disabled={busy}>
        <div className="model-fields">
          <label>
            连接名称
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="给这个连接起个容易辨认的名字"
              required
              maxLength={100}
            />
          </label>
          <label>
            接口类型
            <select
              value={profile.api_family}
              onChange={(event) =>
                setProfile({
                  ...profile,
                  api_family: event.target.value as ModelProfile["api_family"],
                  ...(event.target.value !== "anthropic_messages"
                    ? {
                        cache_system_prompt: false,
                        capabilities: {
                          ...profile.capabilities,
                          prompt_caching: false,
                        },
                      }
                    : {}),
                })
              }
            >
              <option value="openai_chat_completions">
                OpenAI 兼容 · Chat Completions
              </option>
              <option value="openai_responses">OpenAI · Responses</option>
              <option value="anthropic_messages">Anthropic · Messages</option>
            </select>
          </label>
          <label>
            模型名称
            <input
              value={profile.model}
              onChange={(event) =>
                setProfile({ ...profile, model: event.target.value })
              }
              placeholder="填写服务商提供的模型名称"
              required
            />
          </label>
          <label>
            服务地址
            <input
              type="url"
              value={profile.base_url}
              onChange={(event) =>
                setProfile({ ...profile, base_url: event.target.value })
              }
              placeholder="填写服务商提供的完整接口地址"
              required
              autoComplete="off"
            />
          </label>
        </div>
        <label>
          本次运行的模型密钥
          <input
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby="model-key-help"
            placeholder={
              cloning
                ? "请单独输入新预设的密钥"
                : existing?.credentials_available
                  ? "留空可保留本次运行已有的密钥"
                  : "可选，调用需要凭据的服务时填写"
            }
          />
        </label>
        <p id="model-key-help" className="field-help">
          密钥仅在本地服务的内存中使用，保存成功后输入框会清空。服务重启后需要重新提供；修改服务地址时也请重新提供密钥。
          {cloning && "新预设需要单独提交密钥，不会读取或复制原连接的凭据。"}
        </p>
        <details className="model-capabilities">
          <summary>高级选项：服务支持的能力</summary>
          <p className="small muted">
            请按服务商说明填写，页面不会根据模型名称猜测能力。
          </p>
          <div className="checkbox-row">
            {(
              [
                "streaming",
                "tools",
                "thinking",
                "images",
                "pdf",
                "temperature",
                "top_p",
                "prompt_caching",
              ] as const
            )
              .filter(
                (capability) =>
                  capability !== "prompt_caching" ||
                  profile.api_family === "anthropic_messages",
              )
              .map((capability) => (
                <label key={capability}>
                  <input
                    type="checkbox"
                    checked={profile.capabilities[capability] ?? false}
                    onChange={(event) =>
                      setProfile({
                        ...profile,
                        ...(!event.target.checked &&
                        capability === "temperature"
                          ? { temperature: null }
                          : {}),
                        ...(!event.target.checked && capability === "top_p"
                          ? { top_p: null }
                          : {}),
                        ...(!event.target.checked &&
                        capability === "prompt_caching"
                          ? { cache_system_prompt: false }
                          : {}),
                        capabilities: {
                          ...profile.capabilities,
                          [capability]: event.target.checked,
                        },
                      })
                    }
                  />
                  {
                    {
                      streaming: "流式输出",
                      tools: "工具调用",
                      thinking: "思考选项",
                      images: "图片输入",
                      pdf: "PDF 输入",
                      temperature: "支持温度参数",
                      top_p: "支持核采样参数",
                      prompt_caching: "支持 Anthropic 系统前缀缓存",
                    }[capability]
                  }
                </label>
              ))}
          </div>
          <label>
            支持的思考强度
            <input
              value={effort}
              onChange={(event) => setEffort(event.target.value)}
              placeholder="不支持请留空；多个值用逗号分隔"
            />
          </label>
        </details>
        <ModelLimits
          profile={{
            ...profile,
            capabilities: {
              ...profile.capabilities,
              effort_levels: effortLevels,
            },
          }}
          change={setProfile}
        />
        {error && <ErrorNotice message={error} />}
        {existing && !cloning && (
          <button
            type="button"
            className="button secondary"
            onClick={clonePreset}
          >
            另存为新预设
          </button>
        )}
        <button
          className="button primary"
          disabled={
            busy ||
            !name.trim() ||
            !profile.model.trim() ||
            !profile.base_url.trim() ||
            (cloning && !key.trim())
          }
        >
          {busy ? "正在保存…" : cloning ? "保存新预设" : "保存模型配置"}
        </button>
      </fieldset>
    </form>
  );
}
