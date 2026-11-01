export function taskCreateExecutionFields(
  lane: "harness" | "agent",
  workspacePath: string,
) {
  return {
    execution_lane: lane,
    execution_mode: lane === "harness" && !workspacePath.trim() ? "team" : "single",
    workspace_path: lane === "harness" ? workspacePath.trim() || null : null,
  };
}
