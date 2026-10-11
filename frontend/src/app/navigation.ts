import {
  ListTodo,
  Globe,
  Workflow,
  Files,
  House,
  MessageCircle,
  Settings2,
} from "lucide-react";

// Feature navigation is separate from the shell so new pages can be added here.
export const navigation = [
  { id: "overview", label: "概览", icon: House },
  { id: "chat", label: "对话", icon: MessageCircle },
  { id: "content", label: "资料", icon: Files },
  { id: "tasks", label: "任务", icon: ListTodo },
  { id: "browser", label: "浏览器", icon: Globe },
  { id: "background", label: "后台工作", icon: Workflow },
  { id: "settings", label: "设置", icon: Settings2 },
] as const;
export type PageId = (typeof navigation)[number]["id"];
