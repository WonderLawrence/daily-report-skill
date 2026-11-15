/**
 * DSH skill provider for the `daily-report` skill.
 *
 * The bundle ships the skill exactly as a filesystem skill root does: `SKILL.md`
 * carries the front matter and the instructions, and `resourceBase` points at the
 * package root, so `references/`, `examples/`, and `scripts/` resolve the same way
 * a locally installed copy does.
 *
 * @module daily-report-skill
 */

import { readFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const PROVIDER_NAME = 'daily-report'
const SKILL_NAME = 'daily-report'
const SKILL_FILE = fileURLToPath(new URL('../SKILL.md', import.meta.url))
const RESOURCE_DIRECTORY = dirname(SKILL_FILE)

/**
 * Precedence rank of packaged skill providers: the rank DSH gives the skills it
 * ships. A project or user copy of `daily-report` still wins, so the bundle only
 * fills the gap on a machine that has no local copy.
 */
const PACKAGED_SKILL_RANK = 600

const FALLBACK_DESCRIPTION =
  '基于本期参与的 PR、其中本人提交及其编码 agent 会话（Claude Code / Codex / DeepSeek Harness），撰写和修改中文日报、周报。'

/** Invocation policy: visible to the model catalog and to the user surface. */
const INVOCATION = { modelInvocable: true, userInvocable: true }

/** Parsed skill, cached so `list` and `get` agree on one body. */
let cached

function loadSkill() {
  cached ??= readFile(SKILL_FILE, 'utf8').then(parse)
  return cached
}

/**
 * Split YAML front matter from the instruction body.
 *
 * Only the two scalar fields the skill registry needs are interpreted; the body is
 * returned untouched so the model sees the same instructions as a filesystem copy.
 */
function parse(text) {
  const source = text.replace(/^\uFEFF/u, '')
  const match = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/u.exec(source)
  if (match === null) return { attributes: {}, body: source.trim() }
  const attributes = {}
  for (const line of match[1].split(/\r?\n/u)) {
    const field = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/u.exec(line)
    if (field === null) continue
    attributes[field[1]] = unquote(field[2])
  }
  return { attributes, body: source.slice(match[0].length).trim() }
}

function unquote(value) {
  const text = value.trim()
  if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) return JSON.parse(text)
  if (text.length >= 2 && text.startsWith("'") && text.endsWith("'")) return text.slice(1, -1).replaceAll("''", "'")
  return text
}

function skillName(attributes) {
  const declared = attributes.name
  return typeof declared === 'string' && /^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(declared) ? declared : SKILL_NAME
}

function skillDescription(attributes) {
  const declared = attributes.description
  return typeof declared === 'string' && declared.trim() !== '' ? declared : FALLBACK_DESCRIPTION
}

function summary(attributes) {
  return {
    name: skillName(attributes),
    description: skillDescription(attributes),
    invocation: INVOCATION,
    provider: PROVIDER_NAME,
    source: 'bundled',
    resourceBase: { kind: 'directory', path: RESOURCE_DIRECTORY },
    rank: PACKAGED_SKILL_RANK,
    locator: SKILL_FILE,
  }
}

const provider = {
  name: PROVIDER_NAME,

  async list() {
    const { attributes } = await loadSkill()
    return [summary(attributes)]
  },

  async get() {
    const { attributes, body } = await loadSkill()
    return {
      name: skillName(attributes),
      description: skillDescription(attributes),
      invocation: INVOCATION,
      source: 'bundled',
      provider: PROVIDER_NAME,
      resourceBase: { kind: 'directory', path: RESOURCE_DIRECTORY },
      path: SKILL_FILE,
      content: body,
    }
  },
}

/** Cordis plugin name. */
export const name = 'skill-daily-report'

/** The skill registry this provider registers on. */
export const inject = ['skills']

/** Register the packaged `daily-report` skill provider. */
export function apply(ctx) {
  ctx.skills.registerProvider(() => provider)
}
