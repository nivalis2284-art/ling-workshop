import { ErrorNotice, Loading } from "../../shared/ui";
import "./management.css";

export function ResourceNotice({
  status,
  error,
  hasData,
  refresh,
}: {
  status: string;
  error?: string;
  hasData: boolean;
  refresh: () => Promise<void>;
}) {
  return (
    <>
      {status === "loading" && !hasData && <Loading />}
      {status === "error" && (
        <ErrorNotice
          message={error ?? "读取未完成。"}
          retry={() => void refresh()}
        />
      )}
    </>
  );
}
export function RecordData({
  value,
  label = "查看完整记录",
}: {
  value: unknown;
  label?: string;
}) {
  return (
    <details className="management-record">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
