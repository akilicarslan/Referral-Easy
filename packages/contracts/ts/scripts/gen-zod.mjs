#!/usr/bin/env node
/**
 * Generates Zod schemas + inferred TypeScript types from packages/contracts/schemas/*.json.
 *
 * `packages/contracts/schemas/*.json` is the single source of truth (§1.1). This script is the
 * only thing that produces `src/generated/`; never hand-edit that directory.
 *
 * ── Why this is not a one-liner ─────────────────────────────────────────────────────────────
 *
 * **`json-schema-to-zod` does not resolve `$ref`.** Verified against v2.6.1: given a `$ref` —
 * whether to a sibling `$defs` entry, a root `$ref`, or with `definitions` supplied alongside —
 * it emits `z.any()`. A Zod schema full of `z.any()` type-checks, runs, and *validates nothing*,
 * so the failure is invisible until something invalid sails through a boundary it should not.
 *
 * Every `$ref` is therefore fully dereferenced before generation, and `tests/generated.test.ts`
 * asserts the output contains no `z.any()`. If that test ever fails, this is what changed.
 *
 * **Definition names are only unique within a file.** `AdapterType` exists in both booking.json
 * and directory.json meaning different things, so exports are namespaced `origin$Name`.
 *
 * **Sibling keywords beside `$ref` must survive.** Several schemas write
 * `{"$ref": "...", "description": "..."}`; in JSON Schema 2020-12 both apply, so the resolved
 * target is merged with its siblings rather than replaced.
 */

import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { jsonSchemaToZod } from 'json-schema-to-zod';

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = resolve(HERE, '..');
const SCHEMAS_DIR = resolve(PKG, '..', 'schemas');
// Overridable so `generate.sh --check` can regenerate into a temp directory for diffing.
const OUT_DIR = process.env.CONTRACTS_TS_OUT ?? join(PKG, 'src', 'generated');

const SCHEMA_FILES = [
  'common',
  'task',
  'patient',
  'coverage',
  'directory',
  'documents',
  'booking',
  'workflow',
  'sse',
];

const cache = new Map();
function readSchema(name) {
  if (!cache.has(name)) {
    cache.set(name, JSON.parse(readFileSync(join(SCHEMAS_DIR, `${name}.json`), 'utf8')));
  }
  return cache.get(name);
}

/** Keywords that are documentation or identity, not structure. Dropped so output stays readable. */
const DROPPED = new Set(['$comment', '$id', '$schema', '$defs']);

/**
 * Fully replace every `$ref` with the schema it points at.
 *
 * @param {unknown} node
 * @param {string} originFile  file the *containing* schema came from, for relative refs
 * @param {string[]} stack     ref keys already being expanded, to detect cycles
 */
function deref(node, originFile, stack) {
  if (Array.isArray(node)) return node.map((n) => deref(n, originFile, stack));
  if (node === null || typeof node !== 'object') return node;

  if (typeof node.$ref === 'string') {
    const [filePart, frag = ''] = node.$ref.split('#');
    const targetFile = filePart === '' ? originFile : filePart.replace(/\.json$/, '');
    const match = /^\/\$defs\/(.+)$/.exec(frag);
    if (!match) throw new Error(`unsupported $ref: ${node.$ref}`);
    const defName = match[1];

    const key = `${targetFile}#${defName}`;
    if (stack.includes(key)) {
      throw new Error(
        `cyclic $ref: ${[...stack, key].join(' -> ')}. ` +
          'A cycle needs z.lazy(), which this generator does not emit; add the guard rather than ' +
          'letting the generator quietly emit z.any().',
      );
    }

    const target = readSchema(targetFile).$defs?.[defName];
    if (!target) throw new Error(`missing $defs/${defName} in ${targetFile}.json`);

    const resolved = deref(target, targetFile, [...stack, key]);
    // Siblings beside $ref still apply (2020-12), so merge them over the target.
    const siblings = Object.fromEntries(
      Object.entries(node).filter(([k]) => k !== '$ref' && !DROPPED.has(k)),
    );
    return Object.keys(siblings).length === 0
      ? resolved
      : { ...resolved, ...deref(siblings, originFile, stack) };
  }

  const out = {};
  for (const [key, value] of Object.entries(node)) {
    if (DROPPED.has(key)) continue;
    out[key] = deref(value, originFile, stack);
  }
  return out;
}

/** Strip the generator's own `import { z } from "zod"` header; one import per file is emitted. */
function withoutImport(code) {
  return code
    .split('\n')
    .filter((line) => !/^\s*import\s.*from\s*['"]zod['"];?\s*$/.test(line))
    .join('\n')
    .trim();
}

/**
 * `z.any()` and `z.unknown()` accept exactly the same values, but only `z.unknown()` keeps the
 * inferred type honest: `z.infer` of `z.any()` is `any`, which silently switches off type-checking
 * for every field that touches it. The generator emits `z.any()` for `oneOf` branches (paired with
 * a genuine `superRefine`, so runtime validation is correct) — this rewrites those to `z.unknown()`
 * so the TypeScript surface is safe too.
 */
function harden(code) {
  return code.replaceAll('z.any()', 'z.unknown()');
}

/**
 * Collect every inline enum that carries a `title`, so it can be exported by name.
 *
 * `json-schema-to-zod` inlines enums into their parent object, so a consumer that needs the
 * *values* — a status union, a result kind — would otherwise have to reach into the parent schema
 * to get at them, and the Python side (which hoists them via `--use-title-as-name`) would have a
 * surface TypeScript lacks. Both expressions are emitted from the same JSON in the same run, so
 * they cannot drift; the generation drift check covers the file as a whole.
 */
function collectTitledEnums(node, out) {
  if (Array.isArray(node)) {
    for (const item of node) collectTitledEnums(item, out);
    return;
  }
  if (node === null || typeof node !== 'object') return;

  if (Array.isArray(node.enum) && typeof node.title === 'string') {
    const values = node.enum.filter((v) => typeof v === 'string');
    if (values.length > 0) out.set(node.title, values);
  }
  for (const value of Object.values(node)) collectTitledEnums(value, out);
}

rmSync(OUT_DIR, { recursive: true, force: true });
mkdirSync(OUT_DIR, { recursive: true });

const written = [];
let exportCount = 0;

for (const file of SCHEMA_FILES) {
  const doc = readSchema(file);
  const defNames = Object.keys(doc.$defs ?? {});
  if (defNames.length === 0) continue;

  const lines = [
    '// GENERATED — do not edit. Source: packages/contracts/schemas/*.json',
    '// Regenerate with: pnpm gen:contracts',
    "import { z } from 'zod';",
    '',
  ];

  const titledEnums = new Map();
  for (const defName of defNames) {
    const alias = `${file}$${defName}`;
    const schema = deref(doc.$defs[defName], file, [`${file}#${defName}`]);
    collectTitledEnums(schema, titledEnums);
    const body = harden(withoutImport(jsonSchemaToZod(schema, { module: 'esm', name: alias })));
    lines.push(body.endsWith(';') ? body : `${body};`);
    lines.push(`export type ${alias} = z.infer<typeof ${alias}>;`);
    lines.push('');
    exportCount += 1;
  }

  // Enums are also exported by name, for consumers that need the values rather than the shape.
  // A title that is *already* a $def in this file is skipped: the def is emitted above, and
  // re-emitting it from an inlined copy of itself would be a redeclaration.
  const ownDefNames = new Set(defNames);
  const extraEnums = [...titledEnums].filter(([title]) => !ownDefNames.has(title));
  if (extraEnums.length > 0) {
    lines.push('// Inline enums, also exported by name.');
    for (const [title, values] of extraEnums.sort(([a], [b]) => a.localeCompare(b))) {
      const alias = `${file}$${title}`;
      const literals = values.map((v) => JSON.stringify(v)).join(', ');
      lines.push(`export const ${alias} = z.enum([${literals}]);`);
      lines.push(`export type ${alias} = z.infer<typeof ${alias}>;`);
      lines.push('');
      exportCount += 1;
    }
  }

  writeFileSync(join(OUT_DIR, `${file}.ts`), lines.join('\n'), 'utf8');
  written.push(file);
}

writeFileSync(
  join(OUT_DIR, 'index.ts'),
  ['// GENERATED — do not edit.', ...written.map((n) => `export * from './${n}.js';`), ''].join(
    '\n',
  ),
  'utf8',
);

console.log(`Generated ${exportCount} definitions across ${written.length} modules → ${OUT_DIR}`);
