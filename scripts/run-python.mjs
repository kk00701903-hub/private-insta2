import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const win = process.platform === "win32";
const venvPy = path.join(root, ".venv", win ? "Scripts/python.exe" : "bin/python");
const python = existsSync(venvPy) ? venvPy : "python";
const args = process.argv.slice(2);

const child = spawn(python, args, {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env },
});

child.on("exit", (code, signal) => {
  if (signal) process.exit(1);
  process.exit(code ?? 0);
});
