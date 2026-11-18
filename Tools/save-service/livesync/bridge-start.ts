// Runs in the livesync-bridge container before the bridge itself.
//
// The bridge has to use exactly the vault settings that the first Obsidian
// device stored when it initialised the database: encryption, path
// obfuscation, ID key version, path normalisation and filename case. It can't
// choose them itself, and a mismatch makes the devices unable to read what it
// writes. So this waits until a device has initialised the database, then
// writes /app/dat/config.json from those settings plus the credentials in the
// environment.

const env = (name: string, fallback = ""): string => (Deno.env.get(name) ?? fallback).trim();

const url = env("COUCHDB_URL", "http://couchdb:5984").replace(/\/+$/, "");
const username = env("COUCHDB_USER");
const password = env("COUCHDB_PASSWORD");
const database = env("LIVESYNC_DB", "obsidian");
const passphrase = env("LIVESYNC_PASSPHRASE");
const idKey = env("LIVESYNC_ID_KEY");
const configPath = env("BRIDGE_CONFIG", "/app/dat/config.json");

// Never uploaded from the server's copy:
//   - dot files and folders: .git, .obsidian, .trash, .env (the bridge can't carry nested dot files anyway)
//   - Python caches and partial downloads
const ignore = [
  "**/.*",
  "**/__pycache__/**",
  "**/*.part",
  "**/*.ytdl",
  ...env("BRIDGE_IGNORE").split(",").map((p) => p.trim()).filter(Boolean),
];

type Tweaks = Record<string, unknown>;
type Milestone = { state: "ready"; tweaks: Tweaks } | { state: "missing" } | { state: "denied"; status: number };

function fail(message: string): never {
  console.error(`ERROR: ${message}`);
  Deno.exit(1);
}

function basicAuth(user: string, pass: string): string {
  const bytes = new TextEncoder().encode(`${user}:${pass}`);
  return "Basic " + btoa(String.fromCharCode(...bytes));
}

// The first device stores its settings as "tweak values" in this document.
async function readMilestone(): Promise<Milestone> {
  const res = await fetch(`${url}/${encodeURIComponent(database)}/_local/obsydian_livesync_milestone`, {
    headers: { Authorization: basicAuth(username, password) },
  });
  if (res.status === 401 || res.status === 403) {
    await res.body?.cancel();
    return { state: "denied", status: res.status };
  }
  if (!res.ok) {
    await res.body?.cancel();
    return { state: "missing" };
  }
  const doc = await res.json();
  const values = (doc.tweak_values ?? {}) as Record<string, Tweaks>;
  return { state: "ready", tweaks: values["PREFERRED"] ?? Object.values(values)[0] ?? {} };
}

if (!username || !password) fail("set COUCHDB_USER and COUCHDB_PASSWORD");

let tweaks: Tweaks | undefined;
let announced = false;
while (tweaks === undefined) {
  let milestone: Milestone = { state: "missing" };
  try {
    milestone = await readMilestone();
  } catch (_e) {
    // CouchDB isn't up yet
  }
  if (milestone.state === "denied") {
    fail(`CouchDB refused ${username} (HTTP ${milestone.status}): check COUCHDB_USER / COUCHDB_PASSWORD`);
  }
  if (milestone.state === "ready") {
    tweaks = milestone.tweaks;
    break;
  }
  if (!announced) {
    console.log(`Waiting for the first Obsidian device to initialise the "${database}" database ` +
      `(README: "Deploying"). Checking every 15 s.`);
    announced = true;
  }
  await new Promise((resolve) => setTimeout(resolve, 15_000));
}

const encrypted = tweaks.encrypt === true;
const obfuscated = tweaks.usePathObfuscation === true;
const idVersion = Number(tweaks.idDerivationVersion ?? 0);
const pathNormalizationVersion = Number(tweaks.pathNormalizationVersion ?? 0);
const handleFilenameCaseSensitive = tweaks.handleFilenameCaseSensitive === true;

if ((encrypted || obfuscated) && !passphrase) {
  fail("the database is end-to-end encrypted: set LIVESYNC_PASSPHRASE");
}
if (idVersion >= 1 && !/^(sls-id-v1:)?[0-9a-f]{64}$/.test(idKey)) {
  fail("the database uses an ID key: set LIVESYNC_ID_KEY to the same sls-id-v1:... code the Setup URI was made with");
}

// Both peers in one group must agree on how paths are compared.
const shared = { group: "vault", pathNormalizationVersion, handleFilenameCaseSensitive };
const config = {
  peers: [
    {
      type: "couchdb",
      name: "couchdb",
      ...shared,
      url,
      database,
      username,
      password,
      passphrase: encrypted ? passphrase : "",
      obfuscatePassphrase: obfuscated ? passphrase : "",
      ...(idVersion >= 1 ? { idDerivationKey: idKey } : {}),
      baseDir: "",
      useRemoteTweaks: true,
    },
    {
      type: "storage",
      name: "vault",
      ...shared,
      baseDir: "/vault/",
      scanOfflineChanges: true,
      // The default watcher. Chokidar reports every existing file at once on
      // startup, and the bridge then reads and uploads all of them in parallel:
      // with a 4 GB vault it ran out of memory. The default watcher scans one
      // file at a time. Files in new folders that it misses are picked up by
      // the hourly rescan (bridge-entrypoint.sh).
      useChokidar: false,
      ignore,
    },
  ],
};

await Deno.writeTextFile(configPath, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
console.log(
  `Bridge config written: encryption ${encrypted ? "on" : "off"}, path obfuscation ${obfuscated ? "on" : "off"}, ` +
    `ID key v${idVersion}, path normalisation v${pathNormalizationVersion}, ` +
    `case-sensitive names ${handleFilenameCaseSensitive}, not uploaded: ${ignore.join(" ")}`,
);
