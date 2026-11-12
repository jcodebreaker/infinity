import type { ReactNode } from "react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import type { FileStudioApi } from "../../../components/cards/context";

const ArtifactStudio = lazy(() =>
  import("./ArtifactStudio").then((m) => ({ default: m.ArtifactStudio })),
);

/** The route's file: `?file=<groupId>` (any version id is taken, and renamed to the group). */
export const FILE_PARAM = "file";
/** History state of an entry this tab pushed to open a file: closing pops it. */
const PUSHED = "fileStudio";

/**
 * The File Studio as a route state (ADR 0030): `?file=` names the open file, so back closes it
 * and a reload keeps it. `api` opens a file (a card, a Library item, a citation); `studio` is the
 * studio to lay over the app while one is open. Closing pops the entry it pushed (or drops the
 * parameter on a reloaded or pasted address), then gives focus back to what opened it, or
 * scrolls Super Chat to a message (`showMessage`) when the studio pointed at one.
 */
export function useFileStudioRoute(showMessage: (itemId: string) => void): {
  api: FileStudioApi;
  studio: ReactNode;
  open: boolean;
} {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const fileId = params.get(FILE_PARAM);
  // What only the opener knows: its title, a PDF page, and where focus goes back to.
  const opened = useRef<{ title?: string; page?: number; opener: HTMLElement | null } | null>(null);
  const reveal = useRef<string | null>(null);
  const pushed = (location.state as Record<string, unknown> | null)?.[PUSHED] === true;

  const api = useMemo<FileStudioApi>(
    () => ({
      open: (artifactId, title, page) => {
        const opener =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        opened.current = { title, page, opener };
        setParams(
          (current) => {
            const next = new URLSearchParams(current);
            next.set(FILE_PARAM, artifactId);
            return next;
          },
          { state: { [PUSHED]: true } },
        );
      },
    }),
    [setParams],
  );

  const close = useCallback(
    (revealItemId?: string) => {
      reveal.current = revealItemId ?? null;
      if (pushed) {
        navigate(-1);
        return;
      }
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.delete(FILE_PARAM);
          return next;
        },
        { replace: true },
      );
    },
    [pushed, navigate, setParams],
  );

  // Named by its group id once the file is known, so one file has one address.
  const resolved = useCallback(
    (groupId: string) =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set(FILE_PARAM, groupId);
          return next;
        },
        { replace: true, state: location.state },
      ),
    [setParams, location.state],
  );

  // Closed (by close, or the browser's back): back at the card, or at the message asked for.
  const showMessageRef = useRef(showMessage);
  showMessageRef.current = showMessage;
  useEffect(() => {
    if (fileId) return;
    const opener = opened.current?.opener;
    opened.current = null;
    const itemId = reveal.current;
    reveal.current = null;
    if (itemId) showMessageRef.current(itemId);
    else if (opener?.isConnected) opener.focus({ preventScroll: true });
  }, [fileId]);

  const studio = fileId ? (
    <div className="absolute inset-0 z-40 flex">
      <Suspense fallback={null}>
        <ArtifactStudio
          artifactId={fileId}
          title={opened.current?.title}
          page={opened.current?.page}
          onClose={close}
          onResolved={resolved}
        />
      </Suspense>
    </div>
  ) : null;

  return { api, studio, open: fileId !== null };
}
