// DCAP's listener for opencode. Copy to a project's .opencode/plugin/ folder and set DCAP_HOME.
// The AI gets no tool or instruction from this. It only sees an offer, or code after a yes.
import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

export const Dcap = async ({ client, directory }) => {
  const home = process.env.DCAP_HOME;
  const definitions = process.env.DCAP_DEFINITIONS;
  const said = [];

  const listen = (wrote) => {
    const input = JSON.stringify({ said: said.splice(0), wrote });
    const args = ["-m", "dcap.cli", "--workspace", directory];
    if (definitions) args.push("--definitions", definitions);
    const run = spawnSync("python3", [...args, "listen"], {
      input,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: join(home, "src"), PYTHONDONTWRITEBYTECODE: "1" },
    });
    const message = (run.stdout || "").trim();
    try {
      mkdirSync(join(directory, ".dcap"), { recursive: true });
      appendFileSync(join(directory, ".dcap", "plugin.log"), JSON.stringify({ input: JSON.parse(input), message }) + "\n");
    } catch {}
    return message;
  };

  // Before a tool runs: is the AI reaching behind DCAP? Exit code 2 means it must not run.
  const blocked = (args) => {
    const run = spawnSync("python3", ["-m", "dcap.cli", "--workspace", directory, "guard"], {
      input: JSON.stringify({ input: args || {}, cwd: directory }),
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: join(home, "src"), PYTHONDONTWRITEBYTECODE: "1" },
    });
    return run.status === 2;
  };

  return {
    "tool.execute.before": async (_input, output) => {
      if (blocked(output.args)) throw new Error("That isn't available here.");
    },
    "experimental.text.complete": async (_input, output) => {
      if (output.text) said.push(output.text);
    },
    "tool.execute.after": async (input, output) => {
      const args = input.args || {};
      const wrote = ["write", "edit"].includes(input.tool)
        ? [args.content, args.newString].filter((text) => typeof text === "string")
        : [];
      const message = listen(wrote);
      if (message) output.output = `${output.output}\n\n${message}`;
    },
    event: async ({ event }) => {
      if (event.type !== "session.idle") return;
      const message = listen([]);
      if (message) {
        await client.session.prompt({
          path: { id: event.properties.sessionID },
          body: { parts: [{ type: "text", text: message }] },
        });
      }
    },
  };
};
