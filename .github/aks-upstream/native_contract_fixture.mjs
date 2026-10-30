// Test-only adapter around pinned Core routes and native file staging. No Azure.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [qm, fixture] = process.argv.slice(2).map((value) => resolve(value));
assert.equal(
  execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: qm,
    encoding: "utf8",
  }).trim(),
  "4420b2416507cb00c34c831839a793b3e1a0aeff",
  "Use the matched, pinned Core source",
);
const load = (path) => import(pathToFileURL(join(qm, path)).href);
const { createKeychain } = await load("src/credentials/keychain.ts");
const { prepareExecutionFiles } = await load(
  "src/credentials/execute-files.ts",
);
const { createMemoryMap } = await load("src/persistence/durable-map.ts");
const { deriveConnectorKey } = await load(
  "src/connectors/connector-client-store.ts",
);
const { mintCapabilityToken, verifyCapabilityToken } = await load(
  "src/auth/capability-token.ts",
);
const { keychainRoutes } = await load("src/api/routes/keychain.ts");
const signingKey = randomBytes(32).toString("base64");
const keychain = createKeychain({
  creds: createMemoryMap(),
  grants: createMemoryMap(),
  asks: createMemoryMap(),
  key: deriveConnectorKey(randomBytes(32)),
});
const claims = {
  actorId: "alice",
  scopeId: "personal:alice",
  exp: Date.now() + 600000,
};
const ownerToken = await mintCapabilityToken(claims, signingKey);
const groupToken = await mintCapabilityToken(
  { ...claims, scopeId: "group:contract-project", padding: "x".repeat(5000) },
  signingKey,
  true,
);

// Translate the native /tmp namespace into a private fixture root at the Sandbox
// boundary so Core's stale-directory sweep can never touch this host's /tmp.
const mapped = (value) => value.replaceAll("/tmp/", fixture + "/virtual-tmp/");
mkdirSync(join(fixture, "virtual-tmp"), { recursive: true, mode: 0o700 });
const sandbox = {
  async run(_handle, command) {
    const result = spawnSync("/bin/sh", ["-c", mapped(command)], {
      encoding: "utf8",
      env: { PATH: "/usr/bin:/bin", HOME: fixture },
    });
    return {
      code: result.status ?? 1,
      stdout: result.stdout,
      stderr: result.stderr,
    };
  },
  async writeFileBytes(handle, path, bytes) {
    const target = join(mapped(handle.rootDir), path);
    mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
    writeFileSync(target, bytes, { mode: 0o600 });
  },
  async readFileBytes(handle, path) {
    return readFileSync(join(mapped(handle.rootDir), path));
  },
};
const app = {
  async directoryMember(id) {
    return { principalId: id, displayName: id, type: "internal" };
  },
  async belongsToScope() {
    return true;
  },
  async listContexts() {
    return [];
  },
  async getCron() {
    return null;
  },
};
const respond = (res, code, body) => {
  res.writeHead(code, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};
const server = createServer(async (req, res) => {
  try {
    const capability = await verifyCapabilityToken(
      req.headers["x-agent-capability"] ?? "",
      signingKey,
    );
    if (!capability) return respond(res, 401, { error: "unauthorized" });
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    if (req.url === "/__fixture/approve" && req.method === "POST") {
      const answer = await keychain.approveAsk({
        askId: body.askId,
        ownerId: capability.actorId,
        mode: "standing",
        purpose: body.purpose,
      });
      return respond(res, 200, { grant: answer.grant });
    }
    if (req.url === "/__fixture/stage" && req.method === "POST") {
      const prepared = await keychain.prepareMaterialize(
        body.grant,
        capability.scopeId,
        capability.actorId,
      );
      await prepared.commit();
      assert.equal(prepared.materialized.kind, "file");
      let captured;
      const staged = await prepareExecutionFiles(
        sandbox,
        { rootDir: fixture },
        [
          {
            files: prepared.materialized.files,
            save: async (files) => {
              captured = files;
            },
          },
        ],
      );
      assert.match(
        staged.env.HOME,
        /^\/tmp\/qm-credentials\.[A-Za-z0-9]+\/home$/,
      );
      const executed = [];
      if (body.bundleRoot) {
        const toolsHome = join(fixture, "tools-home");
        const install = join(toolsHome, ".local/share/azure-cli/2.89.1");
        const extensions = join(toolsHome, ".local/share/azure-cli/extensions/support");
        mkdirSync(join(install, "bin"), { recursive: true });
        mkdirSync(extensions, { recursive: true });
        mkdirSync(join(toolsHome, ".local/bin"), { recursive: true });
        writeFileSync(join(install, ".complete"), "2.89.1\n");
        const cli = join(install, "bin/az");
        writeFileSync(cli, `#!/usr/bin/python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:1] == ["version"]:
    print("2.89.1")
elif args[:2] == ["extension", "show"]:
    print("2.0.1")
else:
    profile = Path(os.environ["AZURE_CONFIG_DIR"])
    login = json.loads((profile / "azureProfile.json").read_text())
    print(json.dumps({"login": login["fixtureLogin"], "profile": str(profile)}))
`);
        chmodSync(cli, 0o700);
        symlinkSync("../share/azure-cli/2.89.1/bin/az", join(toolsHome, ".local/bin/az"));
        const home = mapped(staged.env.HOME);
        for (const bundle of ["azure-support-ticket", "azure-ops"]) {
          const command = bundle === "azure-ops"
            ? ["/usr/bin/python3", [join(body.bundleRoot, bundle, "scripts/az_guard.py"), "--tools-home", toolsHome, "--", "account", "show"]]
            : ["bash", [join(body.bundleRoot, bundle, "scripts/azure_cli.sh"), "--tools-home", toolsHome, "account", "show"]];
          const result = JSON.parse(execFileSync(command[0], command[1], {
            encoding: "utf8",
            env: { HOME: home, AZURE_CONFIG_DIR: join(home, ".azure"), PATH: "/usr/bin:/bin" },
          }));
          assert.equal(result.login, "interactive-login");
          assert.equal(result.profile, join(home, ".azure"));
          executed.push(bundle);
        }
      }
      writeFileSync(
        join(mapped(staged.plan.directory), "kubeconfig"),
        "fixture-not-a-real-kubeconfig",
        { mode: 0o600 },
      );
      await staged.finish();
      assert(
        !existsSync(mapped(staged.plan.directory)),
        "Native finish must remove HOME and sibling kubeconfig",
      );
      assert.deepEqual(captured.map((file) => file.path).sort(), [
        ".azure/azureProfile.json",
        ".azure/msal_token_cache.json",
      ]);
      return respond(res, 200, {
        nativeStaging: true,
        cleanup: true,
        executed,
        savedPaths: captured.map((file) => file.path),
      });
    }
    const route = keychainRoutes.find(
      (route) => route.path === req.url && route.method === req.method,
    );
    if (!route) return respond(res, 404, { error: "not_found" });
    await route.handle({
      res,
      app,
      deps: { keychain },
      pathname: req.url,
      method: req.method,
      body,
      capability,
      params: {},
    });
  } catch {
    respond(res, 500, { error: "fixture_contract_failed" });
  }
});
server.listen(0, "127.0.0.1", () => {
  writeFileSync(
    join(fixture, "ready.json"),
    JSON.stringify({
      url: `http://127.0.0.1:${server.address().port}`,
      ownerToken,
      groupToken,
    }),
    { mode: 0o600 },
  );
});
process.on("SIGTERM", () => server.close(() => process.exit(0)));
