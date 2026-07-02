const fs = require("fs");
const path = require("path");
const { ReplitConnectors } = require("@replit/connectors-sdk");

const connectors = new ReplitConnectors();
const OWNER = "tatiananoian87-boop";
const REPO = "DoveLoButtoAI";
const BRANCH = "main";
const ROOT = path.resolve(__dirname, "..");

const SKIP = [
  ".git", "node_modules", ".local", ".pythonlibs", ".npm", ".config", ".cache",
  "tmp", "__pycache__", ".agents", "artifacts/api-server/dist",
  "artifacts/api-server/.replit-artifact",
];
const SKIP_EXTS = new Set([".pyc", ".db", ".db-journal", ".db-shm", ".db-wal", ".tsbuildinfo", ".log", ".DS_Store"]);
const SKIP_NAMES = new Set(["pnpm-lock.yaml", ".gitignore", "waste.db"]);

function shouldSkip(p) {
  const rel = path.relative(ROOT, p).replace(/\\/g, "/");
  if (SKIP.some((s) => rel.includes(s))) return true;
  if (SKIP_EXTS.has(path.extname(p))) return true;
  if (SKIP_NAMES.has(path.basename(p))) return true;
  return false;
}

async function proxy(method, endpoint, opts = {}) {
  const response = await connectors.proxy("github", endpoint, opts);
  const text = await response.text();
  let body = null;
  try { body = JSON.parse(text); } catch {}
  return { status: response.status, body };
}

async function pushFile(relPath, content) {
  const encoded = Buffer.from(content, "utf-8").toString("base64");
  const apiPath = `/repos/${OWNER}/${REPO}/contents/${encodeURIComponent(relPath)}`;

  // Try to get existing SHA
  let sha = null;
  try {
    const existing = await proxy("GET", `${apiPath}?ref=${BRANCH}`);
    if (existing.body && existing.body.sha) sha = existing.body.sha;
  } catch {}

  const body = { message: `Add ${relPath}`, content: encoded, branch: BRANCH };
  if (sha) body.sha = sha;

  const result = await proxy("PUT", apiPath, {
    method: "PUT",
    body: JSON.stringify(body),
  });

  if (result.status >= 400) {
    throw new Error(`HTTP ${result.status}: ${JSON.stringify(result.body)}`);
  }
  return result;
}

async function main() {
  const files = [];
  function walk(dir) {
    for (const entry of fs.readdirSync(dir)) {
      const full = path.join(dir, entry);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) {
        if (!shouldSkip(full)) walk(full);
      } else if (!shouldSkip(full)) {
        files.push(full);
      }
    }
  }
  walk(ROOT);
  files.sort();
  console.log(`Found ${files.length} files to push.`);

  let ok = 0, fail = 0;
  for (let i = 0; i < files.length; i++) {
    const full = files[i];
    const rel = path.relative(ROOT, full).replace(/\\/g, "/");
    try {
      const content = fs.readFileSync(full, "utf-8");
      await pushFile(rel, content);
      ok++;
      process.stdout.write(`[${i + 1}/${files.length}] \u2705 ${rel}\n`);
    } catch (err) {
      fail++;
      process.stdout.write(`[${i + 1}/${files.length}] \u274c ${rel}: ${err.message}\n`);
    }
    // Throttle: ~3 req/sec stays well under GitHub's 5000/hour limit
    await new Promise((r) => setTimeout(r, 350));
  }
  console.log(`\nDone. ${ok} pushed, ${fail} failed.`);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
