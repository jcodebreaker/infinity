import { useEffect, useState } from "react";
import { SheetView } from "../../features/sheets/SheetView";
import { createSheetFixtureSource } from "./sheet-fixture";

/**
 * Dev-only route (`/dev/sheet`, gated by `import.meta.env.DEV` in App.tsx): the sheet editor on
 * an in-memory workbook, so it can be eyeballed and screenshotted without a session or engine.
 * `?theme=light|dark` forces a theme.
 */
export function SheetPreviewPage() {
  const [{ source, head }] = useState(createSheetFixtureSource);
  const [id, setId] = useState(head);
  useEffect(() => {
    const theme = new URLSearchParams(window.location.search).get("theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  }, []);
  return (
    <div className="flex h-dvh justify-center bg-background p-2">
      <section className="flex min-h-0 w-[min(80vw,1200px)] flex-col overflow-hidden rounded-[18px] border border-border bg-card">
        <SheetView
          artifact={{ id, name: "q3-sales-clean.xlsx", version: Number(id.slice(1)) || 1 }}
          onEdited={setId}
          source={source}
        />
      </section>
    </div>
  );
}
