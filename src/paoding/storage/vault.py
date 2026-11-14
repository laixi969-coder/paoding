import json
import os
import tempfile
from pathlib import Path

import yaml

DIRECTORIES = {
    "playbook": "00_Meta_Playbooks_跨界元规则",
    "persona": "00_Creators_DNA_博主数字分身",
    "trend": "01_Trend_Radar_趋势雷达简报",
    "hook": str(Path("02_Atom_Library_原子弹药库") / "Hooks_黄金前3秒与文案钩子"),
    "visual": str(Path("02_Atom_Library_原子弹药库") / "Visual_Specs_色彩与版式网格"),
    "storyboard": str(Path("02_Atom_Library_原子弹药库") / "Storyboards_分镜调度与节奏"),
    "sop": str(Path("02_Atom_Library_原子弹药库") / "Marketing_SOP_营销战役与符号"),
    "raw": "03_Raw_Archive_原始底稿与媒体映射",
    "taxonomy": str(Path("00_Meta_Playbooks_跨界元规则") / "_分类变更记录"),
}


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".paoding-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def frontmatter(data):
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n\n"


class Vault:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def initialize(self):
        for directory in DIRECTORIES.values():
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def write_node(self, node, atoms):
        self.initialize()
        raw = self.root / DIRECTORIES["raw"] / f"{node.id}.json"
        atomic_write(
            raw,
            json.dumps(
                {
                    "id": node.id,
                    "source_url": node.source_url,
                    "analysis": node.analysis,
                    "provenance": node.provenance,
                    "media_paths": node.media_paths,
                },
                ensure_ascii=False,
                indent=2,
            ).encode(),
        )
        path = self.root / DIRECTORIES["raw"] / f"{node.id}.md"
        body = frontmatter(
            {
                "id": node.id,
                "source": node.source_url,
                "platform": node.platform,
                "tags": node.analysis["tags"],
                "schema_version": "1.0",
                "demo": node.provenance.get("demo", False),
            }
        )
        body += f"# {node.title}\n\n{node.summary}\n\n## 跨界基因\n\n"
        for name, value in node.cross_domain_dna.items():
            body += f"- **{name}**：{value}\n"
        body += (
            "\n## 完整解剖\n\n```json\n"
            + json.dumps(node.analysis, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        body += "\n## 原始媒体\n\n" + "\n".join(f"- {p}" for p in node.media_paths) + "\n"
        atomic_write(path, body.encode())
        for atom in atoms:
            atom_path = self.root / DIRECTORIES[atom.atom_type] / f"{atom.id}.md"
            text = frontmatter(
                {
                    "id": atom.id,
                    "node_id": node.id,
                    "type": atom.atom_type,
                    "tags": atom.tags,
                    "evergreen": atom.evergreen,
                }
            )
            text += f"# {node.title}\n\n{atom.content}\n\n来源：[[{node.id}]]\n"
            atomic_write(atom_path, text.encode())
        return str(path)

    def write_evolution(self, record):
        self.initialize()
        path = self.root / DIRECTORIES[record.kind] / f"{record.id}.md"
        document = record.document
        body = frontmatter({"id": record.id, "kind": record.kind, "tags": document.get("tags", [])})
        body += f"# {document['title']}\n\n{document['summary']}\n\n"
        body += "\n".join(f"- {rule}" for rule in document["rules"])
        if document.get("system_prompt"):
            body += "\n\n## System Prompt\n\n" + document["system_prompt"]
        body += "\n\n## 依据\n\n" + "\n".join(f"- [[{i}]]" for i in document["source_node_ids"])
        atomic_write(path, body.encode())
        return str(path)

    def write_persona(self, account, record):
        path = self.root / DIRECTORIES["persona"] / account.id / "Creator_Profile.md"
        body = frontmatter(
            {"creator_id": account.id, "name": account.name, "evolution_id": record.id}
        )
        body += f"# {account.name}\n\n{record.document.get('system_prompt', '')}\n\n"
        body += "\n".join(f"- [[{id_}]]" for id_ in record.document["source_node_ids"])
        atomic_write(path, body.encode())
        return str(path)

    def write_taxonomy(self, record, categories, redirects):
        self.write_evolution(record)
        root = self.root / DIRECTORIES["playbook"]
        lines = ["# 自动分类树", "", "分类由已入库内容支持，原始卡片保持可追溯。", ""]
        for category in categories:
            directory = root / category["root"] / category["id"]
            path = directory / "index.md"
            body = frontmatter(
                {k: category[k] for k in ("id", "name", "root", "aliases", "source_count")}
            )
            body += f"# {category['name']}\n\n" + "\n".join(
                f"- [[{node_id}]]" for node_id in category["source_node_ids"]
            )
            atomic_write(path, body.encode())
            relative = path.relative_to(root).as_posix()
            lines.append(
                f"- {category['root']} / [{category['name']}]({relative}) · {category['source_count']} 张卡片"
            )
        for old in redirects:
            path = root / old["root"] / old["id"] / "index.md"
            atomic_write(
                path,
                (
                    frontmatter(old)
                    + f"本分类已合并：[查看新分类](../{old['merged_into']}/index.md)\n"
                ).encode(),
            )
        index = root / "00_分类索引.md"
        atomic_write(index, "\n".join(lines).encode())
        return str(index)
