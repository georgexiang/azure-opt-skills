// Optional read-only compatibility probe against an installed QM source tree.
// Node 24+ and the QM tree's existing dependencies are required; no platform API.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
if (!process.argv[2])
  throw new Error("Usage: node check_qm_ingest.mjs /path/to/qm-source");
const qm = resolve(process.argv[2]);
const { planIngest, collectSharedBundle, importPack } = await import(
  pathToFileURL(resolve(qm, "src/skills/ingest.ts")).href
);
const { createSkillStore } = await import(
  pathToFileURL(resolve(qm, "src/skills/skill-store.ts")).href
);
const paths = execFileSync("git", ["ls-files", "-z"], {
  cwd: root,
  encoding: "utf8",
})
  .split("\0")
  .filter(Boolean);
const repo = {
  commit: "offline-compatibility-probe",
  files: paths.map((path) => ({
    path,
    text: readFileSync(resolve(root, path), "utf8"),
    binary: false,
  })),
};
const config = { skillGlobs: ["azure-aks-ops"] };
const plan = planIngest(repo, { nativeNames: new Set(), config });
assert.equal(plan.candidates.length, 1, "Expected exactly one AKS skill entry");
assert.equal(plan.candidates[0].upstreamName, "azure-aks-ops");
assert.equal(
  plan.candidates[0].eligible,
  true,
  "AKS skill must normalize and be eligible",
);
const shared = collectSharedBundle(repo);
assert.deepEqual(
  shared,
  [],
  "Unexpected shared assets: maintenance files must remain metadata",
);
const store = createSkillStore(); // No disk/database adapter: isolated in-memory store only.
const result = await importPack(repo, store, {
  pack: {
    id: "offline-aks-probe",
    targetScopeId: "project:offline-aks-probe",
    subset: ["azure-aks-ops"],
    config,
  },
  selected: ["azure-aks-ops"],
  nativeNames: new Set(),
  bundleFiles: shared,
});
assert.deepEqual(result.imported, ["azure-aks-ops"]);
const skills = await store.list();
assert.equal(skills.length, 1);
assert.equal(skills[0].status, "published");
assert.deepEqual(skills[0].manifest.requiredCapabilities, []);
const actual = skills[0].manifest.files.map((file) => file.path).sort();
const manifest = JSON.parse(
  readFileSync(resolve(root, "azure-aks-ops/UPSTREAM.json"), "utf8"),
);
const expected = [
  "UPSTREAM.json",
  "LICENSES/microsoft-aks-skills.txt",
  "tests/cases.md",
  ...manifest.files.map((file) => file.path.slice("azure-aks-ops/".length)),
].sort();
assert.deepEqual(
  actual,
  expected,
  "Importer must retain every declared text asset and no maintenance code",
);
console.log(
  `QM ingest compatibility passed: one eligible AKS skill, ${actual.length} assets, zero shared maintenance files`,
);
console.log(
  "This in-memory source probe is not local VM/Scope or production acceptance.",
);
