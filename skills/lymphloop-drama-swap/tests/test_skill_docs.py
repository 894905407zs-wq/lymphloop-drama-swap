"""SKILL.md has to be readable in one go, and the documents it points to have to exist.

2026-11-27: Antigravity's read_file returns at most 46,080 bytes per call. Its run of 2026-11-26 read SKILL.md once
(62.6 KB then) and never asked for the rest, so the last rule of the working rules, all of "不可违反的规则" and the file map
were not in front of it. The rules now come first and the file stays well under the limit; details live in references/."""
import re, unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
READ_LIMIT = 46080     # bytes Antigravity's read_file returns per call
BUDGET = 42000         # what SKILL.md itself promises to stay under


def size(path):
    return len(path.read_text(encoding='utf-8').encode('utf-8'))     # newlines counted as LF on every system


class SkillDocs(unittest.TestCase):
    def test_skill_md_fits_in_one_read(self):
        n = size(SKILL/'SKILL.md')
        self.assertLess(BUDGET, READ_LIMIT)
        self.assertLessEqual(n, BUDGET, f'SKILL.md is {n} bytes: move detail into references/ instead of adding it here')

    def test_the_rules_come_before_the_steps(self):
        text = (SKILL/'SKILL.md').read_text(encoding='utf-8')
        order = [text.index(h) for h in ('## 不可违反的规则', '## Agent 执行守则', '## 固定事实', '## 流程')]
        self.assertEqual(order, sorted(order))

    def test_detail_files_fit_in_one_read_too(self):
        for name in ('skill-rules-full.md', 'skill-steps-full.md', 'watermark.md', 'file-map.md'):
            self.assertLessEqual(size(SKILL/'references'/name), READ_LIMIT, name)

    def test_every_linked_document_exists(self):
        docs = [SKILL/'SKILL.md', *sorted((SKILL/'references').glob('*.md'))]
        missing = []
        for doc in docs:
            for m in re.finditer(r'\]\(([^)#\s]+\.md)(?:#[^)]*)?\)', doc.read_text(encoding='utf-8')):
                if not m.group(1).startswith(('http://', 'https://')) and not (doc.parent/m.group(1)).is_file():
                    missing.append(f'{doc.name} -> {m.group(1)}')
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
