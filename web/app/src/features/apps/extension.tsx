import { useLingui } from "@lingui/react/macro";
import {
  type Artifact,
  type ArtifactPublish,
  type ArtifactPublishAudience,
  isDeckArtifactName,
} from "@superchat/contracts";
import { Button, DropdownMenuItem } from "@superchat/ui-web";
import { useEffect, useState } from "react";
import { rpc } from "../../lib/rpc";
import {
  type ArtifactExtension,
  type ArtifactPanelParts,
  useArtifactPublishOptions,
} from "../artifacts";
import { isPageArtifact, pageEditorKind } from "./PageEditor";
import { PublishDialog, PublishedBar, PublishedPill, viewsText } from "./PublishApp";

/** The artifact panel's publish controls: pill, Publish button, address bar and dialog. */
function useAppsPanel({
  artifact,
  title,
}: {
  artifact: Artifact | undefined;
  title?: string;
}): ArtifactPanelParts {
  const { t } = useLingui();
  const [publishing, setPublishing] = useState(false);
  const [publish, setPublish] = useState<ArtifactPublish | null>(null);
  useEffect(() => {
    setPublish(artifact?.publish ?? null);
  }, [artifact]);

  const options = useArtifactPublishOptions(artifact);

  const app = artifact?.mimeType === "text/html" ? artifact : undefined;
  if (!app) return {};
  const { id: artifactId, name, version } = app;

  async function publishWith(audience: ArtifactPublishAudience) {
    const published = await rpc.apps.publish({
      artifactId,
      audience,
      version,
    });
    setPublish(published);
    setPublishing(false);
  }

  async function unpublish() {
    await rpc.apps.unpublish({ artifactId });
    setPublish(null);
  }

  // A deck keeps its header quiet: Publish… lives in the `⋯` menu next to Open in new tab.
  const quiet = isDeckArtifactName(name);
  const publishLabel = publish ? t`Publish settings` : t`Publish…`;
  return {
    badge: publish ? <PublishedPill /> : null,
    overflow: quiet ? (
      <DropdownMenuItem onClick={() => setPublishing(true)}>{publishLabel}</DropdownMenuItem>
    ) : null,
    actions: quiet ? null : (
      <Button size="sm" variant="secondary" onClick={() => setPublishing(true)}>
        {publishLabel}
      </Button>
    ),
    below: publish ? (
      <PublishedBar
        publish={publish}
        onSettings={() => setPublishing(true)}
        onUnpublish={unpublish}
      />
    ) : null,
    overlay: publishing ? (
      <PublishDialog
        name={title || name}
        current={publish}
        onClose={() => setPublishing(false)}
        onConfirm={publishWith}
        options={options}
      />
    ) : null,
    holdsEscape: publishing,
  };
}

function PublishedBadge() {
  const { t } = useLingui();
  return (
    <span
      data-testid="library-card-published"
      className="superchat-glass-pill inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[11px] font-medium text-ink-2"
    >
      <span aria-hidden="true" className="size-1.5 rounded-full bg-success" />
      {t`Published`}
    </span>
  );
}

export const appsExtension: ArtifactExtension = {
  usePanel: useAppsPanel,
  editor: (artifact) => (isPageArtifact(artifact) ? pageEditorKind : null),
  card: (artifact) =>
    artifact.publish
      ? { meta: viewsText(artifact.publish.stats), badge: <PublishedBadge /> }
      : null,
};
