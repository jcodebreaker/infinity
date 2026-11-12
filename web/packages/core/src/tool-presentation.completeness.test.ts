import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ALWAYS_ON_SUPERSIDE_CHAT_TOOLS, TOOL_PRESENTATION } from "./tool-presentation.js";

// Forces registering a tool's presentation the moment it's added to Superchat's agent bundle
// (engine/agents/bundles/superchat-claude/, generated from engine/agents/templates/) — see
// docs/helpers.md "How to register a tool". The bundle enables tools with `tools: allow:`
// lists; no YAML dependency in this workspace, so this reads them with a small
// indentation-aware scan rather than a real parser; good enough for this bundle's shape.
// A glob entry (`"files_*"`) names a family the engine expands, so only exact names are checked.

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "../../../..");
const SUPERCHAT_AGENT_BUNDLE_DIR = join(REPO_ROOT, "engine/agents/bundles/superchat-claude");

function findConfigYamlFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...findConfigYamlFiles(full));
    } else if (entry === "config.yaml") {
      found.push(full);
    }
  }
  return found;
}

/** Every exact tool name under a `tools: allow:` list (globs such as `"files_*"` are skipped). */
function allowedToolNames(yamlText: string): string[] {
  const names: string[] = [];
  let toolsIndent: number | null = null;
  let allowIndent: number | null = null;
  for (const line of yamlText.split("\n")) {
    if (line.trim() === "" || line.trim().startsWith("#")) continue;
    const indent = (/^(\s*)/.exec(line)?.[1] ?? "").length;
    if (allowIndent !== null) {
      const entry = /^\s*-\s*"?([^"\s]+)"?\s*$/.exec(line)?.[1];
      if (entry && indent >= allowIndent) {
        if (!entry.includes("*")) names.push(entry);
        continue;
      }
      allowIndent = null;
    }
    if (toolsIndent !== null && indent <= toolsIndent) toolsIndent = null;
    if (/^\s*tools:\s*$/.test(line)) {
      toolsIndent = indent;
      continue;
    }
    if (toolsIndent !== null && /^\s*allow:\s*$/.test(line)) allowIndent = indent;
  }
  return names;
}

/** Tools the bundle enables that still show the generic presentation. The list only shrinks:
 * register a tool's presentation, then delete it here (the test fails while it lingers). A tool
 * newly added to the bundle must be registered, not listed. */
const NOT_YET_PRESENTED = new Set([
  "daily_note_get",
  "daily_note_update",
  "deck_check",
  "deck_new",
  "deck_theme_set",
  "deck_themes",
  "files_get",
  "files_multi_get",
  "files_query",
  "files_read_page",
  "files_search",
  "files_status",
  "files_vsearch",
  "live_update_context",
  "live_update_save",
  "raise_flag",
  "recall",
  "recall_get",
  "report_progress",
  "sys_session_close",
  "sys_session_get_info",
  "sys_session_list",
]);

describe("tool presentation registry completeness", () => {
  it("has a presentation entry for every tool Superchat's agent bundle enables", () => {
    const configFiles = findConfigYamlFiles(SUPERCHAT_AGENT_BUNDLE_DIR);
    expect(configFiles.length).toBeGreaterThan(0);

    const allowedTools = new Set<string>();
    for (const file of configFiles) {
      for (const name of allowedToolNames(readFileSync(file, "utf-8"))) allowedTools.add(name);
    }
    expect(allowedTools.size).toBeGreaterThan(0);

    for (const name of allowedTools) {
      if (NOT_YET_PRESENTED.has(name)) continue;
      expect(
        TOOL_PRESENTATION[name],
        `no tool-presentation registry entry for '${name}'`,
      ).toBeDefined();
    }
  });

  it("lists as not yet presented only tools the bundle enables and the registry lacks", () => {
    const allowedTools = new Set(
      findConfigYamlFiles(SUPERCHAT_AGENT_BUNDLE_DIR).flatMap((file) =>
        allowedToolNames(readFileSync(file, "utf-8")),
      ),
    );
    for (const name of NOT_YET_PRESENTED) {
      expect(allowedTools.has(name), `'${name}' is no longer in the bundle`).toBe(true);
      expect(TOOL_PRESENTATION[name], `'${name}' is registered now; drop it`).toBeUndefined();
    }
  });

  it("has a presentation entry for every always-on superside-chat tool", () => {
    for (const name of ALWAYS_ON_SUPERSIDE_CHAT_TOOLS) {
      expect(
        TOOL_PRESENTATION[name],
        `no tool-presentation registry entry for '${name}'`,
      ).toBeDefined();
    }
  });
});
