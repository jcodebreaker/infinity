import type { WorkspaceAttachmentRef } from "@superchat/core";
import { FileText, Image as ImageIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { rpc } from "../lib/rpc";

const thumbnails = new Map<string, Promise<string | null>>();

/** An image from the host workspace as a data URL, read once per path for the life of the tab. */
function loadThumbnail(botId: string, ref: WorkspaceAttachmentRef): Promise<string | null> {
  const key = `${botId}:${ref.path}`;
  let found = thumbnails.get(key);
  if (!found) {
    found = rpc.files
      .read({ botId, path: ref.path })
      .then((file) =>
        file.contentBase64
          ? `data:${file.mimeType || ref.mimeType};base64,${file.contentBase64}`
          : null,
      )
      .catch(() => {
        thumbnails.delete(key);
        return null;
      });
    thumbnails.set(key, found);
  }
  return found;
}

/** Test seam: forget the cached thumbnails. */
export function resetWorkspaceThumbnails() {
  thumbnails.clear();
}

/** A file the person sent from their workspace, as a chip above the message: a small thumbnail for
 * images (read lazily, the chip alone while it loads or if it can't be read), else a type icon. */
export function WorkspaceAttachmentChip({
  botId,
  attachment,
}: {
  botId: string | undefined;
  attachment: WorkspaceAttachmentRef;
}) {
  const name = attachment.path.split("/").pop() || attachment.path;
  const isImage = attachment.mimeType.startsWith("image/");
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage || !botId) return;
    let live = true;
    void loadThumbnail(botId, attachment).then((url) => {
      if (live) setSrc(url);
    });
    return () => {
      live = false;
    };
  }, [attachment, botId, isImage]);
  const Icon = isImage ? ImageIcon : FileText;
  return (
    <div
      data-testid="message-attachment-chip"
      className="flex w-fit max-w-full items-center gap-2 rounded-full border border-border bg-muted py-1.5 ps-2 pe-3 text-[13px] text-foreground/75"
    >
      {src ? (
        <img src={src} alt="" className="size-6 shrink-0 rounded object-cover" />
      ) : (
        <Icon size={14} strokeWidth={1.8} className="ms-1 shrink-0" />
      )}
      <span className="min-w-0 flex-1 truncate" dir="auto">
        {name}
      </span>
    </div>
  );
}
