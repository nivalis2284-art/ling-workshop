import type { ModelProfile } from "../../api/types";

export function ModelLimits({
  profile,
  change,
}: {
  profile: ModelProfile;
  change: (profile: ModelProfile) => void;
}) {
  const anthropic = profile.api_family === "anthropic_messages";
  const levels = profile.capabilities.effort_levels;
  return (
    <>
      <details className="model-capabilities">
        <summary>高级选项：上下文与回复长度</summary>
        <p className="field-help">
          通常保留默认即可。只有确认服务商限制或需要更长回复时调整；思考内容也可能占用回复额度。
        </p>
        <label>
          上下文窗口上限
          <input
            aria-label="上下文窗口上限"
            type="number"
            min={1024}
            step={1}
            value={profile.context_window_tokens ?? ""}
            placeholder="未填写时使用保守估算"
            onChange={(e) =>
              change({
                ...profile,
                context_window_tokens: e.target.value
                  ? Number(e.target.value)
                  : null,
              })
            }
          />
        </label>
        <label>
          单次回复长度上限
          <input
            aria-label="单次回复长度上限"
            type="number"
            min={128}
            step={1}
            value={profile.max_output_tokens ?? ""}
            placeholder="默认 4096，思考较多时可提高"
            onChange={(e) =>
              change({
                ...profile,
                max_output_tokens: e.target.value
                  ? Number(e.target.value)
                  : null,
              })
            }
          />
        </label>
      </details>
      <details className="model-capabilities">
        <summary>高级选项：生成设置与费用</summary>
        <p className="field-help">
          留空使用服务商默认值。请先确认并声明参数支持；较新的推理模型可能拒绝温度和核采样参数。通常只调整其中一个。
        </p>
        <label>
          默认思考强度
          <select
            value={profile.default_effort ?? ""}
            disabled={!levels.length && !profile.default_effort}
            onChange={(e) =>
              change({ ...profile, default_effort: e.target.value || null })
            }
          >
            <option value="">服务商默认</option>
            {profile.default_effort &&
              !levels.includes(profile.default_effort) && (
                <option value={profile.default_effort}>
                  当前值不在支持的档位中
                </option>
              )}
            {levels.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>
        <p className="field-help">本次交流明确选择的思考强度优先于此预设。</p>
        <label>
          温度（temperature）
          <input
            type="number"
            min={0}
            max={anthropic ? 1 : 2}
            step="any"
            disabled={!profile.capabilities.temperature}
            value={profile.temperature ?? ""}
            placeholder="留空使用服务商默认"
            onChange={(e) =>
              change({
                ...profile,
                temperature: e.target.value ? Number(e.target.value) : null,
              })
            }
          />
        </label>
        <label>
          核采样（top_p）
          <input
            type="number"
            min={0}
            max={1}
            step="any"
            disabled={!profile.capabilities.top_p}
            value={profile.top_p ?? ""}
            placeholder="留空使用服务商默认"
            onChange={(e) =>
              change({
                ...profile,
                top_p: e.target.value ? Number(e.target.value) : null,
              })
            }
          />
        </label>
        {anthropic && (
          <>
            <label>
              <input
                type="checkbox"
                checked={profile.cache_system_prompt ?? false}
                disabled={!profile.capabilities.prompt_caching}
                onChange={(e) =>
                  change({ ...profile, cache_system_prompt: e.target.checked })
                }
              />
              缓存稳定系统前缀
            </label>
            <p className="field-help">
              明确开启后请求短时缓存稳定系统前缀。是否命中由服务商返回的用量确认；内容、思考设置或渠道变化可能使缓存失效。
            </p>
          </>
        )}
        <label>
          输入价格（USD / 百万 token）
          <input
            type="number"
            min={0}
            step="any"
            value={profile.input_price_per_million ?? ""}
            placeholder="未知"
            onChange={(e) =>
              change({
                ...profile,
                input_price_per_million: e.target.value
                  ? Number(e.target.value)
                  : null,
              })
            }
          />
        </label>
        <label>
          输出价格（USD / 百万 token）
          <input
            type="number"
            min={0}
            step="any"
            value={profile.output_price_per_million ?? ""}
            placeholder="未知"
            onChange={(e) =>
              change({
                ...profile,
                output_price_per_million: e.target.value
                  ? Number(e.target.value)
                  : null,
              })
            }
          />
        </label>
        <p className="field-help">
          按实际渠道填写价格；未填写表示未知。价格仅供估算，缓存读写、其他收费和最终账单需按渠道确认。
        </p>
      </details>
    </>
  );
}
