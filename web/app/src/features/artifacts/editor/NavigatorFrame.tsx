import { Button, cn } from "@superchat/ui-web";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { createContext, type ReactNode, useContext, useState } from "react";

/** Whether the person folded the navigator away; kept for the session so it survives switching
 * between viewing and editing, and opening another file. */
let collapsedForSession = false;

export type NavigatorOrientation = "vertical" | "horizontal";

const Orientation = createContext<NavigatorOrientation>("vertical");

/** The editor shell says how its navigator lies: a column beside the stage, or a strip under it
 * (a file's studio on a phone). */
export const NavigatorOrientationProvider = Orientation.Provider;

/** How the navigator lies, for the list a kind puts in it (`aria-orientation`, its flow). */
export const useNavigatorOrientation = (): NavigatorOrientation => useContext(Orientation);

/**
 * The editor's left column, for any kind's navigator (a deck's slides, a page's layers): its
 * toggle folds it to a slim strip (the kind's `folded` content, e.g. slide numbers) so the stage
 * gets the room; it never floats over the stage. Horizontal, it is that slim content as a strip
 * under the stage, or nothing for a kind without one.
 */
export function NavigatorFrame({
  label,
  showLabel,
  hideLabel,
  testId,
  folded,
  orientation,
  children,
}: {
  /** The group's accessible name ("Slide rail", "Layers"). */
  label: string;
  showLabel: string;
  hideLabel: string;
  testId?: string;
  /** What the slim strip shows; nothing by default. */
  folded?: ReactNode;
  /** The shell's (`NavigatorOrientationProvider`) unless given. */
  orientation?: NavigatorOrientation;
  children: ReactNode;
}) {
  const shell = useNavigatorOrientation();
  const lies = orientation ?? shell;
  const [collapsed, setCollapsed] = useState(collapsedForSession);
  const toggle = () => {
    collapsedForSession = !collapsed;
    setCollapsed(!collapsed);
  };
  if (lies === "horizontal") {
    if (!folded) return null;
    return (
      <Orientation.Provider value="horizontal">
        {/* biome-ignore lint/a11y/useSemanticElements: a fieldset would be a form group */}
        <div
          role="group"
          aria-label={label}
          data-testid={testId}
          data-slot="navigator"
          data-orientation="horizontal"
          className="flex w-full shrink-0 border-t border-border"
        >
          {folded}
        </div>
      </Orientation.Provider>
    );
  }
  return (
    <Orientation.Provider value="vertical">
      {/* biome-ignore lint/a11y/useSemanticElements: a fieldset would be a form group */}
      <div
        role="group"
        aria-label={label}
        data-testid={testId}
        data-slot="navigator"
        data-orientation="vertical"
        data-collapsed={collapsed ? "true" : undefined}
        className={cn(
          "flex shrink-0 flex-col overflow-hidden border-e border-border",
          "motion-safe:transition-[width] motion-safe:duration-200 motion-safe:ease-out",
          collapsed ? "w-12" : "w-[164px]",
        )}
      >
        <div
          data-slot="navigator-toggle"
          className={cn("flex shrink-0 px-2 pt-2", collapsed ? "justify-center" : "justify-end")}
        >
          <Button
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground"
            aria-label={collapsed ? showLabel : hideLabel}
            title={collapsed ? showLabel : hideLabel}
            aria-expanded={!collapsed}
            onClick={toggle}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          </Button>
        </div>
        {collapsed ? (folded ?? null) : children}
      </div>
    </Orientation.Provider>
  );
}

/** Test helper. */
export const resetNavigatorFrame = (): void => {
  collapsedForSession = false;
};
