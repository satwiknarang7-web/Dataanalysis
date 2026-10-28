"""The shapes the engine passes around.

Everything that crosses a boundary (the cache, the database, the browser) is a
pydantic model, so it serialises to JSON and is validated on the way back in.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["number", "date", "boolean", "text"]
Role = Literal["time", "measure", "dimension", "id", "text", "ignore"]
Agg = Literal["sum", "avg", "median", "min", "max"]
Format = Literal["number", "currency", "percent"]
Grain = Literal["year", "quarter", "month", "week", "day", "weekday", "hour"]
TileKind = Literal["kpi", "trend", "breakdown", "table", "relationship", "distribution"]
Viz = Literal["kpi", "line", "area", "bar", "hbar", "donut", "table", "scatter", "histogram"]
Tone = Literal["finding", "neutral", "flat"]


class FieldInfo(BaseModel):
    """What one column is, and how the dashboard should treat it."""

    name: str
    label: str
    kind: Kind
    role: Role
    why: str = ""
    agg: Literal["sum", "avg"] | None = None
    format: Format = "number"
    percent_scale: int = 1  # 1 when a percentage is stored as 0.21, 100 when as 21
    integer: bool = False
    ordinal: bool = False
    order: list[str] | None = None  # levels with an order of their own
    outcome: str | None = None  # the level whose rate is watched (Churned, Yes…)
    time_unit: Literal["year"] | None = None  # a column of plain years
    origin: bool = False  # a birth or founding date: never the time axis
    filled: int = 0
    distinct: int = 0
    min: float | str | None = None
    max: float | str | None = None
    top: list[tuple[str, int]] = Field(default_factory=list)


class Reading(BaseModel):
    """The whole table as an analyst reads it."""

    row_count: int
    fields: list[FieldInfo]
    time: str | None = None
    row_noun: str = "row"

    def field(self, name: str) -> FieldInfo:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def by_role(self, *roles: Role) -> list[FieldInfo]:
        return [f for f in self.fields if f.role in roles]


class Measure(BaseModel):
    """A number the business talks about: revenue, orders, the churn rate."""

    id: str
    label: str
    type: Literal["count", "agg", "distinct", "rate"]
    field: str | None = None
    agg: Agg | None = None
    event: str | None = None  # rate: the value whose share is measured
    format: Format = "number"
    percent_scale: int = 1
    additive: bool = False  # does adding it across groups give the total?
    good_up: bool = True  # is a rise good news? (not for churn, returns, costs)


class Filter(BaseModel):
    """Keep rows whose `field` (bucketed by `grain`, if any) is one of `values`."""

    field: str
    values: list[str]
    grain: Grain | None = None
    exclude: bool = False


class Dim(BaseModel):
    field: str
    grain: Grain | None = None


class Spec(BaseModel):
    """A structured query. Every tile, headline number and finding is one."""

    measures: list[Measure]
    by: list[Dim] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    sort: Literal["value", "label", "order"] = "value"
    descending: bool = True
    limit: int | None = None
    other: bool = False  # fold everything past `limit` into one "Other" row
    order: list[str] | None = None  # explicit level order, for sort="order"


class Tile(BaseModel):
    """One chart (or headline number) on the board, before it is computed."""

    id: str
    kind: TileKind
    title: str
    viz: Viz
    spec: Spec | None = None  # kpi, trend, breakdown, table
    x: str | None = None  # relationship
    y: str | None = None  # relationship
    field: str | None = None  # distribution
    filters: list[Filter] = Field(default_factory=list)  # relationship, distribution


class Caption(BaseModel):
    text: str
    headline: str = ""
    tone: Tone = "neutral"
    score: float = 0.0


class RenderedTile(BaseModel):
    tile: Tile
    data: dict
    caption: Caption
    value: str | None = None  # kpi: the formatted figure
    change: str | None = None  # kpi: the formatted change on the last period


class Plan(BaseModel):
    """What the board shows, decided once over every row."""

    primary: str
    kpis: list[Tile]
    tiles: list[Tile]
    measures: list[Measure]


class Finding(BaseModel):
    tile_id: str
    brief: str
    text: str


class Dashboard(BaseModel):
    headline: str
    kpis: list[RenderedTile]
    tiles: list[RenderedTile]
    findings: list[Finding]
    rows: int
    filters: list[Filter]
