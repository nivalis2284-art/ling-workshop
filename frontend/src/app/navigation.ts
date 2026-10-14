import { Globe, Files, House, MessageCircle, Settings2 } from "lucide-react";

// Feature navigation is separate from the shell so new pages can be added here.
// 任务不再是顶层入口：C 阶段起对话是唯一入口，多步工作直接在对话里完成。
// 「后台工作」并入「设置 → 备份与数据」（2026-10-15 所有者定案），导航定为五项。
export const navigation = [
  { id: "overview", label: "概览", icon: House },
  { id: "chat", label: "对话", icon: MessageCircle },
  { id: "content", label: "资料", icon: Files },
  { id: "browser", label: "浏览器", icon: Globe },
  { id: "settings", label: "设置", icon: Settings2 },
] as const;
export type PageId = (typeof navigation)[number]["id"];
