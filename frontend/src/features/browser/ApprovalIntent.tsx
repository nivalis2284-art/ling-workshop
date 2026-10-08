import { actionLabels } from "./api";
import type { BrowserApproval } from "./api";
import { RecordData } from "../tasks/ManagementUi";

export function ApprovalIntent({ approval }: { approval: BrowserApproval }) {
  const intent = approval.intent;
  const target =
    intent.target && typeof intent.target === "object"
      ? (intent.target as Record<string, unknown>)
      : null;
  const form =
    intent.form && typeof intent.form === "object"
      ? (intent.form as Record<string, unknown>)
      : null;
  const fields = Array.isArray(form?.fields)
    ? form.fields.filter(
        (field): field is Record<string, unknown> =>
          Boolean(field) && typeof field === "object",
      )
    : [];
  return (
    <div>
      <p>
        操作：
        {actionLabels[String(intent.action)] ??
          String(intent.action ?? "服务未提供")}
      </p>
      {typeof intent.url === "string" && (
        <p className="management-copy">页面：{intent.url}</p>
      )}
      {typeof target?.text === "string" && (
        <p className="management-copy">
          目标：<strong>{target.text}</strong>
        </p>
      )}
      {typeof target?.href === "string" && (
        <p className="management-copy">目标地址：{target.href}</p>
      )}
      {form && (
        <>
          <p className="field-label">即将提交的表单内容</p>
          {typeof form.action === "string" && (
            <p className="management-copy">提交到：{form.action}</p>
          )}
          <dl className="entry-list">
            {fields.map((field, index) => (
              <div key={index}>
                <dt>
                  {String(field.name || field.id || "字段 " + (index + 1))}
                  {field.disabled ? "（当前禁用）" : ""}
                </dt>
                <dd>
                  {field.secret ? "敏感内容已隐藏" : String(field.value ?? "")}
                  {["checkbox", "radio"].includes(String(field.type))
                    ? field.checked
                      ? "（已选中）"
                      : "（未选中）"
                    : ""}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
      <RecordData value={intent} label="核对完整操作意图" />
      <p className="metadata">本次操作核对码</p>
      <p className="management-copy metadata">{approval.intent_hash}</p>
    </div>
  );
}
