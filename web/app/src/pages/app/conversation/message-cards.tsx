import { useLingui } from "@lingui/react/macro";
import { Button, Dialog, DialogClose, DialogContent, DialogTitle } from "@superchat/ui-web";
import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { fetchArtifactBytes } from "../../../lib/artifact-open";

export function ArtifactImage({
  artifactId,
  name,
  mimeType,
}: {
  artifactId: string;
  name: string;
  mimeType?: string;
}) {
  const { t } = useLingui();
  const [src, setSrc] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(false);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = container.current;
    if (!element || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "320px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setSrc(null);
    void fetchArtifactBytes(artifactId)
      .then((bytes) => {
        objectUrl = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: mimeType }));
        if (cancelled) URL.revokeObjectURL(objectUrl);
        else setSrc(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifactId, mimeType, visible]);

  return (
    <div ref={container}>
      {src ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="max-w-[240px] overflow-hidden rounded-[20px]"
        >
          <img src={src} alt={name} className="max-h-48 w-full object-cover" />
        </button>
      ) : (
        <div className="rounded-[20px] border border-border bg-muted px-4 py-3 text-[14px] text-muted-foreground">
          {name}
        </div>
      )}
      {src ? (
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent
            showCloseButton={false}
            className="w-auto max-w-none bg-transparent p-0 ring-0 sm:max-w-none"
          >
            <DialogTitle className="sr-only">{name}</DialogTitle>
            <DialogClose
              aria-label={t`Close image preview`}
              render={<Button variant="ghost" size="icon-sm" className="absolute end-2 top-2" />}
            >
              <X />
            </DialogClose>
            <img
              src={src}
              alt={name}
              className="max-h-[85vh] max-w-[90vw] rounded-xl object-contain"
            />
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  );
}
