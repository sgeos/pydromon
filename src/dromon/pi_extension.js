// Dromon's extension for a Pi session line. Dromon's Pi adapter, src/dromon/pi.py,
// loads it with --extension after switching off every other extension, and documents
// it in docs/protocol/PI.md. It holds no state and does two things.
//
// 1. It registers Dromon's own tools as the MCP server "dromon", started with the
//    command in DROMON_PI_BRIDGE, a JSON array of strings, and given the line's token
//    from DROMON_LINE_TOKEN. Pi has no command-line option for an MCP server, and its
//    extension guide documents pi.registerMcpServer as the way to add one for a run.
//    The server is registered only when both variables are present.
//
// 2. It blocks, before the tool runs, every tool call whose name is not in
//    DROMON_PI_TOOLS, a JSON array of strings. Pi's --tools allowlist already limits
//    the tools, so this is a second check inside Pi. A missing or malformed list
//    blocks every call. Pi turns an exception in a tool_call handler into a failed
//    call, so an error here fails closed as well.

function stringList(name) {
  const raw = process.env[name];
  if (typeof raw !== "string") {
    return null;
  }
  try {
    const value = JSON.parse(raw);
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
      return value;
    }
  } catch {
    return null;
  }
  return null;
}

export default function dromon(pi) {
  const allowed = new Set(stringList("DROMON_PI_TOOLS") ?? []);
  const bridge = stringList("DROMON_PI_BRIDGE");
  const token = process.env.DROMON_LINE_TOKEN;
  if (bridge !== null && bridge.length > 0 && typeof token === "string" && token) {
    pi.registerMcpServer("dromon", {
      command: bridge[0],
      args: bridge.slice(1),
      env: { DROMON_LINE_TOKEN: token },
      exposure: "direct",
    });
  }
  pi.on("tool_call", (event) => {
    if (allowed.has(event.toolName)) {
      return undefined;
    }
    return {
      block: true,
      reason: `Dromon did not give this line the tool ${event.toolName}`,
    };
  });
}
