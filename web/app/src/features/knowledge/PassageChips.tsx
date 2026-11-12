import { useLingui } from "@lingui/react/macro";
import type { ReplyCardDataOf } from "@superchat/contracts";
import { FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { useFileStudio, useReplyCardBotId } from "../../components/cards/context";
import { rpc } from "../../lib/rpc";
import { FilePreviewDialog } from "../files";

const thumbnails = new Map<string, Promise<string | null>>();

/** A page's thumbnail as a data URL, fetched once per page for the life of the tab. */
function loadThumbnail(fileId: string, page: number): Promise<string | null> {
  const key = `${fileId}:${page}`;
  let found = thumbnails.get(key);
  if (!found) {
    found = rpc.knowledge
      .pageThumbnail({ fileId, page })
      .then((thumb) => `data:${thumb.mimeType};base64,${thumb.contentBase64}`)
      .catch(() => {
        thumbnails.delete(key);
        return null;
      });
    thumbnails.set(key, found);
  }
  return found;
}

/** Test seam: forget the cached thumbnails. */
export function resetThumbnailCache() {
  thumbnails.clear();
}

function Thumbnail({ fileId, page }: { fileId: string; page: number }) {
  // undefined: still loading (a skeleton); null: the page truly has no thumbnail (a page icon).
  const [src, setSrc] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let live = true;
    setSrc(undefined);
    void loadThumbnail(fileId, page).then((url) => {
      if (live) setSrc(url);
    });
    return () => {
      live = false;
    };
  }, [fileId, page]);
  if (src === undefined) {
    return (
      <span data-testid="passage-thumbnail-loading" className="size-full animate-pulse bg-muted" />
    );
  }
  return src ? (
    <img src={src} alt="" className="size-full object-cover object-top" />
  ) : (
    <FileText size={15} className="text-muted-foreground" />
  );
}

/** The pages a search found, as small page-thumbnail chips under the answer. A chip opens the
 * file at that page: in the File Studio for a Library artifact, in the host's file preview for
 * any other workspace file. */
export function PassageChips({ data }: { data: ReplyCardDataOf<"passages"> }) {
  const { t } = useLingui();
  const studio = useFileStudio();
  const botId = useReplyCardBotId();
  // A file that is not a Library artifact opens in the host's workspace preview.
  const [preview, setPreview] = useState<{ path: string; page: number } | null>(null);
  return (
    <>
      <ul data-testid="passage-chips" className="flex max-w-full flex-wrap gap-2">
        {data.items.map((item) => {
          const { artifactId, fileId, path } = item;
          const body = (
            <>
              <span className="grid h-11 w-9 shrink-0 place-items-center overflow-hidden rounded-md bg-card ring-1 ring-border">
                {item.hasThumbnail && fileId ? (
                  <Thumbnail fileId={fileId} page={item.page} />
                ) : (
                  <FileText size={15} className="text-muted-foreground" />
                )}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-[12px] font-medium text-foreground" dir="auto">
                  {item.name}
                </span>
                <span className="block text-[11px] text-muted-foreground tabular-nums">
                  {t`p. ${item.page}`}
                </span>
              </span>
            </>
          );
          const chip =
            "superchat-glass-pill flex max-w-[15rem] items-center gap-2 rounded-xl p-1 pe-3 text-start";
          return (
            <li key={`${fileId ?? artifactId}:${item.page}`} className="min-w-0">
              {artifactId ? (
                <button
                  type="button"
                  disabled={!studio}
                  onClick={() => studio?.open(artifactId, item.name, item.page)}
                  aria-label={t`Open ${item.name}, page ${item.page}`}
                  className={`${chip} outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none`}
                >
                  {body}
                </button>
              ) : path && botId ? (
                <button
                  type="button"
                  onClick={() => setPreview({ path, page: item.page })}
                  aria-label={t`Open ${item.name}, page ${item.page}`}
                  className={`${chip} outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring`}
                >
                  {body}
                </button>
              ) : (
                <div className={chip}>{body}</div>
              )}
            </li>
          );
        })}
      </ul>
      {preview && botId ? (
        <FilePreviewDialog
          botId={botId}
          path={preview.path}
          page={preview.page}
          onOpenChange={(open) => {
            if (!open) setPreview(null);
          }}
        />
      ) : null}
    </>
  );
}
