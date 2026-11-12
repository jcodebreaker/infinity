import { MousePointerClick, Table2 } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

const ICONS = { sheet: Table2, element: MousePointerClick } as const;
const TEST_IDS = { sheet: "sheet-ask-chip", element: "element-ask-chip" } as const;

/** The compact chip for something attached to a message ("Sales!C2:C8 · 7 cells", "Slide 2 ·
 * Heading"): in the composer (with a remove button) and in the transcript above a sent message.
 * Other props reach the root, so a popover trigger can render as the chip. */
export function ComposerAttachmentChip({
  kind,
  label,
  children,
  testId,
  className = "",
  ...rest
}: {
  kind: string;
  label: string;
  children?: ReactNode;
  testId?: string;
  className?: string;
} & Omit<ComponentProps<"div">, "children" | "className">) {
  const Icon = ICONS[kind as keyof typeof ICONS] ?? Table2;
  return (
    <div
      data-testid={testId ?? TEST_IDS[kind as keyof typeof TEST_IDS] ?? "composer-attachment-chip"}
      {...rest}
      className={`flex w-fit max-w-full items-center gap-2 rounded-full border border-border bg-muted px-3 py-1.5 text-[13px] text-foreground/75 ${className}`}
    >
      <Icon size={14} strokeWidth={1.8} className="shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate" dir="auto">
        {label}
      </span>
      {children}
    </div>
  );
}
