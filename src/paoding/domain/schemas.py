from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class IngestRequest(StrictModel):
    url: HttpUrl
    instruction: str = Field(default="", max_length=2000)
    priority: Literal["low", "normal", "high"] = "normal"
    title: str = Field(default="", max_length=300)


class CrossDomainDNA(StrictModel):
    universal_principle: str = Field(min_length=1)
    transfer_to_short_video: str = Field(min_length=1)
    transfer_to_xiaohongshu: str = Field(min_length=1)
    transfer_to_wechat_article: str = Field(min_length=1)


class Evidence(StrictModel):
    basis: Literal["source_text", "image_pixels", "native_video", "demo"]
    limitations: list[str] = Field(default_factory=list)


class AnalysisBase(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(min_length=1, max_length=20)
    evergreen: bool = False
    cross_domain_dna: CrossDomainDNA
    evidence: Evidence


class SOPStep(StrictModel):
    phase: str
    action: str
    success_metric: str | None


class StrategyAnalysis(AnalysisBase):
    modality: Literal["text"] = "text"
    core_pain_point: str
    consumer_insight: str
    big_idea: str
    super_symbol: str | None
    execution_sop: list[SOPStep] = Field(min_length=1)


class ColorSwatch(StrictModel):
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    proportion: float = Field(ge=0, le=1)


class ColorSystem(StrictModel):
    palette: list[ColorSwatch] = Field(min_length=1)
    contrast_rule: str
    mood: list[str]


class Layout(StrictModel):
    grid: str
    focal_point: str
    reading_path: str
    whitespace_percent: float | None = Field(ge=0, le=100)


class Typography(StrictModel):
    weight_contrast: str
    font_pairing: str
    depth_interleaving: str


class VisualAnalysis(AnalysisBase):
    modality: Literal["image"] = "image"
    color_system: ColorSystem
    layout_and_grid: Layout
    typography: Typography
    reusable_mj_prompt: str


class Pacing(StrictModel):
    cuts_first_3s: int | None = Field(ge=0)
    average_shot_length_seconds: float | None = Field(gt=0)
    bgm_turns: list[str]


class GoldenHook(StrictModel):
    visual: str
    spoken_line: str | None
    psychological_mechanism: str


class Beat(StrictModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    shot_size: Literal["ECU", "CU", "MS", "WS", "unknown"]
    camera_movement: str
    description: str
    ai_video_prompt_en: str

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Beat end must be after its start")
        return self


class VideoAnalysis(AnalysisBase):
    modality: Literal["video"] = "video"
    pacing_metrics: Pacing
    golden_hook_0_to_3s: GoldenHook
    beat_sheet: list[Beat] = Field(min_length=1)
    reusable_script_formula: str

    @model_validator(mode="after")
    def ordered_beats(self):
        for before, after in zip(self.beat_sheet, self.beat_sheet[1:], strict=False):
            if after.start_seconds < before.end_seconds:
                raise ValueError("Beat sheet must be chronological and non-overlapping")
        return self


Analysis = Annotated[
    StrategyAnalysis | VisualAnalysis | VideoAnalysis, Field(discriminator="modality")
]
ANALYSIS_TYPES = {
    Modality.TEXT: StrategyAnalysis,
    Modality.IMAGE: VisualAnalysis,
    Modality.VIDEO: VideoAnalysis,
}


class NormalizedContent(StrictModel):
    source_url: str
    platform: str
    title: str
    modality: Modality
    text: str = ""
    media_paths: list[Path] = Field(default_factory=list)
    palette: list[ColorSwatch] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    creator_id: str | None = None
    limitations: list[str] = Field(default_factory=list)
    audio_paths: list[Path] = Field(default_factory=list)
    duration_seconds: float | None = None
    published_at: str | None = None
    video_segments: list[dict] = Field(default_factory=list)
    original_media_paths: list[Path] = Field(default_factory=list)


class CollectorItem(StrictModel):
    url: HttpUrl
    title: str = ""
    text: str = ""
    modality: Modality = Modality.TEXT
    media_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)
    metrics: dict[str, float] = Field(default_factory=dict)
    local_media_paths: list[Path] = Field(default_factory=list, max_length=8)
    published_at: str | None = None
    limitations: list[str] = Field(default_factory=list)
    resolved: bool = False


class CollectorPage(StrictModel):
    items: list[CollectorItem] = Field(max_length=100)
    next_cursor: str | None = None
    has_more: bool = False
    scan_complete: bool = True
    scanned_count: int = 0
    limitations: list[str] = Field(default_factory=list)


class TaxonomyGroup(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    parent: Literal["hook", "visual", "storyboard", "sop"]
    aliases: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)
    source_node_ids: list[str] = Field(min_length=1)


class TaxonomyPlan(StrictModel):
    groups: list[TaxonomyGroup] = Field(max_length=30)


class EvolutionDocument(StrictModel):
    title: str
    summary: str
    rules: list[str] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    system_prompt: str | None = None
    source_node_ids: list[str] = Field(min_length=1)


class FeedbackRequest(StrictModel):
    is_adopted: bool
    event_id: str = Field(min_length=1, max_length=120)
