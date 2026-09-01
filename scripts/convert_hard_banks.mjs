// 把 T62 无解识别题转成 app/hard_items.json，供 Python 端 hardbank.py 读取。
//
// 题面本身就是评测资产；从上游确定性生成可避免手工复制改变题面或答案。
//
// 用法（在 test/ 目录下跑）：node scripts/convert_hard_banks.mjs
// 上游 HardcoreLogic 资产变更后重跑。hardbank.py 按内容计算 HARD_VERSION，
// 题面一变版本号就变，旧的硬题标杆会自动标为失效而不是默默给个错答案。

import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const outPath = join(here, '..', 'app', 'hard_items.json');

const sourceRoot = process.env.API_EVALUATOR_ROOT || 'Z:\\API-evaluator-main';
const { HARDCORE_LOGIC_SCENARIOS } = await import(
  new URL(`file:///${sourceRoot.replaceAll('\\', '/')}/server/scenarios/hardcore-logic.mjs`));

function normalize(raw, bank) {
  const expected = Array.isArray(raw.expected) ? raw.expected : [raw.expected];
  const item = {
    id: raw.id,
    bank,
    name: raw.name,
    // 四道无解识别题在报告中使用同一分组。
    group: '无解识别',
    category: raw.category,
    difficulty: raw.difficulty,
    max_tokens: raw.maxTokens,
    scorer: raw.scorer,
    prompt: raw.prompt,
    expected: expected.map((e) => typeof e === 'string' ? e : JSON.stringify(e)),
  };
  if (raw.source) item.source = raw.source;
  return item;
}

const items = [
  ...HARDCORE_LOGIC_SCENARIOS
    .filter((scenario) => scenario.config === 'unsolvable')
    .map((scenario) => normalize(scenario, 'hardcore_unsolvable')),
];

// 只支持 exact 判分。真出现别的判分器要显式报错，不要静默当 exact 处理 ——
// 那会把一道本该用别的口径判的题算成全错，拉低整份标杆。
const bad = items.filter((i) => !['exact', 'structured'].includes(i.scorer));
if (bad.length) {
  throw new Error(`只支持 scorer=exact，这些题不是：${bad.map((i) => i.id).join(', ')}`);
}
const dupes = items.map((i) => i.id).filter((id, n, arr) => arr.indexOf(id) !== n);
if (dupes.length) throw new Error(`题目 id 重复：${dupes.join(', ')}`);
for (const i of items) {
  if (!i.prompt || !i.expected.length || !i.expected[0]) {
    throw new Error(`题面或答案为空：${i.id}`);
  }
  if (!Number.isInteger(i.max_tokens) || i.max_tokens <= 0) {
    throw new Error(`max_tokens 不合法：${i.id}`);
  }
}

writeFileSync(outPath, JSON.stringify({ items }, null, 2) + '\n', 'utf8');

const byBank = {};
for (const i of items) byBank[i.bank] = (byBank[i.bank] || 0) + 1;
const cap = items.reduce((n, i) => n + i.max_tokens, 0);
console.log(`wrote ${items.length} items -> app/hard_items.json`);
console.log(`  by bank: ${JSON.stringify(byBank)}`);
console.log(`  max_tokens cap total: ${cap}`);
