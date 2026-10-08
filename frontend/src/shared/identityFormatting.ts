const entryLabels = new Map([
  ["identity.description", "自我描述"],
  ["identity.name", "当前名字"],
  ["personality.expression", "表达倾向"],
  ["values.understanding", "价值理解"],
  ["communication.response_length", "回复详略"],
  ["communication.explanation_order", "说明顺序"],
]);

const entryValues = new Map<string, Map<string, string>>([
  [
    "communication.response_length",
    new Map([
      ["concise", "简洁"],
      ["balanced", "适中"],
      ["detailed", "详细"],
    ]),
  ],
  [
    "communication.explanation_order",
    new Map([
      ["conclusion_first", "先说结论"],
      ["steps_first", "先讲步骤"],
    ]),
  ],
]);

export function identityEntryLabel(key: string): string {
  return entryLabels.get(key) ?? "自我认识条目";
}

export function identityEntryText(key: string, text: string): string {
  return entryValues.get(key)?.get(text) ?? text;
}
