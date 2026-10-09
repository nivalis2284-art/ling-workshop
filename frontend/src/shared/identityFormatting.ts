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

// The seven buckets the owner page shows, in the owner's chosen order.
export const kindOrder = [
  "identity",
  "major_personality",
  "habit",
  "relationship",
  "value",
  "major_capability",
  "custom",
];

export const kindLabels: Record<string, string> = {
  identity: "身份理解",
  major_personality: "性格",
  habit: "习惯",
  relationship: "关系",
  value: "价值观",
  major_capability: "能力",
  custom: "自定义条目",
};

const kindPrefixes: Record<string, string> = {
  identity: "identity.",
  major_personality: "personality.",
  habit: "communication.",
  relationship: "relationship.",
  value: "values.",
  major_capability: "capability.",
  custom: "custom.",
};

const prefixes: string[] = Object.values(kindPrefixes);

export function kindLabel(kind?: string) {
  return (kind && kindLabels[kind]) || "自定义条目";
}

/** Preset name, else the owner's own name, else the raw key — never a blank stand-in. */
export function identityEntryLabel(key: string): string {
  const preset = entryLabels.get(key);
  if (preset) return preset;
  for (const prefix of prefixes) {
    if (key.startsWith(prefix)) return key.slice(prefix.length);
  }
  return key;
}

export function identityEntryText(key: string, text: string): string {
  return entryValues.get(key)?.get(text) ?? text;
}

/** Fixed choices for the two keys whose values are an agreed short list. */
export function entryChoices(
  key: string,
): { value: string; label: string }[] | null {
  const map = entryValues.get(key);
  return map
    ? [...map].map(([value, label]) => ({ value, label }))
    : null;
}

export function entryKeyFor(kind: string, name: string): string {
  const prefix = kindPrefixes[kind] ?? "custom.";
  const trimmed = name.trim();
  return trimmed.startsWith(prefix) ? trimmed : prefix + trimmed;
}
