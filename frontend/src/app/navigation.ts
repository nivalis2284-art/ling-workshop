import {
  Activity,
  ListTodo,
  Globe,
  Workflow,
  Sparkles,
  BookOpen,
  Files,
  House,
  MessageCircle,
  Settings2,
  ShieldCheck,
} from "lucide-react";

// Feature navigation is separate from the shell so chat/activity modules can be added here.
export const navigation = [
  { id: "overview", label: "概览", icon: House },
  { id: "chat", label: "对话", icon: MessageCircle },
  { id: "identity", label: "身份审阅", icon: ShieldCheck },
  { id: "memory", label: "记忆", icon: BookOpen },
  { id: "content", label: "资料", icon: Files },
  { id: "tasks", label: "任务", icon: ListTodo },
  { id: "activities", label: "活动与关注", icon: Activity },
  { id: "skills", label: "技能", icon: Sparkles },
  { id: "browser", label: "浏览器", icon: Globe },
  { id: "background", label: "后台工作", icon: Workflow },
  { id: "settings", label: "设置", icon: Settings2 },
] as const;
export type PageId = (typeof navigation)[number]["id"];
export const upcoming: {
  label: string;
  icon: typeof Activity;
  description: string;
}[] = [];
