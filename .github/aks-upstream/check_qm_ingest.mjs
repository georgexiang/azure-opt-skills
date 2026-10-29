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
assert(
  skills[0].manifest.requiredCapabilities.includes(
    "egress:management.azure.com",
  ),
);
assert(
  skills[0].manifest.requiredCapabilities.every((capability) =>
    capability.startsWith("egress:"),
  ),
);
const actual = skills[0].manifest.files.map((file) => file.path).sort();
const manifest = JSON.parse(
  readFileSync(resolve(root, "azure-aks-ops/UPSTREAM.json"), "utf8"),
);
const expected = [
  "UPSTREAM.json",
  "LICENSES/microsoft-aks-skills.txt",
  ...JSON.parse(
    readFileSync(
      resolve(root, ".github/aks-upstream/runtime-assets.json"),
      "utf8",
    ),
  ),
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
// Exercise the real reconciliation API with two scopes and an independent
// synthetic pack. No fetcher, store or helper touches a running platform.
const { createSkillMethods } = await import(
  pathToFileURL(resolve(qm, "src/api/app-skills.ts")).href
);
const { createSkillPackStore } = await import(
  pathToFileURL(resolve(qm, "src/skills/skill-pack-store.ts")).href
);
const records = createSkillStore();
const packs = createSkillPackStore();
const source = (versions) => ({
  commit: JSON.stringify(versions),
  files: Object.entries(versions).map(([name, version]) => ({
    path: `${name}/SKILL.md`,
    text: `---\nname: ${name}\ndescription: Fixture ${name}\n---\n${version}\n`,
    binary: false,
  })),
});
const snapshots = {
  old: source({ "azure-ops": "vm-v1", "azure-support-ticket": "support-v1" }),
  candidate: source({
    "azure-ops": "vm-v1",
    "azure-support-ticket": "support-v1",
    "azure-aks-ops": "aks-v2",
  }),
  widespread: source({
    "azure-ops": "vm-v3",
    "azure-support-ticket": "support-v3",
    "azure-aks-ops": "aks-v2",
  }),
};
const methods = createSkillMethods(
  {
    skills: records,
    skillPacks: packs,
    skillFetcher: { fetch: async (pack) => snapshots[pack.ref] },
  },
  {},
);
const pack = await packs.create({
  kind: "git",
  url: "https://example.invalid/fixture.git",
  ref: "old",
  syncMode: "pinned",
  trustTier: "internal",
  subset: "all",
  targetScopeId: "group:scope-a",
  createdBy: "fixture",
});
await methods.importSkillPack(pack.id, ["azure-ops"], ["group:scope-a"]);
await methods.importSkillPack(
  pack.id,
  ["azure-support-ticket"],
  ["group:scope-b"],
);
await methods.updateSkillPack(pack.id, { ref: "candidate" });
const preserved = await methods.importSkillPack(
  pack.id,
  ["azure-ops", "azure-aks-ops"],
  ["group:scope-a"],
);
assert.deepEqual(preserved.archived, []);
const lookup = async (scope, name) =>
  (await records.list()).find(
    (item) => item.scopeId === scope && item.manifest.name === name,
  );
assert.equal(
  (await lookup("group:scope-b", "azure-support-ticket")).manifest.body.trim(),
  "support-v1",
);
const unsafe = await methods.importSkillPack(
  pack.id,
  ["azure-aks-ops"],
  ["group:scope-a"],
);
assert.deepEqual(
  unsafe.archived,
  ["azure-ops"],
  "Regression must demonstrate why AKS-only import is unsafe on an existing scope",
);
await methods.importSkillPack(
  pack.id,
  ["azure-ops", "azure-aks-ops"],
  ["group:scope-a"],
);
await methods.updateSkillPack(pack.id, { ref: "widespread" });
await methods.syncSkillPack(pack.id);
assert.equal(
  (await lookup("group:scope-a", "azure-ops")).manifest.body.trim(),
  "vm-v3",
);
assert.equal(
  (await lookup("group:scope-b", "azure-support-ticket")).manifest.body.trim(),
  "support-v3",
  "Sync reconciles every imported scope, not only AKS",
);
await methods.updateSkillPack(pack.id, { ref: "old" });
const rollback = await methods.importSkillPack(
  pack.id,
  ["azure-ops"],
  ["group:scope-a"],
);
await methods.importSkillPack(
  pack.id,
  ["azure-support-ticket"],
  ["group:scope-b"],
);
assert.deepEqual(rollback.archived, ["azure-aks-ops"]);
assert.equal(
  (await lookup("group:scope-a", "azure-ops")).manifest.body.trim(),
  "vm-v1",
);
assert.equal(
  (await lookup("group:scope-b", "azure-support-ticket")).manifest.body.trim(),
  "support-v1",
);
assert.equal((await lookup("group:scope-a", "azure-ops")).status, "published");
console.log(
  "QM reconciliation passed: complete preserve sets, separate scope selections, pack-wide sync and rollback",
);
console.log(
  "These in-memory source probes are not local VM/Scope or production acceptance.",
);
