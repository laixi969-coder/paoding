from fastmcp import FastMCP

from paoding.services.runtime import Runtime


def create_mcp(runtime=None):
    rt = runtime or Runtime()
    mcp = FastMCP(
        "paoding-mcp",
        instructions="庖丁知识库：查询结构化原子、跨界规律、趋势与创作者画像。空结果表示暂无数据。",
    )

    @mcp.tool()
    def paoding_query_playbook(domain: str, target_platform: str | None = None) -> list[dict]:
        """优先查询跨界元规则与目标平台的迁移建议。"""
        return rt.library.query_playbook(domain, target_platform)

    @mcp.tool()
    def paoding_fetch_atoms(
        atom_type: str | None = None,
        tags: list[str] | None = None,
        target_platform: str | None = None,
        limit: int = 20,
        category: str | None = None,
    ) -> list[dict]:
        """按 hook/visual/storyboard/sop 与标签获取创作弹药。"""
        return rt.library.fetch_atoms(atom_type, tags, target_platform, limit, category=category)

    @mcp.tool()
    def paoding_scan_trends(days: int = 7, platforms: list[str] | None = None) -> dict:
        """获取已追踪账号的近期趋势；缺少互动率的条目不会冒充爆款。"""
        return rt.library.scan_trends(days, platforms)

    @mcp.tool()
    def paoding_summon_creator(creator_name: str) -> dict:
        """按名称或 UUID 获取博主画像 System Prompt 和代表作。"""
        return rt.library.summon_creator(creator_name)

    @mcp.tool()
    def paoding_feedback_utility(atom_id: str, is_adopted: bool, event_id: str) -> dict:
        """反馈采纳结果。event_id 为本次调用的唯一标识；重试复用同一个值防止重复加分。"""
        return rt.library.feedback(atom_id, is_adopted, event_id)

    return mcp


def main():
    # Local stdio inherits the user's filesystem/database permissions; no unauthenticated network listener.
    create_mcp().run(transport="stdio")
