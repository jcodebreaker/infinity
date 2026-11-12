// The engine tools the activity views read by name (engine `step_title_for_call`): one list, so
// the run page's story and the work strip agree on what a Step is.

/** A Helper's progress sentence: the title of the Step. */
export const PROGRESS_TOOL = "report_progress";
/** Something a Helper flagged: titled "Flagged: <text>". */
export const FLAG_TOOL = "raise_flag";
export const FLAG_PREFIX = "Flagged: ";
export const ARTIFACT_SAVE_TOOL = "artifact_save";
export const SHELL_TOOL = "sys_os_shell";
export const READ_TOOL = "sys_os_read";
export const WRITE_TOOLS: ReadonlySet<string> = new Set(["sys_os_write", "sys_os_edit"]);
