import { Trans, useLingui } from "@lingui/react/macro";
import type { EngineModelCatalog, EngineModelCategory, EngineModelRow } from "@superchat/contracts";
import { NativeSelect, NativeSelectOption } from "@superchat/ui-web";
import { Cpu, Feather, Hammer } from "lucide-react";
import { useEffect, useState } from "react";
import type { SettingsIcon } from "../../components/settings/kit";
import { SettingsGroup, SettingsRow } from "../../components/settings/kit";
import { errorText } from "../../lib/error-text";
import { rpc } from "../../lib/rpc";
import { engineModelsChanged } from "./engine-models";

/** A model as the person reads it: its name, and its family when the name does not say it. */
export function modelOptionLabel(model: Pick<EngineModelRow, "label" | "family">): string {
  const family = model.family;
  return family && !model.label.toLowerCase().includes(family.toLowerCase())
    ? `${model.label} · ${family}`
    : model.label;
}

/** The model the person's chats run on: their Foundation (their pick, else the automatic one). */
export function foundationOf(catalog: EngineModelCatalog): string {
  return (
    catalog.foundationModel ??
    catalog.defaultModel ??
    catalog.models.find((model) => model.isDefault)?.id ??
    ""
  );
}

type Category = EngineModelCategory;

const ICONS: Record<Category, SettingsIcon> = {
  foundation: { tone: "indigo", glyph: <Cpu strokeWidth={2.4} /> },
  builder: { tone: "orange", glyph: <Hammer strokeWidth={2.4} /> },
  light: { tone: "teal", glyph: <Feather strokeWidth={2.4} /> },
};

/** One category's pick: "Automatic · <model>" (`""`) or a model the connection serves. */
function CategoryRow({
  harness,
  category,
  title,
  catalog,
  onError,
}: {
  harness: string;
  category: Category;
  title: string;
  catalog: EngineModelCatalog;
  onError: (message: string | null) => void;
}) {
  const { t } = useLingui();
  const [value, setValue] = useState(catalog.picks[category] ?? "");
  const automatic = catalog.models.find((row) => row.id === catalog.automatic[category]);
  const automaticWord = t`Automatic`;

  async function choose(model: string) {
    const before = value;
    setValue(model);
    onError(null);
    try {
      await rpc.engineModels.setPick({ harness, category, model: model || null });
      engineModelsChanged();
    } catch (err) {
      setValue(before);
      onError(errorText(err, t`Could not save.`));
    }
  }

  return (
    <SettingsRow label={title} icon={ICONS[category]}>
      <NativeSelect
        size="sm"
        aria-label={title}
        data-testid={`${category}-model-${harness}`}
        value={value}
        onChange={(event) => void choose(event.target.value)}
      >
        <NativeSelectOption value="">
          {automatic ? `${automaticWord} · ${modelOptionLabel(automatic)}` : automaticWord}
        </NativeSelectOption>
        {catalog.models.map((model) => (
          <NativeSelectOption key={model.id} value={model.id}>
            {modelOptionLabel(model)}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </SettingsRow>
  );
}

function HarnessPicks({
  harness,
  label,
}: {
  harness: string;
  /** Names the harness when the person's Superchat could run on more than one. */
  label: string | null;
}) {
  const { t } = useLingui();
  const [catalog, setCatalog] = useState<EngineModelCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void rpc.engineModels
      .catalog({ harness })
      .then((next) => {
        if (!cancelled) setCatalog(next);
      })
      .catch((err) => {
        if (!cancelled) setError(errorText(err, t`Could not load models.`));
      });
    return () => {
      cancelled = true;
    };
  }, [harness, t]);

  const titles: Record<Category, string> = label
    ? {
        foundation: t`Foundation · ${label}`,
        builder: t`Builder · ${label}`,
        light: t`Light · ${label}`,
      }
    : { foundation: t`Foundation`, builder: t`Builder`, light: t`Light` };
  if (!catalog) {
    return error ? <SettingsRow label={titles.foundation} sub={error} /> : null;
  }
  if (catalog.status !== "ready" || catalog.models.length === 0) {
    return (
      <SettingsRow
        label={titles.foundation}
        sub={catalog.error ?? t`Add a key to choose a model.`}
      />
    );
  }
  return (
    <>
      {(["foundation", "builder", "light"] as const).map((category) => (
        <CategoryRow
          key={category}
          harness={harness}
          category={category}
          title={titles[category]}
          catalog={catalog}
          onError={setError}
        />
      ))}
      {error ? (
        <p role="alert" className="px-3 pb-2 text-[12.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </>
  );
}

/** The person's three picks (Foundation, Builder, Light) for each harness their Superchat may
 * run on. */
export function DefaultModel({ harnesses }: { harnesses: { id: string; label: string }[] }) {
  if (harnesses.length === 0) return null;
  return (
    <SettingsGroup title={<Trans>Model</Trans>}>
      {harnesses.map((harness) => (
        <HarnessPicks
          key={harness.id}
          harness={harness.id}
          label={harnesses.length > 1 ? harness.label : null}
        />
      ))}
    </SettingsGroup>
  );
}
