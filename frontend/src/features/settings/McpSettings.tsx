import { useCallback, useState } from "react";
import type { LingApi } from "../../api/client";
import { errorMessage } from "../../api/client";
import type { McpServerEntry } from "../../api/types";
import { useResource } from "../../shared/useResource";
import { ErrorNotice, Loading } from "../../shared/ui";

function serverSummary(entry: McpServerEntry) {
  if (entry.transport === "http") return "http · " + (entry.url ?? "");
  const command = [entry.command, ...(entry.args ?? [])].join(" ");
  return "stdio · " + command;
}

/** MCP 工具连接：远程工具与本地工具走同一权限闸门；未配置时不建立任何连接，
 * 配置无效时逐条报错、旧配置保持不变。 */
export function McpSettings({ api }: { api: LingApi }) {
  const load = useCallback(
    (signal: AbortSignal) => api.mcpServers(signal),
    [api],
  );
  const resource = useResource(load);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const servers = resource.status === "ready" ? resource.data.servers : [];

  const save = async (next: McpServerEntry[]): Promise<boolean> => {
    if (busy) return false;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.saveMcpServers(next);
      setNotice("已保存。新的配置会在这台服务下一次使用时生效。");
      await resource.refresh();
      return true;
    } catch (exception) {
      setError(errorMessage(exception));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const entry: McpServerEntry =
      transport === "http"
        ? { name: name.trim(), transport: "http", url: url.trim() }
        : {
            name: name.trim(),
            transport: "stdio",
            command: command.trim(),
            ...(args.trim() ? { args: args.trim().split(/\s+/) } : {}),
          };
    if (!entry.name || (transport === "http" ? !entry.url : !entry.command)) {
      setError("请填写名称，以及命令（stdio）或地址（http）。");
      return;
    }
    if (await save([...servers, entry])) {
      setName("");
      setCommand("");
      setArgs("");
      setUrl("");
    }
  };

  return (
    <section className="panel section-panel stack" aria-label="MCP 工具连接">
      <div className="section-heading">
        <h3>MCP 工具连接</h3>
        <button
          className="button small-button secondary"
          disabled={resource.status === "loading"}
          onClick={() => void resource.refresh()}
        >
          刷新
        </button>
      </div>
      <p className="muted">
        远程工具与本地工具走同一权限闸门；未配置时不会建立任何连接，某个服务器出问题也不会影响对话。修改在保存后于下一次使用时生效。
      </p>
      {resource.status === "loading" && <Loading label="正在读取 MCP 配置…" />}
      {resource.status === "error" && (
        <ErrorNotice
          message={resource.error}
          retry={() => void resource.refresh()}
        />
      )}
      {resource.status === "ready" && (
        <>
          {servers.length === 0 ? (
            <p className="muted small">还没有配置服务器。</p>
          ) : (
            <ul className="model-list">
              {servers.map((entry) => (
                <li key={entry.name}>
                  <div>
                    <strong>{entry.name}</strong>
                    <p className="metadata">
                      {serverSummary(entry)}
                      {entry.enabled === false ? " · 已停用" : ""}
                    </p>
                  </div>
                  <div className="button-row">
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() =>
                        void save(
                          servers.map((item) =>
                            item.name === entry.name
                              ? { ...item, enabled: item.enabled === false }
                              : item,
                          ),
                        )
                      }
                    >
                      {entry.enabled === false ? "启用" : "停用"}
                    </button>
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() =>
                        void save(
                          servers.filter((item) => item.name !== entry.name),
                        )
                      }
                    >
                      移除
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <div className="mcp-add">
            <label>
              名称
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如 filesystem"
              />
            </label>
            <label>
              传输方式
              <select
                value={transport}
                onChange={(event) =>
                  setTransport(event.target.value as "stdio" | "http")
                }
              >
                <option value="stdio">stdio（本地进程）</option>
                <option value="http">http（远程地址）</option>
              </select>
            </label>
            {transport === "stdio" ? (
              <>
                <label>
                  命令
                  <input
                    value={command}
                    onChange={(event) => setCommand(event.target.value)}
                    placeholder="例如 npx"
                  />
                </label>
                <label>
                  参数（以空格分隔）
                  <input
                    value={args}
                    onChange={(event) => setArgs(event.target.value)}
                    placeholder="例如 -y @modelcontextprotocol/server-filesystem"
                  />
                </label>
              </>
            ) : (
              <label>
                地址
                <input
                  value={url}
                  onChange={(event) => setUrl(event.target.value)}
                  placeholder="例如 https://kb.local/mcp"
                />
              </label>
            )}
            <div>
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void add()}
              >
                添加服务器
              </button>
            </div>
          </div>
        </>
      )}
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="notice success" role="status">
          {notice}
        </p>
      )}
    </section>
  );
}
