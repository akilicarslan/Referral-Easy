import { bundle } from "@remotion/bundler";
import { getCompositions, openBrowser, renderStill } from "@remotion/renderer";
import { mkdir, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";

await mkdir("qa", { recursive: true });
await rm(path.resolve(".cache/qa-bundle"), { recursive: true, force: true });
const serveUrl = await bundle({
  entryPoint: path.resolve("src/index.ts"),
  outDir: path.resolve(".cache/qa-bundle"),
  rspack: true,
  symlinkPublicDir: true,
});
const browser = await openBrowser("chrome", {
  browserExecutable:
    process.env.REMOTION_BROWSER_EXECUTABLE ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
});
try {
  const compositions = await getCompositions(serveUrl, {
    puppeteerInstance: browser,
  });
  const composition = compositions.find((c) => c.id === "ReferralEasy");
  if (
    !composition ||
    composition.durationInFrames !== 1800 ||
    composition.fps !== 30
  )
    throw new Error("Video must be exactly 60 seconds");
  const shots = [
    ["opening", 130],
    ["hospital", 228],
    ["review", 450],
    ["architecture", 735],
    ["booking", 1010],
    ["continuity", 1250],
    ["recovery", 1520],
    ["traces", 1655],
    ["closing", 1755],
    ["label-240", 240],
    ["label-241", 241],
    ["label-330", 330],
    ["label-420", 420],
    ["label-480", 480],
    ["label-534", 534],
  ];
  for (const [name, frame] of shots) {
    await renderStill({
      serveUrl,
      composition,
      frame,
      output: path.resolve(`qa/${name}.png`),
      puppeteerInstance: browser,
    });
    console.log(`${name}: frame ${frame}`);
  }
  const labelSamples = shots.filter(([name]) =>
    String(name).startsWith("label-"),
  );
  const labelHashes = labelSamples.map(([name, frame]) => ({
    frame,
    hash: execFileSync(
      "ffmpeg",
      [
        "-v",
        "error",
        "-i",
        `qa/${name}.png`,
        "-vf",
        "crop=942:36:861:876",
        "-pix_fmt",
        "rgb24",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
      ],
      { encoding: "utf8" },
    ).trim(),
  }));
  if (new Set(labelHashes.map((x) => x.hash)).size !== 1)
    throw new Error("Demo label moved or changed between frames");
  await writeFile(
    "qa/label-stability.json",
    JSON.stringify(
      {
        crop: { x: 861, y: 876, width: 942, height: 36 },
        identical: true,
        samples: labelHashes,
      },
      null,
      2,
    ) + "\n",
  );
  console.log(
    "Stationary demo note: identical pixels across six samples, including adjacent frames.",
  );
  await writeFile(
    "qa/metadata.json",
    JSON.stringify(
      {
        id: composition.id,
        frames: composition.durationInFrames,
        fps: composition.fps,
        width: composition.width,
        height: composition.height,
        shots,
      },
      null,
      2,
    ) + "\n",
  );
} finally {
  await browser.close({ silent: true });
  await rm(path.resolve(".cache/qa-bundle"), { recursive: true, force: true });
}
