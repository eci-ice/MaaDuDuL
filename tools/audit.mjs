import {spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";

// Yarn Classic's --level only filters output; it does not change the exit code.
// Read the summary so audit errors cannot be mistaken for low-severity findings.
const result = spawnSync(
    "yarn",
    [
        "audit",
        "--json",
    ],
    {
        cwd: fileURLToPath(new URL("../", import.meta.url)),
        encoding: "utf8",
        shell: process.platform === "win32",
        maxBuffer: 16 * 1024 * 1024,
    },
);

if (result.stdout) process.stdout.write(result.stdout);
if (result.stderr) process.stderr.write(result.stderr);

try {
    if (result.error) throw result.error;
    if (result.signal || result.status === null) throw new Error("Yarn audit did not finish normally");

    const events = result.stdout
        .split(/\r?\n/)
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line));
    if (events.some((event) => event.type === "error")) throw new Error("Yarn audit reported an error");
    const summaries = events.filter((event) => event.type === "auditSummary");
    if (summaries.length !== 1) throw new Error("Expected one complete Yarn audit summary");

    const counts = summaries[0].data.vulnerabilities;
    const severities = [
        "info",
        "low",
        "moderate",
        "high",
        "critical",
    ];
    let expectedStatus = 0;
    for (const [
        index,
        severity,
    ] of severities.entries()) {
        if (!Number.isSafeInteger(counts[severity]) || counts[severity] < 0) {
            throw new Error(`Invalid audit count for ${severity}`);
        }
        if (counts[severity] > 0) expectedStatus |= 1 << index;
    }
    if (result.status !== expectedStatus) throw new Error(`Unexpected Yarn audit exit code: ${result.status}`);

    console.log(`Audit: ${counts.high} high, ${counts.critical} critical (blocking); lower severities are advisory.`);
    process.exitCode = counts.high > 0 || counts.critical > 0 ? 1 : 0;
} catch (error) {
    console.error(`Dependency audit failed: ${error.message}`);
    process.exitCode = 1;
}
