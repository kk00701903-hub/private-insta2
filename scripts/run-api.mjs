import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const win = process.platform === "win32";
const venvPy = path.join(root, ".venv", win ? "Scripts/python.exe" : "bin/python");
const python = existsSync(venvPy) ? venvPy : "python";

const child = spawn(
  python,
  ["-m", "uvicorn", "insta_auto.web:create_app", "--factory", "--host", "127.0.0.1", "--port", "8787"],
  {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env },
  },
);

child.on("exit", (code, signal) => {
  if (signal) process.exit(1);
  process.exit(code ?? 0);
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => child.kill(sig));
}
