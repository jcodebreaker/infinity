#!/usr/bin/env node
// Renders Superchat's Omnigent Super Chat bundles (`superchat-claude`, `superchat-pi`;
// docs/architecture.md) from the templates under engine/agents/templates/, so
// the prompt, config and Sub-agent Types stay generated rather than hand-edited. Run after
// editing anything under templates/:
//
//   node engine/agents/render-agents.mjs
//
// No YAML library: every template is plain text with {{PLACEHOLDER}} tokens, substituted with
// simple string replacement, so the output stays byte-for-byte predictable.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
/** Where bundles are written; `SUPERCHAT_RENDER_AGENTS_DIR` redirects it (tests render to a temp dir). */
const AGENTS_DIR = process.env.SUPERCHAT_RENDER_AGENTS_DIR || join(here, "bundles");
/** One entry per Muse bundle. The engine's `OMNIGENT_SUPERCHAT_DEFAULT_AGENT` (`superchat-claude` |
 * `superchat-pi`) picks which one a new Muse runs on (ADR 0009): this script renders BOTH every run
 * from the same templates. The choice is total: every Sub-agent Type (worker, subworker, notes, editor) of a bundle runs on that bundle's harness, so the Muse, its Helpers and
 * scheduled/background Helper runs all share one harness. Pi's extra keys keep it from fighting Omnigent: no AGENTS.md/CLAUDE.md
 * auto-discovery, our prompt replaces pi's base prompt, no host skills; they are defined once
 * here and rendered into the Muse and every Sub-agent Type alike. (Compaction is switched off
 * engine-side, in harnesses/pi_executor.py, for superside-chat.)
 */
const MUSE_BUNDLES = [
  {
    name: "superchat-claude",
    harness: "claude-sdk",
    harnessName: "Claude Agent SDK",
    // Omnigent owns skills (load_skill, ADR 0006): no native Claude Code Skill tool or plugin.
    executorExtra: "    native_skills: false",
    skillsFilter: "",
  },
  {
    name: "superchat-pi",
    harness: "pi",
    harnessName: "Pi",
    executorExtra: "    context_files: false\n    system_prompt_mode: replace",
    skillsFilter: "skills: none",
  },
];
/** One directory per Sub-agent Type (docs/glossary.md), each a config.yaml template rendered
 * with the bundle's harness keys. */
const SUB_AGENT_TYPES = ["worker", "worker/agents/subworker", "notes", "editor"];
/** The deck kit's layout menu (engine/omnigent/superchat/decks/kit/menu.md): Helper prompts and
 * the Conversation's deck skill. */
const DECK_LAYOUTS_PATH = "../omnigent/superchat/decks/kit/menu.md";
const GENERATED_HEADER =
  "# GENERATED FILE — do not edit by hand. Edit the matching file under\n" +
  "# engine/agents/templates/, then run `node engine/agents/render-agents.mjs`.\n\n";

function render(template, values) {
  return Object.entries(values).reduce((text, [key, value]) => {
    // A line holding only an empty placeholder is dropped, so optional keys leave no blank line.
    const withoutEmptyLine = value === "" ? text.replaceAll(`{{${key}}}\n`, "") : text;
    return withoutEmptyLine.replaceAll(`{{${key}}}`, value);
  }, template);
}

/** Indents every line but the first, so a multi-line partial fits a YAML block scalar. */
function indent(text, spaces) {
  const pad = " ".repeat(spaces);
  return text
    .trimEnd()
    .split("\n")
    .map((line, i) => (i === 0 || line === "" ? line : pad + line))
    .join("\n");
}

function main() {
  const configTemplate = readFileSync(join(here, "templates/config.yaml.tmpl"), "utf8");
  const filesystem = readFileSync(join(here, "templates/filesystem.md"), "utf8").trimEnd();
  // The layout menu is the deck kit's own file (one source for the Helper prompts and the deck
  // skill); the rules around it live in templates/deck-rules.md.
  const deckLayouts = readFileSync(join(here, DECK_LAYOUTS_PATH), "utf8").trimEnd();
  // The full deck rules go to every Helper Type that builds decks. The Conversation gets the
  // short templates/decks.md instead, and the layouts in its deck skill.
  const deckRules = render(readFileSync(join(here, "templates/deck-rules.md"), "utf8"), {
    DECK_LAYOUTS: deckLayouts,
  }).trimEnd();
  const decks = readFileSync(join(here, "templates/decks.md"), "utf8").trimEnd();
  // The Conversation's deck skill (ADR 0031): the same layout menu, read with load_skill when it
  // builds a deck, instead of riding every prompt or coming back from deck_new.
  const deckSkill = render(readFileSync(join(here, "templates/deck-skill.md"), "utf8"), {
    DECK_LAYOUTS: deckLayouts,
  });
  // The template opens with a provenance comment for ported text; the prompt does not carry it.
  const agentsTemplate = readFileSync(join(here, "templates/AGENTS.md"), "utf8").replace(
    /^<!--[\s\S]*?-->\n/,
    "",
  );

  for (const bundle of MUSE_BUNDLES) {
    const outDir = join(AGENTS_DIR, bundle.name);
    mkdirSync(outDir, { recursive: true });
    writeFileSync(
      join(outDir, "config.yaml"),
      render(configTemplate, {
        BUNDLE_NAME: bundle.name,
        HARNESS: bundle.harness,
        HARNESS_NAME: bundle.harnessName,
        EXECUTOR_EXTRA: bundle.executorExtra,
        SKILLS_FILTER: bundle.skillsFilter,
      }),
    );
    writeFileSync(
      join(outDir, "AGENTS.md"),
      render(agentsTemplate, {
        FILESYSTEM: filesystem,
        DECKS: decks,
        HARNESS_NAME: bundle.harnessName,
      }),
    );
    const skillDir = join(outDir, "skills", "decks");
    mkdirSync(skillDir, { recursive: true });
    writeFileSync(join(skillDir, "SKILL.md"), deckSkill);
    console.log(`wrote ${outDir}/config.yaml, AGENTS.md and skills/decks/SKILL.md`);

    for (const type of SUB_AGENT_TYPES) {
      const subTemplate = readFileSync(join(here, `templates/agents/${type}/config.yaml`), "utf8");
      const subDir = join(outDir, "agents", type);
      mkdirSync(subDir, { recursive: true });
      writeFileSync(
        join(subDir, "config.yaml"),
        GENERATED_HEADER +
          render(subTemplate, {
            FILESYSTEM: indent(filesystem, 2),
            DECK_RULES: indent(deckRules, 2),
            HARNESS: bundle.harness,
            HARNESS_NAME: bundle.harnessName,
            EXECUTOR_EXTRA: bundle.executorExtra,
            SKILLS_FILTER: bundle.skillsFilter,
          }),
      );
      console.log(`wrote ${subDir}/config.yaml`);
    }
  }
}

main();
