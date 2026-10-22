#!/usr/bin/env python3
"""本地材料存在性辅助；不读取材料正文，不作投标合规结论。"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import fnmatch
import html
import json
import os
from pathlib import Path
import stat
import sys

FIELDS = ['id', 'title', 'required', 'pattern']
LABELS = {'found': '已找到（仅文件名）', 'missing': '必需附件缺失',
          'optional_missing': '可选附件未提供', 'review': '匹配歧义，待人工复核'}
NOTICE = ('本报告只是材料存在性辅助，仅按文件名匹配，不读取或理解文档正文。'
          '文件存在不代表内容正确、有效、签章齐全或投标合规；不保证中标。'
          '所有项目仍需人工核对招标原文及附件正文。')


def checked_path(value):
    """在规范化前检查路径各级，拒绝软链接及 ..，不跟随越界链接。"""
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise ValueError('路径不得包含 ..')
    path = path.absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError('路径包含软链接：{}'.format(part))
    return path.resolve()


def load_requirements(path):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('要求清单必须是普通 CSV 文件')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != FIELDS:
            raise ValueError('CSV 表头必须依次为 ' + ','.join(FIELDS))
        requirements = []
        ids = set()
        for row in reader:
            line = reader.line_num
            if set(row) != set(FIELDS) or any(v is None for v in row.values()):
                raise ValueError('CSV 第 {} 行列数不正确'.format(line))
            row = {key: value.strip() for key, value in row.items()}
            if any(not value or any(ord(c) < 32 for c in value)
                   for value in row.values()):
                raise ValueError('CSV 第 {} 行含空字段或控制字符'.format(line))
            if row['id'] in ids:
                raise ValueError('CSV 第 {} 行 ID 重复：{}'.format(line, row['id']))
            if row['required'] not in ('yes', 'no'):
                raise ValueError('CSV 第 {} 行 required 只能为 yes/no'.format(line))
            pattern = row['pattern']
            if (any(c in pattern for c in '/\\[]:') or '**' in pattern
                    or pattern in ('.', '..')):
                raise ValueError('CSV 第 {} 行 pattern 只允许文件名及 *、? 通配符'.format(line))
            ids.add(row['id'])
            requirements.append(row)
    if not requirements:
        raise ValueError('CSV 清单不能为空')
    return requirements


def inventory(root):
    if not root.is_dir():
        raise ValueError('材料路径必须是目录')
    names = []

    def fail(error):
        raise error

    for folder, directories, files in os.walk(root, followlinks=False, onerror=fail):
        for name in directories + files:
            path = Path(folder) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('材料树包含软链接，停止检查：{}'.format(path))
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError('材料树包含非普通文件，停止检查：{}'.format(path))
            if stat.S_ISREG(mode):
                names.append(path.relative_to(root).as_posix())
    return sorted(names)


def match(requirements, names):
    items = []
    for requirement in requirements:
        candidates = [name for name in names
                      if fnmatch.fnmatchcase(name.rsplit('/', 1)[-1], requirement['pattern'])]
        status = ('review' if len(candidates) > 1 else 'found' if candidates
                  else 'missing' if requirement['required'] == 'yes' else 'optional_missing')
        items.append(dict(requirement, status=status, candidates=candidates, note=''))
    uses = Counter(name for item in items for name in item['candidates'])
    for item in items:
        if any(uses[name] > 1 for name in item['candidates']):
            item['status'] = 'review'
            item['note'] = '候选文件被多个要求匹配，请核对规则与附件用途。'
        elif item['status'] == 'review':
            item['note'] = '多个候选/版本，程序不自动选择最新版本。'
    return items


def safe_cell(value):
    # Excel/LibreOffice 常见公式前缀；引号由 csv.writer 另行处理。
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def write_reports(output, report):
    escape = lambda value: html.escape(str(value), quote=True)
    rows = []
    for item in report['items']:
        values = [item['id'], item['title'], '必需' if item['required'] == 'yes' else '可选',
                  item['pattern'], LABELS[item['status']], '\n'.join(item['candidates']) or '—',
                  item['note']]
        rows.append('<tr>' + ''.join('<td>{}</td>'.format(escape(v)) for v in values) + '</tr>')
    summary = '；'.join('{}：{}'.format(LABELS[k], report['summary'].get(k, 0)) for k in LABELS)
    page = '''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>标书材料人工复核报告</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;max-width:1200px;margin:32px auto;padding:0 20px;color:#172b3a}}h1{{font-size:28px}}.notice{{background:#fff3d5;padding:18px;border-left:5px solid #ae6b00}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{border:1px solid #aab8c2;text-align:left;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere}}th{{background:#eaf1f5}}small{{color:#465b6a}}@media print{{body{{margin:0;font-size:11px}}}}</style>
<h1>标书材料人工复核报告</h1><p class="notice">{notice}</p>
<p>{summary}</p><small>生成时间（UTC）：{time}；扫描普通文件数：{count}</small>
<p>要求清单：{requirements}<br>材料目录：{materials}</p>
<table><thead><tr><th>ID</th><th>材料要求</th><th>必需性</th><th>文件名规则</th><th>存在性结果</th><th>候选相对路径</th><th>复核提示</th></tr></thead><tbody>{rows}</tbody></table>
<p>人工复核：□ 清单来源与完整性 □ 附件用途及版本 □ 正文与格式 □ 签字盖章及有效期</p></html>'''.format(notice=escape(NOTICE), summary=escape(summary),
                               time=escape(report['generated_at']), count=report['file_count'],
                               requirements=escape(report['requirements']),
                               materials=escape(report['materials']), rows=''.join(rows))
    output.mkdir()  # 不覆盖既有目录；仅在全部输入校验及匹配成功后创建。
    with (output / 'report.html').open('x', encoding='utf-8') as stream:
        stream.write(page)
    with (output / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    columns = FIELDS + ['status', 'candidates', 'note']
    with (output / 'report.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for item in report['items']:
            writer.writerow([safe_cell('\n'.join(item[key]) if key == 'candidates' else item[key])
                             for key in columns])


def main(argv=None):
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument('--requirements', required=True, help='UTF-8 CSV 要求清单')
    parser.add_argument('--materials', required=True, help='只读枚举的材料目录')
    parser.add_argument('--output', required=True, help='新建报告目录，父目录须存在')
    args = parser.parse_args(argv)
    try:
        requirements_path = checked_path(args.requirements)
        materials = checked_path(args.materials)
        output = checked_path(args.output)
        if output == materials or materials in output.parents:
            raise ValueError('报告目录不得位于材料目录中')
        if output.exists():
            raise ValueError('报告目录已存在，请指定新的目录名')
        if not output.parent.is_dir():
            raise ValueError('报告目录的父目录不存在')
        # 目录身份检查也覆盖大小写不敏感文件系统和同目录别名。
        if any(parent.samefile(materials) for parent in output.parents):
            raise ValueError('报告目录不得位于材料目录中（含目录别名）')
        requirements = load_requirements(requirements_path)
        names = inventory(materials)
        items = match(requirements, names)
        report = {'schema_version': 1, 'notice': NOTICE,
                  'generated_at': datetime.now(timezone.utc).isoformat(),
                  'requirements': str(requirements_path), 'materials': str(materials),
                  'file_count': len(names), 'summary': dict(Counter(i['status'] for i in items)),
                  'items': items}
        write_reports(output, report)
        print('报告已生成：{}；所有项目仍需人工复核。'.format(output))
        return 1 if any(i['status'] in ('missing', 'review') for i in items) else 0
    except (OSError, ValueError, csv.Error) as error:
        print('错误：{}。未得出完整检查结论；若输出目录已创建，报告可能不完整。'.format(error),
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
