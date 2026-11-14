"""Explicit isolated demo data; never used by the production gateway."""

from paoding.domain.schemas import EvolutionDocument, Modality


def demo_analysis(content):
    base = {
        "title": "[DEMO] " + content.title[:290],
        "summary": "离线演示记录，用于验证管道，不代表模型分析。\n" + content.text[:200],
        "tags": ["demo", "离线验证"],
        "evergreen": False,
        "evidence": {"basis": "demo", "limitations": ["Synthetic fixture, not model inference"]},
        "cross_domain_dna": {
            "universal_principle": "[DEMO] 先呈现问题，再给出步骤。",
            "transfer_to_short_video": "[DEMO] 开场问题、过程、结论。",
            "transfer_to_xiaohongshu": "[DEMO] 首图问题、内页步骤。",
            "transfer_to_wechat_article": "[DEMO] 用案例组织长文。",
        },
        "modality": content.modality.value,
    }
    if content.modality == Modality.TEXT:
        base.update(
            core_pain_point="[DEMO] 示例问题",
            consumer_insight="[DEMO] 示例洞察",
            big_idea="[DEMO] 示例策略",
            super_symbol=None,
            execution_sop=[{"phase": "演示", "action": "验证数据闭环", "success_metric": None}],
        )
    elif content.modality == Modality.IMAGE:
        base.update(
            color_system={
                "palette": [c.model_dump() for c in content.palette],
                "contrast_rule": "[DEMO] 未分析",
                "mood": ["演示"],
            },
            layout_and_grid={
                "grid": "未分析",
                "focal_point": "未分析",
                "reading_path": "未分析",
                "whitespace_percent": None,
            },
            typography={
                "weight_contrast": "未分析",
                "font_pairing": "未分析",
                "depth_interleaving": "未分析",
            },
            reusable_mj_prompt="Demo only; no visual analysis performed",
        )
    else:
        base.update(
            pacing_metrics={
                "cuts_first_3s": None,
                "average_shot_length_seconds": None,
                "bgm_turns": [],
            },
            golden_hook_0_to_3s={
                "visual": "未分析",
                "spoken_line": None,
                "psychological_mechanism": "未分析",
            },
            beat_sheet=[
                {
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "shot_size": "unknown",
                    "camera_movement": "未分析",
                    "description": "演示占位",
                    "ai_video_prompt_en": "Demo only",
                }
            ],
            reusable_script_formula="[DEMO] 未分析",
        )
    return base


def demo_evolution(nodes, kind):
    return EvolutionDocument(
        title=f"[DEMO] {kind}",
        summary="离线演示合成，非模型结论",
        rules=["[DEMO] 基于演示输入验证进化事务"],
        tags=["demo"],
        system_prompt="[DEMO] 示例创作者风格" if kind == "persona" else None,
        source_node_ids=[n.id for n in nodes],
    )
