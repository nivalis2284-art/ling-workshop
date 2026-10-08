import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, type LingApi } from "../../api/client";
import type { Attachment } from "../../api/types";
import { useResource } from "../../shared/useResource";
import type { ChatSession } from "./session";

export function useChatAttachments(
  api: LingApi,
  agentId: string,
  session: ChatSession,
  referencedIds: string[],
) {
  const load = useCallback(
    (signal: AbortSignal) => api.attachments(agentId, signal),
    [api, agentId],
  );
  const resource = useResource(load);
  const [extra, setExtra] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string>();
  const [lookupError, setLookupError] = useState<string>();
  const uploadController = useRef<AbortController | null>(null);
  const items = [
    ...(resource.status === "ready" ? resource.data : []),
    ...extra,
  ].filter(
    (item, index, all) =>
      all.findIndex((value) => value.id === item.id) === index,
  );
  const missing = JSON.stringify(
    [...new Set(referencedIds)].filter(
      (id) => !items.some((item) => item.id === id),
    ),
  );
  // The saved-file list is bounded. Resolve referenced older files individually
  // so restored selections and message bubbles retain their actual names.
  useEffect(() => {
    if (resource.status !== "ready") return;
    const ids: string[] = JSON.parse(missing);
    if (!ids.length) {
      setLookupError(undefined);
      return;
    }
    const controller = new AbortController();
    setLookupError(undefined);
    void Promise.all(ids.map((id) => api.attachment(id, controller.signal)))
      .then((values) => {
        if (!controller.signal.aborted)
          setExtra((previous) => [...previous, ...values]);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLookupError(errorMessage(error));
      });
    return () => controller.abort();
  }, [api, missing, resource.status]);
  useEffect(
    () => () => {
      uploadController.current?.abort();
      uploadController.current = null;
    },
    [],
  );
  const upload = async (file: File) => {
    if (uploadController.current) return;
    const controller = new AbortController();
    uploadController.current = controller;
    setUploading(true);
    setUploadError(undefined);
    try {
      const attachment = await api.uploadAttachment(
        agentId,
        file,
        controller.signal,
      );
      if (controller.signal.aborted || uploadController.current !== controller)
        return;
      setExtra((previous) => [attachment, ...previous]);
      session.setAttachments([
        ...session.getSnapshot().attachmentIds,
        attachment.id,
      ]);
    } catch (error) {
      if (!controller.signal.aborted && uploadController.current === controller)
        setUploadError(errorMessage(error));
    } finally {
      if (uploadController.current === controller) {
        uploadController.current = null;
        setUploading(false);
      }
    }
  };
  return {
    items,
    uploading,
    upload,
    uploadError,
    loading: resource.status === "loading",
    error: resource.status === "error" ? resource.error : lookupError,
    refresh: () => {
      setLookupError(undefined);
      void resource.refresh();
    },
  };
}
