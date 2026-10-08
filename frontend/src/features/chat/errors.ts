import { ApiError } from "../../api/client";

export class ChatStreamError extends Error {
  constructor(public readonly code: string) {
    super(code);
  }
}

export function chatErrorMessage(error: unknown): string {
  const code =
    error instanceof ChatStreamError || error instanceof ApiError
      ? error.code
      : undefined;
  switch (code) {
    case "missing_credentials":
    case "credentials_unavailable":
    case "no_credentials":
    case "本次运行尚未配置该模型的凭据":
      return "当前模型尚未提供密钥，请到设置中填写本次运行的模型密钥后再发送。";
    case "no_primary_model":
    case "model_not_configured":
    case "profile_not_found":
    case "模型连接不存在":
    case "Select a primary model or supply profile_id before chatting.":
      return "没有可用的模型配置，请到设置中添加模型并选择主用模型。";
    case "provider_error":
    case "http_error":
      return "模型服务暂时无法完成回复，请检查设置中的服务地址、模型名称和密钥，再重试。";
    case "invalid_request":
      return "模型暂时无法接受这条消息，请检查模型配置、凭据和输入后重试。";
    case "timeout":
      return "模型响应超时。可以稍后重试，或到设置中检查模型连接。";
    case "malformed_stream":
    case "content_type":
      return "收到的回复数据无法读取。请重新连接以恢复当前生成。";
    case "unexpected_eof":
    case "transport_error":
      return "回复连接已中断，生成可能仍在继续。请重新连接当前生成。";
    default:
      if (error instanceof ApiError && error.status === 401)
        return error.message;
      if (error instanceof ApiError && error.status === 403)
        return "本地服务拒绝了访问，请从启动器重新打开。";
      if (error instanceof ApiError && error.status === 404)
        return "找不到这次生成，请检查本地服务是否已重启。";
      return "暂时无法取得回复，请检查本地服务和模型连接后重试。";
  }
}

// Runtime error events are terminal, unlike a browser subscriber disconnect.
export function chatRunErrorMessage(code: string): string {
  switch (code) {
    case "transport_error":
      return "模型服务连接失败，本次生成已结束。请检查模型连接后再次发送。";
    case "unexpected_eof":
    case "incomplete_response":
      return "模型服务未能完成回复，本次生成已结束。请稍后再次发送。";
    case "malformed_stream":
    case "content_type":
      return "模型服务返回了无法读取的数据，本次生成已结束。请检查模型配置后再次发送。";
    case "interrupted":
      return "本地服务停止或重启，本次生成已中断。请确认服务已恢复后再次发送。";
    case "context_invalidated":
      return "生成期间相关资料已被纠正，本次回复已停止保存。请根据最新资料重新发送。";
    case "credential_in_response":
      return "服务返回了敏感运行信息，这次回复已停止保存。请检查服务连接。";
    case "incomplete_output":
      return "模型因输出限制等原因未完成回复；已收到内容仅供回看。请调整输出设置后重试。";
    case "tools_unsupported":
      return "模型请求使用工具，当前对话仅支持文字。请调整模型配置后再次发送。";
    default:
      return chatErrorMessage(new ChatStreamError(code));
  }
}
