"""Validated contracts shared by the MCP transport and the storage layer."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


TaskStatus = Literal["TODO", "DOING", "BLOCKED", "DONE", "DROPPED"]
KnowledgeStatus = Literal["WISHLIST", "LEARNING", "MASTERED"]
TaskType = Literal["STUDY", "PRACTICE", "PROJECT"]
Priority = Literal["P0", "P1", "P2", "P3"]
Relation = Literal["PREREQUISITE_FOR", "SUBTOPIC_OF", "APPLIES_TO"]
Name = Annotated[str, Field(min_length=1, max_length=300)]
Description = Annotated[str, Field(min_length=1, max_length=5000)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ProbeAnswer(StrictInput):
    concept_name: Name
    domain: Name
    choice: Literal["A", "B", "C", "D"]
    summary: Description = Field(
        description="Brief one-sentence technical definition of the concept"
    )


class PipelineTaskInput(StrictInput):
    title: Name = Field(
        description="Action-oriented verb-object title, e.g. 推导直方图均衡化公式"
    )
    task_type: TaskType
    priority: Priority = "P2"
    estimated_minutes: int = Field(default=45, ge=15, le=180)
    definition_of_done: Description = Field(description="Clear acceptance criteria")
    linked_concepts: list[Name] = Field(default_factory=list, max_length=100)

    @field_validator("linked_concepts")
    @classmethod
    def clean_concepts(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("Concept names cannot be blank")
        return list(dict.fromkeys(cleaned))


class KnowledgeEdgeInput(StrictInput):
    source_concept: Name
    target_concept: Name
    relation: Relation


class GoalInput(StrictInput):
    goal_title: Name
    domain: Name
    required_concepts: list[Name] = Field(min_length=1, max_length=100)


class PipelineInput(StrictInput):
    goal_title: Name
    domain: Name
    probe_answers: list[ProbeAnswer] = Field(max_length=100)
    tasks_pipeline: list[PipelineTaskInput] = Field(min_length=1, max_length=500)
    edges: list[KnowledgeEdgeInput] = Field(default_factory=list, max_length=1000)


class TaskUpdateInput(StrictInput):
    task_id: int = Field(gt=0)
    new_status: TaskStatus
    notes: str = Field(default="", max_length=20000)


class KnowledgeInput(StrictInput):
    name: Name
    domain: Name
    status: KnowledgeStatus = "LEARNING"
    summary: str = Field(default="", max_length=5000)
    parent_concept: Name | None = None
    prerequisite_concept: Name | None = None


class OverviewInput(StrictInput):
    domain: Name | None = None
    status_filter: TaskStatus | None = None
