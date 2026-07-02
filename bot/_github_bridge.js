const { ReplitConnectors } = require("@replit/connectors-sdk");
const connectors = new ReplitConnectors();

async function main() {
  const [method, endpoint, bodyStr] = process.argv.slice(2);
  const opts = { method: method || "GET" };
  if (bodyStr && bodyStr.trim()) {
    // Pass raw JSON string as body so the proxy forwards it to GitHub
    opts.body = bodyStr;
  }
  try {
    const response = await connectors.proxy("github", endpoint, opts);
    const text = await response.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    console.log(JSON.stringify({ status: response.status, body: json !== null ? json : text }));
    process.exit(0);
  } catch (err) {
    console.log(JSON.stringify({ error: err.message }));
    process.exit(1);
  }
}
main();
