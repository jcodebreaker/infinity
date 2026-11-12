import { useObjectUrl } from "../lib/use-object-url";

/** A PDF in the browser's viewer; `page` opens it at that page (1-based). */
export function PdfViewer({
  bytes,
  title,
  page,
}: {
  bytes: Uint8Array;
  title: string;
  page?: number;
}) {
  const url = useObjectUrl(bytes, "application/pdf");
  if (!url) return null;
  // A new page is a new src: the viewer only reads the fragment when it loads.
  return (
    <iframe
      key={page ?? 0}
      title={title}
      src={page ? `${url}#page=${page}` : url}
      className="h-full w-full border-0"
    />
  );
}
